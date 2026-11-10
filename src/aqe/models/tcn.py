"""Small causal TCN (B07.3), written after the residual causal-convolution design of
Bai, Kolter & Koltun (locuslab/TCN, MIT). No code was copied; see THIRD_PARTY_NOTICES.md.

Shapes: input (batch, n_features, window=256) -> per-timestep logits (batch, 3, window);
the classifier uses the LAST timestep only. Causality: every Conv1d is left-padded by
dilation*(kernel-1) and nothing is padded on the right, so output[t] depends on input[<=t].
Receptive field = 1 + sum_blocks 2*(k-1)*d = 1 + 2*2*(1+2+4+8+16+32) = 253.
Normalization: per-feature mean/std fitted on the training rows only; no time-axis normalization.
"""

from __future__ import annotations

import copy
import math
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch
from torch import nn
from torch.nn.utils.parametrizations import weight_norm

from aqe.config import TCNConfig
from aqe.contracts.core import CLASS_ORDER
from aqe.models.lgbm import check_probs


class CausalConv1d(nn.Module):
    def __init__(self, c_in: int, c_out: int, kernel_size: int, dilation: int):
        super().__init__()
        self.left_pad = dilation * (kernel_size - 1)
        self.conv = weight_norm(nn.Conv1d(c_in, c_out, kernel_size, dilation=dilation, padding=0))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.conv(nn.functional.pad(x, (self.left_pad, 0)))


class TemporalBlock(nn.Module):
    def __init__(self, c_in: int, c_out: int, kernel_size: int, dilation: int, dropout: float):
        super().__init__()
        self.conv1 = CausalConv1d(c_in, c_out, kernel_size, dilation)
        self.conv2 = CausalConv1d(c_out, c_out, kernel_size, dilation)
        self.drop = nn.Dropout(dropout)
        self.act = nn.ReLU()
        self.down = nn.Conv1d(c_in, c_out, 1) if c_in != c_out else None

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        y = self.drop(self.act(self.conv1(x)))
        y = self.drop(self.act(self.conv2(y)))
        res = x if self.down is None else self.down(x)
        return self.act(y + res)


class SmallTCN(nn.Module):
    def __init__(self, n_features: int, channels: int = 32, kernel_size: int = 3,
                 dilations: list[int] | tuple[int, ...] = (1, 2, 4, 8, 16, 32), dropout: float = 0.1):
        super().__init__()
        blocks = []
        c_in = n_features
        for d in dilations:
            blocks.append(TemporalBlock(c_in, channels, kernel_size, d, dropout))
            c_in = channels
        self.blocks = nn.Sequential(*blocks)
        self.head = nn.Conv1d(channels, 3, 1)  # per-timestep classifier; we read the last step
        self.kernel_size = kernel_size
        self.dilations = tuple(dilations)

    def receptive_field(self) -> int:
        return 1 + sum(2 * (self.kernel_size - 1) * d for d in self.dilations)

    def forward_sequence(self, x: torch.Tensor) -> torch.Tensor:
        if x.dim() != 3:
            raise ValueError("expected (batch, features, time)")
        return self.head(self.blocks(x))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.forward_sequence(x)[:, :, -1]


class Normalizer:
    def __init__(self, mean: np.ndarray, std: np.ndarray, clip: float = 10.0):
        self.mean, self.std, self.clip = mean.astype("float32"), std.astype("float32"), clip

    @classmethod
    def fit(cls, X: np.ndarray) -> "Normalizer":
        mean = np.nanmean(X, axis=0)
        std = np.nanstd(X, axis=0)
        std[~np.isfinite(std) | (std < 1e-8)] = 1.0
        return cls(mean, std)

    def transform(self, X: np.ndarray) -> np.ndarray:
        return np.clip((X - self.mean) / self.std, -self.clip, self.clip).astype("float32")

    def to_json(self) -> dict[str, Any]:
        return {"mean": self.mean.tolist(), "std": self.std.tolist(), "clip": self.clip}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Normalizer":
        return cls(np.asarray(d["mean"], "float32"), np.asarray(d["std"], "float32"), d["clip"])


