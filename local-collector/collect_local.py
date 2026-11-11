"""Run the existing AQE public-data collector; no training, LLM or order calls.

Compatible with aqe-research commit 25b3623. Use the project's Python 3.12 venv.
This launcher lives outside the aqe package and does not change project config.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone


class AlreadyRunning(RuntimeError):
    pass


class DestinationLock:
    """OS-managed lock: a crashed process releases it; never delete the lock file."""

    def __init__(self, path: Path):
        self.path, self.handle = path, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            if exc.errno in (errno.EACCES, errno.EAGAIN) or getattr(exc, "winerror", 0) in (32, 33):
                raise AlreadyRunning(f"Another local launcher holds {self.path}") from exc
            raise
        return self

    def __exit__(self, *_):
        if self.handle is not None:
            # Closing the descriptor also releases Windows byte-range locks / POSIX flock.
            self.handle.close()
            self.handle = None


def emit(message: str) -> None:
    print(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}", flush=True)


def disk_check(root: Path, min_free_gb: float) -> float:
    free = shutil.disk_usage(root).free / 2**30
    if free < min_free_gb:
        raise RuntimeError(f"Disk free {free:.2f} GiB < required {min_free_gb:.2f} GiB: {root}")
    return free


def preflight(cfg, min_free_gb: float, clock_factory=None) -> dict:
    root = Path(cfg.paths.data_root)
    if not root.is_absolute():
        raise RuntimeError(f"data_root must be absolute: {root}")
    if not Path(root.anchor).exists():
        raise RuntimeError(f"Data drive is not mounted: {root.anchor}")
    root.mkdir(parents=True, exist_ok=True)
    free = disk_check(root, min_free_gb)
    # A unique probe cannot overwrite an existing data file.
    fd, probe = tempfile.mkstemp(prefix=".aqe-collect-probe-", dir=root)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(b"AQE collection write check\n")
            f.flush()
            os.fsync(f.fileno())
    finally:
        Path(probe).unlink(missing_ok=True)
    if clock_factory is None:
        from aqe.ingestion.binance_rest import BinanceRest
        clock_factory = BinanceRest
    client = clock_factory(cfg.binance)
    try:
        measured = client.measure_clock(samples=3)
    finally:
        client.close()
    skew = measured.local_minus_server_ms
    if not math.isfinite(skew) or abs(skew) > cfg.clock.max_abs_skew_ms:
        raise RuntimeError(
            f"Clock local-minus-Binance={skew:.1f} ms; limit={cfg.clock.max_abs_skew_ms:.1f} ms. "
            "Windows Settings > Time & language > Date & time > Sync now, then retry."
        )
    return {"data_root": str(root), "free_gib": round(free, 2), "clock_skew_ms": round(skew, 1)}


def read_health(root: Path, symbol: str, stale_seconds: float = 60) -> dict:
    path = root / "status" / f"collector_{symbol}.json"
    try:
        # The filesystem timestamp uses the PC clock too, so its age is not confused
        # with the collector's offset-corrected exchange timestamps.
        age = max(0.0, time.time() - path.stat().st_mtime)
        data = json.loads(path.read_text(encoding="utf-8"))
        counts = data.get("message_counts", {})
        connections = data.get("connections", {})
        book = data.get("book", {}).get("state", "UNKNOWN")
        offset = data.get("clock", {}).get("offset_ms_server_minus_local", 0)
        now_exchange = time.time() + offset / 1000
        lags = {}
        for group in ("public", "market"):
            value = connections.get(group, {}).get("last_message_at")
            lags[group] = (max(0.0, now_exchange - datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
                           if value else None)
        raw_files = []
        for group in ("public", "market", "rest"):
            directory = root / "raw" / "binance" / "ws" / group
            for f in directory.glob(f"{group}_{symbol}.*.jsonl.gz.open"):
                try:
                    raw_files.append({"path": str(f), "bytes": f.stat().st_size})
                except FileNotFoundError:  # concurrent rotation is normal
                    continue
        state = "OK"
        if data.get("final"):
            state = "STOPPED"
        elif age > stale_seconds:
            state = "STALE"
        elif any(lags[g] is None or lags[g] > stale_seconds for g in lags):
            state = "FEED_LAG"
        elif book != "SYNCED":
            state = "SYNCING"
        elif not counts.get("depth") or not counts.get("aggTrade"):
            state = "WAITING_FOR_DATA"
        elif not sum(f["bytes"] for f in raw_files):
            state = "WAITING_FOR_RAW_FLUSH"
        return {"state": state, "status_age_seconds": round(age, 1), "run_id": data.get("run_id"),
                "book": book, "message_counts": counts,
                "feed_lag_seconds": {g: None if v is None else round(v, 1) for g, v in lags.items()},
                "clock_offset_server_minus_local_ms": offset,
                "raw_active_bytes": sum(f["bytes"] for f in raw_files), "raw_active_files": raw_files,
                "parse_errors": data.get("parse_errors"), "disk_free_gib": round(shutil.disk_usage(root).free / 2**30, 2)}
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return {"state": "WAITING_OR_UNREADABLE", "detail": f"{type(exc).__name__}: {exc}", "status_file": str(path)}


def keep_awake(enable: bool) -> bool:
    if os.name != "nt":
        return False
    fn = ctypes.windll.kernel32.SetThreadExecutionState
    fn.argtypes, fn.restype = [ctypes.c_uint32], ctypes.c_uint32
    # Keep the system awake; display may still switch off. No permanent power-plan edit.
    return bool(fn(0x80000001 if enable else 0x80000000))


CHILD_BOOTSTRAP = (
    "import signal; "
    "getattr(signal, 'SIGBREAK', None) is not None and "
    "signal.signal(signal.SIGBREAK, signal.default_int_handler); "
    "from aqe.cli.main import main; raise SystemExit(main())"
)


def spawn_collector(project: Path, config: Path, symbol: str, output, duration=None):
    command = [sys.executable, "-u", "-c", CHILD_BOOTSTRAP, "collect", "--config", str(config), "--instrument", symbol]
    if duration is not None:
        command += ["--duration-seconds", str(duration)]
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    env = os.environ.copy()
    # Bind both supervisor and child to this checkout even without a fresh editable install.
    env["PYTHONPATH"] = str(project / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(command, cwd=project, stdout=output, stderr=subprocess.STDOUT, env=env, **options)


def stop_child(child, grace_seconds: float = 30) -> bool:
    """Request KeyboardInterrupt so Collector.finally flushes. Return True if forced."""
    if child is None or child.poll() is not None:
        return False
    emit("Stopping collector; waiting for buffer flush...")
    try:
        child.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
    except OSError:
        if child.poll() is not None:
            return False
        raise
    try:
        child.wait(timeout=grace_seconds)
        return False
    except subprocess.TimeoutExpired:
        emit("WARNING: graceful-stop timeout; killing collector. Check recovery logs next start.")
        child.kill()
        child.wait(timeout=10)
        return True


def supervise(cfg, args, project: Path, config: Path, symbol: str) -> int:
    root = Path(cfg.paths.data_root)
    child = None
    # A coarse status check also catches a collector started with the old supervisor.
    # The OS lock below specifically prevents multiple copies of this launcher.
    status_path = root / "status" / f"collector_{symbol}.json"
    if status_path.exists():
        health = read_health(root, symbol)
        if health.get("state") != "STOPPED" and health.get("status_age_seconds", math.inf) < 60:
            raise AlreadyRunning("An existing collector has a fresh status file. Stop that collector before using this launcher.")
    with DestinationLock(root / "status" / f"local_collect_{symbol}.lock"):
        checked = preflight(cfg, args.min_free_gb)
        emit("CHECK OK " + json.dumps(checked))
        awake = keep_awake(True)
        if os.name == "nt" and not awake:
            emit("WARNING: automatic sleep prevention failed; disable Windows automatic sleep manually.")
        emit(f"Collecting {symbol}; Ctrl+C stops after flushing. Data: {root}")
        backoff = 5
        try:
            log_dir = root / "logs" / "local_collect"
            log_dir.mkdir(parents=True, exist_ok=True)
            while True:
                disk_check(root, args.min_free_gb)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                log_path = log_dir / f"collect_{symbol}_{stamp}.log"
                with log_path.open("ab", buffering=0) as output:
                    child = spawn_collector(project, config, symbol, output, args.duration_seconds)
                    started = time.monotonic()
                    emit(f"START pid={child.pid}; log={log_path}")
                    while child.poll() is None:
                        disk_check(root, args.min_free_gb)
                        health = read_health(root, symbol)
                        emit("HEALTH " + json.dumps({k: v for k, v in health.items() if k != "raw_active_files"}))
                        try:
                            child.wait(timeout=args.poll_seconds)
                        except subprocess.TimeoutExpired:
                            pass
                    code = child.returncode
                    emit(f"EXIT code={code}; inspect log: {log_path}")
                if args.duration_seconds is not None and code == 0:
                    return 0
                if time.monotonic() - started > 600:
                    backoff = 5
                emit(f"RESTART in {backoff}s")
                time.sleep(backoff)
                backoff = min(300, backoff * 2)
        finally:
            try:
                stop_child(child)
            finally:
                if awake:
                    keep_awake(False)


def positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("configs/local.yaml"))
    parser.add_argument("--instrument", default="BTCUSDT")
    parser.add_argument("--min-free-gb", type=positive, default=20.0)
    parser.add_argument("--poll-seconds", type=positive, default=30.0)
    parser.add_argument("--duration-seconds", type=positive, help="one bounded collection run, then exit")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="storage + live exchange clock preflight only")
    mode.add_argument("--status", action="store_true", help="read local status without exchange/API calls")
    parser.add_argument("--watch", action="store_true", help="repeat --status; no collector is started")
    args = parser.parse_args(argv)
    if args.watch and not args.status:
        parser.error("--watch requires --status")
    project = args.project_root.resolve()
    config = args.config if args.config.is_absolute() else project / args.config
    try:
        if sys.version_info[:2] != (3, 12):
            raise RuntimeError("Use the AQE Python 3.12 virtual environment.")
        if not (project / "src" / "aqe" / "cli" / "main.py").is_file():
            raise RuntimeError(f"AQE checkout not found at {project}")
        sys.path.insert(0, str(project / "src"))
        os.chdir(project)
        from aqe.config import load_local_config
        cfg = load_local_config(config)
        symbol = args.instrument.upper()
        if symbol not in {i.instrument_id for i in cfg.instruments}:
            raise RuntimeError(f"{symbol} is not configured in {config}")
        if args.status:
            while True:
                health = read_health(cfg.paths.data_root, symbol)
                print(json.dumps(health, ensure_ascii=False, indent=2), flush=True)
                if not args.watch:
                    return 0 if health["state"] == "OK" else 2
                time.sleep(args.poll_seconds)
        if args.check:
            print(json.dumps(preflight(cfg, args.min_free_gb), indent=2), flush=True)
            return 0
        return supervise(cfg, args, project, config, symbol)
    except KeyboardInterrupt:
        emit("STOPPED by user")
        return 130
    except Exception as exc:
        emit(f"BLOCKED: {type(exc).__name__}: {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
