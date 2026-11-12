"""Check that every pinned requirement has an installable wheel for a target platform (index METADATA only).

    python scripts/check_wheels.py requirements/linux-arm64-cpu.txt --platform linux_aarch64 [--json out.json]
    python scripts/check_wheels.py requirements/linux-x86_64-cpu.txt --platform linux_x86_64

Reads PyPI JSON (and the extra index named in the file, e.g. download.pytorch.org) — no install, no build. Result per
pin: WHEEL (matching cp312/abi3/py3 wheel), SDIST_ONLY (would need a local build: uncertain), MISSING. This is
evidence that a wheel EXISTS; it is not evidence that the stack imports and runs on that host.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

PLATFORM_TAGS = {
    "linux_aarch64": re.compile(r"(manylinux[_0-9a-z]*_aarch64|linux_aarch64)"),
    "linux_x86_64": re.compile(r"(manylinux[_0-9a-z]*_x86_64|linux_x86_64)"),
    "win_amd64": re.compile(r"win_amd64"),
}
PY = ("cp312", "abi3", "py3", "py2.py3")


def parse_requirements(path: Path, seen: set[Path] | None = None) -> tuple[list[tuple[str, str]], list[str]]:
    seen = seen or set()
    pins, indexes = [], []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("--extra-index-url", "--index-url")):
            indexes.append(line.split()[-1])
        elif line.startswith("-r "):
            sub = (path.parent / line[3:].strip()).resolve()
            if sub not in seen:
                seen.add(sub)
                p2, i2 = parse_requirements(sub, seen)
                pins += p2
                indexes += i2
        elif "==" in line:
            name, ver = line.split("==", 1)
            pins.append((name.strip(), ver.strip()))
    return pins, indexes


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "aqe-wheel-check/0.1", "Accept": "text/html,application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def _wheel_ok(fname: str, platform: str) -> bool:
    if not fname.endswith(".whl"):
        return False
    tags = fname[:-4].split("-")[-3:]  # python tag, abi tag, platform tag
    py_ok = any(t in tags[0].split(".") for t in PY) or tags[1] == "abi3"
    plat_ok = tags[2] == "any" or bool(PLATFORM_TAGS[platform].search(tags[2]))
    return py_ok and plat_ok


def files_for(name: str, version: str, indexes: list[str]) -> tuple[list[str], str]:
    if "+" in version:  # local version (torch builds): only on the extra index
        for idx in indexes:
            html = _get(f"{idx.rstrip('/')}/{name.lower()}/")
            files = [f.split("#")[0] for f in re.findall(r'href="[^"]*?([^"/]+\.whl[^"]*)"', html)]
            files = [f.replace("%2B", "+") for f in files if f.replace("%2B", "+").startswith(f"{name}-{version}-")]
            if files:
                return files, idx
        return [], ",".join(indexes)
    d = json.loads(_get(f"https://pypi.org/pypi/{name}/{version}/json"))
    return [u["filename"] for u in d.get("urls", [])], "pypi.org"


def check(path: Path, platform: str) -> dict:
    pins, indexes = parse_requirements(path)
    rows = []
    for name, ver in pins:
        try:
            files, src = files_for(name, ver, indexes)
        except Exception as exc:  # noqa: BLE001
            rows.append({"package": name, "version": ver, "status": "ERROR", "detail": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        wheels = [f for f in files if _wheel_ok(f, platform)]
        status = "WHEEL" if wheels else ("SDIST_ONLY" if any(f.endswith((".tar.gz", ".zip")) for f in files) else "MISSING")
        rows.append({"package": name, "version": ver, "status": status, "source": src, "example": wheels[0] if wheels else None})
    summary = {s: sum(1 for r in rows if r["status"] == s) for s in ("WHEEL", "SDIST_ONLY", "MISSING", "ERROR")}
    return {"requirements": str(path), "platform": platform, "python": "cp312", "summary": summary, "packages": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("requirements", type=Path)
    ap.add_argument("--platform", required=True, choices=sorted(PLATFORM_TAGS))
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    res = check(a.requirements, a.platform)
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res["summary"]))
    for r in res["packages"]:
        if r["status"] != "WHEEL":
            print(f"  {r['status']:10} {r['package']}=={r['version']} {r.get('detail', '')}")
    return 0 if res["summary"]["WHEEL"] == len(res["packages"]) else 1


if __name__ == "__main__":
    sys.exit(main())
