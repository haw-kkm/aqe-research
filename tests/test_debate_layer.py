"""Analysis/debate layer (backend `aqe_debate`) — AQE_Agent_Debate_Structure_KO v0.1 §3, §5–§7, §10 steps 1–2.

Unit tests run in the core venv without any LLM. The integration tests run the real LangGraph worker in
`.venv-ta` with the offline scripted model (no network, no API key); they are skipped if that venv is absent.
These are wiring/contract tests — not evidence of analysis quality.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from aqe.context.debate import (
    DebateBackend,
    activation,
    active_priors,
    apply_to_ledger,
    build_prompts,
    check_debate,
    debate_summary,
    ledger_path,
    load_ledger,
    project,
    prompts_hash,
    role_slice,
    states_at,
)
from aqe.contracts.context import (
    ContextStatus,
    EvidenceBundle,
    EvidenceItem,
    SourceHealth,
    SourceHealthStatus,
    SourceKind,
    TemporalScope,
    VerificationStatus,
    compute_bundle_hash,
)
from aqe.contracts.core import Disposition, InstrumentKey, RuleOutcome
from aqe.contracts.debate import (
    AnalystRole,
    HorizonSynthesis,
    Hypothesis,
    HypothesisState,
    RoleStatus,
)
from aqe.risk.engine import evaluate_risk
from aqe.storage.metadb import JobQueue
from tests.conftest import ROOT, utc
from tests.test_context_risk import _ok_inputs, _rs

INST = InstrumentKey(venue="binance", market_type="usdm_perpetual", instrument_id="BTCUSDT")
AS_OF = utc(2026, 1, 10, 0, 0, 10)
H = [3600, 86400]


def _item(eid, kind=SourceKind.MARKET_FLOW, as_of=AS_OF):
    return EvidenceItem(evidence_id=eid, source_kind=kind, source_id="t", title=eid, excerpt=f"value of {eid}", content_hash=eid,
                        received_at=as_of - timedelta(seconds=5), available_at=as_of - timedelta(seconds=5),
                        verification_status=VerificationStatus.VERIFIED)


def make_bundle(as_of=AS_OF, ids=("mkt-bars-1", "mkt-daily-1", "mkt-funding-1", "aqe-num-h3600-1")) -> EvidenceBundle:
    items = tuple(_item(i, SourceKind.AQE_NUMERIC if i.startswith("aqe-num") else SourceKind.MARKET_FLOW, as_of) for i in ids)
    health = (SourceHealth(source_kind=SourceKind.MARKET_FLOW, status=SourceHealthStatus.AVAILABLE),
              SourceHealth(source_kind=SourceKind.NEWS, status=SourceHealthStatus.NOT_CONFIGURED),
              SourceHealth(source_kind=SourceKind.MACRO_CALENDAR, status=SourceHealthStatus.NOT_CONFIGURED),
              SourceHealth(source_kind=SourceKind.CROSS_ASSET, status=SourceHealthStatus.NOT_CONFIGURED),
              SourceHealth(source_kind=SourceKind.AQE_NUMERIC, status=SourceHealthStatus.AVAILABLE))
    h = compute_bundle_hash((INST,), as_of, items, (), health)
    return EvidenceBundle(bundle_id=f"b-{h[:8]}", instruments=(INST,), as_of=as_of, items=items, source_health=health, bundle_hash=h)


def hyp(hid, h, ev=("mkt-bars-1",), direction="NONE", thesis="RANGE"):
    return {"hypothesis_id": hid, "horizon_seconds": h, "thesis_type": thesis, "direction": direction, "statement": f"{hid} statement",
            "evidence_ids": list(ev), "invalidation_conditions": ["break of the 24h range"], "qualitative_confidence": "LOW"}


def report(role, ev, rnd=0):
    return {"role": role, "round": rnd, "statements": [{"statement_id": f"{role}-s1", "kind": "OBSERVATION", "text": "obs", "evidence_ids": [ev]}],
            "hypotheses": [hyp(f"{role}-h1", 3600, (ev,))], "counter_evidence": [], "missing_data": []}


def synthesis(prior_decisions=(), horizons=H, ev=("mkt-bars-1",)):
    return {"horizons": [{"horizon_seconds": h, "primary": hyp(f"p{h}", h, ev), "opposing": [hyp(f"o{h}", h, ev, "UP", "REBOUND")],
                          "unresolved": [], "changed_since_previous": ""} for h in horizons],
            "prior_decisions": list(prior_decisions), "limitations": []}


def result(drafts=None, critique=None, revision=None, synth=None):
    drafts = drafts or {"CHART": report("CHART", "mkt-bars-1"), "FLOW": report("FLOW", "mkt-funding-1")}
    return {"status": "OK", "drafts": {k: {"parsed": v, "raw": "x", "error": None} for k, v in drafts.items()},
            "critique": {"parsed": critique if critique is not None else {"issues": [], "revision_role": None}},
            "revision": revision, "synthesis": {"parsed": synth if synth is not None else synthesis()}, "audit": {}}


ACTIVE = [AnalystRole.CHART, AnalystRole.FLOW]


# ------------------------------------------------------------------ contracts
def test_hypothesis_contract_rules():
    with pytest.raises(ValidationError):
        Hypothesis.model_validate({**hyp("a", 3600), "invalidation_conditions": []})  # must be falsifiable
    with pytest.raises(ValidationError):
        Hypothesis.model_validate({**hyp("a", 3600), "qualitative_confidence": "80%"})  # no numeric confidence
    with pytest.raises(ValidationError):
        HorizonSynthesis.model_validate({"horizon_seconds": 3600, "primary": hyp("a", 86400)})  # no horizon mixing
    with pytest.raises(ValidationError):
        HorizonSynthesis.model_validate({"horizon_seconds": 3600, "primary": None})  # abstain needs a reason


# ------------------------------------------------------------------ activation & slices (§4, §5)
def test_roles_activate_only_with_their_evidence():
    st, notes = activation(make_bundle(), ["CHART", "FLOW", "NEWS", "CROSS_MARKET"])
    assert st[AnalystRole.CHART] == st[AnalystRole.FLOW] == RoleStatus.ACTIVE
    assert st[AnalystRole.NEWS] == st[AnalystRole.CROSS_MARKET] == RoleStatus.NOT_ACTIVATED
    assert "not 'no event'" in notes[AnalystRole.NEWS]
    st2, _ = activation(make_bundle(ids=("mkt-bars-1", "mkt-daily-1")), ["CHART", "FLOW"])
    assert st2[AnalystRole.FLOW] == RoleStatus.NOT_ACTIVATED  # no funding/OI/flow data
    st3, n3 = activation(make_bundle(), ["CHART"])
    assert st3[AnalystRole.FLOW] == RoleStatus.NOT_ACTIVATED and n3[AnalystRole.FLOW] == "disabled in config"


def test_role_slices_and_independent_drafts():
    b = make_bundle()
    assert {i.evidence_id for i in role_slice(b, AnalystRole.CHART)} == {"mkt-bars-1", "mkt-daily-1", "aqe-num-h3600-1"}
    assert "aqe-num-h3600-1" not in {i.evidence_id for i in role_slice(b, AnalystRole.FLOW)}
    p = build_prompts(b, ACTIVE, H, priors=[])
    flow = next(r for r in p["roles"] if r["role"] == "FLOW")
    chart = next(r for r in p["roles"] if r["role"] == "CHART")
    assert "[ev:aqe-num-h3600-1]" in chart["user"] and "[ev:aqe-num-h3600-1]" not in flow["user"]
    for r in p["roles"]:  # first drafts never see other reports or prior conclusions
        assert "DRAFTS_JSON" not in r["user"] and "PRIOR_ACTIVE_HYPOTHESES" not in r["user"]
    assert "{{DRAFTS_JSON}}" in p["critic"]["user"] and "PRIOR_ACTIVE_HYPOTHESES" in p["synthesis"]["user"]
    assert all("HORIZON_SECONDS: 86400" in r["system"] for r in p["roles"])
    assert prompts_hash(p) == prompts_hash(build_prompts(b, ACTIVE, H, priors=[]))


# ------------------------------------------------------------------ code checks (§6 step 7)
def test_valid_debate_passes_checks():
    c = check_debate(result(), make_bundle(), ACTIVE, H, [])
    assert c["status"] == "OK" and not c["failures"] and len(c["drafts"]) == 2


def test_hallucinated_evidence_fails_that_role_only():
    r = result(drafts={"CHART": report("CHART", "ghost-id"), "FLOW": report("FLOW", "mkt-funding-1")})
    c = check_debate(r, make_bundle(), ACTIVE, H, [])
    assert c["role_status"][AnalystRole.CHART] == RoleStatus.FAILED and c["role_status"][AnalystRole.FLOW] == RoleStatus.ACTIVE
    assert c["status"] == "PARTIAL" and any("ghost-id" in f for f in c["failures"])


def test_revision_only_for_the_requested_role():
    crit = {"issues": [{"target_role": "FLOW", "target_ref": "FLOW-s1", "issue_type": "UNSUPPORTED", "severity": "MAJOR",
                        "explanation": "x"}], "revision_role": "FLOW"}
    ok = check_debate(result(critique=crit, revision={"role": "FLOW", "parsed": report("FLOW", "mkt-funding-1", 1)}), make_bundle(), ACTIVE, H, [])
    assert ok["revision"] is not None and ok["revision"].round == 1
    bad = check_debate(result(critique=crit, revision={"role": "CHART", "parsed": report("CHART", "mkt-bars-1", 1)}), make_bundle(), ACTIVE, H, [])
    assert bad["revision"] is None and any("revision without" in f for f in bad["failures"])
    inactive = {"issues": [], "revision_role": "NEWS"}
    c = check_debate(result(critique=inactive), make_bundle(), ACTIVE, H, [])
    assert c["critique"].revision_role is None


@pytest.mark.parametrize("synth,expect", [
    (synthesis(horizons=[3600, 300]), "INVALID"),  # horizon not configured
    (synthesis(ev=("ghost",)), "INVALID"),  # hallucinated id in synthesis
    (synthesis(horizons=[3600]), "PARTIAL"),  # missing horizon -> recorded ABSTAIN
])
def test_synthesis_checks(synth, expect):
    c = check_debate(result(synth=synth), make_bundle(), ACTIVE, H, [])
    assert c["status"] == expect
    if expect == "PARTIAL":
        assert [hz.primary for hz in c["synthesis"].horizons if hz.horizon_seconds == 86400] == [None]


# ------------------------------------------------------------------ hypothesis ledger (§7)
def _apply(tmp_path, as_of, synth_dict, prior=()):
    b = make_bundle(as_of)
    records = load_ledger(tmp_path, INST)
    priors = active_priors(records, as_of)
    c = check_debate(result(synth=synth_dict), b, ACTIVE, H, priors)
    assert c["synthesis"] is not None, c["failures"]
    rows, applied, note, id_map = apply_to_ledger(tmp_path, INST, f"debate-{as_of.hour:02d}{as_of.minute:02d}", as_of, c["synthesis"], records)
    return rows, applied, note, id_map


def test_ledger_lifecycle_keep_revise_invalidate_expire_append_only(tmp_path):
    t0 = AS_OF
    rows, applied, _, ids = _apply(tmp_path, t0, synthesis())
    assert applied and {r.state for r in rows} == {HypothesisState.ACTIVE}
    p1h, p24 = ids["p3600"], ids["p86400"]
    lines_before = ledger_path(tmp_path, INST).read_text(encoding="utf-8").splitlines()
    t1 = t0 + timedelta(minutes=30)
    dec = [{"hypothesis_id": p24, "decision": "KEEP", "reason": "still consistent", "evidence_ids": ["mkt-bars-1"]},
           {"hypothesis_id": p1h, "decision": "INVALIDATE", "reason": "range broke", "evidence_ids": ["mkt-daily-1"]},
           {"hypothesis_id": ids["o3600"], "decision": "REVISE", "reason": "refined", "evidence_ids": ["mkt-bars-1"], "replacement_id": "p3600"}]
    rows2, applied2, _, ids2 = _apply(tmp_path, t1, synthesis(prior_decisions=dec))
    st = states_at(load_ledger(tmp_path, INST), t1)
    assert st[p1h].state == HypothesisState.INVALIDATED and st[p1h].evidence_ids == ("mkt-daily-1",)
    assert st[p24].state == HypothesisState.ACTIVE and st[p24].valid_until == t1 + timedelta(hours=24)
    assert st[ids["o3600"]].state == HypothesisState.REVISED
    assert st[ids2["p3600"]].previous_id == ids["o3600"]  # lineage kept
    lines_after = ledger_path(tmp_path, INST).read_text(encoding="utf-8").splitlines()
    assert lines_after[: len(lines_before)] == lines_before  # nothing overwritten
    # time passes beyond 1h validity without re-confirmation -> EXPIRED by code (not by the LLM)
    t2 = t1 + timedelta(hours=2)
    rows3, _, _, _ = _apply(tmp_path, t2, synthesis())
    expired = {r.hypothesis_id for r in rows3 if r.state == HypothesisState.EXPIRED}
    assert ids2["p3600"] in expired and ids2["o3600"] in expired and p24 not in expired


def test_invalidate_requires_evidence_and_known_prior(tmp_path):
    _, _, _, ids = _apply(tmp_path, AS_OF, synthesis())
    priors = active_priors(load_ledger(tmp_path, INST), AS_OF + timedelta(minutes=5))
    b = make_bundle(AS_OF + timedelta(minutes=5))
    no_ev = synthesis(prior_decisions=[{"hypothesis_id": ids["p3600"], "decision": "INVALIDATE", "reason": "felt like it", "evidence_ids": []}])
    assert check_debate(result(synth=no_ev), b, ACTIVE, H, priors)["status"] == "INVALID"
    unknown = synthesis(prior_decisions=[{"hypothesis_id": "nope", "decision": "KEEP", "reason": "x"}])
    assert check_debate(result(synth=unknown), b, ACTIVE, H, priors)["status"] == "INVALID"


def test_late_arriving_debate_is_not_applied(tmp_path):
    _apply(tmp_path, AS_OF + timedelta(hours=1), synthesis())
    rows, applied, note, _ = _apply(tmp_path, AS_OF, synthesis())
    assert applied is False and rows == [] and "late arrival" in note


# ------------------------------------------------------------------ projection & risk independence
def test_projection_keeps_numeric_and_risk_unchanged(fixture_bars, tmp_path):
    c = check_debate(result(), make_bundle(), ACTIVE, H, [])
    _, _, _, ids = apply_to_ledger(tmp_path, INST, "debate-x", AS_OF, c["synthesis"], [])
    claims, counter, review = project(c, ids)
    assert any(cl.kind.value == "OBSERVATION" for cl in claims) and any(cl.direction.value == "UP" for cl in counter)
    assert all(cl.falsifier for cl in claims if cl.kind.value == "INFERENCE")
    from tests.test_context_risk import _assessment, _bundle

    b = _bundle(utc(2026, 1, 10, 0, 0, 10))
    ctx = _assessment(b, (claims[0].model_copy(update={"evidence_ids": ("e1",)}),), rating=None)
    rs, h = _rs()
    inp = _ok_inputs(fixture_bars, context=ctx)
    inp.ensemble = inp.ensemble.model_copy(update={"extra": {"oos_verdict": "SKILL_DEMONSTRATED", "oos_cost_verdict": "NOT_POSITIVE"}})
    d = evaluate_risk(inp, rs, h)
    assert d.disposition == Disposition.RESEARCH_ONLY  # a debate cannot lift a restriction
    assert inp.ensemble.probabilities.p_up == 0.5  # numeric probabilities untouched


def test_job_coalescing_supersedes_older_pending(tmp_path):
    q = JobQueue(tmp_path / "q.sqlite")
    a = q.enqueue("k1", "BTCUSDT", AS_OF, "b1", "aqe_debate")
    b = q.enqueue("k2", "BTCUSDT", AS_OF + timedelta(minutes=5), "b2", "aqe_debate")
    assert q.supersede_pending("BTCUSDT", "aqe_debate", AS_OF + timedelta(minutes=5), b["job_id"]) == 1
    assert q.get(a["job_id"])["status"] == "SUPERSEDED" and q.get(b["job_id"])["status"] == "PENDING"


# ------------------------------------------------------------------ integration: real LangGraph worker, scripted LLM
WORKER_PY = ROOT / ".venv-ta" / "Scripts" / "python.exe"
needs_worker = pytest.mark.skipif(not WORKER_PY.exists(), reason=".venv-ta not installed")


def _backend(fixture_cfg, tmp_path, **opts):
    cfg = fixture_cfg.model_copy(deep=True)
    cfg.paths.data_root = tmp_path
    cfg.context.backend = "aqe_debate"
    cfg.context.llm.provider = "offline_scripted"
    cfg.context.llm.model_id = "aqe-offline-scripted-debate"
    cfg.context.tradingagents.worker_python = WORKER_PY
    cfg.context.job_timeout_seconds = 300
    for k, v in opts.items():
        setattr(cfg.context.debate, k, v)
    return DebateBackend(cfg, JobQueue(tmp_path / "q.sqlite"), project_root=ROOT, horizons=H)


@needs_worker
@pytest.mark.slow
def test_worker_graph_end_to_end_with_revision_and_format_fix(fixture_cfg, tmp_path):
    be = _backend(fixture_cfg, tmp_path)
    b = make_bundle()
    a = be.assess(b, {"job_id": "j1", "idempotency_key": "k1", "scripted_options": {"revise_role": "FLOW", "bad_synthesis_first": True}})
    assert a.backend == "aqe_debate" and a.status == ContextStatus.PARTIAL  # NEWS/CROSS_MARKET not activated
    assert a.temporal_scope == TemporalScope.INTRADAY_ASOF_VERIFIED and a.contextual_rating is None
    assert any("NEWS" in r and "not 'no event'" in r for r in a.status_reasons)
    rec = json.loads((tmp_path / "context" / "debates" / INST.slug() / f"{a.assessment_id}.debate.json").read_text(encoding="utf-8"))
    assert rec["roles"] == {"CHART": "ACTIVE", "FLOW": "ACTIVE", "NEWS": "NOT_ACTIVATED", "CROSS_MARKET": "NOT_ACTIVATED"}
    assert rec["revision"]["role"] == "FLOW" and rec["revision"]["round"] == 1
    # 2 drafts + critic + 1 revision + synthesis + 1 format-only repair = 6 <= 8
    assert rec["llm_calls"] == 6 and rec["max_llm_calls"] == 8 and rec["status"] in ("OK", "PARTIAL")
    assert rec["applied_to_ledger"] is True
    states = states_at(load_ledger(tmp_path, INST), b.as_of)
    assert sum(1 for r in states.values() if r.state == HypothesisState.ACTIVE) == 4  # primary + opposing per horizon
    summary = debate_summary(tmp_path, INST, a.assessment_id, b.as_of)
    assert summary["horizons"][0]["primary"]["canonical_id"] and summary["revision_role"] == "FLOW"
    from aqe.reporting.report import render_debate

    md = "\n".join(render_debate(summary))
    assert "3b. 분석·토론 가설" in md and "철회 조건" in md and "상태 ACTIVE" in md
    assert "NEWS: `NOT_ACTIVATED`" in md and "LLM 호출 6/8" in md
    # follow-up run: prior hypotheses are offered to the synthesis and kept/re-confirmed
    b2 = make_bundle(AS_OF + timedelta(minutes=5))
    a2 = be.assess(b2, {"job_id": "j2", "idempotency_key": "k2", "scripted_options": {}})
    rec2 = json.loads((tmp_path / "context" / "debates" / INST.slug() / f"{a2.assessment_id}.debate.json").read_text(encoding="utf-8"))
    assert rec2["synthesis"]["prior_decisions"] and rec2["synthesis"]["prior_decisions"][0]["decision"] == "KEEP"


@needs_worker
@pytest.mark.slow
def test_worker_call_budget_is_hard(fixture_cfg, tmp_path):
    be = _backend(fixture_cfg, tmp_path, max_llm_calls=3)  # 2 drafts + critic exhaust it: no synthesis possible
    a = be.assess(make_bundle(), {"job_id": "j3", "idempotency_key": "k3", "scripted_options": {"revise_role": "FLOW"}})
    rec = json.loads((tmp_path / "context" / "debates" / INST.slug() / f"{a.assessment_id}.debate.json").read_text(encoding="utf-8"))
    assert rec["llm_calls"] <= 3 and rec["revision"] is None
    assert a.status == ContextStatus.INVALID and rec["status"] == "INVALID"


@needs_worker
@pytest.mark.slow
def test_worker_hallucinated_ids_fail_role(fixture_cfg, tmp_path):
    be = _backend(fixture_cfg, tmp_path)
    a = be.assess(make_bundle(), {"job_id": "j4", "idempotency_key": "k4", "scripted_options": {"hallucinate_role": "CHART"}})
    rec = json.loads((tmp_path / "context" / "debates" / INST.slug() / f"{a.assessment_id}.debate.json").read_text(encoding="utf-8"))
    assert rec["roles"]["CHART"] == "FAILED" and any("ghost-id" in f for f in rec["check_failures"])


_ = (RuleOutcome, Path)
