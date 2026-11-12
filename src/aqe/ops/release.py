"""Model releases: Google trains -> bundle -> Oracle staging -> checks + inference smoke -> approved activation -> rollback.

Nothing here retrains, loosens an evaluation criterion, enables trading or deletes anything:
  build      (training host) copy the artifacts referenced by a candidate registry + their OOS/evaluation records into
             `<out>/<release_id>/bundle/<experiment>/...` with `release.json` (SHA-256 of every file, code commit, host).
  stage      (inference host) copy a received bundle into `<artifact_root>/_releases/<release_id>/` (hash-verified).
  verify     integrity, metadata contract, OOS fold leakage (train labels end before each OOS window), and an inference
             smoke on THIS host (`predict_horizon` = the same identity/schema/lineage checks as `aqe analyze`, plus the
             risk engine) — results in `checks.json`; the active model is not touched.
  activate   only with `approve == release_id` AND a passing `checks.json` for the same release.json hash: copy missing
             artifact dirs (an existing artifact id with other content aborts), back up the current registry into
             `registry_history/`, atomically write the candidate registry, record `active_release.json`.
  rollback   restore a backed-up registry (the current one is backed up first). Artifacts are never removed.
Passing checks means "loadable and consistent here", not "profitable": RESEARCH_ONLY restrictions are unchanged.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from aqe.models.registry import REQUIRED_META_KEYS
from aqe.storage import atomic_write_json, read_json, sha256_file

CopyDecision = str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _files(root: Path) -> list[dict[str, Any]]:
    return [{"rel": p.relative_to(root).as_posix(), "bytes": p.stat().st_size, "sha256": sha256_file(p)}
            for p in sorted(root.rglob("*")) if p.is_file()]


def release_dir(artifact_root: Path, release_id: str) -> Path:
    return Path(artifact_root) / "_releases" / release_id


# ---------------------------------------------------------------- build (training host)
def build_release(artifact_root: Path, experiment_id: str, out_dir: Path, *, release_id: str | None = None,
                  include_dataset_manifest: bool = True, note: str = "") -> dict[str, Any]:
    from aqe.ops.transfer import code_commit

    exp = Path(artifact_root) / experiment_id
    reg = read_json(exp / "registry.json")
    aids = sorted({aid for slots in reg.get("horizons", {}).values() for aid in slots.values()})
    if not aids:
        raise ValueError(f"registry of {experiment_id} references no artifacts")
    problems = []
    for aid in aids:
        meta_p = exp / "artifacts" / aid / "metadata.json"
        if not meta_p.exists():
            problems.append(f"{aid}: missing metadata.json")
            continue
        missing = [k for k in REQUIRED_META_KEYS if k not in read_json(meta_p)]
        if missing:
            problems.append(f"{aid}: metadata missing {missing}")
    if problems:
        raise ValueError("cannot build release: " + "; ".join(problems))
    rid = release_id or datetime.now(timezone.utc).strftime(f"rel-{experiment_id}-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:4]
    rdir = Path(out_dir) / rid
    if rdir.exists():
        raise FileExistsError(f"release dir exists: {rdir}")
    bexp = rdir / "bundle" / experiment_id
    for aid in aids:
        shutil.copytree(exp / "artifacts" / aid, bexp / "artifacts" / aid)
    for sub in ("oos", "evaluation"):
        for p in sorted((exp / sub).glob("*.json")) + sorted((exp / sub).glob("*.jsonl")) + sorted((exp / sub).glob("*.md")):
            (bexp / sub).mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, bexp / sub / p.name)
    if include_dataset_manifest and (exp / "dataset" / "manifest.json").exists():
        (bexp / "dataset").mkdir(parents=True, exist_ok=True)
        shutil.copy2(exp / "dataset" / "manifest.json", bexp / "dataset" / "manifest.json")
    atomic_write_json(bexp / "registry.json", reg)
    manifest = {"release_id": rid, "experiment_id": experiment_id, "created_at": _now(), "registry": reg,
                "artifact_ids": aids, "built_on": {"machine": platform.machine(), "system": platform.system(),
                                                   "python": platform.python_version()},
                "code_commit": code_commit(), "note": note, "files": _files(rdir / "bundle")}
    atomic_write_json(rdir / "release.json", manifest)
    return {"release_id": rid, "path": str(rdir), "artifacts": len(aids), "files": len(manifest["files"])}


def _release_hash(rdir: Path) -> str:
    return sha256_file(rdir / "release.json")


def check_integrity(rdir: Path) -> list[str]:
    rel = read_json(rdir / "release.json")
    probs = []
    listed = {f["rel"] for f in rel["files"]}
    for f in rel["files"]:
        p = rdir / "bundle" / f["rel"]
        if not p.is_file():
            probs.append(f"missing {f['rel']}")
        elif p.stat().st_size != f["bytes"] or sha256_file(p) != f["sha256"]:
            probs.append(f"content differs: {f['rel']}")
    extra = {p.relative_to(rdir / "bundle").as_posix() for p in (rdir / "bundle").rglob("*") if p.is_file()} - listed
    if extra:
        probs.append(f"unlisted files: {sorted(extra)[:5]}")
    return probs


# ---------------------------------------------------------------- stage (inference host)
def stage_release(received_dir: Path, artifact_root: Path) -> dict[str, Any]:
    """Copy a received release dir (containing release.json + bundle/) into `<artifact_root>/_releases/<id>`."""
    rel = read_json(Path(received_dir) / "release.json")
    dst = release_dir(artifact_root, rel["release_id"])
    if dst.exists():
        raise FileExistsError(f"release already staged: {dst}")
    probs = check_integrity(Path(received_dir))
    if probs:
        raise ValueError("received bundle fails integrity: " + "; ".join(probs[:5]))
    tmp = dst.with_name(dst.name + ".staging-tmp")
    shutil.copytree(received_dir, tmp)
    probs = check_integrity(tmp)
    if probs:
        raise ValueError("staged copy fails integrity: " + "; ".join(probs[:5]))
    os.rename(tmp, dst)
    return {"release_id": rel["release_id"], "staged_at": str(dst)}


def _leak_problems(bexp: Path) -> list[str]:
    probs = []
    for m in sorted((bexp / "oos").glob("*.manifest.json")):
        d = read_json(m)
        for f in d.get("folds", []):
            end, start = f.get("train_label_end_max_observed"), f.get("oos_start")
            if end and start and pd.Timestamp(end) >= pd.Timestamp(start):
                probs.append(f"{m.name} fold {f.get('fold_id')}: train labels end {end} >= OOS start {start} (leakage)")
    return probs


def default_smoke(staged_artifact_root: Path, experiment_cfg: Path, bars: pd.DataFrame, ruleset: Path) -> dict[str, Any]:
    """Run the analysis inference path (identity/schema/lineage checks included) on `bars` with the staged artifacts,
    then the risk engine. Returns per-horizon statuses. Works on any host (fixture bars carry no market meaning)."""
    from aqe.config import load_experiment_config
    from aqe.contracts.core import QualityStatus, RuntimeMode
    from aqe.ensemble.online import predict_horizon
    from aqe.features.ohlcv_v0 import MAX_LOOKBACK_BARS, compute_ohlcv_v0
    from aqe.features.snapshot import build_snapshot
    from aqe.risk.engine import RiskInputs, evaluate_risk, load_ruleset
    from aqe.storage.bars import bars_as_of, to_grid

    exp, _ = load_experiment_config(experiment_cfg)
    inst = exp.instrument
    last_close = bars.loc[bars["is_closed"], "close_time"].max()
    as_of = (last_close + pd.Timedelta(seconds=exp.decision_delay_seconds)).to_pydatetime()
    snap, _ = build_snapshot(bars, inst, as_of, exp.bar_interval_seconds)
    sel = bars_as_of(bars, as_of).tail(MAX_LOOKBACK_BARS + 600)
    ff = compute_ohlcv_v0(to_grid(sel, exp.bar_interval_seconds), exp.bar_interval_seconds)
    rs, rs_hash = load_ruleset(ruleset)
    out = {"as_of": as_of.isoformat(), "feature_schema_hash": snap.feature_schema_hash, "horizons": {}}
    for h in exp.horizons_seconds:
        preds, ens, metas = predict_horizon(artifact_root=staged_artifact_root, experiment_id=exp.experiment_id, snapshot=snap,
                                            feature_frame=ff, horizon_seconds=h, mode=RuntimeMode.REPLAY,
                                            interval_seconds=exp.bar_interval_seconds, context=None)
        risk = evaluate_risk(RiskInputs(
            analysis_id="release-smoke", mode=RuntimeMode.REPLAY, decision_time=as_of, horizon_seconds=h, snapshot=snap,
            predictions=[p for p in preds if p.status.value == "OK"], ensemble=ens, artifact_meta=metas, context=None,
            clock_skew_ms=None, source_quality={"bars": QualityStatus.OK}, quote_volume_24h=None,
            cost_assumptions={"taker_fee_bps": exp.evaluation.taker_fee_bps, "slippage_bps": exp.evaluation.slippage_bps},
            portfolio=None, last_price=float(sel["close"].iloc[-1])), rs, rs_hash)
        out["horizons"][str(h)] = {
            "base": {p.model_kind: {"status": p.status.value, "reason": p.status_reason, "artifact_id": p.artifact_id} for p in preds},
            "ensemble_status": ens.status.value, "ensemble_reasons": list(ens.abstain_reasons)[:5],
            "oos_verdict": ens.extra.get("oos_verdict"), "oos_cost_verdict": ens.extra.get("oos_cost_verdict"),
            "risk_disposition": risk.disposition.value, "approval_required": risk.approval_required,
            "execution_enabled": risk.execution_enabled}
    return out


def verify_release(artifact_root: Path, release_id: str, experiment_cfg: Path, bars: pd.DataFrame, ruleset: Path,
                   smoke: Callable[..., dict[str, Any]] = default_smoke) -> dict[str, Any]:
    rdir = release_dir(artifact_root, release_id)
    rel = read_json(rdir / "release.json")
    bexp = rdir / "bundle" / rel["experiment_id"]
    failures: list[str] = check_integrity(rdir)
    for aid in rel["artifact_ids"]:
        meta = read_json(bexp / "artifacts" / aid / "metadata.json")
        missing = [k for k in REQUIRED_META_KEYS if k not in meta]
        if missing:
            failures.append(f"{aid}: metadata missing {missing}")
    failures += _leak_problems(bexp)
    smoke_out: dict[str, Any] = {}
    if not failures:
        try:
            smoke_out = smoke(rdir / "bundle", experiment_cfg, bars, ruleset)
            for h, r in smoke_out["horizons"].items():
                for kind, b in r["base"].items():
                    if b["artifact_id"] and b["status"] != "OK":
                        failures.append(f"smoke h{h} {kind}: {b['status']} {b['reason']}")
                if r["execution_enabled"] or not r["approval_required"]:
                    failures.append(f"smoke h{h}: safety flags changed")
        except Exception as exc:  # noqa: BLE001 - a crash is a failed check, recorded
            failures.append(f"smoke crashed: {type(exc).__name__}: {exc}"[:400])
    comparison = _compare_with_active(artifact_root, rel)
    checks = {"release_id": release_id, "release_json_sha256": _release_hash(rdir), "checked_at": _now(),
              "host": {"machine": platform.machine(), "system": platform.system(), "python": platform.python_version()},
              "passed": not failures, "failures": failures, "smoke": smoke_out, "comparison_with_active": comparison,
              "note": "passing = consistent and loadable on this host; not evidence of skill or profitability"}
    atomic_write_json(rdir / "checks.json", checks)
    return checks


def _compare_with_active(artifact_root: Path, rel: dict[str, Any]) -> dict[str, Any]:
    exp = Path(artifact_root) / rel["experiment_id"]
    active = read_json(exp / "registry.json") if (exp / "registry.json").exists() else {"horizons": {}}
    out = {}
    for h in sorted(set(active.get("horizons", {})) | set(rel["registry"].get("horizons", {}))):
        a, c = active.get("horizons", {}).get(h, {}), rel["registry"]["horizons"].get(h, {})
        out[h] = {"changed_slots": sorted(k for k in set(a) | set(c) if a.get(k) != c.get(k)), "active": a, "candidate": c}
    evals = sorted((Path(artifact_root) / "_releases" / rel["release_id"] / "bundle" / rel["experiment_id"] / "evaluation").glob("eval_*.json"))
    out["candidate_latest_evaluation"] = evals[-1].name if evals else None
    out["active_latest_evaluation"] = (sorted((exp / "evaluation").glob("eval_*.json")) or [None])[-1]
    out["active_latest_evaluation"] = out["active_latest_evaluation"].name if out["active_latest_evaluation"] else None
    return out


# ---------------------------------------------------------------- activate / rollback (explicit approval only)
def activate_release(artifact_root: Path, release_id: str, approve: str | None) -> dict[str, Any]:
    if approve != release_id:
        raise PermissionError(f"activation requires explicit approval: --approve {release_id}")
    rdir = release_dir(artifact_root, release_id)
    checks_p = rdir / "checks.json"
    if not checks_p.exists():
        raise PermissionError("no checks.json: run `release verify` on this host first")
    checks = read_json(checks_p)
    if not checks.get("passed") or checks.get("release_json_sha256") != _release_hash(rdir):
        raise PermissionError("checks did not pass for this exact release.json")
    if check_integrity(rdir):
        raise PermissionError("staged release changed since verification")
    rel = read_json(rdir / "release.json")
    exp = Path(artifact_root) / rel["experiment_id"]
    bexp = rdir / "bundle" / rel["experiment_id"]
    to_copy = []
    for aid in rel["artifact_ids"]:
        dst = exp / "artifacts" / aid
        if dst.exists():
            if _files(dst) != _files(bexp / "artifacts" / aid):
                raise FileExistsError(f"artifact id {aid} already exists with different content (never overwritten)")
        else:
            to_copy.append(aid)
    for aid in to_copy:
        tmp = exp / "artifacts" / f".{aid}.activating"
        shutil.copytree(bexp / "artifacts" / aid, tmp)
        os.rename(tmp, exp / "artifacts" / aid)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    hist = exp / "registry_history"
    hist.mkdir(parents=True, exist_ok=True)
    backup = None
    if (exp / "registry.json").exists():
        backup = hist / f"{stamp}-before-{release_id}.json"
        shutil.copy2(exp / "registry.json", backup)
    atomic_write_json(exp / "registry.json", rel["registry"])
    record = {"release_id": release_id, "activated_at": _now(), "approved_as": approve, "previous_registry_backup": str(backup) if backup else None,
              "copied_artifacts": to_copy, "checks_sha256": sha256_file(checks_p)}
    atomic_write_json(exp / "active_release.json", record)
    with open(hist / "activations.jsonl", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record) + "\n")
    return record


def rollback_registry(artifact_root: Path, experiment_id: str, backup_name: str, approve: str | None) -> dict[str, Any]:
    if approve != backup_name:
        raise PermissionError(f"rollback requires explicit approval: --approve {backup_name}")
    exp = Path(artifact_root) / experiment_id
    src = exp / "registry_history" / backup_name
    if not src.is_file():
        raise FileNotFoundError(src)
    target = read_json(src)
    for slots in target.get("horizons", {}).values():
        for aid in slots.values():
            if not (exp / "artifacts" / aid / "metadata.json").exists():
                raise FileNotFoundError(f"rollback target references missing artifact {aid}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cur_backup = exp / "registry_history" / f"{stamp}-before-rollback-to-{backup_name}"
    shutil.copy2(exp / "registry.json", cur_backup)
    atomic_write_json(exp / "registry.json", target)
    record = {"rolled_back_to": backup_name, "at": _now(), "previous_registry_backup": str(cur_backup)}
    atomic_write_json(exp / "active_release.json", {"release_id": None, **record})
    with open(exp / "registry_history" / "activations.jsonl", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record) + "\n")
    return record