def valid_window_ends(X: np.ndarray, window: int) -> np.ndarray:
    """Boolean per row: True if rows [i-window+1, i] are all finite (contiguous grid assumed)."""
    bad = (~np.isfinite(X)).any(axis=1).astype("int32")
    c = np.concatenate([[0], np.cumsum(bad)])
    ok = np.zeros(len(X), dtype=bool)
    idx = np.arange(window - 1, len(X))
    ok[idx] = (c[idx + 1] - c[idx + 1 - window]) == 0
    return ok


def resolve_device(name: str) -> torch.device:
    if name == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if name.startswith("cuda") and not torch.cuda.is_available():
        return torch.device("cpu")
    return torch.device(name)


def _windows(Xn: torch.Tensor, ends: np.ndarray, window: int) -> torch.Tensor:
    # Xn: (T, F) -> (B, F, window); index tensor lives on Xn's device
    idx = torch.as_tensor(ends, dtype=torch.long, device=Xn.device)[:, None] - torch.arange(window - 1, -1, -1, device=Xn.device)[None, :]
    return Xn[idx].permute(0, 2, 1).contiguous()


def predict_windows(model: SmallTCN, Xn: torch.Tensor, ends: np.ndarray, window: int, batch: int = 2048) -> np.ndarray:
    model.eval()
    out = []
    with torch.no_grad():
        for i in range(0, len(ends), batch):
            logits = model(_windows(Xn, ends[i:i + batch], window))
            out.append(torch.softmax(logits.double(), dim=1).cpu().numpy())
    return np.concatenate(out) if out else np.zeros((0, 3))


def train_tcn(grid_df: pd.DataFrame, train_mask: np.ndarray, h: int, features: list[str], cfg: TCNConfig, seed: int,
              log=print) -> tuple[SmallTCN, Normalizer, dict[str, Any]]:
    """grid_df: complete contiguous grid (dataset rows in time order). train_mask: rows eligible as
    training targets (already purged by the caller). Validation = last val_fraction of eligible rows
    with purge (fit targets' labels end before validation starts)."""
    torch.manual_seed(seed)
    np.random.seed(seed)
    torch.set_num_threads(cfg.num_threads)
    device = resolve_device(cfg.device)
    X = grid_df[features].to_numpy("float32")
    y = grid_df[f"y_{h}"].to_numpy()
    label_end = grid_df[f"label_end_at_{h}"].to_numpy()
    as_of = grid_df["as_of"].to_numpy()
    eligible = np.where(train_mask & valid_window_ends(X, cfg.window) & np.isfinite(y))[0]
    if len(eligible) < 5000:
        raise ValueError(f"not enough TCN training windows ({len(eligible)})")
    n_val = max(1000, int(len(eligible) * cfg.val_fraction))
    val_idx = eligible[-n_val:]
    val_start = as_of[val_idx[0]]
    fit_idx = eligible[:-n_val]
    fit_idx = fit_idx[label_end[fit_idx] <= val_start]
    fit_idx = fit_idx[:: cfg.train_stride]
    if cfg.max_train_samples and len(fit_idx) > cfg.max_train_samples:
        fit_idx = fit_idx[-cfg.max_train_samples:]  # most recent samples
    # normalizer fitted on rows feeding the fit windows only (no validation / future rows)
    first_row = max(0, fit_idx[0] - cfg.window + 1)
    norm = Normalizer.fit(X[first_row: fit_idx[-1] + 1])
    Xn = torch.from_numpy(np.nan_to_num(norm.transform(X), nan=0.0)).to(device)  # NaN rows never inside a valid window
    model = SmallTCN(len(features), cfg.channels, cfg.kernel_size, cfg.dilations, cfg.dropout).to(device)
    assert model.receptive_field() <= cfg.window
    opt = torch.optim.AdamW(model.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)
    lossf = nn.CrossEntropyLoss()
    y_t = torch.as_tensor(np.nan_to_num(y, nan=-1).astype("int64"), device=device)
    best, best_state, bad, hist = math.inf, None, 0, []
    rng = np.random.default_rng(seed)
    for epoch in range(cfg.max_epochs):
        model.train()
        t0 = time.time()
        perm = rng.permutation(fit_idx)
        tot, nb = 0.0, 0
        for i in range(0, len(perm), cfg.batch_size):
            ends = perm[i:i + cfg.batch_size]
            xb = _windows(Xn, ends, cfg.window)
            yb = y_t[torch.as_tensor(ends, device=device)]
            opt.zero_grad()
            loss = lossf(model(xb), yb)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), cfg.grad_clip)
            opt.step()
            tot += float(loss.detach())
            nb += 1
        pv = predict_windows(model, Xn, val_idx, cfg.window)
        yv = y[val_idx].astype(int)
        vloss = float(-np.mean(np.log(np.clip(pv[np.arange(len(yv)), yv], 1e-12, 1))))
        hist.append({"epoch": epoch, "train_loss": tot / max(nb, 1), "val_logloss": vloss, "seconds": round(time.time() - t0, 1)})
        log(f"    tcn h={h} epoch {epoch} train {tot / max(nb, 1):.4f} val {vloss:.4f} ({time.time() - t0:.0f}s)")
        if vloss < best - 1e-5:
            best, best_state, bad = vloss, copy.deepcopy(model.state_dict()), 0
        else:
            bad += 1
            if bad >= cfg.patience:
                break
    if best_state is not None:
        model.load_state_dict(best_state)
    model = model.to("cpu")
    info = {"device": str(device), "fit_samples": int(len(fit_idx)), "val_samples": int(len(val_idx)), "history": hist, "best_val_logloss": best,
            "fit_as_of": [str(as_of[fit_idx[0]]), str(as_of[fit_idx[-1]])], "val_as_of": [str(as_of[val_idx[0]]), str(as_of[val_idx[-1]])],
            "train_label_cutoff": str(pd.Timestamp(label_end[eligible].max())), "receptive_field": model.receptive_field()}
    return model, norm, info


