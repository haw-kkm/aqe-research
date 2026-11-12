"""Role preflight: run on the target host BEFORE enabling a service (docs/CLOUD_DEPLOYMENT.md).

PASS/WARN/FAIL per check; any FAIL -> non-zero exit (systemd `ExecStartPre` refuses to start). Network checks
(Binance REST ping + clock skew) are skipped only with `offline=True`, which the collector role does not accept.
Nothing is written except a uniquely named probe file that is removed immediately.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from aqe.ops.platform import detect, role_requirements


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"check": name, "status": status, "detail": detail}


def timesync_status() -> tuple[str, str]:
    if os.name == "nt":
        return "WARN", "Windows: verify with `w32tm /query /status` (not checked automatically)"
    try:
        r = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], capture_output=True, text=True, timeout=10)
        val = r.stdout.strip().lower()
        return ("PASS", "NTPSynchronized=yes") if val == "yes" else ("FAIL", f"NTPSynchronized={val or 'unknown'}: enable chrony/systemd-timesyncd")
    except (OSError, subprocess.SubprocessError) as exc:
        return "WARN", f"timedatectl unavailable ({type(exc).__name__}); check chrony manually"


def run_preflight(cfg, role: str, *, offline: bool = False, clock_factory=None, host: dict[str, Any] | None = None) -> dict[str, Any]:
    req = role_requirements(role)
    host = host or detect()
    checks: list[dict[str, str]] = []
    py = ".".join(host["python"].split(".")[:2])
    checks.append(_check("python", "PASS" if py == req["python"] else "FAIL", f"{host['python']} (required {req['python']})"))
    if req["os"]:
        checks.append(_check("os", "PASS" if host["os"] == req["os"] else "WARN", f"{host['os']} (role expects {req['os']})"))
    if req["arch"]:
        checks.append(_check("arch", "PASS" if host["arch"] == req["arch"] else "WARN", f"{host['arch']} (role expects {req['arch']})"))
    for label, root in (("data_root", Path(cfg.paths.data_root)), ("artifact_root", Path(cfg.paths.artifact_root))):
        if not root.is_absolute():
            checks.append(_check(label, "FAIL", f"{root} is not absolute"))
            continue
        try:
            root.mkdir(parents=True, exist_ok=True)
            fd, probe = tempfile.mkstemp(prefix=".aqe-preflight-probe-", dir=root)
            os.close(fd)
            Path(probe).unlink()
            free = shutil.disk_usage(root).free / 2**30
            checks.append(_check(label, "PASS" if free >= req["min_free_gb"] else "FAIL", f"{root} writable, {free:.1f} GiB free (min {req['min_free_gb']})"))
        except OSError as exc:
            checks.append(_check(label, "FAIL", f"{root}: {type(exc).__name__}: {exc}"))
    mods = ["numpy", "pandas", "pyarrow", "pydantic", "yaml", "httpx", "websockets"]
    if role != "oracle-collector":
        mods += ["lightgbm", "sklearn", "torch"]
    for m in mods:
        try:
            v = getattr(importlib.import_module(m), "__version__", "?")
            checks.append(_check(f"import:{m}", "PASS", v))
        except Exception as exc:  # noqa: BLE001
            checks.append(_check(f"import:{m}", "FAIL", f"{type(exc).__name__}: {exc}"[:200]))
    if req["gpu"]:
        checks.append(_check("cuda", "PASS" if host.get("cuda_available") else "FAIL", f"cuda_available={host.get('cuda_available')} {host.get('cuda_device') or ''}"))
    if role == "oracle-collector":
        status_p = Path(cfg.paths.data_root) / "status" / f"collector_{cfg.instruments[0].instrument_id}.json"
        final = False
        if status_p.exists():
            try:
                final = bool(json.loads(status_p.read_text(encoding="utf-8")).get("final"))
            except ValueError:
                final = False
        if status_p.exists() and not final and time.time() - status_p.stat().st_mtime < 60:
            checks.append(_check("single_collector", "FAIL", f"{status_p} updated within 60s and not final: a collector "
                                                             "is already writing this data_root (or just crashed; retry after 60s)"))
        else:
            checks.append(_check("single_collector", "PASS", "no fresh collector status on this data_root"))
        ts, detail = timesync_status()
        checks.append(_check("timesync", ts, detail))
    if req["binance"] or role == "oracle-shadow":
        if offline:
            checks.append(_check("binance", "FAIL" if req["binance"] else "WARN", "skipped (offline): the collector role requires the live check"))
        else:
            if clock_factory is None:
                from aqe.ingestion.binance_rest import BinanceRest
                clock_factory = BinanceRest
            try:
                client = clock_factory(cfg.binance)
                try:
                    skew = client.measure_clock(samples=3).local_minus_server_ms
                finally:
                    client.close()
                ok = math.isfinite(skew) and abs(skew) <= cfg.clock.max_abs_skew_ms
                checks.append(_check("binance_clock", "PASS" if ok else "FAIL",
                                     f"REST reachable; local-minus-Binance {skew:.1f} ms (limit {cfg.clock.max_abs_skew_ms:.0f})"))
            except Exception as exc:  # noqa: BLE001
                checks.append(_check("binance_clock", "FAIL", f"Binance REST unreachable: {type(exc).__name__}: {exc}"[:300]))
    overall = "FAIL" if any(c["status"] == "FAIL" for c in checks) else ("WARN" if any(c["status"] == "WARN" for c in checks) else "PASS")
    return {"role": role, "overall": overall, "host": {k: host.get(k) for k in ("os", "arch", "python", "torch", "cuda_available")},
            "checks": checks, "executable": sys.executable}
