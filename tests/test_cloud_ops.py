"""Cloud deployment tooling (docs/CLOUD_DEPLOYMENT.md): config/paths, venv mapping, collector durability, transfer,
SQLite backup/restore, inventory, model release/rollback, health, preflight, code release, cutover gate.

All local: temp dirs, synthetic files, a fake ssh/scp runner that executes the remote side on this machine.
Nothing here is evidence that ARM64, a Google GPU, real SSH or Binance from a cloud host works.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace as NS

import pandas as pd
import pytest
import yaml

from aqe.config import expand_env, load_experiment_config, load_local_config, project_root_from
from aqe.ops import release as rel
from aqe.ops.coderelease import check_code_release
from aqe.ops.cutover import approve, compare_collectors, readiness, write_report
from aqe.ops.health import collector_health
from aqe.ops.inventory import check_manifest, classify_data, rebase, scan, sqlite_backup, sqlite_restore, validate_parquet
from aqe.ops.platform import resolve_venv_python, role_requirements, venv_python
from aqe.ops.preflight import run_preflight
from aqe.ops.transfer import TRANSFER_DIR, Endpoint, rollback, run_transfer
from aqe.storage import sha256_file
from tests.conftest import ROOT

CLOUD = ROOT / "configs" / "cloud"


# ------------------------------------------------------------------ configs, paths, platform
def test_env_defaults_and_unresolved_paths(monkeypatch):
    monkeypatch.delenv("AQE_X_ROOT", raising=False)
    assert expand_env("${AQE_X_ROOT:-/srv/aqe/data}/raw") == "/srv/aqe/data/raw"
    monkeypatch.setenv("AQE_X_ROOT", "/mnt/disk")
    assert expand_env("${AQE_X_ROOT:-/srv/aqe/data}/raw") == "/mnt/disk/raw"
    monkeypatch.delenv("AQE_X_ROOT")
    with pytest.raises(ValueError, match="unresolved"):
        expand_env("${AQE_X_ROOT}/raw")  # no default: never left as a literal path
    assert project_root_from(CLOUD / "oracle_collector.yaml") == ROOT  # nested configs/<group>/<file>
    assert project_root_from(ROOT / "configs" / "local.yaml") == ROOT


@pytest.mark.parametrize("name,mode,exp", [("oracle_collector", "research", "experiment_oracle"), ("oracle_shadow", "shadow", "experiment_oracle"),
                                           ("google_collector", "research", "experiment_google"),
                                           ("google_cpu_train", "research", "experiment_google"),
                                           ("google_gpu_train", "research", "experiment_google_gpu")])
def test_role_configs_load_with_server_paths(monkeypatch, tmp_path, name, mode, exp):
    monkeypatch.setenv("AQE_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AQE_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    cfg = load_local_config(CLOUD / f"{name}.yaml")
    assert cfg.mode.value == mode and cfg.paths.data_root == tmp_path / "data" and cfg.paths.artifact_root == tmp_path / "artifacts"
    assert cfg.risk_ruleset.exists() and cfg.analysis.experiment_config == CLOUD / f"{exp}.yaml"
    # safety conditions unchanged on servers: no LLM spend, no context backend, dev trade plans off
    assert cfg.context.max_cost_usd_per_day == 0.0 and cfg.context.backend == "none" and cfg.planning.enabled is False
    e, base = load_experiment_config(cfg.analysis.experiment_config)
    assert base.paths.artifact_root == tmp_path / "artifacts"  # base_config points at a server config, not E:\...


@pytest.mark.parametrize("exp", ["experiment_oracle", "experiment_google", "experiment_google_gpu"])
def test_cloud_experiments_equal_the_original_except_base_config(exp):
    orig = yaml.safe_load((ROOT / "configs" / "experiment.yaml").read_text(encoding="utf-8"))
    cloud = yaml.safe_load((CLOUD / f"{exp}.yaml").read_text(encoding="utf-8"))
    assert cloud.pop("base_config").startswith("configs/cloud/") and orig.pop("base_config") == "configs/local.yaml"
    assert cloud == orig  # same experiment id, segments, horizons, model params: artifacts stay comparable


def test_role_configs_keep_collector_settings_identical_to_windows():
    local = yaml.safe_load((ROOT / "configs" / "local.yaml").read_text(encoding="utf-8"))
    for name in ("oracle_collector", "oracle_shadow", "google_collector", "google_cpu_train", "google_gpu_train"):
        c = yaml.safe_load((CLOUD / f"{name}.yaml").read_text(encoding="utf-8"))
        for key in ("instruments", "binance", "backfill", "collector", "database", "clock", "risk_ruleset"):
            assert c[key] == local[key], (name, key)


def test_worker_venv_resolves_across_windows_and_linux_layouts(tmp_path):
    (tmp_path / ".venv-ta" / "bin").mkdir(parents=True)
    (tmp_path / ".venv-ta" / "bin" / "python").write_text("", encoding="utf-8")
    win_style = resolve_venv_python(".venv-ta/Scripts/python.exe", tmp_path)
    (tmp_path / ".venv-w" / "Scripts").mkdir(parents=True)
    (tmp_path / ".venv-w" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    linux_style = resolve_venv_python(".venv-w/bin/python", tmp_path)
    if os.name == "nt":
        assert linux_style == tmp_path / ".venv-w" / "Scripts" / "python.exe"
        assert win_style == tmp_path / ".venv-ta" / "Scripts" / "python.exe"  # not present on Windows layout -> configured path
    else:
        assert win_style == tmp_path / ".venv-ta" / "bin" / "python"
    assert resolve_venv_python(tmp_path / "nowhere.exe", tmp_path) == tmp_path / "nowhere.exe"  # missing stays missing
    assert resolve_venv_python(None, tmp_path) is None
    assert venv_python(Path("v"), windows=False) == Path("v/bin/python") and venv_python(Path("v"), windows=True) == Path("v/Scripts/python.exe")
    assert role_requirements("oracle-collector")["arch"] == "arm64" and role_requirements("google-gpu-train")["gpu"]
    with pytest.raises(ValueError):
        role_requirements("aws")


def test_rebase_maps_recorded_windows_and_posix_paths(tmp_path):
    assert rebase(r"E:\aqe-v2\data\normalized\bars\x\5m\2019-09.parquet", tmp_path) == tmp_path / "normalized/bars/x/5m/2019-09.parquet"
    assert rebase("/srv/aqe/data/raw/a.jsonl.gz", tmp_path) == tmp_path / "raw/a.jsonl.gz"
    assert rebase("relative/no/anchor.parquet", tmp_path) is None


# ------------------------------------------------------------------ collector durability on Linux / restarts
def test_quality_events_are_journaled_durably_across_restarts(tmp_path):
    from aqe.ingestion.collector import GROUP_MARKET, GROUP_PUBLIC, Collector
    from tests.test_collector_offline import INST, agg, depth, make_cfg

    cfg = make_cfg(tmp_path)
    a = Collector(cfg, INST, "run-a")
    a.book.on_connect()
    a.handle_text(GROUP_PUBLIC, depth(100, 110, 99, bids=[(70000, 1)], asks=[(70001, 2)]))
    a.book.begin_snapshot()
    a.apply_snapshot_payload({"lastUpdateId": 105, "bids": [["70000", "0.5"]], "asks": [["70001", "1"]]})
    a.handle_text(GROUP_MARKET, agg(10))
    a.book.on_disconnect("network drop")  # depth stream gap: book must resynchronise
    a._drain_book_quality()
    a.close()
    b = Collector(cfg, INST, "run-b")  # restart on the same data root
    b.handle_text(GROUP_MARKET, agg(12))  # 11 missed while down
    b.close()
    days = sorted(a.quality_dir.glob("*.jsonl"))
    events = [json.loads(line) for d in days for line in d.read_text(encoding="utf-8").splitlines()]
    kinds = [(e["run_id"], e["kind"]) for e in events]
    assert any(r == "run-a" and k.startswith("BOOK_") for r, k in kinds)  # the gap is recorded, not just in memory
    assert ("run-b", "RESTART") in kinds  # restart from checkpoint recorded by the next run
    assert all(e["instrument_id"] == "BTCUSDT" and e["at"] for e in events)
    n = len(events)
    c = Collector(cfg, INST, "run-c")
    c.close()
    assert sum(len(d.read_text(encoding="utf-8").splitlines()) for d in sorted(a.quality_dir.glob("*.jsonl"))) > n  # append-only


def _load_lc():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lc_cloud_test", ROOT / "local-collector" / "collect_local.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sigterm_takes_the_graceful_stop_path_on_posix():
    lc = _load_lc()
    assert "os.name != 'nt' and signal.signal(signal.SIGTERM, signal.default_int_handler)" in lc.CHILD_BOOTSTRAP
    before = signal.getsignal(signal.SIGTERM)
    try:
        assert lc.install_posix_term_handler(posix=False) is False and signal.getsignal(signal.SIGTERM) == before
        assert lc.install_posix_term_handler(posix=True) is True
        assert signal.getsignal(signal.SIGTERM) is signal.default_int_handler  # -> KeyboardInterrupt -> flush + checkpoint
    finally:
        signal.signal(signal.SIGTERM, before)
    unit = (ROOT / "deploy/linux/systemd/aqe-collector@.service").read_text(encoding="utf-8")
    assert "KillSignal=SIGINT" in unit and "KillMode=mixed" in unit and "Restart=always" in unit and "ExecStartPre=" in unit


def test_clock_failure_hint_matches_the_os(tmp_path):
    lc = _load_lc()
    assert "timedatectl" in lc.clock_sync_hint(windows=False) and "Windows Settings" in lc.clock_sync_hint(windows=True)
    cfg = NS(paths=NS(data_root=tmp_path / "data"), binance=NS(), clock=NS(max_abs_skew_ms=1000))

    class Clock:
        def __init__(self, _c): pass
        def measure_clock(self, samples): return NS(local_minus_server_ms=5000.0)
        def close(self): pass

    with pytest.raises(RuntimeError, match="5000.0 ms") as e:
        lc.preflight(cfg, 0.000001, clock_factory=Clock)
    assert lc.clock_sync_hint() in str(e.value)  # this host's hint (Windows here, chrony on Linux)


# ------------------------------------------------------------------ inventory, parquet, SQLite
def _make_data_root(root: Path) -> None:
    old = time.time() - 7200
    files = {
        "raw/binance/ws/public/public_BTCUSDT.20261110T194122Z.jsonl.gz": b"raw-capture-1",
        "raw/binance/ws/public/public_BTCUSDT.20261112T131201Z.jsonl.gz.open": b"being written",
        "status/collector_BTCUSDT.checkpoint.json": b"{}",
        "context/assessments/x/a.json": b"{}",
        "manifests/m.json": b"",
        ".aqe-collect-probe-123": b"x",
    }
    for rel_, data in files.items():
        p = root / rel_
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.utime(p, (old, old))
    bars = root / "normalized/bars/binance/usdm_perpetual/BTCUSDT/5m/2019-09.parquet"
    bars.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"open_time": pd.to_datetime(["2019-09-08T17:55:00Z", "2019-09-30T23:55:00Z"]), "close": [1.0, 2.0]}).to_parquet(bars)
    os.utime(bars, (old, old))
    (root / "manifests/m.json").write_text(json.dumps({"files": [{"path": "E:\\aqe-v2\\data\\normalized\\bars\\binance\\usdm_perpetual\\BTCUSDT\\5m\\2019-09.parquet",
                                                                  "sha256": sha256_file(bars)}]}), encoding="utf-8")
    os.utime(root / "manifests/m.json", (old, old))
    db = root / "meta/aqe_meta.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db)) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, v TEXT)")
        c.executemany("INSERT INTO jobs (v) VALUES (?)", [("a",), ("b",), ("c",)])
        c.commit()


def test_inventory_classifies_and_verifies_without_writing(tmp_path):
    root = tmp_path / "data"
    _make_data_root(root)
    before = {p: p.stat().st_mtime for p in root.rglob("*") if p.is_file()}
    inv = scan(root, "data")
    cls = {f["rel"]: f["class"] for f in inv["files"]}
    assert cls["raw/binance/ws/public/public_BTCUSDT.20261110T194122Z.jsonl.gz"] == "transfer"
    assert cls["raw/binance/ws/public/public_BTCUSDT.20261112T131201Z.jsonl.gz.open"] == "live"
    assert cls["status/collector_BTCUSDT.checkpoint.json"] == "host_local" and cls["meta/aqe_meta.sqlite"] == "snapshot"
    assert cls[".aqe-collect-probe-123"] == "temp"
    assert inv["manifests"][0]["ok"] and inv["manifests"][0]["ok_files"] == 1  # Windows path in manifest mapped, hash verified
    assert inv["sqlite"][0]["integrity"] == "ok" and inv["sqlite"][0]["tables"] == {"jobs": 3}
    assert inv["time_ranges"]["normalized/bars"][0].startswith("2019-09-08")
    assert {p: p.stat().st_mtime for p in root.rglob("*") if p.is_file()} == before  # read-only
    assert classify_data("quality/binance/usdm_perpetual/BTCUSDT/2000-01-01.jsonl", 0)[0] == "transfer"
    assert classify_data("quality/binance/usdm_perpetual/BTCUSDT/2999-01-01.jsonl", 0)[0] == "live"
    assert classify_data("whatever/new.bin", 0)[0] == "unclassified"


def test_manifest_detects_mismatch_and_missing_files(tmp_path):
    root = tmp_path / "data"
    _make_data_root(root)
    bars = next(root.rglob("2019-09.parquet"))
    bars.write_bytes(bars.read_bytes() + b"tamper")
    r = check_manifest(root / "manifests/m.json", root)
    assert not r["ok"] and r["mismatch"]
    bars.unlink()
    assert check_manifest(root / "manifests/m.json", root)["missing"]


def test_parquet_validation_flags_corrupt_files(tmp_path):
    good = tmp_path / "g.parquet"
    pd.DataFrame({"a": [1]}).to_parquet(good)
    bad = tmp_path / "b.parquet"
    bad.write_bytes(b"PAR1 not a parquet footer")
    assert validate_parquet(good)["ok"] and validate_parquet(good)["rows"] == 1
    assert not validate_parquet(bad)["ok"]


def test_sqlite_online_backup_of_a_live_db_and_restore(tmp_path):
    db = tmp_path / "live.sqlite"
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE t (x INTEGER)")
        writer.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(1000)])
        writer.commit()
        writer.execute("INSERT INTO t VALUES (-1)")  # an open, uncommitted write in progress
        b = sqlite_backup(db, tmp_path / "bk" / "live.sqlite")
        assert b["integrity"] == "ok" and b["tables"] == {"t": 1000}  # consistent committed state only
        with pytest.raises(FileExistsError):
            sqlite_backup(db, tmp_path / "bk" / "live.sqlite")  # never overwrites
    finally:
        writer.rollback()
        writer.close()
    r = sqlite_restore(tmp_path / "bk" / "live.sqlite", tmp_path / "restored" / "db.sqlite")
    assert r["integrity"] == "ok"
    with closing(sqlite3.connect(tmp_path / "restored" / "db.sqlite")) as c:
        assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 1000
    with pytest.raises(FileExistsError):
        sqlite_restore(tmp_path / "bk" / "live.sqlite", db)  # never onto an existing DB


# ------------------------------------------------------------------ transfer
def _local(p: Path) -> Endpoint:
    return Endpoint(root=str(p))


def test_transfer_dry_run_changes_nothing(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _make_data_root(src)
    dst.mkdir()
    r = run_transfer(_local(src), _local(dst), "data", work_dir=tmp_path / "w")
    assert r["execute"] is False and "DRY-RUN" in r["note"] and list(dst.iterdir()) == []
    assert r["totals"]["COPY"]["files"] >= 4 and r["totals"]["COPY_SNAPSHOT"]["files"] == 1
    assert r["excluded_by_class"].get("live") == 1 and r["excluded_by_class"].get("host_local") == 1


def test_transfer_execute_verifies_places_and_logs(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _make_data_root(src)
    r = run_transfer(_local(src), _local(dst), "data", execute=True, transfer_id="tx-1", work_dir=tmp_path / "w")
    assert r["complete"] and r["placed"] >= 4 and not r["conflicts"]
    for p in src.rglob("*"):
        rel_ = p.relative_to(src).as_posix()
        if p.is_file() and classify_data(rel_, p.stat().st_mtime)[0] == "transfer":
            assert sha256_file(dst / rel_) == sha256_file(p)
    assert not (dst / "status").exists() and not list(dst.rglob("*.open")) and not (dst / "meta" / "aqe_meta.sqlite").exists()
    snaps = list((dst / TRANSFER_DIR / "tx-1" / "sqlite").rglob("*.sqlite"))
    assert len(snaps) == 1  # live DB arrives only as a verified snapshot, outside the live path
    with closing(sqlite3.connect(snaps[0])) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    log = (dst / TRANSFER_DIR / "transfers.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(log[-1])["bytes_placed"] > 0  # transfer volume is recorded
    again = run_transfer(_local(src), _local(dst), "data", execute=True, transfer_id="tx-2", work_dir=tmp_path / "w2")
    assert again["placed"] == 0 and again["skipped_same"] >= 4  # duplicates detected by content, nothing copied twice


def test_conflicting_content_is_never_overwritten(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _make_data_root(src)
    rel_ = "context/assessments/x/a.json"
    (dst / rel_).parent.mkdir(parents=True)
    (dst / rel_).write_text('{"written by": "the other collector"}', encoding="utf-8")
    r = run_transfer(_local(src), _local(dst), "data", execute=True, transfer_id="tx-c", work_dir=tmp_path / "w")
    assert (dst / rel_).read_text(encoding="utf-8") == '{"written by": "the other collector"}'
    kept = dst / TRANSFER_DIR / "tx-c" / "conflicts" / rel_
    assert r["conflicts"][0]["rel"] == rel_ and kept.read_bytes() == (src / rel_).read_bytes()


def test_sha256_mismatch_is_a_failure_and_nothing_is_placed(tmp_path, monkeypatch):
    import aqe.ops.transfer as tr

    src, dst = tmp_path / "src", tmp_path / "dst"
    _make_data_root(src)
    real = shutil.copy2

    def corrupt(a, b, *k, **kw):
        out = real(a, b, *k, **kw)
        if str(a).endswith(".jsonl.gz"):
            Path(b).write_bytes(Path(b).read_bytes() + b"bitflip")
        return out

    monkeypatch.setattr(tr.shutil, "copy2", corrupt)
    r = run_transfer(_local(src), _local(dst), "data", execute=True, transfer_id="tx-bad", work_dir=tmp_path / "w")
    assert not r["complete"] and any("SHA-256 mismatch" in f["reason"] for f in r["copy_failures"])
    assert not list(dst.rglob("public_BTCUSDT.20261110T194122Z.jsonl.gz"))  # corrupt copy never placed
    assert any("incoming file missing" in f["reason"] for f in r["failed"])


def test_interrupted_transfer_resumes_without_recopying(tmp_path, monkeypatch):
    import aqe.ops.transfer as tr

    src, dst = tmp_path / "src", tmp_path / "dst"
    _make_data_root(src)
    real, calls = tr._copy_one, {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt  # process killed in the middle of the copy phase
        return real(*a, **kw)

    monkeypatch.setattr(tr, "_copy_one", flaky)
    with pytest.raises(KeyboardInterrupt):
        run_transfer(_local(src), _local(dst), "data", execute=True, transfer_id="tx-r", work_dir=tmp_path / "w")
    copied_before = sorted(p.name for p in (dst / TRANSFER_DIR / "tx-r" / "incoming").rglob("*") if p.is_file())
    assert len(copied_before) == 2
    monkeypatch.setattr(tr, "_copy_one", real)
    copies = []
    real_copy2 = shutil.copy2
    monkeypatch.setattr(tr.shutil, "copy2", lambda a, b, *k, **kw: copies.append(Path(a).name) or real_copy2(a, b))
    r = run_transfer(_local(src), _local(dst), "data", execute=True, transfer_id="tx-r", work_dir=tmp_path / "w2")
    assert r["complete"] and not set(copied_before) & set(copies[:-1])  # verified incoming files were not copied again


def test_rollback_moves_only_unchanged_placed_files(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _make_data_root(src)
    run_transfer(_local(src), _local(dst), "data", execute=True, transfer_id="tx-rb", work_dir=tmp_path / "w")
    changed = dst / "context/assessments/x/a.json"
    changed.write_text('{"edited": true}', encoding="utf-8")
    dry = rollback(dst, "tx-rb")
    assert "context/assessments/x/a.json" in dry["changed_since_placement_not_moved"] and dry["would_move"]
    done = rollback(dst, "tx-rb", execute=True)
    assert changed.exists() and all(not (dst / r).exists() for r in done["moved"])
    assert all((dst / TRANSFER_DIR / "tx-rb" / "rolled_back" / r).exists() for r in done["moved"])  # moved, not deleted


def _fake_remote_runner(calls: list):
    """ssh/scp emulation: the 'remote' side runs `python -m aqe.ops ...` on this machine (no network)."""
    def run(argv, stdin=None):
        calls.append(argv)
        if argv[0] == "scp":
            src, dst = (a.split(":", 1)[1] if a.startswith("fakehost:") else a for a in argv[-2:])
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)  # scp -p keeps times
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert argv[0] == "ssh" and argv[-2] == "fakehost"
        args = shlex.split(argv[-1])
        if args[0] == "mkdir":
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if args[0] == "mv":
            os.replace(args[-2], args[-1])
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run([sys.executable, *args[1:]], input=stdin, capture_output=True, text=True, cwd=ROOT)
    return run


@pytest.mark.slow
def test_push_to_and_pull_from_a_remote_host_with_the_same_protocol(tmp_path):
    src, remote, back = tmp_path / "pc-data", tmp_path / "oracle-data", tmp_path / "google-data"
    _make_data_root(src)
    calls: list = []
    runner = _fake_remote_runner(calls)
    oracle = Endpoint(root=str(remote), host="fakehost", python=sys.executable)
    push = run_transfer(_local(src), oracle, "data", execute=True, transfer_id="tx-push", runner=runner, work_dir=tmp_path / "w1")
    assert push["complete"] and push["placed"] >= 4 and (remote / TRANSFER_DIR / "tx-push" / "receipt.json").exists()
    assert any(c[0] == "scp" for c in calls) and any("transfer-finalize" in c[-1] for c in calls if c[0] == "ssh")
    pull = run_transfer(oracle, _local(back), "data", execute=True, transfer_id="tx-pull", runner=runner, work_dir=tmp_path / "w2")
    assert pull["complete"] and pull["placed"] >= 4
    rel_ = "raw/binance/ws/public/public_BTCUSDT.20261110T194122Z.jsonl.gz"
    assert sha256_file(back / rel_) == sha256_file(src / rel_)
    assert not (back / "status").exists()  # host-local state never travels (incl. the transfer bookkeeping)


def test_remote_to_remote_is_refused():
    with pytest.raises(ValueError, match="remote-to-remote"):
        run_transfer(Endpoint(root="/a", host="h1"), Endpoint(root="/b", host="h2"), "data")
    assert Endpoint.parse("ssh://aqe@oracle/srv/aqe/data").root == "/srv/aqe/data"
    with pytest.raises(ValueError):
        Endpoint.parse("ssh://hostonly")


# ------------------------------------------------------------------ model release / rollback
META_KEYS = dict(instrument_id="BTCUSDT", feature_profile="ohlcv_v0", feature_schema_hash="h", feature_names=["f"],
                 class_order=["down", "neutral", "up"], train_label_cutoff="2025-01-01T00:00:00Z", dataset_manifest_hash="d",
                 config_hash="c", code_version="0.1", seed=1, readiness="OOS_EVALUATED", created_at="2026-11-10T00:00:00Z")


def _fake_art(root: Path, exp: str, aids: dict[str, str], content: str = "model-v1") -> None:
    e = root / exp
    for slot, aid in aids.items():
        d = e / "artifacts" / aid
        d.mkdir(parents=True, exist_ok=True)
        (d / "metadata.json").write_text(json.dumps({"artifact_id": aid, "kind": slot, "experiment_id": exp, "horizon_seconds": 3600, **META_KEYS}),
                                         encoding="utf-8")
        (d / "model.txt").write_text(content + aid, encoding="utf-8")
    (e / "oos").mkdir(parents=True, exist_ok=True)
    (e / "oos" / "lgbm_h3600.manifest.json").write_text(json.dumps({"folds": [
        {"fold_id": "f0", "train_label_end_max_observed": "2021-12-31 23:55:02+00:00", "oos_start": "2022-01-01T00:00:00Z"}]}), encoding="utf-8")
    (e / "registry.json").write_text(json.dumps({"experiment_id": exp, "horizons": {"3600": aids}}), encoding="utf-8")


def _ok_smoke(*_a, **_k):
    return {"horizons": {"3600": {"base": {"lgbm": {"status": "OK", "reason": None, "artifact_id": "x"}},
                                  "execution_enabled": False, "approval_required": True}}}


def test_release_build_stage_verify_activate_and_rollback(tmp_path):
    exp = "exp_t"
    google, oracle = tmp_path / "google_art", tmp_path / "oracle_art"
    _fake_art(oracle, exp, {"lgbm": "lgbm-old"}, "old")
    _fake_art(google, exp, {"lgbm": "lgbm-new"}, "new")
    built = rel.build_release(google, exp, tmp_path / "out")
    rid = built["release_id"]
    staged = rel.stage_release(Path(built["path"]), oracle)
    assert Path(staged["staged_at"]).exists()
    with pytest.raises(PermissionError, match="checks.json"):
        rel.activate_release(oracle, rid, rid)  # no verification yet
    checks = rel.verify_release(oracle, rid, Path("unused"), pd.DataFrame(), Path("unused"), smoke=_ok_smoke)
    assert checks["passed"] and checks["comparison_with_active"]["3600"]["changed_slots"] == ["lgbm"]
    with pytest.raises(PermissionError, match="approval"):
        rel.activate_release(oracle, rid, None)  # no auto-activation
    act = rel.activate_release(oracle, rid, rid)
    reg = json.loads((oracle / exp / "registry.json").read_text(encoding="utf-8"))
    assert reg["horizons"]["3600"]["lgbm"] == "lgbm-new" and act["copied_artifacts"] == ["lgbm-new"]
    assert (oracle / exp / "artifacts" / "lgbm-old" / "model.txt").exists()  # previous model kept
    backup = Path(act["previous_registry_backup"]).name
    with pytest.raises(PermissionError):
        rel.rollback_registry(oracle, exp, backup, "wrong")
    rel.rollback_registry(oracle, exp, backup, backup)
    assert json.loads((oracle / exp / "registry.json").read_text(encoding="utf-8"))["horizons"]["3600"]["lgbm"] == "lgbm-old"


def test_release_integrity_leakage_and_artifact_conflicts_block_activation(tmp_path):
    exp = "exp_t"
    google, oracle = tmp_path / "g", tmp_path / "o"
    _fake_art(google, exp, {"lgbm": "lgbm-new"})
    built = rel.build_release(google, exp, tmp_path / "out")
    rid = built["release_id"]
    rel.stage_release(Path(built["path"]), oracle)
    model = rel.release_dir(oracle, rid) / "bundle" / exp / "artifacts" / "lgbm-new" / "model.txt"
    model.write_text("tampered", encoding="utf-8")
    bad = rel.verify_release(oracle, rid, Path("x"), pd.DataFrame(), Path("x"), smoke=_ok_smoke)
    assert not bad["passed"] and any("content differs" in f for f in bad["failures"])
    # leakage in an OOS fold
    _fake_art(google, "exp_l", {"lgbm": "lgbm-l"})
    (google / "exp_l" / "oos" / "lgbm_h3600.manifest.json").write_text(json.dumps({"folds": [
        {"fold_id": "f0", "train_label_end_max_observed": "2022-02-01T00:00:00Z", "oos_start": "2022-01-01T00:00:00Z"}]}), encoding="utf-8")
    b2 = rel.build_release(google, "exp_l", tmp_path / "out")
    rel.stage_release(Path(b2["path"]), oracle)
    leak = rel.verify_release(oracle, b2["release_id"], Path("x"), pd.DataFrame(), Path("x"), smoke=_ok_smoke)
    assert not leak["passed"] and any("leakage" in f for f in leak["failures"])
    # an existing artifact id with different content is never overwritten
    _fake_art(google, "exp_c", {"lgbm": "lgbm-same-id"}, "v2")
    _fake_art(oracle, "exp_c", {"lgbm": "lgbm-same-id"}, "v1")
    b3 = rel.build_release(google, "exp_c", tmp_path / "out")
    rel.stage_release(Path(b3["path"]), oracle)
    assert rel.verify_release(oracle, b3["release_id"], Path("x"), pd.DataFrame(), Path("x"), smoke=_ok_smoke)["passed"]
    with pytest.raises(FileExistsError):
        rel.activate_release(oracle, b3["release_id"], b3["release_id"])
    assert (oracle / "exp_c" / "artifacts" / "lgbm-same-id" / "model.txt").read_text(encoding="utf-8").startswith("v1")


DEV_ART = Path("E:/aqe-v2-dev/artifacts")


@pytest.mark.slow
@pytest.mark.skipif(not (DEV_ART / "exp_btcusdt_ohlcv_v0_001" / "registry.json").exists(), reason="dev snapshot artifacts not present")
def test_real_artifacts_load_and_infer_through_the_release_smoke(tmp_path):
    """x86-64 Windows only: LightGBM text + TCN state_dict artifacts load and predict through the analysis path.
    ARM64 inference is NOT covered by this test (no ARM host here)."""
    from tests.conftest import ROOT as R

    built = rel.build_release(DEV_ART, "exp_btcusdt_ohlcv_v0_001", tmp_path / "out")  # reads the dev COPY only
    rel.stage_release(Path(built["path"]), tmp_path / "art")
    from aqe.contracts.core import InstrumentKey
    from aqe.storage.bars import load_fixture_bars

    bars = load_fixture_bars(R / "tests/fixtures/synthetic_bars_btcusdt_5m.csv",
                             InstrumentKey(venue="binance", market_type="usdm_perpetual", instrument_id="BTCUSDT"))
    checks = rel.verify_release(tmp_path / "art", built["release_id"], R / "configs/experiment.yaml", bars, R / "configs/risk_rules_v1.yaml")
    assert checks["passed"], checks["failures"]
    for h in ("3600", "86400"):
        base = checks["smoke"]["horizons"][h]["base"]
        assert base["lgbm"]["status"] == "OK" and base["tcn"]["status"] == "OK"
        assert checks["smoke"]["horizons"][h]["execution_enabled"] is False


# ------------------------------------------------------------------ health, preflight
def _status(root: Path, *, final=False, age=0.0, book="SYNCED", lag=1.0) -> None:
    now = time.time()
    last = pd.Timestamp(now - lag, unit="s", tz="UTC").isoformat()
    p = root / "status" / "collector_BTCUSDT.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"run_id": "r1", "final": final, "book": {"state": book}, "clock": {"offset_ms_server_minus_local": 3.0},
                             "connections": {"public": {"last_message_at": last}, "market": {"last_message_at": last}}}), encoding="utf-8")
    os.utime(p, (now - age, now - age))
    f = root / "normalized/flow_summaries/binance/usdm_perpetual/BTCUSDT/2026-11-12/13.parquet"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x")


def test_health_levels(tmp_path):
    _status(tmp_path)
    assert collector_health(tmp_path, "BTCUSDT", min_free_gb=0.0001, critical_free_gb=0.00001)["level"] == "OK"
    _status(tmp_path, age=600)
    assert collector_health(tmp_path, "BTCUSDT", min_free_gb=0.0001, critical_free_gb=0.00001)["level"] == "CRITICAL"
    _status(tmp_path, final=True)
    assert collector_health(tmp_path, "BTCUSDT", min_free_gb=0.0001, critical_free_gb=0.00001)["level"] == "CRITICAL"
    _status(tmp_path, book="DESYNC")
    h = collector_health(tmp_path, "BTCUSDT", min_free_gb=0.0001, critical_free_gb=0.00001)
    assert h["level"] == "WARN" and any("depth gap" in i["detail"] for i in h["issues"])
    _status(tmp_path)
    assert collector_health(tmp_path, "BTCUSDT", min_free_gb=10**9)["level"] == "WARN"  # below the warning level
    assert collector_health(tmp_path, "BTCUSDT", min_free_gb=10**9, critical_free_gb=10**9)["level"] == "CRITICAL"


def test_preflight_requires_live_binance_for_the_collector_and_blocks_a_second_collector(tmp_path):
    cfg = NS(paths=NS(data_root=tmp_path / "data", artifact_root=tmp_path / "art"), binance=NS(), clock=NS(max_abs_skew_ms=1000),
             instruments=[NS(instrument_id="BTCUSDT")])
    host = {"os": "Linux", "arch": "arm64", "python": "3.12.3", "cuda_available": False}

    class Clock:
        def __init__(self, _c): pass
        def measure_clock(self, samples): return NS(local_minus_server_ms=12.0)
        def close(self): pass

    off = run_preflight(cfg, "oracle-collector", offline=True, host=host)
    assert off["overall"] == "FAIL" and any(c["check"] == "binance" and c["status"] == "FAIL" for c in off["checks"])
    ok = run_preflight(cfg, "oracle-collector", clock_factory=Clock, host=host)
    assert any(c["check"] == "binance_clock" and c["status"] == "PASS" for c in ok["checks"])
    _status(tmp_path / "data")  # a running collector on the same data root
    dup = run_preflight(cfg, "oracle-collector", clock_factory=Clock, host=host)
    assert any(c["check"] == "single_collector" and c["status"] == "FAIL" for c in dup["checks"])
    _status(tmp_path / "data", final=True)  # graceful stop just happened: restart allowed
    assert any(c["check"] == "single_collector" and c["status"] == "PASS"
               for c in run_preflight(cfg, "oracle-collector", clock_factory=Clock, host=host)["checks"])
    gpu = run_preflight(cfg, "google-gpu-train", offline=True, host={**host, "arch": "x86_64"})
    assert any(c["check"] == "cuda" and c["status"] == "FAIL" for c in gpu["checks"])


# ------------------------------------------------------------------ code release (one verified commit)
def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


def test_code_release_requires_one_clean_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "release candidate")
    ok = check_code_release(repo)
    assert ok["verified"] and len(ok["commit"]) == 40 and "pyproject.toml" in ok["locks"]
    assert not check_code_release(repo, expect_commit="0000000")["verified"]
    (repo / "untracked.py").write_text("x = 1\n", encoding="utf-8")
    dirty = check_code_release(repo)
    assert not dirty["verified"] and "not clean" in dirty["problems"][0]
    script = (ROOT / "deploy/linux/install_release.sh").read_text(encoding="utf-8")
    assert "^[0-9a-f]{40}$" in script and "systemctl restart" not in script.split("echo")[0]  # SHA only, no auto-restart


# ------------------------------------------------------------------ cutover gate
def _flow_hours(root: Path, start: str, hours: int, drop_minutes: int = 0, trades: int = 10) -> None:
    for h in pd.date_range(start, periods=hours, freq="h", tz="UTC"):
        mins = pd.date_range(h, periods=60, freq="min", tz="UTC")[drop_minutes:]
        p = root / "normalized/flow_summaries/binance/usdm_perpetual/BTCUSDT" / h.strftime("%Y-%m-%d") / f"{h:%H}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"interval_start": mins, "book_valid": True, "n_trades": trades}).to_parquet(p)


def test_cutover_gate_requires_coverage_health_migration_and_explicit_approval(tmp_path):
    pc, cloud = tmp_path / "pc", tmp_path / "cloud"
    _flow_hours(pc, "2026-11-20T00:00:00Z", 24)
    _flow_hours(cloud, "2026-11-20T00:00:00Z", 24)
    health = [{"checked_at": f"2026-11-20T{h:02d}:30:00+00:00", "level": "OK"} for h in range(24)]
    comp = compare_collectors(pc, cloud, "BTCUSDT", "2026-11-20T00:00:00Z", "2026-11-21T00:00:00Z")
    assert comp["coverage_new"] == 1.0 and comp["trades_rel_diff_overlap"] == 0.0
    go = readiness(comp, health)
    assert go["ready"] and "approval" in go["verdict"]
    rep = write_report(tmp_path / "rep" / "cutover.json", comp, go)
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"transfer_id": "tx-1", "complete": False}), encoding="utf-8")
    with pytest.raises(PermissionError):
        approve(tmp_path / "rep" / "cutover.json", "wrong-id", receipt)
    with pytest.raises(PermissionError, match="migration"):
        approve(tmp_path / "rep" / "cutover.json", rep["report_id"], receipt)
    receipt.write_text(json.dumps({"transfer_id": "tx-1", "complete": True}), encoding="utf-8")
    rec = approve(tmp_path / "rep" / "cutover.json", rep["report_id"], receipt)
    assert "Ctrl+C" in rec["next_manual_step"]  # the tool never stops the PC collector itself
    # gaps or a CRITICAL health record -> NO-GO
    gappy = tmp_path / "gappy"
    _flow_hours(gappy, "2026-11-20T00:00:00Z", 24, drop_minutes=5)
    assert not readiness(compare_collectors(pc, gappy, "BTCUSDT", "2026-11-20T00:00:00Z", "2026-11-21T00:00:00Z"), health)["ready"]
    assert not readiness(comp, health + [{"checked_at": "2026-11-20T12:00:00+00:00", "level": "CRITICAL"}])["ready"]


# ------------------------------------------------------------------ collector cloud migration (2026-11-12 follow-up)
def test_oracle_collector_defaults_to_srv_paths(monkeypatch):
    monkeypatch.delenv("AQE_DATA_ROOT", raising=False)
    monkeypatch.delenv("AQE_ARTIFACT_ROOT", raising=False)
    raw = yaml.safe_load((CLOUD / "oracle_collector.yaml").read_text(encoding="utf-8"))["paths"]
    assert expand_env(raw["data_root"]) == "/srv/aqe/data" and expand_env(raw["artifact_root"]) == "/srv/aqe/artifacts"


NO_ML_SCRIPT = """
import importlib.abc, importlib.util, sys
from pathlib import Path
BLOCK = {"torch", "lightgbm", "sklearn", "scipy"}
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in BLOCK:
            raise ImportError("blocked " + name)
