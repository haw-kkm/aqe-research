"""Operational regression tests. No Binance, LLM, paid API or user data calls."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace as NS

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "collect_local.py"
spec = importlib.util.spec_from_file_location("local_collect_test_module", MODULE_PATH)
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)


def fake_aqe_package(src_root):
    """Fixture `aqe.cli.main` as a REGULAR package: a namespace package would lose to an installed (e.g. editable)
    `aqe`, and the child would run the real CLI instead of the fixture."""
    package = src_root / "aqe" / "cli"
    package.mkdir(parents=True)
    (src_root / "aqe" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    return package


def config(tmp_path):
    return NS(paths=NS(data_root=tmp_path / "data"), binance=NS(), clock=NS(max_abs_skew_ms=1000))


def clock_factory(skew, clients):
    class Clock:
        def __init__(self, _cfg):
            self.closed = False
            clients.append(self)

        def measure_clock(self, samples):
            return NS(local_minus_server_ms=skew)

        def close(self):
            self.closed = True
    return Clock


@pytest.mark.parametrize("skew", [64000, -64000, float("nan")])
def test_bad_clock_blocks_start_and_closes_client(tmp_path, skew):
    cfg, clients = config(tmp_path), []
    with pytest.raises(RuntimeError, match="Clock"):
        lc.preflight(cfg, 0.000001, clock_factory(skew, clients))
    assert clients[0].closed
    assert not list(cfg.paths.data_root.glob(".aqe-collect-probe-*"))


def test_good_preflight_preserves_existing_data(tmp_path):
    cfg, clients = config(tmp_path), []
    cfg.paths.data_root.mkdir()
    data = cfg.paths.data_root / "existing.parquet"
    data.write_bytes(b"existing-data-unchanged")
    result = lc.preflight(cfg, 0.000001, clock_factory(25, clients))
    assert result["clock_skew_ms"] == 25
    assert data.read_bytes() == b"existing-data-unchanged"
    assert sorted(p.name for p in cfg.paths.data_root.iterdir()) == ["existing.parquet"]


def test_low_disk_blocks_before_exchange_call(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    called = []
    monkeypatch.setattr(lc.shutil, "disk_usage", lambda _path: NS(free=2 * 2**30))
    with pytest.raises(RuntimeError, match="Disk free"):
        lc.preflight(cfg, 20, lambda c: called.append(c))
    assert not called


def test_lock_blocks_another_process_and_releases(tmp_path):
    lockfile = tmp_path / "collector.lock"
    code = """import importlib.util, pathlib, sys
s=importlib.util.spec_from_file_location('local_collect',sys.argv[1])
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
try:
    with m.DestinationLock(pathlib.Path(sys.argv[2])): pass
except m.AlreadyRunning:
    raise SystemExit(23)
