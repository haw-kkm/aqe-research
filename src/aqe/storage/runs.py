"""Run records (B12): every command leaves run id, config hash, inputs/outputs, status, error code."""

from __future__ import annotations

import platform
import sys
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aqe import __version__
from aqe.contracts.core import iso_utc, utc_now
from aqe.storage import atomic_write_json, dumps, new_id

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 3  # external precondition missing (DB, API key, data)
EXIT_NOT_READY = 4  # artifacts/data not ready; explicit abstain


@dataclass
class RunRecord:
    command: str
    config_path: str
    config_hash: str
    runs_dir: Path
    run_id: str = field(default_factory=lambda: new_id("run"))
    started_at: str = field(default_factory=lambda: iso_utc(utc_now()))
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    status: str = "RUNNING"
    exit_code: int | None = None
    error: str | None = None

    def path(self) -> Path:
        return self.runs_dir / f"{self.run_id}.json"

    def save(self) -> None:
        rec = {
            "run_id": self.run_id,
            "command": self.command,
            "config_path": self.config_path,
            "config_hash": self.config_hash,
            "aqe_version": __version__,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "started_at": self.started_at,
            "finished_at": iso_utc(utc_now()) if self.status != "RUNNING" else None,
            "status": self.status,
            "exit_code": self.exit_code,
            "error": self.error,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "notes": self.notes,
        }
        atomic_write_json(self.path(), rec)
        if self.status != "RUNNING":
            index = self.runs_dir / "runs_index.jsonl"
            with open(index, "a", encoding="utf-8") as fh:
                fh.write(dumps({k: rec[k] for k in ("run_id", "command", "status", "exit_code", "started_at", "finished_at", "config_hash")}, indent=None) + "\n")


@contextmanager
def run_context(command: str, config_path: Path, cfg_hash: str, runs_dir: Path):
    runs_dir.mkdir(parents=True, exist_ok=True)
    rec = RunRecord(command=command, config_path=str(config_path), config_hash=cfg_hash, runs_dir=runs_dir)
    rec.save()
    try:
        yield rec
        if rec.exit_code is None:
            rec.exit_code = EXIT_OK
        rec.status = {EXIT_OK: "OK", EXIT_BLOCKED: "BLOCKED", EXIT_NOT_READY: "NOT_READY"}.get(rec.exit_code, "ERROR")
    except KeyboardInterrupt:
        rec.status, rec.exit_code, rec.error = "INTERRUPTED", 130, "KeyboardInterrupt"
        raise
    except Exception as exc:  # noqa: BLE001 - recorded then re-raised
        rec.status, rec.exit_code = "ERROR", EXIT_ERROR
        rec.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}"
        raise
    finally:
        rec.save()