sys.meta_path.insert(0, Block())
from aqe.cli.main import build_parser
from aqe.ingestion.collector import Collector, GROUP_MARKET
from aqe.ops import health, preflight
from tests.test_collector_offline import INST, agg, make_cfg
col = Collector(make_cfg(Path(sys.argv[1])), INST, "run-noml")
col.handle_text(GROUP_MARKET, agg(1))
col.close()
spec = importlib.util.spec_from_file_location("lc", sys.argv[2])
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)
assert not (BLOCK & {m.split(".")[0] for m in sys.modules})
print("OK")
"""


def test_the_collect_path_imports_no_ml_library(tmp_path):
    """`aqe collect` + the supervisor must run in the collector-only environment (no torch/LightGBM/sklearn/SciPy)."""
    r = subprocess.run([sys.executable, "-c", NO_ML_SCRIPT, str(tmp_path), str(ROOT / "local-collector" / "collect_local.py")],
                       capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0 and r.stdout.strip().endswith("OK"), r.stderr[-800:]
    for name in ("linux-arm64-collector.txt", "linux-x86_64-collector.txt"):
        pins = (ROOT / "requirements" / name).read_text(encoding="utf-8").lower()
        assert all(f"\n{m}==" not in pins for m in ("torch", "lightgbm", "scikit-learn", "scipy")), name


def test_one_data_store_one_collecting_host(tmp_path):
    lc = _load_lc()
    first = lc.claim_data_root(tmp_path, "BTCUSDT", host="oracle-a1")
    again = lc.claim_data_root(tmp_path, "BTCUSDT", host="oracle-a1")  # restart on the same host
    assert again["claimed_at"] == first["claimed_at"] and again["host"] == "oracle-a1"
    with pytest.raises(lc.AlreadyRunning, match="One store, one collecting host"):
        lc.claim_data_root(tmp_path, "BTCUSDT", host="windows-pc")
    assert classify_data("status/collector_owner_BTCUSDT.json", 0)[0] == "host_local"  # never transferred


def _flow_file(path: Path, hour: str, minutes: range, n_trades: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    start = pd.Timestamp(hour)
    pd.DataFrame({"instrument_id": "BTCUSDT", "interval_start": [start + pd.Timedelta(minutes=m) for m in minutes],
                  "book_valid": True, "n_trades": n_trades}).to_parquet(path, index=False)
    old = time.time() - 7200
    os.utime(path, (old, old))


FLOW_REL = "normalized/flow_summaries/binance/usdm_perpetual/BTCUSDT/2026-11-20/13.parquet"


def test_partition_check_and_corrupt_parquet_exclusion(tmp_path):
    from aqe.ops.inventory import partition_problems
    from aqe.ops.transfer import build_manifest

    root = tmp_path / "data"
    _flow_file(root / FLOW_REL, "2026-11-20T13:00:00Z", range(60))
    assert partition_problems(FLOW_REL, root / FLOW_REL) == []
    wrong = FLOW_REL.replace("/13.parquet", "/12.parquet")
    _flow_file(root / wrong, "2026-11-20T13:00:00Z", range(5))  # rows of 13:00 stored in the 12:00 partition
    assert "outside partition" in partition_problems(wrong, root / wrong)[0]
    bad = root / FLOW_REL.replace("/13.parquet", "/14.parquet")
    bad.write_bytes(b"PAR1 truncated")
    os.utime(bad, (time.time() - 7200,) * 2)
    m = build_manifest(root, "data")
    assert any(x["class"] == "corrupt" for x in m["excluded"]) and all(not e["rel"].endswith("/14.parquet") for e in m["entries"])
    assert any(e.get("partition_problem") for e in m["entries"])  # reported with the source's bytes unchanged


def test_mirror_sync_updates_grown_partitions_but_never_local_changes(tmp_path):
    oracle, google = tmp_path / "oracle", tmp_path / "google"
    _flow_file(oracle / FLOW_REL, "2026-11-20T13:00:00Z", range(30))
    r1 = run_transfer(_local(oracle), _local(google), "data", execute=True, transfer_id="m1", mode="mirror", work_dir=tmp_path / "w1")
    assert r1["placed"] == 1
    _flow_file(oracle / FLOW_REL, "2026-11-20T13:00:00Z", range(60))  # the hour completed on Oracle
    safe = run_transfer(_local(oracle), _local(google), "data", transfer_id="s", mode="safe", work_dir=tmp_path / "ws")
    assert safe["conflicts"] == [FLOW_REL]  # safe mode never replaces
    r2 = run_transfer(_local(oracle), _local(google), "data", execute=True, transfer_id="m2", mode="mirror", work_dir=tmp_path / "w2")
    assert r2["complete"] and r2["updated"] == 1 and len(pd.read_parquet(google / FLOW_REL)) == 60
    kept = google / TRANSFER_DIR / "m2" / "replaced" / FLOW_REL
    assert len(pd.read_parquet(kept)) == 30  # the previous version is kept, not deleted
    _flow_file(google / FLOW_REL, "2026-11-20T13:00:00Z", range(5))  # someone changed the Google copy locally
    _flow_file(oracle / FLOW_REL, "2026-11-20T13:00:00Z", range(59))
    r3 = run_transfer(_local(oracle), _local(google), "data", execute=True, transfer_id="m3", mode="mirror", work_dir=tmp_path / "w3")
    assert r3["updated"] == 0 and [c["rel"] for c in r3["conflicts"]] == [FLOW_REL]
    assert len(pd.read_parquet(google / FLOW_REL)) == 5  # local change preserved
    _flow_file(google / FLOW_REL, "2026-11-20T13:00:00Z", range(60))  # back to the m2 version, then roll m2 back
    rb = rollback(google, "m2", execute=True)
    assert rb["moved"] == [FLOW_REL] and len(pd.read_parquet(google / FLOW_REL)) == 30  # previous version restored


def test_windows_to_oracle_cutover_sequence_keeps_both_collectors_rows(tmp_path):
    from aqe.ops.merge import merge_conflicts

    lc = _load_lc()
    pc, oracle = tmp_path / "pc", tmp_path / "oracle"
    lc.claim_data_root(pc, "BTCUSDT", host="windows-pc")
    lc.claim_data_root(oracle, "BTCUSDT", host="oracle-a1")
    h12 = FLOW_REL.replace("/13.parquet", "/12.parquet")
    _flow_file(pc / h12, "2026-11-20T12:00:00Z", range(60))  # before Oracle started: PC only
    _flow_file(pc / FLOW_REL, "2026-11-20T13:00:00Z", range(60), n_trades=7)  # overlap hour: both collectors ran
    _flow_file(oracle / FLOW_REL, "2026-11-20T13:00:00Z", range(20, 60), n_trades=9)  # Oracle started at 13:20
    raw = "raw/binance/ws/public/public_BTCUSDT.20261120T120000Z.jsonl.gz"
    (pc / raw).parent.mkdir(parents=True)
    (pc / raw).write_bytes(b"pc capture")
    os.utime(pc / raw, (time.time() - 7200,) * 2)
    pc_before = {p.relative_to(pc).as_posix(): sha256_file(p) for p in pc.rglob("*") if p.is_file()}
    r = run_transfer(_local(pc), _local(oracle), "data", execute=True, transfer_id="cut", work_dir=tmp_path / "w")
    assert r["complete"] and r["placed"] == 2 and [c["rel"] for c in r["conflicts"]] == [FLOW_REL]
    conflict_copy = oracle / TRANSFER_DIR / "cut" / "conflicts" / FLOW_REL
    conflict_sha = sha256_file(conflict_copy)
    assert json.loads((oracle / "status" / "collector_owner_BTCUSDT.json").read_text(encoding="utf-8"))["host"] == "oracle-a1"
    os.utime(oracle / FLOW_REL, (time.time(),) * 2)  # Oracle collector still writing that partition
    assert merge_conflicts(oracle, "cut", execute=True)["deferred"]  # never merged while being written
    os.utime(oracle / FLOW_REL, (time.time() - 7200,) * 2)
    dry = merge_conflicts(oracle, "cut")
    assert dry["would_merge"][0]["rows_added"] == 20 and len(pd.read_parquet(oracle / FLOW_REL)) == 40  # dry-run changes nothing
    done = merge_conflicts(oracle, "cut", execute=True)
    merged = pd.read_parquet(oracle / FLOW_REL).set_index("interval_start")
    assert len(merged) == 60 and done["merged"][0]["rows_added"] == 20
    assert (merged.iloc[:20]["n_trades"] == 7).all() and (merged.iloc[20:]["n_trades"] == 9).all()  # gap filled, Oracle rows kept
    assert len(pd.read_parquet(Path(done["merged"][0]["backup"]))) == 40  # pre-merge version kept
    assert merge_conflicts(oracle, "cut", execute=True)["deferred"]  # just rewritten -> treated as being written
    assert merge_conflicts(oracle, "cut", execute=True, settle_seconds=0)["covered"]  # idempotent: nothing left to add
    # the PC originals (incl. its status/owner marker) and the quarantined PC copy are byte-identical afterwards
    assert {p.relative_to(pc).as_posix(): sha256_file(p) for p in pc.rglob("*") if p.is_file()} == pc_before
    assert sha256_file(conflict_copy) == conflict_sha


def test_merge_separates_duplicate_rows_from_new_overlap_rows(tmp_path):
    from aqe.ops.merge import merge_conflicts

    root, tid = tmp_path / "oracle", "t"
    base = pd.Timestamp("2026-11-20T13:00:00Z")

    def frame(minutes, trades, unit="ns", tz="UTC"):
        ts = pd.DatetimeIndex([base + pd.Timedelta(minutes=m) for m in minutes]).as_unit(unit)
        if tz is None:
            ts = ts.tz_localize(None)
        return pd.DataFrame({"instrument_id": "BTCUSDT", "interval_start": ts, "book_valid": True, "n_trades": trades})

    dest, inc = root / FLOW_REL, root / TRANSFER_DIR / tid / "conflicts" / FLOW_REL
    for p in (dest, inc):
        p.parent.mkdir(parents=True, exist_ok=True)
    frame(range(10, 40), 9).to_parquet(dest, index=False)  # Oracle: 13:10-13:39
    pd.concat([frame(range(0, 10), 7, "us"),  # PC-only minutes: new rows (other storage unit on purpose)
               frame(range(10, 15), 9, "us"),  # same key, same content: identical duplicates
               frame(range(15, 20), 8, "us"),  # same key, other content: dest kept
               frame([5], 7, "us")], ignore_index=True).to_parquet(inc, index=False)  # duplicate inside the PC file
    for p in (dest, inc):
        os.utime(p, (time.time() - 7200,) * 2)
    inc_sha = sha256_file(inc)
    info = merge_conflicts(root, tid, execute=True)["merged"][0]
    assert (info["rows_added"], info["duplicate_keys"], info["duplicates_identical"], info["duplicates_differing_kept_dest"]) == (10, 10, 5, 5)
    m = pd.read_parquet(dest)
    assert len(m) == 40 and m["interval_start"].is_unique and m["interval_start"].is_monotonic_increasing
    assert str(m["interval_start"].dtype) == str(pd.read_parquet(Path(info["backup"]))["interval_start"].dtype)  # dest dtype kept
    by_min = m.set_index((m["interval_start"] - base).dt.total_seconds() // 60)["n_trades"]
    assert (by_min.loc[0:9] == 7).all() and (by_min.loc[10:39] == 9).all()  # gap filled from PC; every Oracle row unchanged
    assert sha256_file(inc) == inc_sha  # the incoming PC copy is only read
    # time keys without a zone are ambiguous instants -> never merged automatically
    frame(range(40, 50), 1, tz=None).to_parquet(inc, index=False)
    os.utime(dest, (time.time() - 7200,) * 2)
    r = merge_conflicts(root, tid, execute=True)
    assert r["manual"] and "without time zone" in r["manual"][0]["reason"] and len(pd.read_parquet(dest)) == 40
