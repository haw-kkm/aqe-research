"""Copy a consistent, read-only snapshot of finished files from the production data/artifact roots into a dev root.

Never writes to the source roots. Skips files that may still be written by the running collector:
hourly flow/mark/oi partitions of the current and previous UTC hour, raw logs, status/checkpoint files.
Usage:  python scripts/make_dev_snapshot.py --src-data E:/aqe-v2/data --src-art E:/aqe-v2/artifacts
                                           --dst-data E:/aqe-v2-dev/data --dst-art E:/aqe-v2-dev/artifacts
                                           --experiment exp_btcusdt_ohlcv_v0_001 --until-hour 2026-11-11T13
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def copy(src: Path, dst: Path, done: list[str]) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    done.append(str(dst))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-data", required=True, type=Path)
    ap.add_argument("--src-art", required=True, type=Path)
    ap.add_argument("--dst-data", required=True, type=Path)
    ap.add_argument("--dst-art", required=True, type=Path)
    ap.add_argument("--experiment", required=True)
    ap.add_argument("--until-hour", required=True, help="last COMPLETED UTC hour to include, e.g. 2026-11-11T13")
    a = ap.parse_args()
    for d in (a.dst_data, a.dst_art):
        if d.resolve() in (a.src_data.resolve(), a.src_art.resolve()):
            raise SystemExit("destination must differ from the source roots")
    day, hour = a.until_hour.split("T")
    done: list[str] = []
    norm = a.src_data / "normalized"
    # REST-only partitions (not written by the collector)
    for kind in ("bars", "funding", "oi_hist"):
        for f in (norm / kind).rglob("*.parquet"):
            if "5m_live" in f.parts:
                continue
            copy(f, a.dst_data / f.relative_to(a.src_data), done)
    # collector hourly partitions: only completed hours up to --until-hour
    for kind in ("flow_summaries", "mark_funding_estimates"):
        for f in (norm / kind).rglob("*.parquet"):
            d, h = f.parent.name, f.stem
            if (d, h) <= (day, hour):
                copy(f, a.dst_data / f.relative_to(a.src_data), done)
    for f in (a.src_data / "manifests").glob("*.json"):
        copy(f, a.dst_data / f.relative_to(a.src_data), done)
    # registered artifacts only (+ registry); nothing else from the artifact root
    exp_src, exp_dst = a.src_art / a.experiment, a.dst_art / a.experiment
    reg = json.loads((exp_src / "registry.json").read_text(encoding="utf-8"))
    copy(exp_src / "registry.json", exp_dst / "registry.json", done)
    ids = {aid for h in reg["horizons"].values() for aid in h.values()}
    for aid in sorted(ids):
        for f in (exp_src / "artifacts" / aid).iterdir():
            copy(f, exp_dst / "artifacts" / aid / f.name, done)
    print(json.dumps({"files_copied": len(done), "artifacts": sorted(ids)}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
