"""Read-only inventory of a data_root / artifact_root (docs/CLOUD_DEPLOYMENT.md §데이터 인벤토리).

Never writes into the scanned roots: SQLite integrity is checked on an online-backup COPY in a temp directory, manifests
are only read. Every file gets a class and a transfer decision:
  transfer      complete files to migrate (raw captures, normalized data, manifests, models, records)
  snapshot      SQLite databases: migrate an online-backup snapshot, never the live file (+ -wal/-shm)
  live          being written now (`*.open`, current partitions, modified within `settle_seconds`) — next pass
  host_local    collector status/checkpoint/lock of THIS host — never copied to another collector
  regenerable   caches/scratch that can be rebuilt (prediction cache, staging)
  temp          temporary files (`*.tmp`, `*.part`, write probes)
  unclassified  unknown layout — reported for review, not transferred automatically
Manifests store absolute source paths (e.g. `E:\\aqe-v2\\data\\normalized\\...`); they are verified by mapping the
part after `data`/`normalized` onto the current root — the manifest itself is never rewritten (its hash is lineage).
"""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import time
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from aqe.storage import sha256_file

SETTLE_SECONDS = 900
_TS = re.compile(r"(\d{8}T\d{6})")
_TIME_COLS = ("open_time", "interval_start", "funding_time", "timestamp", "event_time", "received_at", "as_of")


def _now() -> float:
    return time.time()


def classify_data(rel: str, mtime: float, now: float | None = None, settle_seconds: int = SETTLE_SECONDS) -> tuple[str, str]:
    """(class, reason) for a path relative to data_root (POSIX separators)."""
    now = _now() if now is None else now
    p = PurePosixPath(rel)
    name, parts = p.name, p.parts
    if name.endswith((".tmp", ".part")) or name.startswith(".aqe-collect-probe-") or ".tmp-" in name:
        return "temp", "temporary file"
    if name.endswith(".open"):
        return "live", "open raw log being written by the collector"
    if parts[0] == "_transfer":
        return "host_local", "transfer bookkeeping / conflict quarantine of this host (review, never re-sent)"
    if parts[0] == "status":
        return "host_local", "collector status/checkpoint/lock of this host (never copied to another collector)"
    if parts[0] == "meta" and name.endswith((".sqlite-wal", ".sqlite-shm", "-journal")):
        return "temp", "SQLite sidecar (captured by the online-backup snapshot)"
    if parts[0] == "meta" and name.endswith((".sqlite", ".db")):
        return "snapshot", "SQLite: migrate an online-backup snapshot, not the live file"
    if parts[:2] == ("context", "_staging"):
        return "regenerable", "staging scratch"
    if now - mtime < settle_seconds:
        return "live", f"modified within {settle_seconds}s (may still be written)"
    if parts[0] == "raw":
        return "transfer", "raw capture/download (irreplaceable original)"
    if parts[0] == "normalized":
        return "transfer", "normalized data"
    if parts[0] == "quality":
        today = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        return ("live", "today's append-only quality journal") if p.stem >= today else ("transfer", "quality journal (complete day)")
    if parts[0] in ("manifests", "imports"):
        return "transfer", "manifest/import (preserved verbatim)"
    if parts[0] in ("context", "runs", "reports", "logs", "pipeline", "ops"):
        return "transfer", "records/history"
    return "unclassified", "unknown layout: review before migrating"


def classify_artifact(rel: str, mtime: float, now: float | None = None, settle_seconds: int = SETTLE_SECONDS) -> tuple[str, str]:
    now = _now() if now is None else now
    parts = PurePosixPath(rel).parts
    name = parts[-1]
    if name.endswith((".tmp", ".part")) or ".tmp-" in name:
        return "temp", "temporary file"
    if parts[0] == "_transfer":
        return "host_local", "transfer bookkeeping / conflict quarantine of this host"
    if parts[0] in ("_releases", "_staging"):
        return "regenerable", "model release staging (re-creatable from the release bundle)"
    if len(parts) >= 2 and parts[1] == "pred_cache":
        return "regenerable", "prediction cache"
    if now - mtime < settle_seconds:
        return "live", f"modified within {settle_seconds}s"
    if len(parts) == 2 and name in ("registry.json", "active_release.json"):
        return "transfer", "model registry (active artifact ids)"
    if len(parts) >= 2 and parts[1] in ("artifacts", "dataset", "oos", "evaluation", "registry_history"):
        return "transfer", {"artifacts": "model artifact", "dataset": "dataset + manifest", "oos": "OOS predictions + manifests",
                            "evaluation": "evaluation records", "registry_history": "registry history"}[parts[1]]
    return "unclassified", "unknown layout: review before migrating"


