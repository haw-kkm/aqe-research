"""AQE command line (B12). Every command writes a run record with run id, config hash, inputs,
outputs, status and exit code under {data_root}/runs/.

Exit codes: 0 OK · 1 error · 3 BLOCKED (external precondition missing) · 4 NOT_READY (explicit abstain)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from aqe.config import config_hash, load_experiment_config, load_local_config
from aqe.storage.runs import EXIT_ERROR, EXIT_OK, run_context

LOCAL_COMMANDS = {"doctor", "init-db", "collect", "backfill", "context-worker", "analyze", "report", "export-schemas", "context-enqueue"}
EXPERIMENT_COMMANDS = {"build-dataset", "train-lgbm", "train-tcn", "build-oos", "train-meta", "calibrate", "evaluate"}


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise SystemExit("--as-of must include a timezone (e.g. 2026-11-10T00:00:10Z)")
    return d


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aqe", description="AQE research system (research/replay/shadow; no execution)")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--config", required=True, type=Path)
        return sp

    d = add("doctor", "diagnose Python, storage, clock, DB and providers")
    d.add_argument("--offline", action="store_true", help="skip network checks")
    add("init-db", "apply DB migrations (PostgreSQL if configured; local SQLite store always)")
    c = add("collect", "continuous public-data collection (WS depth/aggTrade/markPrice/kline + OI polling)")
    c.add_argument("--duration-seconds", type=float, default=None)
    c.add_argument("--instrument", default=None)
    b = add("backfill", "REST backfill of klines / funding / OI history with limits recorded")
    b.add_argument("--what", default="klines,funding,oi", help="comma list: klines,funding,oi")
    b.add_argument("--dry-run", action="store_true", help="print the plan without fetching")
    add("build-dataset", "features + per-horizon labels + walk-forward folds + manifest")
    for name, h in [("train-lgbm", "train final LightGBM base models"), ("train-tcn", "train final small TCN base models"),
                    ("build-oos", "fold-wise retraining and time-ordered OOS predictions"),
                    ("train-meta", "meta model on OOS inputs per horizon/backend"),
                    ("calibrate", "temperature scaling on the independent calibration segment"),
                    ("evaluate", "locked final-segment evaluation incl. costs (E0-E3 available)")]:
        sp = add(name, h)
        if name in ("build-oos", "train-lgbm", "train-tcn"):
            sp.add_argument("--models", default="lgbm,tcn" if name == "build-oos" else None)
            sp.add_argument("--horizons", default=None, help="comma list of horizon seconds (default: all)")
    w = add("context-worker", "process context jobs with evidence-gated backend")
    w.add_argument("--once", action="store_true")
    w.add_argument("--max-jobs", type=int, default=None)
    q = add("context-enqueue", "create an evidence bundle snapshot and enqueue a context job")
    q.add_argument("--as-of", default=None)
    a = add("analyze", "analyse latest valid inputs with registered artifacts; explicit abstain if not ready")
    a.add_argument("--as-of", default=None)
    r = add("report", "re-render a stored analysis as Markdown (does not change judgements)")
    r.add_argument("--analysis-id", default=None)
    e = add("export-schemas", "write JSON Schemas of contracts")
    e.add_argument("--out", type=Path, default=Path("docs/schemas"))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.command
    if cmd in EXPERIMENT_COMMANDS:
        exp, base = load_experiment_config(args.config)
        h = config_hash(exp)
        runs_dir = base.paths.data_root / "runs"
    else:
        base = load_local_config(args.config)
        exp = None
        h = config_hash(base)
        runs_dir = base.paths.data_root / "runs"
    with run_context(cmd, args.config, h, runs_dir) as rec:
        rec.inputs["argv"] = sys.argv[1:] if argv is None else argv
        code = _dispatch(cmd, args, base, exp, h, rec)
        rec.exit_code = code
    print(json.dumps({"run_id": rec.run_id, "command": cmd, "status": rec.status, "exit_code": rec.exit_code,
                      "outputs": rec.outputs, "notes": rec.notes}, ensure_ascii=False, indent=2, default=str))
    return rec.exit_code if rec.exit_code is not None else EXIT_ERROR


def _dispatch(cmd, args, base, exp, h, rec) -> int:
    if cmd == "doctor":
        from aqe.cli.doctor import run_doctor
        from aqe.storage import atomic_write_json

        res = run_doctor(base, network=not args.offline)
        out = base.paths.data_root / "status" / "doctor_latest.json"
        atomic_write_json(out, res)
        rec.outputs.update({"doctor_json": str(out), "overall": res["overall"]})
        for name, c in res["checks"].items():
            print(f"[{c.get('status', '?'):>13}] {name}: " + json.dumps({k: v for k, v in c.items() if k != 'status'}, ensure_ascii=False, default=str))
        return EXIT_ERROR if res["overall"] == "FAIL" else EXIT_OK
    if cmd == "export-schemas":
        from aqe.contracts import export_json_schemas

        paths = export_json_schemas(args.out)
        rec.outputs["schemas"] = [str(p) for p in paths]
        return EXIT_OK
    if cmd == "init-db":
        from aqe.storage.metadb import init_db

        return init_db(base, rec)
    if cmd == "collect":
        from aqe.ingestion.collector import run_collect

        return run_collect(base, rec, duration_seconds=args.duration_seconds, instrument=args.instrument)
    if cmd == "backfill":
        from aqe.ingestion.backfill import run_backfill

        return run_backfill(base, rec, what=args.what.split(","), dry_run=args.dry_run)
    if cmd == "analyze":
        from aqe.cli.analyze import run_analyze

        return run_analyze(base, h, rec, _parse_dt(args.as_of))
    if cmd == "report":
        from aqe.cli.analyze import run_report

        return run_report(base, rec, args.analysis_id)
    if cmd == "context-worker":
        from aqe.context.worker import run_worker

        return run_worker(base, rec, once=args.once, max_jobs=args.max_jobs)
    if cmd == "context-enqueue":
        from aqe.context.worker import enqueue_for_asof

        return enqueue_for_asof(base, rec, _parse_dt(args.as_of))
    if cmd == "build-dataset":
        from aqe.datasets.build import run_build_dataset

        return run_build_dataset(exp, base, h, rec)
    horizons = None
    if getattr(args, "horizons", None):
        horizons = [int(x) for x in args.horizons.split(",")]
    if cmd == "build-oos":
        from aqe.ensemble.oos import run_build_oos

        return run_build_oos(exp, base, h, rec, models=args.models.split(","), horizons=horizons)
    if cmd in ("train-lgbm", "train-tcn"):
        from aqe.ensemble.oos import run_train_final

        return run_train_final(exp, base, h, rec, kind="lgbm" if cmd == "train-lgbm" else "tcn", horizons=horizons)
    if cmd == "train-meta":
        from aqe.ensemble.meta import run_train_meta

        return run_train_meta(exp, base, h, rec)
    if cmd == "calibrate":
        from aqe.ensemble.calibration import run_calibrate

        return run_calibrate(exp, base, h, rec)
    if cmd == "evaluate":
        from aqe.evaluation.evaluate import run_evaluate

        return run_evaluate(exp, base, h, rec)
    raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
