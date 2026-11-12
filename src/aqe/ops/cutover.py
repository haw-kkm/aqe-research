"""Collector cutover gate: Windows PC -> Oracle (docs/CLOUD_DEPLOYMENT.md §수집기 전환).

Runs both collectors in parallel on SEPARATE data roots, then compares the overlap window minute by minute from the
stored flow summaries: coverage (minutes with a row), order-book-valid minutes and trade counts. `readiness` turns
that plus the cloud health history into GO / NO-GO with reasons. `approve` only RECORDS an explicit operator decision
(`cutover_approved.json`); nothing here stops, starts or reconfigures any collector.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


def _flow(root: Path, symbol: str, start: pd.Timestamp, end: pd.Timestamp, venue: str, market: str) -> pd.DataFrame:
    d = Path(root) / "normalized" / "flow_summaries" / venue / market / symbol
    frames = []
    for h in pd.date_range(start.floor("h"), end, freq="h", inclusive="left"):
        p = d / h.strftime("%Y-%m-%d") / f"{h:%H}.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p, columns=["interval_start", "book_valid", "n_trades"]))
    if not frames:
        return pd.DataFrame(columns=["interval_start", "book_valid", "n_trades"])
    df = pd.concat(frames, ignore_index=True)
    df["interval_start"] = pd.to_datetime(df["interval_start"], utc=True)
    df = df[(df["interval_start"] >= start) & (df["interval_start"] < end)]
    return df.drop_duplicates("interval_start", keep="last")


def compare_collectors(root_ref: Path, root_new: Path, symbol: str, start, end, *, venue: str = "binance",
                       market: str = "usdm_perpetual") -> dict[str, Any]:
    start, end = pd.Timestamp(start, tz="UTC") if pd.Timestamp(start).tzinfo is None else pd.Timestamp(start), \
        pd.Timestamp(end, tz="UTC") if pd.Timestamp(end).tzinfo is None else pd.Timestamp(end)
    ref, new = _flow(root_ref, symbol, start, end, venue, market), _flow(root_new, symbol, start, end, venue, market)
    expected = int((end - start) / pd.Timedelta(minutes=1))
    both = ref.merge(new, on="interval_start", suffixes=("_ref", "_new"))
    tr_ref, tr_new = float(both["n_trades_ref"].sum()), float(both["n_trades_new"].sum())
    hours = []
    for h, g in both.groupby(both["interval_start"].dt.floor("h")):
        a, b = float(g["n_trades_ref"].sum()), float(g["n_trades_new"].sum())
        hours.append({"hour": h.isoformat(), "minutes_both": len(g), "trades_ref": a, "trades_new": b,
                      "trades_rel_diff": None if a == 0 else round((b - a) / a, 5)})
    return {"symbol": symbol, "start": start.isoformat(), "end": end.isoformat(), "expected_minutes": expected,
            "minutes_ref": len(ref), "minutes_new": len(new), "minutes_both": len(both),
            "coverage_ref": round(len(ref) / expected, 5) if expected else None,
            "coverage_new": round(len(new) / expected, 5) if expected else None,
            "book_valid_new": round(float(new["book_valid"].astype(bool).mean()), 5) if len(new) else None,
            "trades_rel_diff_overlap": None if tr_ref == 0 else round((tr_new - tr_ref) / tr_ref, 5),
            "hours": hours}


def readiness(comparison: dict[str, Any], health_records: list[dict[str, Any]], *, min_hours: float = 24,
              min_coverage: float = 0.99, max_trade_diff: float = 0.01, min_book_valid: float = 0.98) -> dict[str, Any]:
    reasons = []
    span_h = (pd.Timestamp(comparison["end"]) - pd.Timestamp(comparison["start"])) / pd.Timedelta(hours=1)
    if span_h < min_hours:
        reasons.append(f"overlap window {span_h:.1f} h < {min_hours} h")
    if (comparison["coverage_new"] or 0) < min_coverage:
        reasons.append(f"cloud coverage {comparison['coverage_new']} < {min_coverage}")
    if (comparison["book_valid_new"] or 0) < min_book_valid:
        reasons.append(f"cloud order book valid {comparison['book_valid_new']} < {min_book_valid}")
    d = comparison["trades_rel_diff_overlap"]
    if d is None or abs(d) > max_trade_diff:
        reasons.append(f"trade-count difference vs PC {d} (limit ±{max_trade_diff})")
    window = [r for r in health_records if pd.Timestamp(comparison["start"]) <= pd.Timestamp(r["checked_at"]) <= pd.Timestamp(comparison["end"])]
    if not window:
        reasons.append("no cloud health records in the window")
    crit = [r for r in window if r.get("level") == "CRITICAL"]
    if crit:
        reasons.append(f"{len(crit)} CRITICAL cloud health records in the window")
    return {"ready": not reasons, "verdict": "GO (operator approval still required)" if not reasons else "NO-GO", "reasons": reasons,
            "criteria": {"min_hours": min_hours, "min_coverage": min_coverage, "max_trade_diff": max_trade_diff,
                         "min_book_valid": min_book_valid}}


def write_report(out: Path, comparison: dict[str, Any], ready: dict[str, Any]) -> dict[str, Any]:
    rid = datetime.now(timezone.utc).strftime("cutover-%Y%m%dT%H%M%SZ")
    rep = {"report_id": rid, "created_at": datetime.now(timezone.utc).isoformat(), "comparison": comparison, "readiness": ready,
           "data_migration_complete": None, "note": "the PC collector is stopped manually by the operator, never by this tool"}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    return rep


def approve(report_path: Path, approve_id: str | None, migration_receipt: Path) -> dict[str, Any]:
    """Record the operator's decision. Requires a GO report, the matching id, and a COMPLETE transfer receipt
    (Windows history migrated). Writes cutover_approved.json next to the report. Stops nothing."""
    rep = json.loads(Path(report_path).read_text(encoding="utf-8"))
    if approve_id != rep["report_id"]:
        raise PermissionError(f"approval must name the report: --approve {rep['report_id']}")
    if not rep["readiness"]["ready"]:
        raise PermissionError("report is NO-GO: " + "; ".join(rep["readiness"]["reasons"]))
    receipt = json.loads(Path(migration_receipt).read_text(encoding="utf-8"))
    if not receipt.get("complete"):
        raise PermissionError("data migration receipt is not complete")
    rec = {"approved_report": rep["report_id"], "approved_at": datetime.now(timezone.utc).isoformat(),
           "migration_transfer_id": receipt.get("transfer_id"),
           "next_manual_step": "stop the Windows collector window with Ctrl+C (it flushes), keep its data as a backup"}
    p = Path(report_path).with_name("cutover_approved.json")
    if p.exists():
        raise FileExistsError(f"{p} already exists")
    p.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return rec
