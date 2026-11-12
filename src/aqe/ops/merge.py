"""Union of collector partitions that two collectors wrote for the same period (PC vs Oracle overlap) — explicit step.

A transfer never overwrites; a partition both collectors wrote (same hour/day/month path, different content) is kept
under `<root>/_transfer/<id>/conflicts/<rel>`. `merge_conflicts` adds the incoming rows whose KEY is missing in the
destination file — the same keys the collector's own upsert uses — and never changes an existing destination row
(destination wins). Per file: dry-run report by default; with execute=True the current destination is copied to
`_transfer/<id>/merge_backup/<rel>`, the merged file is written beside it, re-read and verified (row count, every
destination row unchanged), and swapped in atomically only if the destination did not change meanwhile.
A destination modified within `settle_seconds` (the live collector is writing it) is deferred, not merged.
With execute=True every merged or already-covered incoming version is recorded as `absorbed` in
`_transfer/received_index.json`, so later transfer passes skip that exact version instead of quarantining it again.
Raw `.jsonl.gz` captures never collide (timestamped names); anything else is left for manual review.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from aqe.ops.transfer import record_absorbed
from aqe.storage import sha256_file

TRANSFER_DIR = "_transfer"
# same keys as ingestion/collector.py (_upsert_parquet) and storage/bars.write_bars
DATASET_KEYS = (
    (re.compile(r"^normalized/flow_summaries/.+/\d{4}-\d{2}-\d{2}/\d{2}\.parquet$"), ["instrument_id", "interval_start"]),
    (re.compile(r"^normalized/mark_funding_estimates/.+/\d{4}-\d{2}-\d{2}/\d{2}\.parquet$"), ["instrument_id", "source_id"]),
    (re.compile(r"^normalized/oi_observations/.+/\d{4}-\d{2}-\d{2}\.parquet$"), ["instrument_id", "received_at"]),
    (re.compile(r"^normalized/bars/.+/(5m|5m_live)/\d{4}-\d{2}\.parquet$"), ["open_time"]),
)


def keys_for(rel: str) -> list[str] | None:
    return next((k for rx, k in DATASET_KEYS if rx.match(rel)), None)


def _key_frame(df: pd.DataFrame, keys: list[str]) -> pd.Series:
    """Comparable keys: time keys normalised to UTC nanoseconds (so ns vs us storage or another UTC offset of the same
    instant is the SAME key); other keys as strings."""
    cols = []
    for k in keys:
        c = df[k]
        if pd.api.types.is_datetime64_any_dtype(c):
            c = c.dt.tz_convert("UTC").astype("datetime64[ns, UTC]")
        cols.append(c.astype(str))
    return pd.Series(list(zip(*cols)), index=df.index) if keys else pd.Series(dtype=object)


def _naive_time_keys(df: pd.DataFrame, keys: list[str]) -> list[str]:
    return [k for k in keys if pd.api.types.is_datetime64_any_dtype(df[k]) and getattr(df[k].dt, "tz", None) is None]


def merge_one(dest: Path, incoming: Path, keys: list[str]) -> tuple[pd.DataFrame | None, dict[str, Any]]:
    """Rows of `incoming` whose key is absent in `dest` are added; a key present in both is a DUPLICATE and the
    destination row is kept (reported as identical / differing). Overlap in time is decided by key, not by file."""
    d, i = pd.read_parquet(dest), pd.read_parquet(incoming)
    if set(d.columns) != set(i.columns):
        return None, {"status": "manual", "reason": f"schemas differ: {sorted(set(d.columns) ^ set(i.columns))[:5]}"}
    if any(k not in d.columns for k in keys):
        return None, {"status": "manual", "reason": f"key columns {keys} missing"}
    naive = _naive_time_keys(d, keys) + _naive_time_keys(i, keys)
    if naive:
        return None, {"status": "manual", "reason": f"time key without time zone {sorted(set(naive))}: instants are ambiguous"}
    dk, ik = _key_frame(d, keys), _key_frame(i, keys)
    if dk.duplicated().any():
        return None, {"status": "manual", "reason": "destination has duplicate keys"}
    i = i.assign(_k=ik).drop_duplicates(subset="_k", keep="last")  # duplicates inside the incoming file itself
    dset = set(dk)
    coll = i[i["_k"].isin(dset)]
    dest_by_key = dict(zip(dk, d.astype(str).itertuples(index=False, name=None)))
    other = [c for c in d.columns]
    differing = [k for k, row in zip(coll["_k"], coll[other].astype(str).itertuples(index=False, name=None)) if dest_by_key[k] != row]
    new = i[~i["_k"].isin(dset)].drop(columns="_k")
    info = {"dest_rows": len(d), "incoming_rows": len(i), "duplicate_keys": len(coll), "duplicates_identical": len(coll) - len(differing),
            "duplicates_differing_kept_dest": len(differing), "differing_examples": [list(k) for k in differing[:3]]}
    if new.empty:
        return None, {"status": "covered", "rows_added": 0, **info}
    for c in d.columns:  # same logical type, possibly another unit (e.g. us vs ns): store with the destination dtype
        if new[c].dtype != d[c].dtype and pd.api.types.is_datetime64_any_dtype(d[c]):
            new[c] = new[c].dt.tz_convert("UTC").astype(d[c].dtype)
    merged = pd.concat([d, new[d.columns]], ignore_index=True)
    merged = merged.assign(_k=_key_frame(merged, keys)).sort_values("_k").drop(columns="_k").reset_index(drop=True)
    return merged, {"status": "mergeable", "rows_added": len(new), **info}


def _verify(merged_path: Path, dest_before: pd.DataFrame, keys: list[str], expected_rows: int) -> str | None:
    m = pd.read_parquet(merged_path)
    if len(m) != expected_rows:
        return f"row count {len(m)} != {expected_rows}"
    mk = _key_frame(m, keys)
    kept = m[mk.isin(set(_key_frame(dest_before, keys)))].sort_values(keys).reset_index(drop=True)
    before = dest_before.sort_values(keys).reset_index(drop=True)[kept.columns]
    if len(kept) != len(before) or not kept.astype(str).equals(before.astype(str)):
        return "destination rows changed"
    return None


def merge_conflicts(root: Path, transfer_id: str, *, execute: bool = False, settle_seconds: int = 900,
                    now: float | None = None) -> dict[str, Any]:
    root = Path(root)
    now = time.time() if now is None else now
    tx = root / TRANSFER_DIR / transfer_id
    cdir = tx / "conflicts"
    out: dict[str, Any] = {"transfer_id": transfer_id, "execute": execute, "merged": [], "would_merge": [], "covered": [],
                           "deferred": [], "manual": [], "failed": []}
    for inc in sorted(cdir.rglob("*")) if cdir.exists() else []:
        if not inc.is_file():
            continue
        rel = inc.relative_to(cdir).as_posix()
        keys = keys_for(rel)
        dest = root / rel
        if keys is None or not dest.is_file():
            out["manual"].append({"rel": rel, "reason": "not a keyed collector partition" if keys is None else "destination missing"})
            continue
        if now - dest.stat().st_mtime < settle_seconds:
            out["deferred"].append({"rel": rel, "reason": f"destination modified within {settle_seconds}s (being written)"})
            continue
        sha_before = sha256_file(dest)
        d_before = pd.read_parquet(dest)
        merged, info = merge_one(dest, inc, keys)
        info["rel"] = rel
        if info["status"] == "manual":
            out["manual"].append(info)
            continue
        if info["status"] == "covered":
            out["covered"].append(info)
            if execute:  # nothing to add: this incoming version is fully contained in the destination
                record_absorbed(root, rel, sha256_file(inc), transfer_id)
            continue
        if not execute:
            out["would_merge"].append(info)
            continue
        tmp = dest.with_name(dest.name + ".merge.tmp")
        merged.to_parquet(tmp, index=False)
        problem = _verify(tmp, d_before, keys, info["dest_rows"] + info["rows_added"])
        if problem or sha256_file(dest) != sha_before:
            tmp.unlink(missing_ok=True)
            out["failed"].append({"rel": rel, "reason": problem or "destination changed during merge"})
            continue
        backup = tx / "merge_backup" / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dest, backup)
        os.replace(tmp, dest)
        record_absorbed(root, rel, sha256_file(inc), transfer_id)
        with open(tx / "journal.jsonl", "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"rel": rel, "status": "merged", "rows_added": info["rows_added"], "sha256_before": sha_before,
                                 "sha256": sha256_file(dest), "backup": str(backup),
                                 "at": datetime.now(timezone.utc).isoformat()}) + "\n")
        out["merged"].append({**info, "backup": str(backup)})
    return out
