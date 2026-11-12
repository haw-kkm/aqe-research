"""Human-readable + JSON analysis report (B10, prompt §7). Reports never change judgements."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aqe.contracts.context import ContextAssessment
from aqe.contracts.core import iso_utc
from aqe.contracts.data import FeatureSnapshot
from aqe.contracts.plan import TradePlan
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
    debate: dict[str, Any] | None = None  # debate summary (aqe_debate backend); display only
    trade_plans: list[TradePlan] | None = None  # docs/TRADE_PLAN.md; None = feature disabled (report unchanged)
    planning: dict[str, Any] | None = None

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
            "debate": self.debate,
            **({"planning": self.planning, "trade_plans": [p.model_dump(mode="json") for p in self.trade_plans]}
               if self.trade_plans is not None else {}),
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
    if r.debate:
        L.extend(render_debate(r.debate))
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
    if r.trade_plans is not None:
        L.extend(render_trade_plans(r.trade_plans, r.planning or {}))
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


def render_debate(d: dict[str, Any]) -> list[str]:
    """Hypotheses from the analysis/debate layer. LLM wording only — numeric probabilities and risk results above are unchanged."""
    L = ["", "## 3b. 분석·토론 가설 (연구용; 수치 확률·위험 판정을 바꾸지 않음)",
         f"- 토론 `{d.get('debate_id')}` · 상태 `{d.get('status')}` · LLM 호출 {d.get('llm_calls')}/{d.get('max_llm_calls')} · "
         f"제공자 `{d.get('provider')}` 모델 `{d.get('model_id')}` · 이력 반영 `{d.get('applied_to_ledger')}` {d.get('ledger_note') or ''}"]
    for role, st in (d.get("roles") or {}).items():
        note = (d.get("role_notes") or {}).get(role, "")
        L.append(f"- 역할 {role}: `{st}`" + (f" — {note}" if st != "ACTIVE" else ""))
    if d.get("critique_issues") is not None:
        L.append(f"- 비판 지적 {d['critique_issues']}건 (MAJOR {d.get('critique_major', 0)}), 재검토 역할: {d.get('revision_role') or '없음'}")
    for f in (d.get("check_failures") or [])[:5]:
        L.append(f"- 코드 검사 실패: {f}")
    for hz in d.get("horizons") or []:
        h = hz["horizon_seconds"]
        L.append(f"### 기간 {h // 3600}h")
        if hz.get("primary") is None:
            L.append(f"- 주 가설 없음(ABSTAIN): {hz.get('abstain_reason')}")
        for label, hyp in [("주 가설", hz.get("primary")), *[("반대 가설", o) for o in hz.get("opposing") or []]]:
            if not hyp:
                continue
            state = (d.get("states") or {}).get(hyp.get("canonical_id") or "", "?")
            L.append(f"- {label} `{hyp.get('canonical_id')}` [{hyp['thesis_type']}/{hyp['direction']}, 상태 {state}, "
                     f"정성적 확신 {hyp.get('qualitative_confidence') or '—'}]: {hyp['statement']} (근거 {', '.join(hyp['evidence_ids'])})")
            L.append(f"  - 철회 조건: {'; '.join(hyp['invalidation_conditions'])}")
            if hyp.get("review_conditions"):
                L.append(f"  - 재검토 조건: {'; '.join(hyp['review_conditions'])}")
        for u in hz.get("unresolved") or []:
            L.append(f"- 미해결: {u}")
        if hz.get("changed_since_previous"):
            L.append(f"- 이전 대비 변화: {hz['changed_since_previous']}")
    for pdc in d.get("prior_decisions") or []:
        L.append(f"- 이전 가설 `{pdc['hypothesis_id']}` → {pdc['decision']}: {pdc['reason']}")
    for ps in d.get("plan_selections") or []:  # trade-plan selection verdicts (section 4b shows the plan)
        L.append(f"- 계획 후보 선택 {_h(ps['horizon_seconds'])}: `{ps.get('candidate_id') or '관망(null)'}` · `{ps['status']}` — {ps.get('detail', '')}")
    for lim in d.get("limitations") or []:
        L.append(f"- 한계: {lim}")
    L.append("")
    return L


_SIDE_KO = {"LONG": "롱", "SHORT": "숏"}
_DECISION_KO = {"LONG": "롱 검토", "SHORT": "숏 검토", "WAIT": "관망"}
_COND_KO = {"MET": "충족", "NOT_MET": "미충족", "NOT_EVALUATED": "미평가"}


def _v(x) -> str:
    return "미확보" if x is None or x == "" else str(x)


def _candidate_lines(c, chosen: bool) -> list[str]:
    tag = "선택됨" if chosen else "미선택(기술적 후보)"
    L = [f"- [{_SIDE_KO[c.side.value]} 후보 `{c.candidate_id}` · {tag} · 선택 가능 {'예' if c.selectable else '아니오(검사 실패)'}]",
         f"  - 진입 구간: {c.entry_low} ~ {c.entry_high} (보수적 진입가 {c.conservative_entry}) · 산출: {c.entry_basis}",
         "  - 진입 조건: " + "; ".join(f"{e.text} → {_COND_KO[e.status.value]} (관측: {_v(e.observed)})" for e in c.entry_conditions),
         f"  - 손절: {c.stop} · 산출: {c.stop_basis}"]
    for t in c.take_profits:
        rr = "미확보(비용 정보 없음)" if t.net_rr is None else f"{t.net_rr}"
        rw = "미확보" if t.net_reward_fraction is None else f"{t.net_reward_fraction * 100:+.3f}%"
        L.append(f"  - 익절 {t.label}: {t.price} · 비용 후 보상 {rw} · 비용 후 RR {rr} · 산출: {t.basis}")
    loss = "미확보" if c.net_loss_fraction is None else f"-{c.net_loss_fraction * 100:.3f}%"
    L.append(f"  - 비용 후 손실폭(손절 시, 보수적 진입가 기준): {loss}")
    L.append("  - 코드 검사: " + "; ".join(f"{k.name} `{k.outcome.value}`" + (f" ({k.detail})" if k.outcome.value != "PASS" else "")
                                       for k in c.checks))
    L.append(f"  - 무효화 조건: {'; '.join(c.invalidation)}")
    L.append(f"  - 입력 시각: as_of `{iso_utc(c.as_of)}` · 입력 가용 `{iso_utc(c.available_at)}` · 만료 `{iso_utc(c.valid_until)}`")
    return L


def render_trade_plans(plans: list[TradePlan], meta: dict[str, Any]) -> list[str]:
    """Entry/stop/take-profit review proposals. Rule-based candidates, LLM selection and the final decision are shown
    as separate sources; the decision cannot be stronger than the risk disposition."""
    costs = meta.get("costs")
    cost_txt = (f"수수료 {costs['taker_fee_bps']}bps + 슬리피지 {costs['slippage_bps']}bps (편도, 진입·청산 양쪽), funding 미포함 — {costs['source']}"
                if costs else "미확보")
    L = ["## 4b. 진입·손절·익절 검토안 (규칙 계산 후보 · 수동 검토용 · 주문 아님)",
         "> 가격 후보는 완료된 봉으로 코드가 계산한 **미검증 기술적 후보**다(학습된 가격 예측·검증된 수익 목표가 아니다). "
         "ML 방향 확률(§2), 규칙 기반 가격 후보(이 절), LLM 해석(§3b)은 서로 다른 출처다. RR은 비용 후 보상/손실의 가격 비율이며 "
         "성공 확률·기대수익이 아니다. 진입 조건 충족은 체결을 뜻하지 않는다. approval_required=true, execution_enabled=false.",
         f"- 알고리즘 `{_v(meta.get('algorithm_version'))}` (설정 hash `{str(meta.get('config_hash') or '')[:12]}`) · 입력: {_v(meta.get('input'))}",
         f"- 비용 가정: {cost_txt} · tick: {_v(meta.get('tick'))}",
         f"- 선택 출처: `{_v(meta.get('selection_source'))}` — {_v(meta.get('selection_note'))}", ""]
    for p in plans:
        sides = {c.side.value for c in p.candidates}
        tech = " / ".join(f"기술적 {_SIDE_KO[s]} 후보 {'있음' if s in sides else '없음'}" for s in ("LONG", "SHORT"))
        prop = _SIDE_KO[p.proposal_side.value] if p.proposal_side else "없음"
        L.append(f"### 기간 {_h(p.horizon_seconds)} — 결론: **{_DECISION_KO[p.decision.value]}** "
                 f"(분석 후보 방향: {prop} / 최종 판단: {_DECISION_KO[p.decision.value]} / 위험 판정 `{p.risk_disposition.value}`)")
        L.append(f"- {tech} · 계획 상태 `{p.status.value}` · 스냅샷 `{_v(p.snapshot_id)}` (as_of `{iso_utc(p.as_of) if p.as_of else '미확보'}`)")
        L.append(f"- 선택: 출처 `{p.selection_source.value}` · 상태 `{p.selection_status.value}` · 선택 ID `{_v(p.selected_candidate_id)}`"
                 + (f" · 사유(LLM 텍스트, 가격 출처 아님): {p.selection_reason}" if p.selection_reason else ""))
        if p.evidence_ids or p.counter_evidence_ids:
            L.append(f"- 근거: {', '.join(p.evidence_ids) or '없음'} · 반대 근거: {', '.join(p.counter_evidence_ids) or '없음'}")
        if p.hypothesis_ids:
            L.append(f"- 연결 가설: {', '.join(p.hypothesis_ids)}")
        for c in p.candidates:
            L.extend(_candidate_lines(c, chosen=c.candidate_id == p.selected_candidate_id))
        for why in p.no_candidate_reasons:
            L.append(f"- 후보 없음: {why}")
        if p.invalidation_conditions:
            L.append(f"- 무효화 조건(계획·가설): {'; '.join(p.invalidation_conditions)}")
        L.append("- 관망·제한 사유:" if p.decision.value == "WAIT" else "- 남은 제한: 없음 (수동 검토용 제안이며 주문이 아니다)")
        for i, w in enumerate(p.wait_reasons, 1):
            L.append(f"  {i}. {w}")
        L.append(f"- approval_required={str(p.approval_required).lower()}, execution_enabled={str(p.execution_enabled).lower()}")
        L.append("")
    return L