"""
    args = [sys.executable, "-c", code, str(MODULE_PATH), str(lockfile)]
    with lc.DestinationLock(lockfile):
        assert subprocess.run(args, timeout=10, capture_output=True).returncode == 23
    assert subprocess.run(args, timeout=10, capture_output=True).returncode == 0


def write_status(root, *, lag=0, final=False):
    path = root / "status" / "collector_BTCUSDT.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    when = lc.datetime.fromtimestamp(now - lag, lc.timezone.utc).isoformat()
    data = {"final": final, "run_id": "test-run", "clock": {"offset_ms_server_minus_local": 0},
            "message_counts": {"depth": 50, "aggTrade": 20}, "book": {"state": "SYNCED"},
            "connections": {"public": {"last_message_at": lc.datetime.fromtimestamp(now, lc.timezone.utc).isoformat()},
                            "market": {"last_message_at": when}}, "parse_errors": 0}
    path.write_text(json.dumps(data), encoding="utf-8")
    raw = root / "raw/binance/ws/public/public_BTCUSDT.test.jsonl.gz.open"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"raw-data")
    return path


def test_health_requires_recent_both_feeds_and_real_raw_file(tmp_path):
    write_status(tmp_path)
    assert lc.read_health(tmp_path, "BTCUSDT")["state"] == "OK"
    write_status(tmp_path, lag=120)
    assert lc.read_health(tmp_path, "BTCUSDT")["state"] == "FEED_LAG"


def test_old_synced_status_is_not_healthy(tmp_path):
    path = write_status(tmp_path)
    os.utime(path, (time.time() - 120, time.time() - 120))
    assert lc.read_health(tmp_path, "BTCUSDT")["state"] == "STALE"


def test_stopped_and_missing_collectors_are_not_healthy(tmp_path):
    assert lc.read_health(tmp_path, "BTCUSDT")["state"] == "WAITING_OR_UNREADABLE"
    write_status(tmp_path, final=True)
    assert lc.read_health(tmp_path, "BTCUSDT")["state"] == "STOPPED"


def test_existing_direct_collector_is_not_started_again(tmp_path):
    write_status(tmp_path)
    cfg = NS(paths=NS(data_root=tmp_path))
    with pytest.raises(lc.AlreadyRunning, match="fresh status"):
        lc.supervise(cfg, None, tmp_path, tmp_path / "local.yaml", "BTCUSDT")


def test_child_gets_graceful_interrupt_and_finishes_flush(tmp_path):
    # A real isolated child process exercises process group + signal delivery.
    # The fixture writes a flush marker on KeyboardInterrupt, as the real collector does.
    package = fake_aqe_package(tmp_path / "src")
    (package / "main.py").write_text('''from pathlib import Path
import time
def main():
    Path("ready").touch()
    try:
        while True: time.sleep(.05)
    except KeyboardInterrupt:
        return 0
    finally:
        Path("flushed").touch()
''', encoding="utf-8")
    with (tmp_path / "child.log").open("wb") as output:
        child = lc.spawn_collector(tmp_path, tmp_path / "local.yaml", "BTCUSDT", output)
        try:
            deadline = time.monotonic() + 10
            while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
                assert child.poll() is None
                time.sleep(.02)
            assert (tmp_path / "ready").exists()
            assert lc.stop_child(child, grace_seconds=5) is False
            assert child.returncode == 0 and (tmp_path / "flushed").exists()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


def test_hung_child_has_bounded_shutdown():
    class Hung:
        killed = False
        def poll(self): return None
        def send_signal(self, _sig): pass
        def wait(self, timeout):
            if not self.killed:
                raise subprocess.TimeoutExpired("test-child", timeout)
            return -9
        def kill(self): self.killed = True
    child = Hung()
    assert lc.stop_child(child, grace_seconds=.01)
    assert child.killed


def test_windows_sleep_request_is_cleared(monkeypatch):
    flags = []
    class Fn:
        def __call__(self, flag):
            flags.append(flag)
            return 0x80000000
    monkeypatch.setattr(lc.os, "name", "nt")
    monkeypatch.setattr(lc.ctypes, "windll", NS(kernel32=NS(SetThreadExecutionState=Fn())), raising=False)
    assert lc.keep_awake(True) and lc.keep_awake(False)
    assert flags == [0x80000001, 0x80000000]


def test_supervisor_restarts_failed_child_then_exits_bounded_run(tmp_path, monkeypatch):
    project = tmp_path / "project"
    package = fake_aqe_package(project / "src")
    (package / "main.py").write_text('''from pathlib import Path
def main():
    p=Path("attempts")
    n=int(p.read_text())+1 if p.exists() else 1
    p.write_text(str(n))
    return 1 if n == 1 else 0
''', encoding="utf-8")
    cfg = config(tmp_path)
    cfg.paths.data_root.mkdir()
    args = NS(min_free_gb=.000001, poll_seconds=.01, duration_seconds=.1)
    sleeps = []
    monkeypatch.setattr(lc, "preflight", lambda *args: {"test": "offline"})
    monkeypatch.setattr(lc, "keep_awake", lambda *_: False)
    monkeypatch.setattr(lc, "time", NS(time=time.time, monotonic=time.monotonic,
                                     sleep=lambda seconds: sleeps.append(seconds)))
    assert lc.supervise(cfg, args, project, project / "config.yaml", "BTCUSDT") == 0
    assert (project / "attempts").read_text() == "2"
    assert sleeps == [5]


def test_runtime_low_disk_stops_child_and_restores_power_state(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    cfg.paths.data_root.mkdir()
    args = NS(min_free_gb=20, poll_seconds=30, duration_seconds=None)
    calls, power, stopped = [], [], []
    child = NS(pid=12345, poll=lambda: None)
    monkeypatch.setattr(lc, "preflight", lambda *_: {})
    monkeypatch.setattr(lc, "keep_awake", lambda value: power.append(value) or True)
    monkeypatch.setattr(lc, "spawn_collector", lambda *a: child)
    monkeypatch.setattr(lc, "stop_child", lambda value: stopped.append(value))
    def disk(*_):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("Disk free below threshold")
        return 30
    monkeypatch.setattr(lc, "disk_check", disk)
    with pytest.raises(RuntimeError, match="Disk free"):
        lc.supervise(cfg, args, tmp_path, tmp_path / "config.yaml", "BTCUSDT")
    assert stopped == [child] and power == [True, False]