def classify_release(rel: str, mtime: float, now: float | None = None, settle_seconds: int = SETTLE_SECONDS) -> tuple[str, str]:
    """A built model release directory (release.json + bundle/): everything is transferred except temp files."""
    name = PurePosixPath(rel).name
    if name.endswith((".tmp", ".part")) or ".tmp-" in name:
        return "temp", "temporary file"
    return "transfer", "model release bundle"


def _time_range(path: Path) -> tuple[str | None, str | None]:
    """Min/max of the first known time column from Parquet statistics (no data read), or the timestamp in the name."""
    if path.suffix == ".parquet":
        try:
            import pyarrow.parquet as pq

            md = pq.ParquetFile(path).metadata
            names = [md.schema.column(i).name for i in range(md.num_columns)]
            col = next((c for c in _TIME_COLS if c in names), None)
            if col is not None:
                i = names.index(col)
                lo = hi = None
                for rg in range(md.num_row_groups):
                    st = md.row_group(rg).column(i).statistics
                    if st is not None and st.has_min_max:
                        lo = st.min if lo is None else min(lo, st.min)
                        hi = st.max if hi is None else max(hi, st.max)
                return (str(lo) if lo is not None else None, str(hi) if hi is not None else None)
        except Exception:  # noqa: BLE001 - unreadable parquet is reported by validate_parquet
            return None, None
    m = _TS.search(path.name)
    if m:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).isoformat(), None
    return None, None


def validate_parquet(path: Path) -> dict[str, Any]:
    """Readable footer + schema + row count (no full read)."""
    try:
        import pyarrow.parquet as pq

        f = pq.ParquetFile(path)
        return {"ok": True, "rows": f.metadata.num_rows, "columns": f.schema_arrow.names,
                "schema_hash": _schema_hash(f.schema_arrow)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


# collector/backfill partition layouts: (regex on the rel path, time column, partition span)
_PARTITIONS = (
    (re.compile(r"^normalized/bars/[^/]+/[^/]+/[^/]+/[^/]+/(\d{4})-(\d{2})\.parquet$"), "open_time", "month"),
    (re.compile(r"^normalized/(?:flow_summaries)/[^/]+/[^/]+/[^/]+/(\d{4}-\d{2}-\d{2})/(\d{2})\.parquet$"), "interval_start", "hour"),
    (re.compile(r"^normalized/(?:mark_funding_estimates)/[^/]+/[^/]+/[^/]+/(\d{4}-\d{2}-\d{2})/(\d{2})\.parquet$"), "event_time", "hour"),
    (re.compile(r"^normalized/oi_observations/[^/]+/[^/]+/[^/]+/(\d{4}-\d{2}-\d{2})\.parquet$"), "received_at", "day"),
)


def partition_problems(rel: str, path: Path) -> list[str]:
    """Rows of a partitioned Parquet file must lie inside the period its path names (statistics only, no data read)."""
    import pandas as pd

    for rx, col, span in _PARTITIONS:
        m = rx.match(rel)
        if not m:
            continue
        if span == "month":
            lo = pd.Timestamp(f"{m.group(1)}-{m.group(2)}-01", tz="UTC")
            hi = lo + pd.offsets.MonthBegin(1)
        elif span == "hour":
            lo = pd.Timestamp(f"{m.group(1)}T{m.group(2)}:00:00", tz="UTC")
            hi = lo + pd.Timedelta(hours=1)
        else:
            lo = pd.Timestamp(m.group(1), tz="UTC")
            hi = lo + pd.Timedelta(days=1)
        try:
            import pyarrow.parquet as pq

            md = pq.ParquetFile(path).metadata
            names = [md.schema.column(i).name for i in range(md.num_columns)]
            if col not in names:
                return [f"{rel}: time column {col} missing"]
            i = names.index(col)
            mins = [md.row_group(g).column(i).statistics.min for g in range(md.num_row_groups)
                    if md.row_group(g).column(i).statistics is not None and md.row_group(g).column(i).statistics.has_min_max]
            maxs = [md.row_group(g).column(i).statistics.max for g in range(md.num_row_groups)
                    if md.row_group(g).column(i).statistics is not None and md.row_group(g).column(i).statistics.has_min_max]
            if not mins:
                return []
            tmin, tmax = pd.Timestamp(min(mins)), pd.Timestamp(max(maxs))
            tmin = tmin.tz_localize("UTC") if tmin.tzinfo is None else tmin
            tmax = tmax.tz_localize("UTC") if tmax.tzinfo is None else tmax
            if tmin < lo or tmax >= hi:
                return [f"{rel}: {col} {tmin}..{tmax} outside partition {lo}..{hi}"]
        except Exception as exc:  # noqa: BLE001
            return [f"{rel}: unreadable statistics ({type(exc).__name__})"]
        return []
    return []


def _schema_hash(schema) -> str:
    import hashlib

    return hashlib.sha256(json.dumps([(f.name, str(f.type)) for f in schema], sort_keys=False).encode()).hexdigest()[:16]


def rebase(recorded: str, root: Path, anchors: tuple[str, ...] = ("data", "artifacts")) -> Path | None:
    """Map an absolute path recorded on another host (Windows or POSIX) onto `root`: the part after the last
    `<anchor>` component (e.g. `E:\\aqe-v2\\data\\normalized\\x.parquet` -> `<root>/normalized/x.parquet`)."""
    pure = PureWindowsPath(recorded) if ("\\" in recorded or re.match(r"^[A-Za-z]:", recorded)) else PurePosixPath(recorded)
    parts = list(pure.parts)
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].lower() in anchors:
            return Path(root).joinpath(*parts[i + 1:])
    return None


