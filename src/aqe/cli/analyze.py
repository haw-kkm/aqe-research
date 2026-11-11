"""`aqe analyze` / `aqe report` (B12): latest valid inputs + artifacts -> ensemble -> risk -> report.

Missing artifacts/context never become neutral values: they yield explicit ABSTAIN/MISSING.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from aqe.config import LocalConfig, load_experiment_config
from aqe.context.store import latest_assessment
from aqe.contracts.core import QualityStatus, RuntimeMode, iso_utc, utc_now
from aqe.contracts.data import FeatureSnapshot
from aqe.contracts.risk import PortfolioSnapshot
from aqe.ensemble.online import predict_horizon
from aqe.features.ohlcv_v0 import MAX_LOOKBACK_BARS, compute_ohlcv_v0
from aqe.features.snapshot import build_snapshot
from aqe.reporting.report import AnalysisReport, HorizonResult, write_report
from aqe.risk.engine import RiskInputs, evaluate_risk, load_ruleset
from aqe.storage import new_id, read_json
from aqe.storage.bars import bars_as_of, load_fixture_bars, read_bars, to_grid
from aqe.storage.runs import EXIT_NOT_READY, EXIT_OK, RunRecord


def _feature_frame(bars: pd.DataFrame, as_of: datetime, interval: int) -> pd.DataFrame:
    sel = bars_as_of(bars, as_of).tail(MAX_LOOKBACK_BARS + 600)
    if sel.empty:
        return pd.DataFrame()
    return compute_ohlcv_v0(to_grid(sel, interval), interval)


def run_analyze(cfg: LocalConfig, cfg_hash: str, rec: RunRecord, as_of_override: datetime | None = None) -> int:
    exp, _ = load_experiment_config(cfg.analysis.experiment_config)
    inst = cfg.instruments[0]
    interval = exp.bar_interval_seconds
    unverified: list[str] = []
    if cfg.fixture_bars is not None:
        bars = load_fixture_bars(cfg.fixture_bars, inst, cfg.backfill.assumed_latency_seconds)
        unverified.append("입력은 SYNTHETIC_FIXTURE(기능 시험 전용)이며 시장 의미가 없다.")
    else:
        start = (as_of_override or utc_now()) - timedelta(days=12)
        bars = read_bars(cfg.paths.data_root, inst, "5m", start=start)
    mode = cfg.mode
    if as_of_override is not None:
        as_of = as_of_override
    elif mode == RuntimeMode.REPLAY:
        if bars.empty:
            as_of = utc_now()
        else:
            last_close = bars.loc[bars["is_closed"], "close_time"].max()
            as_of = (last_close + pd.Timedelta(seconds=cfg.analysis.decision_delay_seconds)).to_pydatetime()
    else:
        as_of = utc_now()
    decision_time = as_of + timedelta(seconds=cfg.analysis.decision_delay_seconds) if mode == RuntimeMode.REPLAY else utc_now()
    if mode != RuntimeMode.REPLAY and decision_time < as_of:
        decision_time = as_of
    snapshot, _ = build_snapshot(bars, inst, as_of, interval)
    ff = _feature_frame(bars, as_of, interval)

    clock_skew = None
    if mode != RuntimeMode.REPLAY:
        try:
            from aqe.ingestion.binance_rest import BinanceRest

            rest = BinanceRest(cfg.binance)
            clock_skew = rest.measure_clock().local_minus_server_ms
            rest.close()
        except Exception as exc:  # noqa: BLE001
            rec.notes.append(f"clock measurement failed: {type(exc).__name__}")
    avail = bars_as_of(bars, as_of)
    qv = float(avail.tail(288)["quote_volume"].sum()) if len(avail) >= 288 else None
    bar_quality = QualityStatus.OK if snapshot.quality in (QualityStatus.OK, QualityStatus.DEGRADED) else snapshot.quality

    context = latest_assessment(cfg.paths.data_root, inst, as_of, decision_time,
                                replay=(mode == RuntimeMode.REPLAY), backend=cfg.context.backend)
    debate_summary = None
    if context is not None and context.backend == "aqe_debate":
        from aqe.context.debate import debate_summary as _ds
        from aqe.storage.metadb import sqlite_path

        debate_summary = _ds(cfg.paths.data_root, sqlite_path(cfg), inst, context.assessment_id, as_of)
    portfolio = None
    if cfg.analysis.portfolio_snapshot is not None and cfg.analysis.portfolio_snapshot.exists():
        portfolio = PortfolioSnapshot.model_validate(read_json(cfg.analysis.portfolio_snapshot))
    rs, rs_hash = load_ruleset(cfg.risk_ruleset)
    analysis_id = new_id("analysis")
    horizons: list[HorizonResult] = []
    last_price = float(avail["close"].iloc[-1]) if not avail.empty else None
    for h in exp.horizons_seconds:
        preds, ens, metas = predict_horizon(
            artifact_root=cfg.paths.artifact_root, experiment_id=exp.experiment_id, snapshot=snapshot,
            feature_frame=ff, horizon_seconds=h, mode=mode, interval_seconds=interval, context=context,
        )
        used_metas = metas  # base + meta + calibrator: all checked by R1 (identity, label cutoff)
        if mode != RuntimeMode.REPLAY:
            # live decision instant: after the predictions were actually generated (B11: no retroactive use)
            decision_time = max(utc_now(), as_of)
        risk = evaluate_risk(RiskInputs(
            analysis_id=analysis_id, mode=mode, decision_time=decision_time, horizon_seconds=h, snapshot=snapshot,
            predictions=[p for p in preds if p.status.value == "OK"], ensemble=ens,
            artifact_meta=used_metas, context=context,
            clock_skew_ms=clock_skew, source_quality={"bars": bar_quality}, quote_volume_24h=qv,
            cost_assumptions={"taker_fee_bps": exp.evaluation.taker_fee_bps, "slippage_bps": exp.evaluation.slippage_bps},
            portfolio=portfolio, last_price=last_price,
        ), rs, rs_hash)
        horizons.append(HorizonResult(h, preds, ens, risk))
    if context is None:
        unverified.append("컨텍스트(뉴스·일정·교차자산) 근거가 없다: context worker 미실행 또는 유효한 평가 없음.")
    unverified.append("flow_v1(L2·aggTrade CVD·OI·funding 흐름) 모델은 아직 학습되지 않았다.")
    unverified.append("전향(shadow) 성능 검증은 아직 수행되지 않았다.")
    if clock_skew is not None and abs(clock_skew) > cfg.clock.max_abs_skew_ms:
        unverified.append(f"시스템 시계가 거래소 대비 {clock_skew:.0f}ms 어긋나 있다(동기화 필요).")
    dq = {
        "bars_used": int(len(avail)),
        "last_bar_close": iso_utc(avail["close_time"].iloc[-1].to_pydatetime()) if not avail.empty else None,
        "quote_volume_24h": qv,
        "clock_local_minus_server_ms": None if clock_skew is None else round(clock_skew, 1),
    }
    report = AnalysisReport(
        analysis_id=analysis_id, mode=mode.value, generated_at=utc_now(), decision_time=decision_time,
        snapshot=snapshot, horizons=horizons, context=context, data_quality=dq, unverified=unverified,
        config_hash=cfg_hash, experiment_id=exp.experiment_id, debate=debate_summary,
    )
    out_dir = cfg.paths.data_root / "reports"
    jp, mp = write_report(report, out_dir)
    rec.outputs.update({"report_json": str(jp), "report_md": str(mp), "analysis_id": analysis_id,
                        "dispositions": {str(h.horizon_seconds): h.risk.disposition.value for h in horizons}})
    all_abstain = all(h.ensemble.status.value == "ABSTAIN" for h in horizons)
    return EXIT_NOT_READY if all_abstain else EXIT_OK


def run_report(cfg: LocalConfig, rec: RunRecord, analysis_id: str | None) -> int:
    """Re-render a stored analysis JSON (does not recompute or alter judgements)."""
    from aqe.contracts.context import ContextAssessment
    from aqe.contracts.predictions import EnsemblePrediction, ModelPrediction
    from aqe.contracts.risk import RiskDecision
    from aqe.reporting.report import render_markdown
    from aqe.storage import atomic_write_text

    out_dir = cfg.paths.data_root / "reports"
    if analysis_id:
        jp = out_dir / f"{analysis_id}.json"
    else:
        cands = sorted(out_dir.glob("analysis-*.json"))
        if not cands:
            rec.notes.append("no stored analysis; run `aqe analyze` first")
            return EXIT_NOT_READY
        jp = cands[-1]
    d = read_json(jp)
    rep = AnalysisReport(
        analysis_id=d["analysis_id"], mode=d["mode"], generated_at=datetime.fromisoformat(d["generated_at"]),
        decision_time=datetime.fromisoformat(d["decision_time"]), snapshot=FeatureSnapshot.model_validate(d["snapshot"]),
        horizons=[HorizonResult(h["horizon_seconds"], [ModelPrediction.model_validate(p) for p in h["predictions"]],
                                EnsemblePrediction.model_validate(h["ensemble"]), RiskDecision.model_validate(h["risk"]))
                  for h in d["horizons"]],
        context=ContextAssessment.model_validate(d["context"]) if d.get("context") else None,
        data_quality=d["data_quality"], unverified=d.get("unverified", []), config_hash=d.get("config_hash", ""),
        experiment_id=d.get("experiment_id", ""), debate=d.get("debate"),
    )
    mp = Path(str(jp)[:-5] + ".md")
    atomic_write_text(mp, render_markdown(rep))
    rec.outputs.update({"report_json": str(jp), "report_md": str(mp)})
    print(render_markdown(rep))
    return EXIT_OK
