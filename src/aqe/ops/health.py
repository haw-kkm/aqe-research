"""Collector healthcheck for a service timer (Oracle) or a manual look (Windows). Read-only except `record=True`,
which appends one line to `{data_root}/ops/health.jsonl`.

Levels: OK / WARN / CRITICAL (exit codes 0 / 1 / 2). Checks: status-file freshness and final flag, per-feed message
lag, order-book state, disk free, exchange clock offset, age of the latest flow-summary partition (data lag), and the
last 24h of the durable quality journal (book desyncs = unrecoverable depth gaps, disconnects, restarts).
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LEVELS = {"OK": 0, "WARN": 1, "CRITICAL": 2}


def _age(iso: str | None, now: float) -> float | None:
    if not iso:
        return None
    return max(0.0, now - datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def collector_health(data_root: Path, symbol: str, *, venue: str = "binance", market: str = "usdm_perpetual",
                     stale_seconds: float = 120, lag_seconds: float = 120, min_free_gb: float = 20.0,
                     critical_free_gb: float = 5.0, max_clock_offset_ms: float = 1000.0, flow_lag_seconds: float = 600,
                     now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    root = Path(data_root)
    issues: list[tuple[str, str]] = []
    out: dict[str, Any] = {"checked_at": datetime.fromtimestamp(now, timezone.utc).isoformat(), "data_root": str(root), "symbol": symbol}
    status_p = root / "status" / f"collector_{symbol}.json"
    if not status_p.exists():
        issues.append(("CRITICAL", "no collector status file (collector never started on this data_root)"))
    else:
        st = json.loads(status_p.read_text(encoding="utf-8"))
        age = now - status_p.stat().st_mtime
        out.update(run_id=st.get("run_id"), status_age_seconds=round(age, 1), book_state=st.get("book", {}).get("state"),
                   clock_offset_ms=st.get("clock", {}).get("offset_ms_server_minus_local"))
        if st.get("final"):
            issues.append(("CRITICAL", "collector stopped (final status)"))
        elif age > stale_seconds:
            issues.append(("CRITICAL", f"status file stale ({age:.0f}s > {stale_seconds:.0f}s): collector hung or dead"))
        lags = {}
        for g in ("public", "market"):
            last = (st.get("connections", {}).get(g) or {}).get("last_message_at")
            lags[g] = None if last is None else round(_age(last, now + (out.get("clock_offset_ms") or 0) / 1000), 1)
            if lags[g] is None or lags[g] > lag_seconds:
                issues.append(("CRITICAL" if lags[g] is None else "WARN", f"{g} feed lag {lags[g]}s"))
        out["feed_lag_seconds"] = lags
        if out["book_state"] != "SYNCED":
            issues.append(("WARN", f"order book {out['book_state']} (depth gap until resynchronised; see quality journal)"))
        off = out.get("clock_offset_ms")
        if off is None or abs(off) > max_clock_offset_ms:
            issues.append(("WARN", f"exchange clock offset {off} ms (limit {max_clock_offset_ms:.0f})"))
    try:
        free = shutil.disk_usage(root).free / 2**30
        out["disk_free_gb"] = round(free, 2)
        if free < critical_free_gb:
            issues.append(("CRITICAL", f"disk free {free:.1f} GiB < {critical_free_gb} GiB"))
        elif free < min_free_gb:
            issues.append(("WARN", f"disk free {free:.1f} GiB < {min_free_gb} GiB"))
    except OSError as exc:
        issues.append(("CRITICAL", f"data_root not accessible: {exc}"))
    flow = sorted((root / "normalized" / "flow_summaries" / venue / market / symbol).glob("*/*.parquet"))
    if flow:
        lag = now - flow[-1].stat().st_mtime
        out["flow_partition_age_seconds"] = round(lag, 1)
        if lag > flow_lag_seconds:
            issues.append(("WARN", f"newest flow summary partition written {lag:.0f}s ago (> {flow_lag_seconds:.0f}s)"))
    else:
        issues.append(("WARN", "no flow summary partition yet"))
    qdir = root / "quality" / venue / market / symbol
    since = now - 86400
    counts: dict[str, int] = {}
    for day in {datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d") for t in (since, now)}:
        p = qdir / f"{day}.jsonl"
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    counts["UNREADABLE_LINE"] = counts.get("UNREADABLE_LINE", 0) + 1
                    continue
                if _age(ev.get("at"), now) is not None and _age(ev["at"], now) <= 86400:
                    counts[ev.get("kind", "?")] = counts.get(ev.get("kind", "?"), 0) + 1
    out["quality_last_24h"] = dict(sorted(counts.items()))
    desync = sum(v for k, v in counts.items() if k.startswith("BOOK_") and "DESYNC" in k)
    if counts.get("RESTART", 0) > 5:
        issues.append(("WARN", f"{counts['RESTART']} collector restarts in 24h"))
    if desync > 20:
        issues.append(("WARN", f"{desync} order-book desync events in 24h"))
    level = max((lvl for lvl, _ in issues), key=lambda x: LEVELS[x], default="OK")
    out["level"] = level
    out["issues"] = [{"level": lvl, "detail": d} for lvl, d in issues]
    return out


def record_health(data_root: Path, result: dict[str, Any]) -> Path:
    p = Path(data_root) / "ops" / "health.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(result, ensure_ascii=False) + "\n")
    return p


_ = timedelta