def check_manifest(manifest: Path, data_root: Path, hash_files: bool = True) -> dict[str, Any]:
    """Verify every `files[]` / `source_files[]` entry (path + sha256) of a manifest against `data_root`."""
    try:
        m = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"manifest": str(manifest), "ok": False, "error": f"unreadable: {type(exc).__name__}"}
    entries = [e for key in ("files", "source_files") for e in (m.get(key) or []) if isinstance(e, dict) and e.get("path")]
    res = {"manifest": str(manifest), "entries": len(entries), "ok_files": 0, "missing": [], "mismatch": [], "unmapped": [],
           "hashed": hash_files}
    for e in entries:
        target = rebase(e["path"], data_root)
        if target is None:
            res["unmapped"].append(e["path"])
            continue
        if not target.exists():
            res["missing"].append(str(target))
            continue
        if hash_files and e.get("sha256") and sha256_file(target) != e["sha256"]:
            res["mismatch"].append(str(target))
            continue
        res["ok_files"] += 1
    res["ok"] = not (res["missing"] or res["mismatch"] or res["unmapped"])
    return res


def sqlite_backup(src: Path, dest: Path, *, source_readonly: bool = True, attempts: int = 3) -> dict[str, Any]:
    """Consistent copy of a (possibly live, WAL) SQLite DB, verified with `PRAGMA integrity_check`.

    source_readonly=True (default; production/source hosts): SQLite never opens the source — even a `mode=ro`
    connection on a WAL database creates `-wal`/`-shm` sidecars next to it. The DB file and its existing `-wal` are
    byte-copied into a private temp dir (retried until two consecutive copies are identical), and the online-backup
    API runs on that copy (it replays committed WAL frames; uncommitted ones are ignored).
    source_readonly=False (a host we operate, writer may be live): the online-backup API runs on the DB itself."""
    import shutil

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise FileExistsError(f"backup target exists (never overwritten): {dest}")
    src = Path(src)
    if source_readonly:
        with tempfile.TemporaryDirectory(prefix="aqe-sqlite-copy-") as td:
            names = [src.name, src.name + "-wal"]
            prev = None
            for _ in range(attempts):
                for n in names:
                    p, q = src.with_name(n), Path(td) / n
                    q.unlink(missing_ok=True)
                    if p.exists():
                        shutil.copyfile(p, q)
                cur = tuple(sha256_file(Path(td) / n) if (Path(td) / n).exists() else None for n in names)
                if cur == prev:
                    break
                prev = cur
            with closing(sqlite3.connect(Path(td) / src.name)) as s, closing(sqlite3.connect(dest)) as d:
                s.backup(d)
    else:
        with closing(sqlite3.connect(src, timeout=30)) as s, closing(sqlite3.connect(dest)) as d:
            s.backup(d)
    with closing(sqlite3.connect(dest)) as d:  # sqlite3's own context manager does not close the connection
        integrity = d.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [r[0] for r in d.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        counts = {t: d.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
    return {"source": str(src), "backup": str(dest), "integrity": integrity, "tables": counts,
            "sha256": sha256_file(dest), "bytes": dest.stat().st_size, "method": "copy+backup" if source_readonly else "online-backup"}


def sqlite_restore(backup: Path, target: Path) -> dict[str, Any]:
    """Restore a verified backup to a NEW path (refuses to overwrite an existing database)."""
    if target.exists():
        raise FileExistsError(f"restore target exists (never overwritten): {target}")
    with closing(sqlite3.connect(backup)) as b:
        if b.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError(f"backup failed integrity_check: {backup}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(backup)) as b, closing(sqlite3.connect(target)) as t:
        b.backup(t)
    with closing(sqlite3.connect(target)) as t:
        return {"target": str(target), "integrity": t.execute("PRAGMA integrity_check").fetchone()[0]}


def _model_status(art_root: Path) -> list[dict[str, Any]]:
    from aqe.models.registry import REQUIRED_META_KEYS

    out = []
    for reg in sorted(Path(art_root).glob("*/registry.json")):
        exp = reg.parent
        try:
            r = json.loads(reg.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            out.append({"experiment": exp.name, "ok": False, "error": f"registry unreadable: {type(exc).__name__}"})
            continue
        slots = []
        for h, s in (r.get("horizons") or {}).items():
            for slot, aid in s.items():
                d = exp / "artifacts" / aid
                meta = d / "metadata.json"
                row = {"horizon": h, "slot": slot, "artifact_id": aid, "present": d.is_dir(), "metadata": meta.exists()}
                if meta.exists():
                    m = json.loads(meta.read_text(encoding="utf-8"))
                    row.update(missing_keys=[k for k in REQUIRED_META_KEYS if k not in m], readiness=m.get("readiness"),
                               train_label_cutoff=m.get("train_label_cutoff"), kind=m.get("kind"),
                               files=sorted(p.name for p in d.iterdir() if p.is_file()))
                slots.append(row)
        ok = all(s["present"] and s["metadata"] and not s.get("missing_keys") for s in slots)
        out.append({"experiment": exp.name, "ok": ok, "slots": slots,
                    "evaluation_runs": sorted(p.name for p in (exp / "evaluation").glob("eval_*.json")),
                    "oos_manifests": sorted(p.name for p in (exp / "oos").glob("*.manifest.json"))})
    return out


def scan(root: Path, kind: str, *, hash_files: bool = True, settle_seconds: int = SETTLE_SECONDS,
         now: float | None = None) -> dict[str, Any]:
    """Inventory one root (`kind` = data | artifacts). Read-only."""
    root = Path(root)
    now = _now() if now is None else now
    classify = {"data": classify_data, "artifacts": classify_artifact, "release": classify_release}[kind]
    files, by_dir, by_ext, by_class = [], defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        st = p.stat()
        cls, why = classify(rel, st.st_mtime, now, settle_seconds)
        ext = ".jsonl.gz" if rel.endswith(".jsonl.gz") else (".jsonl.gz.open" if rel.endswith(".jsonl.gz.open") else p.suffix or "(none)")
        row = {"rel": rel, "bytes": st.st_size, "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
               "class": cls, "reason": why, "ext": ext}
        if cls == "transfer":
            row["start"], row["end"] = _time_range(p)
            if hash_files:
                row["sha256"] = sha256_file(p)
            if p.suffix == ".parquet":
                v = validate_parquet(p)
                row["parquet_ok"], row["rows"], row["schema_hash"] = v["ok"], v.get("rows"), v.get("schema_hash")
                if not v["ok"]:
                    row["parquet_error"] = v["error"]
                elif kind == "data":
                    probs = partition_problems(rel, p)
                    if probs:
                        row["partition_problem"] = probs[0]
        files.append(row)
        top = "/".join(PurePosixPath(rel).parts[:2]) if kind == "artifacts" else PurePosixPath(rel).parts[0]
        for agg, key in ((by_dir, top), (by_ext, ext), (by_class, cls)):
            agg[key][0] += 1
            agg[key][1] += st.st_size
    out: dict[str, Any] = {
        "root": str(root), "kind": kind, "scanned_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "settle_seconds": settle_seconds, "hashed": hash_files, "total_files": len(files),
        "total_bytes": sum(f["bytes"] for f in files),
        "by_dir": {k: {"files": v[0], "bytes": v[1]} for k, v in sorted(by_dir.items())},
        "by_ext": {k: {"files": v[0], "bytes": v[1]} for k, v in sorted(by_ext.items(), key=lambda kv: -kv[1][1])},
        "by_class": {k: {"files": v[0], "bytes": v[1]} for k, v in sorted(by_class.items())},
        "parquet_failures": [f["rel"] for f in files if f.get("parquet_ok") is False],
        "partition_mismatches": [f["partition_problem"] for f in files if f.get("partition_problem")],
        "parquet_checked": sum(1 for f in files if "parquet_ok" in f),
        "files": files,
    }
    ranges: dict[str, list[str]] = {}
    for f in files:
        if f.get("start"):
            parts = PurePosixPath(f["rel"]).parts
            key = "/".join(parts[:2] if parts[0] in ("raw", "normalized") or kind != "data" else parts[:1])
            lo, hi = ranges.get(key, [f["start"], f.get("end") or f["start"]])
            ranges[key] = [min(lo, f["start"]), max(hi, f.get("end") or f["start"])]
    out["time_ranges"] = ranges
    if kind == "data":
        out["manifests"] = [check_manifest(m, root, hash_files) for m in sorted((root / "manifests").glob("*.json"))]
        out["sqlite"] = []
        for db in sorted((root / "meta").glob("*.sqlite")):
            try:
                with tempfile.TemporaryDirectory(prefix="aqe-inv-sqlite-") as td:
                    out["sqlite"].append({k: v for k, v in sqlite_backup(db, Path(td) / db.name).items() if k != "backup"})
            except Exception as exc:  # noqa: BLE001
                out["sqlite"].append({"source": str(db), "integrity": f"FAILED: {type(exc).__name__}: {exc}"[:300]})
    elif kind == "artifacts":
        out["models"] = _model_status(root)
    return out


def render_markdown(invs: list[dict[str, Any]]) -> str:
    def gb(b: int) -> str:
        return f"{b / 2**30:.3f} GiB"

    L = ["# AQE 데이터 인벤토리 (읽기 전용 조사)", ""]
    for inv in invs:
        L += [f"## {inv['kind']}: `{inv['root']}`", f"- 조사 시각 `{inv['scanned_at']}` · 파일 {inv['total_files']}개 · {gb(inv['total_bytes'])}"
              f" · SHA-256 {'계산' if inv['hashed'] else '생략'} · 안정화 기준 {inv['settle_seconds']}초", "",
              "| 분류 | 파일 | 용량 |", "|---|---|---|"]
        L += [f"| {k} | {v['files']} | {gb(v['bytes'])} |" for k, v in inv["by_class"].items()]
        L += ["", "| 디렉터리 | 파일 | 용량 |", "|---|---|---|"]
        L += [f"| {k} | {v['files']} | {gb(v['bytes'])} |" for k, v in inv["by_dir"].items()]
        L += ["", "| 형식 | 파일 | 용량 |", "|---|---|---|"]
        L += [f"| {k} | {v['files']} | {gb(v['bytes'])} |" for k, v in inv["by_ext"].items()]
        if inv["time_ranges"]:
            L += ["", "| 범위(상위 경로) | 시작 | 끝 |", "|---|---|---|"]
            L += [f"| {k} | {v[0]} | {v[1]} |" for k, v in sorted(inv["time_ranges"].items())]
        if "parquet_checked" in inv:
            L.append(f"- Parquet 검사 {inv['parquet_checked']}개: 읽기 실패 {len(inv['parquet_failures'])} · 파티션 기간 불일치 {len(inv.get('partition_mismatches', []))}")
        if inv.get("parquet_failures"):
            L.append(f"- Parquet 읽기 실패: {inv['parquet_failures'][:10]}")
        if inv.get("partition_mismatches"):
            L.append(f"- 파티션 기간 불일치: {inv['partition_mismatches'][:5]}")
        for m in inv.get("manifests", []):
            L.append(f"- manifest `{Path(m['manifest']).name}`: 항목 {m.get('entries')} · 일치 {m.get('ok_files')} · 누락 {len(m.get('missing', []))}"
                     f" · 해시 불일치 {len(m.get('mismatch', []))} · 경로 매핑 실패 {len(m.get('unmapped', []))} → {'OK' if m.get('ok') else '확인 필요'}")
        for s in inv.get("sqlite", []):
            L.append(f"- SQLite `{Path(s['source']).name}`: integrity `{s.get('integrity')}` · 표 {s.get('tables')}")
        for e in inv.get("models", []):
            L.append(f"- 모델 `{e['experiment']}`: {'OK' if e['ok'] else '확인 필요'} · 슬롯 {len(e.get('slots', []))} · "
                     f"평가 {len(e.get('evaluation_runs', []))} · OOS manifest {len(e.get('oos_manifests', []))}")
        live = [f["rel"] for f in inv["files"] if f["class"] == "live"]
        if live:
            L.append(f"- 이번 이전에서 제외(기록 중): {live[:8]}{' …' if len(live) > 8 else ''}")
        unk = [f["rel"] for f in inv["files"] if f["class"] == "unclassified"]
        if unk:
            L.append(f"- 분류되지 않음(검토 필요): {unk[:8]}")
        L.append("")
    return "\n".join(L)
