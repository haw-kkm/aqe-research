"""`aqe doctor` — environment diagnosis (B12, R01). Never prints secret values."""

from __future__ import annotations

import ctypes
import importlib
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from aqe.config import LocalConfig, resolve_secret

PACKAGES = ["pydantic", "yaml", "numpy", "pandas", "pyarrow", "lightgbm", "sklearn", "torch", "httpx", "websockets", "psycopg"]


def _mem() -> dict[str, Any]:
    if sys.platform == "win32":
        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        ms = MS()
        ms.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
        return {"total_gb": round(ms.ullTotalPhys / 2**30, 1), "available_gb": round(ms.ullAvailPhys / 2**30, 1)}
    try:
        pages, size, avail = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE"), os.sysconf("SC_AVPHYS_PAGES")
        return {"total_gb": round(pages * size / 2**30, 1), "available_gb": round(avail * size / 2**30, 1)}
    except (ValueError, OSError, AttributeError):
        return {"total_gb": None, "available_gb": None}


def _check_path(p: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"path": str(p)}
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".aqe_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        out["writable"] = True
        du = shutil.disk_usage(p)
        out["free_gb"] = round(du.free / 2**30, 1)
        out["status"] = "OK" if du.free > 20 * 2**30 else "WARN"
    except OSError as exc:
        out["writable"] = False
        out["status"] = "FAIL"
        out["error"] = str(exc)
    return out


def run_doctor(cfg: LocalConfig, network: bool = True) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    py_ok = sys.version_info[:2] == (3, 12)
    checks["python"] = {"version": sys.version.split()[0], "executable": sys.executable, "status": "OK" if py_ok else "WARN",
                        "note": None if py_ok else "Python 3.12 is the validated target"}
    checks["platform"] = {"system": platform.platform(), "machine": platform.machine(), "cpu_count": os.cpu_count(), "status": "OK"}
    mem = _mem()
    checks["memory"] = {**mem, "status": "OK" if (mem.get("available_gb") or 0) >= 4 else "WARN",
                        "note": "low free RAM: keep TCN batch small, avoid loading full tick history" if (mem.get("available_gb") or 0) < 4 else None}
    pk = {}
    for name in PACKAGES:
        try:
            m = importlib.import_module(name)
            pk[name] = getattr(m, "__version__", "installed")
        except Exception as exc:  # noqa: BLE001
            pk[name] = f"MISSING ({type(exc).__name__})"
    checks["packages"] = {"versions": pk, "status": "OK" if not any(str(v).startswith("MISSING") for k, v in pk.items() if k != "psycopg") else "FAIL"}
    try:
        import torch

        checks["torch_device"] = {"cuda_available": bool(torch.cuda.is_available()), "build": torch.__version__, "status": "OK"}
    except Exception:  # noqa: BLE001
        checks["torch_device"] = {"status": "FAIL"}
    checks["data_root"] = _check_path(cfg.paths.data_root)
    checks["artifact_root"] = _check_path(cfg.paths.artifact_root)
    dsn = resolve_secret(cfg.database.postgres_dsn_env)
    db: dict[str, Any] = {"postgres_dsn_env": cfg.database.postgres_dsn_env, "dsn_set": dsn is not None}
    if dsn is None:
        db["postgres"] = "NOT_CONFIGURED"
        db["status"] = "BLOCKED"
        db["note"] = "PostgreSQL/TimescaleDB not configured; local SQLite metadata store is used (ADR-0002). DB checks not passed."
    else:
        try:
            import psycopg

            with psycopg.connect(dsn, connect_timeout=5) as conn:
                v = conn.execute("select version()").fetchone()[0]
                ts = conn.execute("select extversion from pg_extension where extname='timescaledb'").fetchone()
            db.update(postgres=v.split(",")[0], timescaledb=ts[0] if ts else None, status="OK")
        except Exception as exc:  # noqa: BLE001
            db.update(postgres=f"connect failed: {type(exc).__name__}", status="FAIL")
    checks["database"] = db
    checks["containers"] = {"docker": shutil.which("docker") is not None, "wsl_exe": shutil.which("wsl") is not None,
                            "status": "INFO", "note": "native Windows execution path is used; Docker not required"}
    llm = cfg.context.llm
    key_present = resolve_secret(llm.api_key_env) is not None if llm.api_key_env else False
    checks["context"] = {"backend": cfg.context.backend, "llm_provider": llm.provider, "llm_model_id": llm.model_id,
                         "api_key_env": llm.api_key_env, "api_key_present": key_present,
                         "max_cost_usd_per_day": cfg.context.max_cost_usd_per_day,
                         "status": "OK" if cfg.context.backend in ("none", "offline_fixture") or key_present else "BLOCKED"}
    ta_py = cfg.context.tradingagents.worker_python
    if ta_py is not None and not ta_py.is_absolute():
        ta_py = Path.cwd() / ta_py
    checks["tradingagents_worker"] = {"worker_python": str(ta_py) if ta_py else None, "exists": bool(ta_py and ta_py.exists()),
                                      "pinned_sha": cfg.context.tradingagents.upstream_sha,
                                      "status": "OK" if ta_py and ta_py.exists() else "NOT_INSTALLED"}
    if network:
        from aqe.ingestion.binance_rest import BinanceRest

        try:
            rest = BinanceRest(cfg.binance)
            m = rest.measure_clock()
            rest.close()
            skew = m.local_minus_server_ms
            ok = abs(skew) <= cfg.clock.max_abs_skew_ms
            checks["clock"] = {"reference": "binance /fapi/v1/time", "local_minus_server_ms": round(skew, 1), "rtt_ms": round(m.rtt_ms, 1),
                               "max_abs_skew_ms": cfg.clock.max_abs_skew_ms, "status": "OK" if ok else "FAIL",
                               "note": None if ok else "system clock is not synchronised; enable Windows automatic time sync (Settings > Time & language > Sync now) or `w32tm /resync` as admin"}
            checks["binance_rest"] = {"status": "OK", "base_url": cfg.binance.rest_base_url}
        except Exception as exc:  # noqa: BLE001
            checks["clock"] = {"status": "UNKNOWN", "error": f"{type(exc).__name__}: {exc}"}
            checks["binance_rest"] = {"status": "FAIL", "error": f"{type(exc).__name__}"}
    else:
        checks["clock"] = {"status": "UNKNOWN", "note": "network checks skipped"}
    statuses = [v.get("status") for v in checks.values() if isinstance(v, dict)]
    overall = "FAIL" if "FAIL" in statuses else ("WARN" if any(s in ("WARN", "BLOCKED", "UNKNOWN", "NOT_INSTALLED") for s in statuses) else "OK")
    return {"overall": overall, "checks": checks}