def save_tcn(model: SmallTCN, norm: Normalizer, adir: Path) -> None:
    adir.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), adir / "model.pt")
    import json

    (adir / "normalizer.json").write_text(json.dumps(norm.to_json()), encoding="utf-8")


class TCNPredictor:
    def __init__(self, model: SmallTCN, norm: Normalizer, features: list[str], window: int):
        self.model, self.norm, self.features, self.window = model, norm, features, window

    @classmethod
    def load(cls, adir: Path, meta: dict[str, Any]) -> "TCNPredictor":
        import json

        if tuple(meta["class_order"]) != CLASS_ORDER:
            raise ValueError("class order mismatch")
        arch = meta["architecture"]
        model = SmallTCN(len(meta["feature_names"]), arch["channels"], arch["kernel_size"], arch["dilations"], arch["dropout"])
        model.load_state_dict(torch.load(Path(adir) / "model.pt", map_location="cpu", weights_only=True))
        model.eval()
        norm = Normalizer.from_json(json.loads((Path(adir) / "normalizer.json").read_text(encoding="utf-8")))
        return cls(model, norm, list(meta["feature_names"]), arch["window"])

    def predict_grid(self, grid_df: pd.DataFrame, rows: np.ndarray) -> np.ndarray:
        """Predict at grid row positions `rows` (positions into grid_df); invalid windows -> NaN rows."""
        X = grid_df[self.features].to_numpy("float32")
        ok = valid_window_ends(X, self.window)
        dev = resolve_device("auto") if len(rows) > 2048 else torch.device("cpu")
        Xn = torch.from_numpy(np.nan_to_num(self.norm.transform(X), nan=0.0)).to(dev)
        out = np.full((len(rows), 3), np.nan)
        good = ok[rows]
        if good.any():
            model = self.model.to(dev)
            out[good] = check_probs(predict_windows(model, Xn, rows[good], self.window))
            self.model = model.to("cpu")
        return out

    def predict_latest(self, feature_frame: pd.DataFrame) -> np.ndarray:
        if len(feature_frame) < self.window:
            raise ValueError(f"need {self.window} contiguous rows, have {len(feature_frame)}")
        p = self.predict_grid(feature_frame.tail(self.window).reset_index(drop=True), np.array([self.window - 1]))
        if not np.all(np.isfinite(p)):
            raise ValueError("latest window contains missing features")
        return p[0]
