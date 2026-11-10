"""`aqe evaluate` (B11): locked final-segment evaluation of E0-E3 on identical rows, labels, costs.

* Prediction quality: log loss, multiclass Brier, accuracy, ECE/reliability, per-class recall,
  regime breakdown (rv_288 terciles fixed from training rows), coverage/abstention.
* Uncertainty: moving-block bootstrap of the log-loss difference vs E0.
* Virtual trading: one fixed, pre-declared policy for all experiments; entry at the open of the
  first bar starting after the prediction is ready, exit at the first bar open after entry + h;
  fees, adverse slippage and settled funding included; cost x2 and +1-bar latency stress.
* E4/E5 (context backends) are recorded as BLOCKED when no historical context assessments exist.
* Every run is appended to an experiment ledger with a trial counter (repeated test looks are visible).
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd

from aqe.config import ExperimentConfig, LocalConfig
from aqe.contracts.core import ModelReadiness, iso_utc, utc_now
from aqe.datasets.build import load_dataset, rows_for_training, rows_in_window
from aqe.ensemble.calibration import TemperatureCalibrator, ece, segment_predictions
from aqe.ensemble.meta import REGIME_FEATURES, MetaModel, logloss
from aqe.evaluation.costs import max_drawdown, trade_return
from aqe.models.registry import artifact_dir, exp_dir, load_metadata, load_registry, save_metadata, set_readiness
from aqe.storage import atomic_write_json, atomic_write_text
from aqe.storage.bars import read_bars
from aqe.storage.runs import EXIT_NOT_READY, EXIT_OK, RunRecord

MAX_TEST_ECE = 0.05


def brier(p: np.ndarray, y: np.ndarray) -> float:
    oh = np.eye(3)[y]
    return float(np.mean(np.sum((p - oh) ** 2, axis=1)))


def metrics(p: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    pred = p.argmax(1)
    out = {"n": int(len(y)), "logloss": logloss(p, y), "brier": brier(p, y), "accuracy": float((pred == y).mean()), "ece": ece(p, y)}
    for c, name in enumerate(("down", "neutral", "up")):
        m = pred == c
        out[f"recall_{name}"] = float((pred[y == c] == c).mean()) if (y == c).any() else None
        out[f"precision_{name}"] = float((y[m] == c).mean()) if m.any() else None
    return out


def block_bootstrap_diff(loss_a: np.ndarray, loss_b: np.ndarray, block: int, n_boot: int, seed: int) -> dict[str, float]:
    d = loss_a - loss_b
    n = len(d)
    if n < 2 * block:
        return {"mean": float(d.mean()), "ci95_low": float("nan"), "ci95_high": float("nan")}
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / block))
    starts_max = n - block
    means = np.empty(n_boot)
    for i in range(n_boot):
        s = rng.integers(0, starts_max + 1, nb)
        idx = (s[:, None] + np.arange(block)[None, :]).ravel()[:n]
        means[i] = d[idx].mean()
    return {"mean": float(d.mean()), "ci95_low": float(np.quantile(means, 0.025)), "ci95_high": float(np.quantile(means, 0.975))}


def virtual_trades(p: np.ndarray, as_of: pd.Series, h: int, opens: pd.Series, funding: pd.DataFrame, fee_bps: float,
                   slip_bps: float, min_edge: float, min_prob: float, latency_bars: int = 0, ready_delay_s: float = 5.0) -> dict[str, Any]:
    """Non-overlapping fixed policy. opens: Series indexed by bar open_time (UTC) -> open price."""
    times = opens.index
    res = []
    busy_until = pd.Timestamp.min.tz_localize("UTC")
    for i in range(len(p)):
        t = as_of.iloc[i]
        if t < busy_until:
            continue
        pdn, _, pup = p[i]
        side = 0
        if pup - pdn >= min_edge and pup >= min_prob:
            side = 1
        elif pdn - pup >= min_edge and pdn >= min_prob:
            side = -1
        if side == 0:
            continue
        ready = t + pd.Timedelta(seconds=ready_delay_s)
        k = times.searchsorted(ready) + latency_bars
        if k >= len(times):
            break
        entry_t = times[k]
        j = times.searchsorted(entry_t + pd.Timedelta(seconds=h))
        if j >= len(times):
            break
        exit_t = times[j]
        r = trade_return(side, float(opens.iloc[k]), float(opens.iloc[j]), entry_t, exit_t, fee_bps, slip_bps, funding)
        res.append(r)
        busy_until = exit_t
    if not res:
        return {"trades": 0}
    net = np.array([r.net_return for r in res])
    return {
        "trades": len(res), "longs": int(sum(r.side == 1 for r in res)), "shorts": int(sum(r.side == -1 for r in res)),
        "sum_net_return": float(net.sum()), "mean_net_return": float(net.mean()), "hit_rate": float((net > 0).mean()),
        "sum_gross_return": float(sum(r.gross_return for r in res)), "sum_fees": float(sum(r.fee_cost for r in res)),
        "sum_slippage": float(sum(r.slippage_cost for r in res)), "sum_funding": float(sum(r.funding_cost for r in res)),
        "max_drawdown_log": max_drawdown(net), "worst_trade": float(net.min()), "best_trade": float(net.max()),
    }


def run_evaluate(exp: ExperimentConfig, cfg: LocalConfig, cfg_hash: str, rec: RunRecord) -> int:
    df, man = load_dataset(cfg, exp)
    seg = exp.segments
    test_end = seg.test_end or man.data_end
    ev = exp.evaluation
    edir = exp_dir(cfg.paths.artifact_root, exp.experiment_id) / "evaluation"
    edir.mkdir(parents=True, exist_ok=True)
    ledger = edir / "ledger.jsonl"
    prior_runs = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()] if ledger.exists() else []
    trial_no = 1 + sum(1 for r in prior_runs if r.get("segment") == [iso_utc(seg.test_start), iso_utc(test_end)])
    inst = exp.instrument
    bars = read_bars(cfg.paths.data_root, inst, "5m", start=seg.test_start)
    opens = bars[bars["is_closed"]].set_index("open_time")["open"].astype("float64")
    fpath = cfg.paths.data_root / "normalized" / "funding" / inst.venue.value / inst.market_type.value / inst.instrument_id / "funding_history.parquet"
    funding = pd.read_parquet(fpath) if fpath.exists() else pd.DataFrame(columns=["funding_time", "funding_rate"])
    report: dict[str, Any] = {"experiment_id": exp.experiment_id, "trial_no_on_this_test_segment": trial_no,
                              "segment": [iso_utc(seg.test_start), iso_utc(test_end)], "config_hash": cfg_hash,
                              "dataset_id": man.dataset_id, "evaluated_at": iso_utc(utc_now()), "horizons": {}}
    for h in exp.horizons_seconds:
        reg = load_registry(cfg.paths.artifact_root, exp.experiment_id)["horizons"].get(str(h), {})
        need = ["lgbm", "tcn", "meta_numeric_only", "calib_numeric_only", "calib_lgbm_only", "calib_tcn_only"]
        missing = [k for k in need if k not in reg]
        if missing:
            rec.notes.append(f"h={h}: missing registered artifacts {missing}")
            return EXIT_NOT_READY
        rows = rows_in_window(df, h, seg.test_start, test_end).index
        base, bmetas = segment_predictions(cfg, exp, df, h, rows, reg)
        mmeta = load_metadata(cfg.paths.artifact_root, exp.experiment_id, reg["meta_numeric_only"])
        mm = MetaModel.load(artifact_dir(cfg.paths.artifact_root, exp.experiment_id, reg["meta_numeric_only"]), mmeta)
        cal = {c: TemperatureCalibrator.load(artifact_dir(cfg.paths.artifact_root, exp.experiment_id, reg[f"calib_{c}"]),
                                             load_metadata(cfg.paths.artifact_root, exp.experiment_id, reg[f"calib_{c}"]))
               for c in ("numeric_only", "lgbm_only", "tcn_only")}
        for c in ("numeric_only", "lgbm_only", "tcn_only"):
            cm = load_metadata(cfg.paths.artifact_root, exp.experiment_id, reg[f"calib_{c}"])
            fitted = cm["fitted_on_artifacts"]
            current = {"meta": reg["meta_numeric_only"], "lgbm": reg["lgbm"], "tcn": reg["tcn"]}
            if any(current[k] != v for k, v in fitted.items()):
                raise AssertionError(f"calibrator {c} was fitted on different base/meta artifacts; re-run calibrate")
        regime = df.loc[rows]
        ok = np.isfinite(base["lgbm"]).all(1) & np.isfinite(base["tcn"]).all(1) & regime[REGIME_FEATURES].notna().all(1).to_numpy()
        y = df.loc[rows, f"y_{h}"].to_numpy()[ok].astype(int)
        as_of = df.loc[rows, "as_of"][ok].reset_index(drop=True)
        train = rows_for_training(df, h, seg.dev_start, seg.calibration_start)
        prior = np.bincount(train[f"y_{h}"].astype(int), minlength=3) / len(train)
        probs = {
            "E0_prior": np.tile(prior, (int(ok.sum()), 1)),
            "E1_lgbm": cal["lgbm_only"].apply(base["lgbm"][ok]),
            "E2_tcn": cal["tcn_only"].apply(base["tcn"][ok]),
            "E3_lgbm_tcn_meta": cal["numeric_only"].apply(mm.predict_proba({k: base[k][ok] for k in mm.base_kinds}, regime[ok])),
        }
        q = np.nanquantile(train["rv_288"], [1 / 3, 2 / 3])
        rv = regime["rv_288"].to_numpy()[ok]
        regime_bucket = np.where(rv <= q[0], "low_vol", np.where(rv <= q[1], "mid_vol", "high_vol"))
        block = int(ev.bootstrap_block_hours * 3600 / exp.bar_interval_seconds)
        hrep: dict[str, Any] = {"rows_in_segment": int(len(rows)), "rows_evaluated": int(ok.sum()),
                                "coverage": float(ok.mean()) if len(ok) else 0.0,
                                "independent_label_windows": int(ok.sum() * exp.bar_interval_seconds / h), "experiments": {}}
        losses = {k: -np.log(np.clip(v[np.arange(len(y)), y], 1e-12, 1)) for k, v in probs.items()}
        for name, p in probs.items():
            m = metrics(p, y)
            m["by_regime_logloss"] = {b: logloss(p[regime_bucket == b], y[regime_bucket == b]) for b in ("low_vol", "mid_vol", "high_vol") if (regime_bucket == b).any()}
            if name != "E0_prior":
                m["logloss_diff_vs_E0_block_bootstrap"] = block_bootstrap_diff(losses[name], losses["E0_prior"], block, ev.bootstrap_samples, exp.seed)
                m["virtual_trading"] = {
                    "base_costs": virtual_trades(p, as_of, h, opens, funding, ev.taker_fee_bps, ev.slippage_bps, ev.policy_min_edge, ev.policy_min_prob),
                    "costs_x2": virtual_trades(p, as_of, h, opens, funding, 2 * ev.taker_fee_bps, 2 * ev.slippage_bps, ev.policy_min_edge, ev.policy_min_prob),
                    "latency_plus_1bar": virtual_trades(p, as_of, h, opens, funding, ev.taker_fee_bps, ev.slippage_bps, ev.policy_min_edge, ev.policy_min_prob, latency_bars=1),
                }
            hrep["experiments"][name] = m
        hrep["experiments"]["E4_simple_llm"] = {"status": "BLOCKED", "reason": "no point-in-time context assessments for the test segment (no LLM provider configured; historical news receipt times unavailable)"}
        hrep["experiments"]["E5_tradingagents"] = {"status": "BLOCKED", "reason": "same as E4; TradingAgents graph not run on historical bundles"}
        e3 = hrep["experiments"]["E3_lgbm_tcn_meta"]
        ci_hi = e3["logloss_diff_vs_E0_block_bootstrap"]["ci95_high"]
        verdict = "SKILL_DEMONSTRATED" if np.isfinite(ci_hi) and ci_hi < 0 else "NO_SIGNIFICANT_SKILL"
        hrep["oos_verdict_E3"] = verdict
        hrep["error_corr_lgbm_tcn"] = float(np.corrcoef(
            -np.log(np.clip(base["lgbm"][ok][np.arange(len(y)), y], 1e-12, 1)),
            -np.log(np.clip(base["tcn"][ok][np.arange(len(y)), y], 1e-12, 1)))[0, 1])
        # readiness: evaluated on the locked OOS segment (a stage, not a quality claim)
        evidence = f"locked test {iso_utc(seg.test_start)}..{iso_utc(test_end)} run {rec.run_id} trial {trial_no}"
        for slot in ("lgbm", "tcn", "meta_numeric_only", "calib_numeric_only"):
            set_readiness(cfg.paths.artifact_root, exp.experiment_id, reg[slot], ModelReadiness.OOS_EVALUATED, evidence)
        cm = load_metadata(cfg.paths.artifact_root, exp.experiment_id, reg["calib_numeric_only"])
        cm["calibration_validated"] = bool(cm.get("sample_sufficient")) and e3["ece"] <= MAX_TEST_ECE
        cm["calibration_validated_basis"] = f"test ECE {e3['ece']:.4f} (max {MAX_TEST_ECE}); sample_sufficient={cm.get('sample_sufficient')}"
        cm["oos_verdict"] = verdict
        vt = e3["virtual_trading"]["base_costs"]
        cost_verdict = "NET_POSITIVE" if vt.get("trades", 0) >= 30 and vt.get("sum_net_return", 0) > 0 and             e3["virtual_trading"]["costs_x2"].get("sum_net_return", 0) > 0 else "NOT_POSITIVE"
        cm["oos_cost_verdict"] = cost_verdict
        cm["oos_cost_basis"] = {"policy": {"min_edge": ev.policy_min_edge, "min_prob": ev.policy_min_prob}, "base": vt,
                                "costs_x2_sum_net": e3["virtual_trading"]["costs_x2"].get("sum_net_return")}
        hrep["oos_cost_verdict_E3"] = cost_verdict
        cm["oos_test_metrics"] = {k: e3[k] for k in ("logloss", "brier", "ece", "n")}
        save_metadata(cfg.paths.artifact_root, exp.experiment_id, cm)
        hrep["calibration_validated"] = cm["calibration_validated"]
        report["horizons"][str(h)] = hrep
    rid = rec.run_id
    jp = edir / f"eval_{rid}.json"
    atomic_write_json(jp, report)
    atomic_write_text(edir / f"eval_{rid}.md", render_eval_md(report))
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"run_id": rid, "segment": report["segment"], "trial_no": trial_no, "config_hash": cfg_hash,
                             "evaluated_at": report["evaluated_at"],
                             "summary": {h: {k: v.get("logloss") for k, v in r["experiments"].items() if isinstance(v, dict) and "logloss" in v}
                                         for h, r in report["horizons"].items()}}) + "\n")
    rec.outputs.update({"report_json": str(jp), "report_md": str(edir / f"eval_{rid}.md"), "trial_no": trial_no,
                        "verdicts": {h: r["oos_verdict_E3"] for h, r in report["horizons"].items()}})
    return EXIT_OK


def render_eval_md(r: dict[str, Any]) -> str:
    L = [f"# 잠금 최종 구간 평가 — {r['experiment_id']}", "",
         f"- 구간: `{r['segment'][0]}` ~ `{r['segment'][1]}` · 이 구간 평가 시도 번호: **{r['trial_no_on_this_test_segment']}** · 데이터셋 `{r['dataset_id']}`",
         "- 입력 provenance: ASSUMED_LATENCY_REPLAY(사후 다운로드 캔들). 실제 실시간 수신 기록이 아니다.",
         "- 이 결과는 과거 구간 재생 평가이며 전향(shadow) 검증이 아니다. 수익 보장이 아니다.", ""]
    for h, hr in r["horizons"].items():
        L += [f"## 기간 {int(h)//3600}h", f"- 평가 행 {hr['rows_evaluated']} / {hr['rows_in_segment']} (coverage {hr['coverage']:.3f}), 비중첩 라벨 창 ≈ {hr['independent_label_windows']}",
              f"- E3 확률 품질 판정: **{hr['oos_verdict_E3']}** · 비용 차감 판정: **{hr.get('oos_cost_verdict_E3')}** · 보정 검증: {hr['calibration_validated']} · LGBM/TCN 손실 상관 {hr['error_corr_lgbm_tcn']:.3f}", "",
              "| 실험 | logloss | Brier | acc | ECE | Δlogloss vs E0 (95% block CI) | 거래수 | 순수익합(기본비용) | 순수익합(비용×2) | 순수익합(+1봉 지연) | MDD(log) |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
        for name, m in hr["experiments"].items():
            if m.get("status") == "BLOCKED":
                L.append(f"| {name} | BLOCKED | | | | {m['reason']} | | | | | |")
                continue
            d = m.get("logloss_diff_vs_E0_block_bootstrap")
            ds = f"{d['mean']:+.4f} [{d['ci95_low']:+.4f}, {d['ci95_high']:+.4f}]" if d else "—"
            vt = m.get("virtual_trading", {})
            b, c2, l1 = vt.get("base_costs", {}), vt.get("costs_x2", {}), vt.get("latency_plus_1bar", {})
            fmt = lambda x: f"{x.get('sum_net_return', 0):+.3f}" if x.get("trades") else "0 trades"  # noqa: E731
            L.append(f"| {name} | {m['logloss']:.4f} | {m['brier']:.4f} | {m['accuracy']:.3f} | {m['ece']:.4f} | {ds} | {b.get('trades', 0)} | {fmt(b)} | {fmt(c2)} | {fmt(l1)} | {b.get('max_drawdown_log', 0):.3f} |")
        L.append("")
        e3 = hr["experiments"]["E3_lgbm_tcn_meta"]
        L.append("- 레짐별 logloss(E3): " + ", ".join(f"{k}={v:.4f}" for k, v in e3["by_regime_logloss"].items()))
        L.append("")
    L.append("가상 운용은 사전 고정 정책(p_dir − p_opp ≥ edge, p_dir ≥ min_prob, 비중첩)으로 계산했으며 실제 수동 매매 성과를 대신하지 않는다.")
    return "\n".join(L)
