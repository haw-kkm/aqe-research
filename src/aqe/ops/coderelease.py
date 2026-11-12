"""Code release = ONE verified git commit (docs/CLOUD_DEPLOYMENT.md §배포 버전 선정).

`check_code_release` refuses a dirty working tree (modified, staged or untracked files), records the exact commit and
tree, the branches/tags containing it, dependency lock hashes, and optionally the test-suite result on THIS host.
Servers install by commit SHA (deploy/linux/install_release.sh); a branch name is never deployed, so unverified
development work cannot reach a server by itself.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOCK_FILES = ("requirements.lock", "requirements-ta.lock", "requirements/linux-arm64-cpu.txt",
              "requirements/linux-x86_64-cpu.txt", "requirements/linux-x86_64-cuda.txt", "pyproject.toml")


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def check_code_release(repo: Path, *, run_tests: bool = False, pytest_args: tuple[str, ...] = ("-q", "-p", "no:cacheprovider"),
                       expect_commit: str | None = None) -> dict[str, Any]:
    repo = Path(repo)
    dirty = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    commit = _git(repo, "rev-parse", "HEAD")
    out: dict[str, Any] = {"commit": commit, "checked_at": datetime.now(timezone.utc).isoformat(),
                           "host": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()}}
    problems = []
    if dirty:
        problems.append(f"working tree not clean ({len(dirty.splitlines())} paths): commit or set aside changes first; "
                        "a release is exactly one commit")
    if expect_commit and not commit.startswith(expect_commit):
        problems.append(f"HEAD {commit[:12]} is not the expected commit {expect_commit}")
    out["tree"] = _git(repo, "rev-parse", "HEAD^{tree}")
    out["branches"] = [b.strip("* ").strip() for b in _git(repo, "branch", "-a", "--contains", commit).splitlines()]
    out["tags"] = _git(repo, "tag", "--points-at", commit).splitlines()
    out["locks"] = {f: hashlib.sha256((repo / f).read_bytes()).hexdigest() for f in LOCK_FILES if (repo / f).exists()}
    if run_tests and not problems:
        r = subprocess.run([sys.executable, "-m", "pytest", *pytest_args], cwd=repo, capture_output=True, text=True, timeout=7200)
        tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
        out["tests"] = {"exit_code": r.returncode, "summary": tail[0], "python": sys.executable}
        if r.returncode != 0:
            problems.append(f"tests failed on this host: {tail[0]}")
    elif run_tests:
        out["tests"] = {"exit_code": None, "summary": "not run (tree not clean)"}
    out["problems"] = problems
    out["verified"] = not problems and (not run_tests or out.get("tests", {}).get("exit_code") == 0)
    return out
