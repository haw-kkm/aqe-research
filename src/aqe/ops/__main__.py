"""`python -m aqe.ops <command>` — operations CLI (docs/CLOUD_DEPLOYMENT.md). JSON on stdout; exit 0 = OK.

Safe defaults: transfers are DRY-RUN without --execute; release activation/rollback need --approve <id>;
nothing here deletes data, creates cloud resources or enables trading.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _bars(args, exp_cfg: Path):
    from aqe.config import load_experiment_config
    from aqe.storage.bars import load_fixture_bars, read_bars

    exp, base = load_experiment_config(exp_cfg)
    if args.fixture_bars:
        return load_fixture_bars(Path(args.fixture_bars), exp.instrument, 2.0)
    data_root = Path(args.data_root) if args.data_root else base.paths.data_root
    return read_bars(data_root, exp.instrument, "5m")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m aqe.ops", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("platform", help="OS / arch / python / torch / CUDA facts of this host")

    p = sub.add_parser("inventory", help="read-only inventory of data/artifact roots")
    p.add_argument("--data-root")
    p.add_argument("--artifact-root")
    p.add_argument("--no-hash", action="store_true")
    p.add_argument("--settle-seconds", type=int, default=900)
    p.add_argument("--out-json")
    p.add_argument("--out-md")

    p = sub.add_parser("transfer", help="plan (default DRY-RUN) or --execute a verified transfer")
    p.add_argument("--source", required=True, help="local path or ssh://user@host/abs/path")
    p.add_argument("--dest", required=True, help="local path or ssh://user@host/abs/path")
    p.add_argument("--kind", choices=("data", "artifacts", "release"), default="data")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--transfer-id")
    p.add_argument("--settle-seconds", type=int, default=900)
    p.add_argument("--remote-python", default="python3")
    p.add_argument("--work-dir")
    p.add_argument("--mode", choices=("safe", "mirror"), default="safe",
                   help="safe: never replace (Windows->Oracle). mirror: update files the destination last received from this source unchanged (Oracle->Google)")

    p = sub.add_parser("transfer-manifest", help="(remote side) source manifest as JSON")
    p.add_argument("--root", required=True)
    p.add_argument("--kind", default="data")
    p.add_argument("--settle-seconds", type=int, default=900)
    p.add_argument("--snapshot-dir")
    p = sub.add_parser("transfer-index", help="(remote side) SHA-256 of listed rel paths (JSON list on stdin)")
    p.add_argument("--root", required=True)
    p = sub.add_parser("transfer-received", help="(remote side) this root's received-version index as JSON")
    p.add_argument("--root", required=True)
    p = sub.add_parser("transfer-merge", help="union collector partitions kept as conflicts into the destination (DRY-RUN default)")
    p.add_argument("--root", required=True)
    p.add_argument("--transfer-id", required=True)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--settle-seconds", type=int, default=900)
    p = sub.add_parser("transfer-finalize", help="(destination side) verify + place a transfer's incoming files")
    p.add_argument("--root", required=True)
    p.add_argument("--transfer-id", required=True)
    p = sub.add_parser("transfer-rollback", help="move files placed by a transfer to its rolled_back dir (DRY-RUN default)")
    p.add_argument("--root", required=True)
    p.add_argument("--transfer-id", required=True)
    p.add_argument("--execute", action="store_true")

    p = sub.add_parser("sqlite-backup", help="online backup of a (live) SQLite DB to a NEW file + integrity check")
    p.add_argument("--db", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("sqlite-restore", help="restore a verified backup to a NEW path")
    p.add_argument("--backup", required=True)
    p.add_argument("--target", required=True)

    p = sub.add_parser("release-build", help="(training host) bundle the registry's artifacts")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--experiment", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--note", default="")
    p = sub.add_parser("release-stage", help="(inference host) stage a received release dir")
    p.add_argument("--received", required=True)
    p.add_argument("--artifact-root", required=True)
    p = sub.add_parser("release-verify", help="integrity + leakage + inference smoke on THIS host")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--release-id", required=True)
    p.add_argument("--experiment-config", required=True)
    p.add_argument("--ruleset", default="configs/risk_rules_v1.yaml")
    p.add_argument("--fixture-bars", help="smoke on synthetic fixture bars (mechanics only, no market meaning)")
    p.add_argument("--data-root")
    p = sub.add_parser("release-activate", help="switch the active registry (needs --approve <release_id>)")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--release-id", required=True)
    p.add_argument("--approve")
    p = sub.add_parser("release-rollback", help="restore a backed-up registry (needs --approve <backup file name>)")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--experiment", required=True)
    p.add_argument("--backup", required=True)
    p.add_argument("--approve")

    p = sub.add_parser("health", help="collector healthcheck (exit 0 OK / 1 WARN / 2 CRITICAL)")
    p.add_argument("--config", required=True)
    p.add_argument("--record", action="store_true", help="append to {data_root}/ops/health.jsonl")
    p.add_argument("--min-free-gb", type=float, default=20.0)
    p = sub.add_parser("preflight", help="role checks before enabling a service")
    p.add_argument("--config", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--offline", action="store_true")
    p = sub.add_parser("cutover-compare", help="compare PC vs cloud collector data over an overlap window (GO/NO-GO)")
    p.add_argument("--ref-root", required=True, help="PC collector data_root (or a copy of it)")
    p.add_argument("--new-root", required=True, help="cloud collector data_root (or a copy of it)")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--health", help="cloud ops/health.jsonl")
    p.add_argument("--out", required=True)
    p = sub.add_parser("cutover-approve", help="record the operator's approval (stops nothing)")
    p.add_argument("--report", required=True)
    p.add_argument("--approve")
    p.add_argument("--migration-receipt", required=True)
    p = sub.add_parser("code-release-check", help="verify the checkout is exactly one clean commit (+ tests)")
    p.add_argument("--repo", default=".")
    p.add_argument("--run-tests", action="store_true")
    p.add_argument("--expect-commit")
    p.add_argument("--out")

    a = ap.parse_args(argv)
    if a.cmd == "platform":
        from aqe.ops.platform import detect

        _print(detect())
        return 0
    if a.cmd == "inventory":
        from aqe.ops.inventory import render_markdown, scan

        invs = []
        if a.data_root:
            invs.append(scan(Path(a.data_root), "data", hash_files=not a.no_hash, settle_seconds=a.settle_seconds))
        if a.artifact_root:
            invs.append(scan(Path(a.artifact_root), "artifacts", hash_files=not a.no_hash, settle_seconds=a.settle_seconds))
        if a.out_json:
            Path(a.out_json).parent.mkdir(parents=True, exist_ok=True)
            Path(a.out_json).write_text(json.dumps(invs, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        if a.out_md:
            Path(a.out_md).parent.mkdir(parents=True, exist_ok=True)
            Path(a.out_md).write_text(render_markdown(invs), encoding="utf-8")
        _print([{k: v for k, v in i.items() if k != "files"} for i in invs])
        return 0
    if a.cmd == "transfer":
        from aqe.ops.transfer import Endpoint, run_transfer

        r = run_transfer(Endpoint.parse(a.source, a.remote_python), Endpoint.parse(a.dest, a.remote_python), a.kind,
                         execute=a.execute, transfer_id=a.transfer_id, settle_seconds=a.settle_seconds,
                         work_dir=Path(a.work_dir) if a.work_dir else None, mode=a.mode)
        _print(r)
        return 0 if (not a.execute or r.get("complete")) else 1
    if a.cmd == "transfer-manifest":
        from aqe.ops.transfer import build_manifest

        _print(build_manifest(Path(a.root), a.kind, settle_seconds=a.settle_seconds,
                              snapshot_dir=Path(a.snapshot_dir) if a.snapshot_dir else None))
        return 0
    if a.cmd == "transfer-index":
        from aqe.ops.transfer import dest_index

        _print(dest_index(Path(a.root), json.loads(sys.stdin.read() or "[]")))
        return 0
    if a.cmd == "transfer-received":
        from aqe.ops.transfer import received_index

        _print(received_index(Path(a.root)))
        return 0
    if a.cmd == "transfer-merge":
        from aqe.ops.merge import merge_conflicts

        r = merge_conflicts(Path(a.root), a.transfer_id, execute=a.execute, settle_seconds=a.settle_seconds)
        _print(r)
        return 0 if not r["failed"] else 1
    if a.cmd == "transfer-finalize":
        from aqe.ops.transfer import Plan, finalize

        plan = Plan.from_json(json.loads((Path(a.root) / "_transfer" / a.transfer_id / "plan.json").read_text(encoding="utf-8")))
        r = finalize(Path(a.root), plan)
        _print(r)
        return 0 if r["complete"] else 1
    if a.cmd == "transfer-rollback":
        from aqe.ops.transfer import rollback

        _print(rollback(Path(a.root), a.transfer_id, execute=a.execute))
        return 0
    if a.cmd == "sqlite-backup":
        from aqe.ops.inventory import sqlite_backup

        r = sqlite_backup(Path(a.db), Path(a.out))
        _print(r)
        return 0 if r["integrity"] == "ok" else 1
    if a.cmd == "sqlite-restore":
        from aqe.ops.inventory import sqlite_restore

        _print(sqlite_restore(Path(a.backup), Path(a.target)))
        return 0
    if a.cmd.startswith("release-"):
        from aqe.ops import release as rel

        if a.cmd == "release-build":
            _print(rel.build_release(Path(a.artifact_root), a.experiment, Path(a.out), note=a.note))
        elif a.cmd == "release-stage":
            _print(rel.stage_release(Path(a.received), Path(a.artifact_root)))
        elif a.cmd == "release-verify":
            r = rel.verify_release(Path(a.artifact_root), a.release_id, Path(a.experiment_config),
                                   _bars(a, Path(a.experiment_config)), Path(a.ruleset))
            _print(r)
            return 0 if r["passed"] else 1
        elif a.cmd == "release-activate":
            _print(rel.activate_release(Path(a.artifact_root), a.release_id, a.approve))
        elif a.cmd == "release-rollback":
            _print(rel.rollback_registry(Path(a.artifact_root), a.experiment, a.backup, a.approve))
        return 0
    if a.cmd in ("health", "preflight"):
        from aqe.config import load_local_config

        cfg = load_local_config(a.config)
        if a.cmd == "health":
            from aqe.ops.health import LEVELS, collector_health, record_health

            r = collector_health(cfg.paths.data_root, cfg.instruments[0].instrument_id, min_free_gb=a.min_free_gb,
                                 max_clock_offset_ms=cfg.clock.max_abs_skew_ms)
            if a.record:
                record_health(cfg.paths.data_root, r)
            _print(r)
            return LEVELS[r["level"]]
        from aqe.ops.preflight import run_preflight

        r = run_preflight(cfg, a.role, offline=a.offline)
        _print(r)
        return 1 if r["overall"] == "FAIL" else 0
    if a.cmd == "cutover-compare":
        from aqe.ops.cutover import compare_collectors, readiness, write_report

        comp = compare_collectors(Path(a.ref_root), Path(a.new_root), a.symbol, a.start, a.end)
        health = [json.loads(x) for x in Path(a.health).read_text(encoding="utf-8").splitlines() if x.strip()] if a.health else []
        rep = write_report(Path(a.out), comp, readiness(comp, health))
        _print({k: v for k, v in rep.items() if k != "comparison"} | {"coverage_new": comp["coverage_new"]})
        return 0 if rep["readiness"]["ready"] else 1
    if a.cmd == "cutover-approve":
        from aqe.ops.cutover import approve

        _print(approve(Path(a.report), a.approve, Path(a.migration_receipt)))
        return 0
    if a.cmd == "code-release-check":
        from aqe.ops.coderelease import check_code_release

        r = check_code_release(Path(a.repo), run_tests=a.run_tests, expect_commit=a.expect_commit)
        if a.out:
            Path(a.out).write_text(json.dumps(r, indent=2), encoding="utf-8")
        _print(r)
        return 0 if r["verified"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
