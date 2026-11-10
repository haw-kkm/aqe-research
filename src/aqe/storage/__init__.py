"""Storage utilities (B05.4): atomic JSON, hashing, run records, raw append-only logs."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from aqe.contracts.core import iso_utc, utc_now


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_default(o: Any):
    if isinstance(o, datetime):
        return iso_utc(o)
    if isinstance(o, Path):
        return str(o)
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(f"not JSON serializable: {type(o)}")


def dumps(obj: Any, indent: int | None = 2) -> str:
    return json.dumps(obj, default=_json_default, ensure_ascii=False, indent=indent, sort_keys=False)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: target briefly locked by a reader
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, dumps(obj))


def read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def new_id(prefix: str) -> str:
    return f"{prefix}-{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


class RawLogWriter:
    """Append-only gzip JSONL writer with size/time rotation (B05.4).

    Active file: <dir>/<stem>.<ts>.jsonl.gz.open ; on rotation/close it is renamed to .jsonl.gz.
    A leftover .open file after a crash is renamed to .recovered.jsonl.gz at startup (readable up
    to the last complete gzip block).
    """

    def __init__(self, directory: Path, stem: str, max_bytes: int = 64 << 20, max_seconds: float = 3600):
        self.directory = Path(directory)
        self.stem = stem
        self.max_bytes = max_bytes
        self.max_seconds = max_seconds
        self.directory.mkdir(parents=True, exist_ok=True)
        self._fh: gzip.GzipFile | None = None
        self._path: Path | None = None
        self._opened_at = 0.0
        self._bytes = 0
        self.closed_files: list[Path] = []
        self._recover()

    def _recover(self) -> None:
        for p in self.directory.glob(f"{self.stem}.*.jsonl.gz.open"):
            target = p.with_name(p.name.replace(".jsonl.gz.open", ".recovered.jsonl.gz"))
            os.replace(p, target)
            self.closed_files.append(target)

    def _open(self) -> None:
        ts = utc_now().strftime("%Y%m%dT%H%M%S%fZ")
        self._path = self.directory / f"{self.stem}.{ts}.jsonl.gz.open"
        self._fh = gzip.open(self._path, "ab", compresslevel=5)
        self._opened_at = time.monotonic()
        self._bytes = 0

    def write(self, record: dict[str, Any]) -> None:
        if self._fh is None:
            self._open()
        line = (json.dumps(record, default=_json_default, separators=(",", ":")) + "\n").encode()
        self._fh.write(line)
        self._bytes += len(line)
        if self._bytes >= self.max_bytes or (time.monotonic() - self._opened_at) >= self.max_seconds:
            self.rotate()

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()

    def rotate(self) -> Path | None:
        if self._fh is None or self._path is None:
            return None
        self._fh.close()
        final = self._path.with_name(self._path.name[: -len(".open")])
        os.replace(self._path, final)
        self.closed_files.append(final)
        self._fh = None
        self._path = None
        return final

    def close(self) -> None:
        self.rotate()


def iter_raw_log(path: Path):
    """Yield records from a (possibly truncated) gzip JSONL file."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        return
    except (EOFError, gzip.BadGzipFile, OSError):
        return
