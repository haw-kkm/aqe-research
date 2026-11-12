"""Collector hosts (oracle-collector, google-collector) and the collector -> training VM sync (docs/CLOUD_DEPLOYMENT.md §15).

Runs in the collector-only environment too (install_release.sh test subset): imports no ML library.
All local: temp dirs, synthetic partitions, a fake ssh/scp runner that executes the remote side on this machine.
Nothing here is evidence that a Google VM, Binance from a Google region, real SSH or systemd works.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pandas as pd
import pytest
import yaml

from aqe.config import expand_env, load_local_config
from aqe.ingestion.binance_rest import BinanceRestError
from aqe.ops.cutover import approve, compare_collectors, readiness, write_report
from aqe.ops.inventory import classify_data
from aqe.ops.merge import merge_conflicts
from aqe.ops.platform import COLLECTOR_ROLES, ROLES, memory_gb, role_requirements
from aqe.ops.preflight import run_preflight
from aqe.ops.transfer import TRANSFER_DIR, Endpoint, received_index, run_transfer, store_identity
from aqe.storage import sha256_file
from tests.conftest import ROOT

CLOUD = ROOT / "configs" / "cloud"
DEPLOY = ROOT / "deploy"
LINUX_ROLES = [r for r in ROLES if r != "windows-dev"]
FLOW_REL = "normalized/flow_summaries/binance/usdm_perpetual/BTCUSDT/2026-11-01/13.parquet"


def _load_lc():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lc_collector_host_test", ROOT / "local-collector" / "collect_local.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _case_map(script: str, var: str) -> dict[str, str]:
    """role -> value from `role1|role2) VAR=value ;;` lines of a deploy script."""
    out: dict[str, str] = {}
    for roles, value in re.findall(rf"^\s*([a-z0-9|-]+)\)\s*{var}=(\S+?)\s*;", script, flags=re.M):
        for r in roles.split("|"):
            out[r] = value
    return out


def _pins(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            name, ver = line.split("==", 1)
            pins[name.strip().lower().replace("_", "-")] = ver.strip()
    return pins


# ------------------------------------------------------------------ role wiring
def test_google_collector_role_is_wired_through_every_deploy_step():
    assert "google-collector" in ROLES and set(COLLECTOR_ROLES) == {"oracle-collector", "google-collector"}
    g = role_requirements("google-collector")
    assert (g["os"], g["arch"], g["binance"], g["collector"], g["separate_data_disk"], g["gpu"]) == ("Linux", "x86_64", True, True, True, False)
    assert role_requirements("oracle-collector")["arch"] == "arm64" and not role_requirements("oracle-collector")["separate_data_disk"]
    assert not role_requirements("google-cpu-train")["collector"] and not role_requirements("oracle-shadow")["binance"]

    install = (DEPLOY / "linux" / "install_release.sh").read_text(encoding="utf-8")
    req, cfg = _case_map(install, "REQ"), _case_map(install, "CFG")
    assert set(req) == set(cfg) == set(LINUX_ROLES)  # every Linux role installs, nothing else does
    assert req["google-collector"] == "requirements/linux-x86_64-collector.txt"  # light by default
    assert cfg["google-collector"] == "configs/cloud/google_collector.yaml"
    for role in LINUX_ROLES:
        assert (ROOT / req[role]).is_file() and (ROOT / cfg[role]).is_file(), role
        arch = role_requirements(role)["arch"]
        assert f"linux-{arch}-" in req[role], (role, req[role])  # the default file matches the role's CPU
    assert "tests/test_collector_host.py" in install and "oracle-collector|google-collector)" in install

    boot = (DEPLOY / "linux" / "bootstrap_host.sh").read_text(encoding="utf-8")
    assert set(_case_map(boot, "COLLECTOR_CFG")) == set(LINUX_ROLES)
    assert "--shell /bin/bash" in boot and "--shell /usr/sbin/nologin" not in boot  # ssh aqe@host <command> must work
    assert "AQE_ROLE=$ROLE" in boot and "mv -n" in boot  # the env file is created once, never overwritten

    unit = (DEPLOY / "linux" / "systemd" / "aqe-collector@.service").read_text(encoding="utf-8")
    assert "--role ${AQE_ROLE}" in unit and "--role oracle-collector" not in unit
    env = (DEPLOY / "linux" / "aqe.env.example").read_text(encoding="utf-8")
    assert re.search(r"^AQE_ROLE=oracle-collector$", env, flags=re.M) and re.search(r"^AQE_COLLECTOR_CONFIG=", env, flags=re.M)


def test_google_collector_config_collects_exactly_like_oracle_and_windows(monkeypatch, tmp_path):
    g = yaml.safe_load((CLOUD / "google_collector.yaml").read_text(encoding="utf-8"))
    o = yaml.safe_load((CLOUD / "oracle_collector.yaml").read_text(encoding="utf-8"))
    local = yaml.safe_load((ROOT / "configs" / "local.yaml").read_text(encoding="utf-8"))
    assert g["analysis"].pop("experiment_config") == "configs/cloud/experiment_google.yaml"
    o["analysis"].pop("experiment_config")
    assert g == o  # only the (unused on the collector VM) experiment reference differs
    for key in ("instruments", "binance", "backfill", "collector", "database", "clock", "risk_ruleset"):
        assert g[key] == local[key], key
    monkeypatch.delenv("AQE_DATA_ROOT", raising=False)
    assert expand_env(g["paths"]["data_root"]) == "/srv/aqe/data"
    monkeypatch.setenv("AQE_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AQE_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    cfg = load_local_config(CLOUD / "google_collector.yaml")
    assert cfg.paths.data_root == tmp_path / "data" and cfg.mode.value == "research"
    assert cfg.context.backend == "none" and cfg.context.max_cost_usd_per_day == 0.0 and cfg.planning.enabled is False


def test_collector_only_requirements_are_identical_on_both_cpus_and_carry_no_ml():
    x86 = _pins(ROOT / "requirements" / "linux-x86_64-collector.txt")
    arm = _pins(ROOT / "requirements" / "linux-arm64-collector.txt")
    common = _pins(ROOT / "requirements" / "common.txt")
    assert x86 == arm and len(x86) == 24
    assert not {"torch", "lightgbm", "scikit-learn", "scipy"} & set(x86)
    assert all(common.get(name) == ver for name, ver in x86.items())  # same versions as the full environments


# ------------------------------------------------------------------ preflight on a Google collector VM
class _Clock:
    def __init__(self, _cfg):
        pass

    def measure_clock(self, samples):
        return NS(local_minus_server_ms=8.0)

    def close(self):
        pass


def _cfg(tmp_path: Path):
    return NS(paths=NS(data_root=tmp_path / "data", artifact_root=tmp_path / "art"), binance=NS(), clock=NS(max_abs_skew_ms=1000),
              instruments=[NS(instrument_id="BTCUSDT")])


GCE_SMALL = {"os": "Linux", "arch": "x86_64", "python": "3.12.3", "cuda_available": False, "mem_total_gb": 1.94, "swap_total_gb": 0.0}


@pytest.fixture(autouse=True)
def _host_independent_timesync(monkeypatch):
    """install_release.sh runs these tests on the real VM: its NTP state must not decide them."""
    import aqe.ops.preflight as pf

    monkeypatch.setattr(pf, "timesync_status", lambda: ("PASS", "fixed for the test"))


def test_preflight_google_collector(tmp_path):
    def checks(r):
        return {c["check"]: c["status"] for c in r["checks"]}

    ok = run_preflight(_cfg(tmp_path), "google-collector", clock_factory=_Clock, host=GCE_SMALL, env_role="google-collector",
                       same_fs=lambda a, b: False)
    c = checks(ok)
    assert c["aqe_role"] == c["arch"] == c["data_disk"] == c["memory"] == c["binance_clock"] == c["single_collector"] == "PASS"
    assert c["timesync"] == "PASS" and not any(k in c for k in ("import:torch", "import:lightgbm", "import:sklearn"))
    # (overall is not asserted: data_root free space of the machine running the tests is a real check)
    # data on the boot disk / e2-micro without swap: warnings, not silent
    tiny = {**GCE_SMALL, "mem_total_gb": 0.95}
    w = checks(run_preflight(_cfg(tmp_path), "google-collector", clock_factory=_Clock, host=tiny, env_role="google-collector",
                             same_fs=lambda a, b: True))
    assert w["data_disk"] == "WARN" and w["memory"] == "WARN"
    assert checks(run_preflight(_cfg(tmp_path), "google-collector", clock_factory=_Clock, host={**tiny, "swap_total_gb": 2.0},
                                env_role="google-collector", same_fs=lambda a, b: False))["memory"] == "PASS"
    # the env file names another role, or the live Binance check is skipped: refuse to start
    wrong = run_preflight(_cfg(tmp_path), "google-collector", clock_factory=_Clock, host=GCE_SMALL, env_role="oracle-collector")
    assert wrong["overall"] == "FAIL" and checks(wrong)["aqe_role"] == "FAIL"
    off = run_preflight(_cfg(tmp_path), "google-collector", offline=True, host=GCE_SMALL, env_role="google-collector")
    assert off["overall"] == "FAIL" and checks(off)["binance"] == "FAIL"
    # training roles keep their ML import checks
    train = checks(run_preflight(_cfg(tmp_path), "google-cpu-train", offline=True, host=GCE_SMALL, env_role="google-cpu-train"))
    assert "import:torch" in train and "single_collector" not in train and "memory" not in train


def test_preflight_names_a_region_block_instead_of_a_network_error(tmp_path):
    class Blocked(_Clock):
        def measure_clock(self, samples):
            raise BinanceRestError(451, '{"code":0,"msg":"Service unavailable from a restricted location"}', "/fapi/v1/time")

    r = run_preflight(_cfg(tmp_path), "google-collector", clock_factory=Blocked, host=GCE_SMALL, env_role="google-collector")
    detail = next(c["detail"] for c in r["checks"] if c["check"] == "binance_clock")
    assert r["overall"] == "FAIL" and "HTTP 451" in detail and "region" in detail

    class Forbidden(_Clock):
        def measure_clock(self, samples):
            raise BinanceRestError(403, "", "/fapi/v1/time")

    r403 = run_preflight(_cfg(tmp_path), "google-collector", clock_factory=Forbidden, host=GCE_SMALL, env_role="google-collector")
    d403 = next(c["detail"] for c in r403["checks"] if c["check"] == "binance_clock")
    assert "HTTP 403" in d403 and "WAF" in d403  # not asserted to be a region block


def test_preflight_cli_explains_a_missing_role(tmp_path, capsys):
    from aqe.ops.__main__ import main

    assert main(["preflight", "--config", str(CLOUD / "google_collector.yaml"), "--role", ""]) == 1
    assert "AQE_ROLE missing" in capsys.readouterr().out


def test_memory_facts_from_proc_meminfo():
    m = memory_gb("MemTotal:        2030044 kB\nMemFree: 1 kB\nSwapTotal:       2097148 kB\n")
    assert m == {"mem_total_gb": 1.94, "swap_total_gb": 2.0}
    assert memory_gb("garbage") == {"mem_total_gb": None, "swap_total_gb": None}


# ------------------------------------------------------------------ data store identity + mirror sync
def test_store_id_is_created_once_and_stays_on_its_host(tmp_path):
    lc = _load_lc()
    lc.claim_data_root(tmp_path, "BTCUSDT", host="gce-collector-1")
    first = json.loads((tmp_path / "status" / "data_store.json").read_text(encoding="utf-8"))
    lc.claim_data_root(tmp_path, "BTCUSDT", host="gce-collector-1")  # restart
    assert json.loads((tmp_path / "status" / "data_store.json").read_text(encoding="utf-8")) == first
    assert store_identity(tmp_path)["store_id"] == first["store_id"] and len(first["store_id"]) == 32
    assert classify_data("status/data_store.json", 0)[0] == "host_local"  # never transferred
    assert not list((tmp_path / "status").glob("*.tmp"))
    assert store_identity(tmp_path / "nowhere") is None
    # an unreadable id file never stops collection and is never overwritten; syncs then compare the address
    broken = tmp_path / "broken"
    (broken / "status").mkdir(parents=True)
    (broken / "status" / "data_store.json").write_text("{not json", encoding="utf-8")
    lc.claim_data_root(broken, "BTCUSDT", host="gce-collector-1")
    assert (broken / "status" / "data_store.json").read_text(encoding="utf-8") == "{not json" and store_identity(broken) is None


def _flow(root: Path, minutes, store_id: str | None = None, rel: str = FLOW_REL) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    start = pd.Timestamp("2026-11-01T13:00:00Z")
    pd.DataFrame({"instrument_id": "BTCUSDT", "interval_start": [start + pd.Timedelta(minutes=m) for m in minutes],
                  "book_valid": True, "n_trades": 3}).to_parquet(p, index=False)
    old = time.time() - 7200
    os.utime(p, (old, old))
    if store_id:
        s = root / "status" / "data_store.json"
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text(json.dumps({"store_id": store_id, "created_at": "2026-11-01T00:00:00+00:00", "created_by_host": "x"}), encoding="utf-8")


def _mirror(src: Path, dst: Path, tid: str, tmp_path: Path, **kw):
    return run_transfer(Endpoint(root=str(src)), Endpoint(root=str(dst)), "data", execute=True, transfer_id=tid, mode="mirror",
                        work_dir=tmp_path / f"w-{tid}", **kw)


def test_mirror_follows_the_data_store_not_the_address(tmp_path):
    train = tmp_path / "train" / "data"
    old_addr, new_addr = tmp_path / "collector-old-address" / "data", tmp_path / "collector-new-address" / "data"
    _flow(old_addr, range(30), store_id="store-a")
    r1 = _mirror(old_addr, train, "s1", tmp_path)
    assert r1["placed"] == 1 and r1["source_store"]["store_id"] == "store-a"
    assert received_index(train)[FLOW_REL]["store_id"] == "store-a"
    # same store reached at a new address (new IP / DNS name / moved disk): the grown partition is mirrored
    _flow(new_addr, range(60), store_id="store-a")
    r2 = _mirror(new_addr, train, "s2", tmp_path)
    assert r2["complete"] and r2["updated"] == 1 and len(pd.read_parquet(train / FLOW_REL)) == 60
    # a DIFFERENT store behind that same address (rebuilt VM, or the Google collector replacing Oracle): never replaces
    _flow(new_addr, range(59), store_id="store-b")
    r3 = _mirror(new_addr, train, "s3", tmp_path)
    assert r3["updated"] == 0 and [c["rel"] for c in r3["conflicts"]] == [FLOW_REL]
    assert len(pd.read_parquet(train / FLOW_REL)) == 60
    # stores without an id (older collectors): the address decides, as before
    train2, legacy, moved = tmp_path / "train2" / "data", tmp_path / "legacy" / "data", tmp_path / "legacy-moved" / "data"
    _flow(legacy, range(10))
    assert _mirror(legacy, train2, "l1", tmp_path)["placed"] == 1 and "store_id" not in received_index(train2)[FLOW_REL]
    _flow(legacy, range(20))
    assert _mirror(legacy, train2, "l2", tmp_path)["updated"] == 1
    _flow(moved, range(30))
    assert [c["rel"] for c in _mirror(moved, train2, "l3", tmp_path)["conflicts"]] == [FLOW_REL]


def test_overlap_from_a_new_collector_is_merged_once_then_skipped(tmp_path):
    """Oracle -> Google collector switch seen from the training VM: the overlap hour arrives from both stores."""
    oracle, gcol, train = tmp_path / "oracle" / "data", tmp_path / "gcol" / "data", tmp_path / "train" / "data"
    _flow(oracle, range(0, 30), store_id="store-oracle")
    assert _mirror(oracle, train, "o1", tmp_path)["placed"] == 1
    _flow(gcol, range(25, 60), store_id="store-google")  # the Google collector started at :25 of the same hour
    first = _mirror(gcol, train, "g1", tmp_path)
    assert [c["rel"] for c in first["conflicts"]] == [FLOW_REL] and len(pd.read_parquet(train / FLOW_REL)) == 30
    dry = merge_conflicts(train, "g1", settle_seconds=0)
    assert [m["rel"] for m in dry["would_merge"]] == [FLOW_REL] and len(pd.read_parquet(train / FLOW_REL)) == 30
    done = merge_conflicts(train, "g1", execute=True, settle_seconds=0)
    assert done["merged"][0]["rows_added"] == 30 and len(pd.read_parquet(train / FLOW_REL)) == 60  # minutes 0-59, Oracle rows kept
    assert received_index(train)[FLOW_REL]["absorbed"] == [sha256_file(gcol / FLOW_REL)]
    again = _mirror(gcol, train, "g2", tmp_path)  # unchanged Google version: not quarantined again
    assert again["complete"] and again["conflicts"] == [] and again["skipped_merged"] == 1
    assert len(pd.read_parquet(train / FLOW_REL)) == 60
    _flow(gcol, range(20, 60), store_id="store-google")  # a NEW version still conflicts (needs its own merge)
    assert [c["rel"] for c in _mirror(gcol, train, "g3", tmp_path)["conflicts"]] == [FLOW_REL]
    # the training run's guard: train_pipeline.sh --sync-from stops on conflicts or an incomplete transfer
    pipe = (DEPLOY / "google" / "train_pipeline.sh").read_text(encoding="utf-8")
    assert "--sync-from" in pipe and '"$SYNC_STATE" != ok' in pipe and "exit 6" in pipe


def _fake_ssh(calls: list):
    """ssh/scp emulation for any host: the 'remote' side runs on this machine (no network)."""
    def run(argv, stdin=None):
        calls.append(argv)
        if argv[0] == "scp":
            src, dst = (a.split(":", 1)[1] if re.match(r"^[\w.-]+@[\w.-]+:", a) else a for a in argv[-2:])
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return subprocess.CompletedProcess(argv, 0, "", "")
        args = shlex.split(argv[-1])
        if args[0] == "mkdir":
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if args[0] == "mv":
            os.replace(args[-2], args[-1])
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run([sys.executable, *args[1:]], input=stdin, capture_output=True, text=True, cwd=ROOT)
    return run


def test_training_vm_pulls_from_the_google_collector_over_ssh(tmp_path):
    gcol, train = tmp_path / "gcol" / "data", tmp_path / "train" / "data"
    _flow(gcol, range(60), store_id="store-google")
    raw = gcol / "raw/binance/ws/public/public_BTCUSDT.20261101T130000Z.jsonl.gz"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"\x1f\x8b-not-really-gzip-but-bytes")
    os.utime(raw, (time.time() - 7200,) * 2)
    (gcol / "status" / "collector_BTCUSDT.json").write_text("{}", encoding="utf-8")  # host-local: must not travel
    calls: list = []
    before = {p: sha256_file(p) for p in (gcol / FLOW_REL, raw)}
    src = Endpoint(root=str(gcol), host="aqe@aqe-collector.asia-northeast3-a.c.example.internal", python=sys.executable)
    with pytest.raises(ValueError, match="KEY=VALUE"):
        run_transfer(src, Endpoint(root=str(train)), "data", ssh_options=["Bad Option"], runner=_fake_ssh(calls))
    assert calls == []  # rejected before any connection
    plan = run_transfer(src, Endpoint(root=str(train)), "data", mode="mirror", transfer_id="p0", runner=_fake_ssh(calls),
                        ssh_options=["StrictHostKeyChecking=accept-new"], work_dir=tmp_path / "w0")
    assert plan["execute"] is False and not train.exists() and plan["source_store"]["store_id"] == "store-google"
    r = run_transfer(src, Endpoint(root=str(train)), "data", mode="mirror", execute=True, transfer_id="p1", runner=_fake_ssh(calls),
                     ssh_options=["StrictHostKeyChecking=accept-new"], work_dir=tmp_path / "w1")
    assert r["complete"] and r["placed"] == 2 and sha256_file(train / FLOW_REL) == sha256_file(gcol / FLOW_REL)
    assert not (train / "status").exists() and received_index(train)[FLOW_REL]["store_id"] == "store-google"
    opt = "StrictHostKeyChecking=accept-new"
    assert all(c[c.index(opt) - 1] == "-o" for c in calls if opt in c) and {c[0] for c in calls if opt in c} == {"ssh", "scp"}
    assert all(sha256_file(p) == h for p, h in before.items())  # the collector's files are only read
    assert not (gcol / TRANSFER_DIR).exists()  # no SQLite here, so not even a snapshot dir on the collector


NO_ML_SOURCE_SIDE = """
import importlib.abc, json, sys, io, contextlib
BLOCK = {"torch", "lightgbm", "sklearn", "scipy"}
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in BLOCK:
            raise ImportError("blocked " + name)
