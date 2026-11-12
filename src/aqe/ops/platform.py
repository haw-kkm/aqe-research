"""OS / CPU / accelerator facts and OS-neutral virtual-environment paths.

Windows venvs keep the interpreter in `Scripts\\python.exe`, POSIX venvs in `bin/python`. Configs written on Windows
(`.venv-ta/Scripts/python.exe`) keep working on Linux and vice versa: the configured path is used if it exists,
otherwise the same venv's interpreter for the running OS.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

ROLES = ("windows-dev", "oracle-collector", "oracle-shadow", "google-collector", "google-cpu-train", "google-gpu-train")
# 24/7 public-data collectors (one per data store). Either can be the only collector: Oracle ARM64 (Oracle + Google
# training) or Google x86-64 (Google-only operation). Both run local-collector/collect_local.py under the same unit.
COLLECTOR_ROLES = ("oracle-collector", "google-collector")


def is_collector_role(role: str) -> bool:
    return role in COLLECTOR_ROLES


def venv_python(venv_dir: Path, windows: bool | None = None) -> Path:
    windows = (os.name == "nt") if windows is None else windows
    return Path(venv_dir) / ("Scripts/python.exe" if windows else "bin/python")


def _venv_root(configured: str) -> str | None:
    """`<venv>` part of `<venv>/Scripts/python.exe` or `<venv>/bin/python[3[.x]]` (either separator), else None."""
    parts = PureWindowsPath(configured).parts if "\\" in configured else PurePosixPath(configured).parts
    low = [p.lower() for p in parts]
    if len(parts) >= 2 and low[-2] == "scripts" and low[-1] in ("python.exe", "python"):
        return str(PurePosixPath(*parts[:-2])) if "\\" not in configured else str(PureWindowsPath(*parts[:-2]))
    if len(parts) >= 2 and low[-2] == "bin" and low[-1].startswith("python"):
        return str(PurePosixPath(*parts[:-2]))
    return None


def resolve_venv_python(configured: Path | str | None, root: Path) -> Path | None:
    """Interpreter for a configured worker venv path, relative paths taken from `root`. Returns the configured path
    when it exists; otherwise the OS-appropriate interpreter of the same venv when that exists; otherwise the
    configured path (callers report 'not installed')."""
    if configured is None:
        return None
    raw = str(configured)
    p = Path(raw)
    p = p if p.is_absolute() else Path(root) / p
    if p.exists():
        return p
    venv = _venv_root(raw)
    if venv is not None:
        alt = venv_python(Path(venv) if Path(venv).is_absolute() else Path(root) / venv)
        if alt.exists():
            return alt
    return p


def detect() -> dict[str, Any]:
    """Facts about this host (no network). GPU = CUDA visible to the installed torch build, if torch imports."""
    info: dict[str, Any] = {"os": platform.system(), "os_release": platform.release(), "machine": platform.machine(),
                            "python": platform.python_version(), "python_impl": platform.python_implementation(),
                            "pointer_bits": 64 if sys.maxsize > 2**32 else 32, "cpu_count": os.cpu_count()}
    arch = platform.machine().lower()
    info["arch"] = "arm64" if arch in ("aarch64", "arm64") else ("x86_64" if arch in ("x86_64", "amd64") else arch)
    try:
        import torch  # noqa: PLC0415

        info["torch"] = torch.__version__
        info["cuda_available"] = bool(torch.cuda.is_available())
        info["cuda_device"] = torch.cuda.get_device_name(0) if info["cuda_available"] else None
    except Exception as exc:  # noqa: BLE001 - torch missing or broken is a fact to report, not a crash
        info["torch"] = None
        info["torch_error"] = f"{type(exc).__name__}: {exc}"[:200]
        info["cuda_available"] = False
    info.update(memory_gb())
    for mod in ("lightgbm", "numpy", "pandas", "pyarrow", "sklearn"):
        try:
            info[mod] = __import__(mod).__version__
        except Exception as exc:  # noqa: BLE001
            info[mod] = None
            info[f"{mod}_error"] = f"{type(exc).__name__}"
    return info


def memory_gb(meminfo: str | None = None) -> dict[str, float | None]:
    """RAM and swap in GiB from /proc/meminfo (Linux); None elsewhere."""
    if meminfo is None:
        try:
            meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
        except OSError:
            return {"mem_total_gb": None, "swap_total_gb": None}
    vals = {}
    for line in meminfo.splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "SwapTotal") and rest.split():
            vals[key] = round(int(rest.split()[0]) / 2**20, 2)  # kB -> GiB
    return {"mem_total_gb": vals.get("MemTotal"), "swap_total_gb": vals.get("SwapTotal")}


def role_requirements(role: str) -> dict[str, Any]:
    """What a role needs from the host (checked by preflight; docs/CLOUD_DEPLOYMENT.md).

    collector roles: live Binance check, single-collector and time-sync checks, no ML imports required.
    `separate_data_disk`: WARN when data_root shares the boot filesystem (Google: the data must survive a VM rebuild).
    `recommended_mem_gb`: WARN below it unless swap makes up the difference (the collector peaks ~150-250 MB RSS;
    pip installs and pandas/pyarrow imports need headroom)."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; one of {ROLES}")
    req: dict[str, Any] = {"python": "3.12", "arch": None, "os": None, "gpu": False, "binance": False, "min_free_gb": 10.0,
                           "collector": is_collector_role(role), "separate_data_disk": False, "recommended_mem_gb": None}
    if role == "windows-dev":
        req.update(os="Windows")
    elif role.startswith("oracle"):
        req.update(os="Linux", arch="arm64", binance=role == "oracle-collector", min_free_gb=20.0)
    elif role == "google-collector":
        # Google-only operation: small x86-64 VM, data on its own persistent disk mounted at /srv/aqe
        req.update(os="Linux", arch="x86_64", binance=True, min_free_gb=10.0, separate_data_disk=True)
    elif role == "google-cpu-train":
        req.update(os="Linux", arch="x86_64", min_free_gb=30.0)
    elif role == "google-gpu-train":
        req.update(os="Linux", arch="x86_64", gpu=True, min_free_gb=30.0)
    if req["collector"]:
        req["recommended_mem_gb"] = 1.5
    return req
