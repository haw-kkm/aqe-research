"""Human-readable + JSON analysis report (B10, prompt §7). Reports never change judgements."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aqe.contracts.context import ContextAssessment
from aqe.contracts.core import iso_utc
from aqe.contracts.data import FeatureSnapshot
from aqe.contracts.predictions import EnsemblePrediction, ModelPrediction
from aqe.contracts.risk import RiskDecision
from aqe.storage import atomic_write_json, atomic_write_text


@dataclass
class HorizonResult:
    horizon_seconds: int
    predictions: list[ModelPrediction]
    ensemble: EnsemblePrediction
    risk: RiskDecision


@dataclass
class AnalysisReport:
    analysis_id: str
    mode: str
    generated_at: Any
    decision_time: Any
    snapshot: FeatureSnapshot
    horizons: list[HorizonResult]
    context: ContextAssessment | None
    data_quality: dict[str, Any]
    unverified: list[str] = field(default_factory=list)
    config_hash: str = ""
    experiment_id: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "analysis_id": self.analysis_id,
            "mode": self.mode,
            "experiment_id": self.experiment_id,
            "config_hash": self.config_hash,
            "generated_at": iso_utc(self.generated_at),
            "decision_time": iso_utc(self.decision_time),
            "approval_required": True,
            "execution_enabled": False,
            "snapshot": self.snapshot.model_dump(mode="json"),
            "data_quality": self.data_quality,
            "context": self.context.model_dump(mode="json") if self.context else None,
            "horizons": [
                {
                    "horizon_seconds": h.horizon_seconds,
                    "predictions": [p.model_dump(mode="json") for p in h.predictions],
                    "ensemble": h.ensemble.model_dump(mode="json"),
                    "risk": h.risk.model_dump(mode="json"),
                }
                for h in self.horizons
            ],
            "unverified": self.unverified,
        }


def _fmt_probs(p) -> str:
    if p is None:
        return "—"
    return f"down {p.p_down:.3f} / neutral {p.p_neutral:.3f} / up {p.p_up:.3f}"


def _h(seconds: int) -> str:
    return f"{seconds // 3600}h" if seconds % 3600 == 0 else f"{seconds}s"


def render_markdown(r: AnalysisReport) -> str:
    s = r.snapshot
    L: list[str] = []
    L.append(f"# AQE 분석 보고 — {s.instrument.instrument_id} ({s.instrument.venue.value} {s.instrument.market_type.value})")
    L.append("")
    L.append("> 이 보고는 연구·수동 검토용이다. **approval_required=true, execution_enabled=false.** 주문 기능은 없다.")
    L.append("")
    L.append("## 1. 시점·입력")
    L.append(f"- 분석 ID: `{r.analysis_id}` · 모드: `{r.mode}` · 실험: `{r.experiment_id}`")
    L.append(f"- as_of: `{iso_utc(s.as_of)}` · 실제 생성 시각(generated_at): `{iso_utc(r.generated_at)}` · 판단 시각: `{iso_utc(r.decision_time)}`")
    L.append(f"- 입력 프로필: `{s.feature_profile.value}` · provenance: `{s.provenance.value}` · 스냅샷 품질: `{s.quality.value}`")
    if s.quality_reasons:
        L.append(f"- 품질 사유: {'; '.join(s.quality_reasons)}")
    for src, wm in s.source_watermarks.items():
        lag = (s.as_of - wm).total_seconds()
        L.append(f"- 소스 `{src}` 최신 가용 시각 `{iso_utc(wm)}` (as_of 대비 {lag:.0f}s 이전)")
    for k, v in r.data_quality.items():
        L.append(f"- {k}: {v}")
    if s.feature_profile.value == "ohlcv_v0":
        L.append("- 이 판단은 `ohlcv_v0`(완성 캔들) 기반이다. 호가(L2)·aggTrade CVD·OI 흐름 근거는 포함되지 않았다.")
    L.append("")
    L.append("## 2. 기간별 수치 확률 (1시간·24시간은 서로 다른 질문이며 하나로 투표하지 않는다)")
    for h in r.horizons:
        e = h.ensemble
        L.append(f"### 기간 {_h(h.horizon_seconds)}")
        for p in h.predictions:
            L.append(f"- {p.model_kind}: {_fmt_probs(p.probabilities)} · 상태 `{p.status.value}` · 준비도 `{p.readiness.value}` · artifact `{p.artifact_id}`" + (f" · 사유: {p.status_reason}" if p.status_reason else ""))
        L.append(f"- 결합({e.combo}): {_fmt_probs(e.probabilities)} · 상태 `{e.status.value}` · 메타 `{e.meta_artifact_id}` · 보정 `{e.calibrator_artifact_id}` · 보정 검증 `{e.calibration_validated}` · OOD `{e.ood_status.value}`")
        if e.disagreement:
            thr = "미검증" if e.disagreement_threshold is None else f"{e.disagreement_threshold:.3f}"
            L.append(f"- 모델 간 불일치: " + ", ".join(f"{k}={v:.3f}" for k, v in e.disagreement.items()) + f" (OOS 기준 임계 {thr})")
        if e.abstain_reasons:
            L.append(f"- 결합 보류 사유: {'; '.join(e.abstain_reasons)}")
        if e.extra.get("note"):
            L.append(f"- 참고: {e.extra['note']}")
        L.append("- 기대수익(EV): unknown — 수익 크기 모델이 학습되지 않았으므로 방향 확률로 EV를 만들지 않는다.")
        L.append("")
    L.append("## 3. 근거와 반대 근거")
    c = r.context
    if c is None:
        L.append("- 컨텍스트 평가 없음(MISSING). 근거 없음은 중립 의견이 아니다. 뉴스·일정·교차자산 근거가 이 판단에 반영되지 않았다.")
    else:
        L.append(f"- backend `{c.backend}` · 상태 `{c.status.value}` · 시점 범위 `{c.temporal_scope.value}` · upstream `{c.upstream_ref}` · 모델 `{c.model_id}` (응답 모델 `{c.response_model}`)")
        if c.status_reasons:
            L.append(f"- 상태 사유: {'; '.join(c.status_reasons)}")
        if c.contextual_rating:
            L.append(f"- contextual_rating(참고용 등급, 확률·주문 아님): `{c.contextual_rating}`")
        for cl in c.claims:
            L.append(f"- [주장/{cl.kind.value}/{cl.direction.value}] {cl.text} (근거 {', '.join(cl.evidence_ids) or '없음'})" + (f" · 반증 조건: {cl.falsifier}" if cl.falsifier else ""))
        for cl in c.counterclaims:
            L.append(f"- [반대/{cl.kind.value}/{cl.direction.value}] {cl.text} (근거 {', '.join(cl.evidence_ids) or '없음'})")
        for f in c.event_flags:
            L.append(f"- [이벤트 {f.severity}] {f.name} (근거 {', '.join(f.evidence_ids)})")
        missing = [k for k, v in c.coverage.items() if v != "AVAILABLE"]
        if missing:
            L.append(f"- 누락·비가용 소스: {', '.join(f'{k}={c.coverage[k]}' for k in missing)}")
        L.append(f"- 사용량: 호출 {c.usage.llm_calls}, 토큰 {c.usage.prompt_tokens}/{c.usage.completion_tokens}, 비용 {c.usage.cost_estimate_usd if c.usage.cost_estimate_usd is not None else 'unknown'} ({c.usage.cost_basis})")
    L.append("")
    L.append("## 4. 위험 판정 (독립 규칙, LLM이 변경 불가)")
    for h in r.horizons:
        d = h.risk
        L.append(f"### 기간 {_h(h.horizon_seconds)} — 최종 표시 `{d.disposition.value}` · 위험 결과 `{d.risk_outcome.value}` · 포트폴리오 `{d.portfolio_status}`")
        L.append(f"- 규칙셋 `{d.ruleset_id}` (hash `{d.ruleset_hash[:12]}`) · 유효기간 `{iso_utc(d.valid_until)}`")
        L.append("| 순서 | 규칙 | 결과 | 사유 | 복구 조건 |")
        L.append("|---|---|---|---|---|")
        for rr in d.rules:
            L.append(f"| {rr.order} | {rr.rule_id} | {rr.outcome.value} | {rr.reason} | {rr.recovery_condition or ''} |")
        L.append(f"- approval_required={str(d.approval_required).lower()}, execution_enabled={str(d.execution_enabled).lower()}")
        L.append("")
    L.append("## 5. 아직 검증하지 못한 부분")
    for u in r.unverified:
        L.append(f"- {u}")
    L.append("")
    return "\n".join(L)


def write_report(r: AnalysisReport, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    jp = out_dir / f"{r.analysis_id}.json"
    mp = out_dir / f"{r.analysis_id}.md"
    atomic_write_json(jp, r.to_json())
    atomic_write_text(mp, render_markdown(r))
    return jp, mp