sys.meta_path.insert(0, Block())
from aqe.ops.__main__ import main
import aqe.ingestion.backfill, aqe.ops.preflight, aqe.ops.health, aqe.ops.platform
out = io.StringIO()
with contextlib.redirect_stdout(out):
    assert main(["transfer-manifest", "--root", sys.argv[1], "--settle-seconds", "0"]) == 0
m = json.loads(out.getvalue())
assert m["store"]["store_id"] == "store-google" and len(m["entries"]) == 1, m
sys.stdin = io.StringIO(json.dumps([e["rel"] for e in m["entries"]]))
out = io.StringIO()
with contextlib.redirect_stdout(out):
    assert main(["transfer-index", "--root", sys.argv[1]]) == 0
assert not (BLOCK & {k.split(".")[0] for k in sys.modules})
print("OK")
"""


def test_collector_vm_serves_the_sync_without_ml_libraries(tmp_path):
    """The training VM runs `aqe-python -m aqe.ops transfer-manifest/-index` ON the collector VM (collector-only env)."""
    gcol = tmp_path / "gcol" / "data"
    _flow(gcol, range(60), store_id="store-google")
    r = subprocess.run([sys.executable, "-c", NO_ML_SOURCE_SIDE, str(gcol)], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0 and r.stdout.strip().endswith("OK"), r.stderr[-800:]


# ------------------------------------------------------------------ cutover and scripts
def test_cutover_from_a_linux_collector_names_the_systemctl_stop(tmp_path):
    for root in (tmp_path / "oracle", tmp_path / "gcol"):
        for h in pd.date_range("2026-11-20", periods=24, freq="h", tz="UTC"):
            p = root / "normalized/flow_summaries/binance/usdm_perpetual/BTCUSDT" / h.strftime("%Y-%m-%d") / f"{h:%H}.parquet"
            p.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame({"interval_start": pd.date_range(h, periods=60, freq="min"), "book_valid": True, "n_trades": 5}).to_parquet(p)
    comp = compare_collectors(tmp_path / "oracle", tmp_path / "gcol", "BTCUSDT", "2026-11-20T00:00:00Z", "2026-11-21T00:00:00Z")
    health = [{"checked_at": f"2026-11-20T{h:02d}:30:00+00:00", "level": "OK"} for h in range(24)]
    rep = write_report(tmp_path / "rep" / "cutover.json", comp, readiness(comp, health))
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"transfer_id": "tx-o2g", "complete": True}), encoding="utf-8")
    with pytest.raises(ValueError):
        approve(tmp_path / "rep" / "cutover.json", rep["report_id"], receipt, reference="oracle")
    rec = approve(tmp_path / "rep" / "cutover.json", rep["report_id"], receipt, reference="linux")
    assert "systemctl disable --now aqe-collector@" in rec["next_manual_step"] and rec["reference_collector"] == "linux"


def test_sync_scripts_pull_in_mirror_mode_only():
    sync = (DEPLOY / "google" / "sync_from_collector.sh").read_text(encoding="utf-8")
    assert "--mode mirror" in sync and "--mode safe" not in sync and "/opt/aqe/bin/aqe-python" in sync
    assert "StrictHostKeyChecking=accept-new" in sync.split("--accept-new-hostkey) ACCEPT_NEW=1")[1]  # only on request
    legacy = (DEPLOY / "google" / "sync_from_oracle.sh").read_text(encoding="utf-8")
    assert "sync_from_collector.sh" in legacy and "exec bash" in legacy


def _usable_bash() -> str | None:
    bash = shutil.which("bash")
    if not bash:
        return None
    try:
        ok = subprocess.run([bash, "-c", "echo ok"], capture_output=True, text=True, timeout=20).stdout.strip() == "ok"
    except (OSError, subprocess.SubprocessError):
        return None
    return bash if ok else None


@pytest.mark.skipif(_usable_bash() is None, reason="no usable bash on this host")
def test_deploy_scripts_parse():
    bash = _usable_bash()
    scripts = sorted((DEPLOY / "linux").glob("*.sh")) + sorted((DEPLOY / "google").glob("*.sh"))
    assert len(scripts) >= 6
    for s in scripts:
        r = subprocess.run([bash, "-n", s.relative_to(ROOT).as_posix()], capture_output=True, text=True, cwd=ROOT, timeout=30)
        assert r.returncode == 0, (s.name, r.stderr)
        assert b"\r\n" not in s.read_bytes(), s.name  # LF only (.gitattributes), or bash breaks on the server
