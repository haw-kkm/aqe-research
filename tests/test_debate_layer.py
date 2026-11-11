"""Analysis/debate layer (backend `aqe_debate`) — AQE_Agent_Debate_Structure_KO v0.1 §3, §5–§7, §10 steps 1–2.

Unit tests run in the core venv without any LLM. The integration tests run the real LangGraph worker in
`.venv-ta` with the offline scripted model (no network, no API key); they are skipped if that venv is absent.
These are wiring/contract tests — not evidence of analysis quality.
"""

from __future__ import annotations

import json
import subprocess
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
    commit_ledger,
    debate_summary,
    ledger_path,
    load_debate,
    load_ledger,
    plan_ledger,
    project,
    prompts_hash,
    role_slice,
    states_at,
)
from aqe.context.store import save_bundle
from aqe.context.worker import process_one
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
    validate_against_bundle,
)
from aqe.contracts.core import Disposition, InstrumentKey, RuleOutcome
from aqe.contracts.debate import (
    AnalystRole,
    HorizonSynthesis,
    Hypothesis,
    HypothesisState,
    RoleStatus,
    SynthesisReport,
)
from aqe.risk.engine import evaluate_risk
from aqe.storage.metadb import JobQueue, connect
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
WORKER_PY = ROOT / ".venv-ta" / "Scripts" / "python.exe"
needs_worker = pytest.mark.skipif(not WORKER_PY.exists(), reason=".venv-ta not installed")


def _cfg(fixture_cfg, data_root, **debate):
    cfg = fixture_cfg.model_copy(deep=True)
    cfg.paths.data_root = data_root
    cfg.context.backend = "aqe_debate"
    cfg.context.llm.provider = "offline_scripted"
    cfg.context.llm.model_id = "aqe-offline-scripted-debate"
    cfg.context.tradingagents.worker_python = WORKER_PY
    cfg.context.job_timeout_seconds = 300
    for k, v in debate.items():
        setattr(cfg.context.debate, k, v)
    return cfg


def _finish(be, when, debate_id, synth=None, records=None, job=None):
    """Candidate assessment from a finished (scripted) worker result; `records` = ledger read BEFORE the LLM run."""
    b = make_bundle(when)
    recs = load_ledger(be.cfg.paths.data_root, INST) if records is None else records
    st, notes = activation(b, [r.value for r in AnalystRole])
    return be.finish(b, result(synth=synth), debate_id, ACTIVE, st, notes, H, active_priors(recs, when), recs,
                     {"provider": "offline_scripted", "model_id": "t"}, 0.0, "ph", job=job)


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
def _backend(fixture_cfg, tmp_path, **opts):
    return DebateBackend(_cfg(fixture_cfg, tmp_path, **opts), JobQueue(tmp_path / "q.sqlite"), project_root=ROOT, horizons=H)


@needs_worker
@pytest.mark.slow
def test_worker_graph_end_to_end_with_revision_and_format_fix(fixture_cfg, tmp_path):
    be = _backend(fixture_cfg, tmp_path)
    b = make_bundle()
    a = be.assess(b, {"job_id": "j1", "idempotency_key": "k1", "scripted_options": {"revise_role": "FLOW", "bad_synthesis_first": True}})
    assert load_ledger(tmp_path, INST) == []  # assess only plans; the ledger changes at commit
    held, a = be.commit(a)
    assert held and any(r == "hypothesis ledger: applied" for r in a.status_reasons)
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
    assert be.commit(a2)[0]
    rec2 = json.loads((tmp_path / "context" / "debates" / INST.slug() / f"{a2.assessment_id}.debate.json").read_text(encoding="utf-8"))
    assert rec2["synthesis"]["prior_decisions"] and rec2["synthesis"]["prior_decisions"][0]["decision"] == "KEEP"
    assert rec2["applied_to_ledger"] is True


@needs_worker
@pytest.mark.slow
def test_worker_call_budget_is_hard(fixture_cfg, tmp_path):
    # 2 drafts + critic + synthesis use all 4: no revision (needs 2 left) and no format repair -> INVALID
    be = _backend(fixture_cfg, tmp_path, max_llm_calls=4)
    a = be.assess(make_bundle(), {"job_id": "j3", "idempotency_key": "k3",
                                  "scripted_options": {"revise_role": "FLOW", "bad_synthesis_first": True}})
    rec = json.loads((tmp_path / "context" / "debates" / INST.slug() / f"{a.assessment_id}.debate.json").read_text(encoding="utf-8"))
    assert rec["llm_calls"] == 4 and rec["revision"] is None
    assert a.status == ContextStatus.INVALID and rec["status"] == "INVALID"


@needs_worker
@pytest.mark.slow
def test_worker_queue_path_commits_with_the_job(fixture_cfg, tmp_path):
    be = _backend(fixture_cfg, tmp_path)
    b = make_bundle()
    save_bundle(tmp_path, b)
    job = be.queue.enqueue("k-queue", "BTCUSDT", AS_OF, b.bundle_id, "aqe_debate")
    r = process_one(be.cfg, be.queue, be, "worker-1")
    assert r["status"] == "PARTIAL" and be.queue.get(job["job_id"])["status"] == "DONE"
    assert be.queue.get(job["job_id"])["result_assessment_id"] == r["assessment_id"]
    ledger = load_ledger(tmp_path, INST)
    assert len(ledger) == 4 and {x.job_id for x in ledger} == {job["job_id"]}
    saved = json.loads(Path(r["path"]).read_text(encoding="utf-8"))
    assert "hypothesis ledger: applied" in saved["status_reasons"]


@needs_worker
@pytest.mark.slow
def test_worker_hallucinated_ids_fail_role(fixture_cfg, tmp_path):
    be = _backend(fixture_cfg, tmp_path)
    a = be.assess(make_bundle(), {"job_id": "j4", "idempotency_key": "k4", "scripted_options": {"hallucinate_role": "CHART"}})
    rec = json.loads((tmp_path / "context" / "debates" / INST.slug() / f"{a.assessment_id}.debate.json").read_text(encoding="utf-8"))
    assert rec["roles"]["CHART"] == "FAILED" and any("ghost-id" in f for f in rec["check_failures"])


# ------------------------------------------------------------------ review 2026-11-12 regressions (D01–D05)
def test_states_at_orders_by_as_of_not_file_position(tmp_path):
    _, _, _, ids = _apply(tmp_path, AS_OF, synthesis())
    recs = load_ledger(tmp_path, INST)
    r0 = next(r for r in recs if r.hypothesis_id == ids["p3600"])
    newer = r0.model_copy(update={"record_id": "hr-new", "state": HypothesisState.INVALIDATED, "as_of": AS_OF + timedelta(minutes=10)})
    older = r0.model_copy(update={"record_id": "hr-old", "state": HypothesisState.ACTIVE, "as_of": AS_OF + timedelta(minutes=5)})
    legacy = [*recs, newer, older]  # an older-as_of row appended after a newer one must not win
    assert states_at(legacy, AS_OF + timedelta(minutes=10))[ids["p3600"]].state == HypothesisState.INVALIDATED
    assert states_at(legacy, AS_OF + timedelta(minutes=7))[ids["p3600"]].record_id == "hr-old"


@pytest.mark.parametrize("old_min,new_min,expect", [(5, 10, "late arrival"), (5, 5, "ledger changed while this debate ran")])
def test_d01_inflight_debate_cannot_reactivate_newer_invalidation(fixture_cfg, tmp_path, old_min, new_min, expect):
    be = DebateBackend(_cfg(fixture_cfg, tmp_path), project_root=ROOT, horizons=H)
    assert be.commit(_finish(be, AS_OF, "debate-base"))[0]
    captured = load_ledger(tmp_path, INST)  # the old run reads the ledger, then its LLM run is slow
    t_old, t_new = AS_OF + timedelta(minutes=old_min), AS_OF + timedelta(minutes=new_min)
    prior = next(r for r in active_priors(captured, t_old) if r.horizon_seconds == 3600 and r.position == "primary")
    keep = [{"hypothesis_id": prior.hypothesis_id, "decision": "KEEP", "reason": "old snapshot", "evidence_ids": ["mkt-bars-1"]}]
    old = _finish(be, t_old, "debate-old", synthesis(prior_decisions=keep), records=captured)
    inv = [{"hypothesis_id": prior.hypothesis_id, "decision": "INVALIDATE", "reason": "range broke", "evidence_ids": ["mkt-bars-1"]}]
    assert be.commit(_finish(be, t_new, "debate-new", synthesis(prior_decisions=inv)))[0]
    lines = ledger_path(tmp_path, INST).read_text(encoding="utf-8").splitlines()
    held, old_final = be.commit(old)  # the old run finishes last
    assert held
    assert ledger_path(tmp_path, INST).read_text(encoding="utf-8").splitlines() == lines  # nothing appended
    ledger = load_ledger(tmp_path, INST)
    assert not any(r.debate_id == "debate-old" for r in ledger)
    assert states_at(ledger, t_new)[prior.hypothesis_id].state == HypothesisState.INVALIDATED
    rec = load_debate(tmp_path, INST, old.assessment_id)  # kept as an audit record, marked not applied
    assert rec.applied_to_ledger is False and expect in rec.ledger_note and rec.hypothesis_id_map == {}
    assert any(expect in r for r in old_final.status_reasons)
    assert not any("hyp-old-" in c.text for c in (*old_final.claims, *old_final.counterclaims))  # unwritten ids not shown


def test_d02_worker_that_lost_its_lease_never_writes_the_ledger(fixture_cfg, tmp_path):
    cfg = _cfg(fixture_cfg, tmp_path)
    b = make_bundle()
    save_bundle(tmp_path, b)
    q = JobQueue(tmp_path / "q.sqlite")
    job = q.enqueue("k-lease", "BTCUSDT", AS_OF, b.bundle_id, "aqe_debate")
    taken = {}

    class SlowDebate(DebateBackend):
        def assess(self, bundle, leased):  # the lease expires during the (simulated) LLM run; another worker takes over
            with connect(q.path) as c:
                c.execute("UPDATE context_jobs SET lease_expires_at=? WHERE job_id=?", ("2000-01-01T00:00:00Z", leased["job_id"]))
            taken["job"] = q.lease("replacement-owner")
            return _finish(self, bundle.as_of, "debate-stale", job=leased)

    r = process_one(cfg, q, SlowDebate(cfg, q, project_root=ROOT, horizons=H), "old-owner")
    assert r["result"].startswith("lease lost")
    assert load_ledger(tmp_path, INST) == []  # no side effect from the rejected worker
    assert q.get(job["job_id"])["status"] == "LEASED" and q.get(job["job_id"])["lease_owner"] == "replacement-owner"
    stale = [load_debate(tmp_path, INST, p.name.split(".")[0]) for p in (tmp_path / "context" / "debates" / INST.slug()).glob("*.debate.json")]
    assert len(stale) == 1 and stale[0].applied_to_ledger is False and "lease lost" in stale[0].ledger_note
    # only the current owner's result is committed, together with the job
    owner = DebateBackend(cfg, q, project_root=ROOT, horizons=H)
    a = validate_against_bundle(_finish(owner, AS_OF, "debate-owner", job=taken["job"]), b)
    held, a = owner.commit(a, taken["job"], "replacement-owner", q)
    assert held and q.get(job["job_id"])["status"] == "DONE" and q.get(job["job_id"])["result_assessment_id"] == a.assessment_id
    ledger = load_ledger(tmp_path, INST)
    assert len(ledger) == 4 and {x.debate_id for x in ledger} == {"debate-owner"} and {x.job_id for x in ledger} == {job["job_id"]}


def test_d02_complete_with_checks_lease_and_rolls_back(tmp_path):
    q = JobQueue(tmp_path / "q.sqlite")
    j = q.enqueue("k", "BTCUSDT", AS_OF, "b", "aqe_debate")
    q.lease("w1")
    ran = []
    assert q.complete_with(j["job_id"], "w2", "a0", lambda: ran.append("w2")) == (False, None) and ran == []

    def boom():
        raise OSError("disk full")

    with pytest.raises(OSError):
        q.complete_with(j["job_id"], "w1", "a1", boom)
    assert q.get(j["job_id"])["status"] == "LEASED" and q.get(j["job_id"])["lease_owner"] == "w1"  # not completed
    assert q.complete_with(j["job_id"], "w1", "a2", lambda: "ok") == (True, "ok")
    assert q.get(j["job_id"])["status"] == "DONE" and q.get(j["job_id"])["result_assessment_id"] == "a2"


def test_d02_commit_error_leaves_job_retryable_and_ledger_untouched(fixture_cfg, tmp_path, monkeypatch):
    cfg = _cfg(fixture_cfg, tmp_path)
    b = make_bundle()
    save_bundle(tmp_path, b)
    q = JobQueue(tmp_path / "q.sqlite", max_attempts=2)
    job = q.enqueue("k-err", "BTCUSDT", AS_OF, b.bundle_id, "aqe_debate")

    class Scripted(DebateBackend):
        def assess(self, bundle, leased):
            return _finish(self, bundle.as_of, "debate-err", job=leased)

    def broken(*_a, **_k):
        raise OSError("ledger volume unavailable")

    monkeypatch.setattr("aqe.context.debate.commit_ledger", broken)
    r = process_one(cfg, q, Scripted(cfg, q, project_root=ROOT, horizons=H), "w1")
    assert r["result"].startswith("commit failed") and q.get(job["job_id"])["status"] == "PENDING"
    assert load_ledger(tmp_path, INST) == []


def test_d02_retried_job_is_not_applied_twice(tmp_path):
    c = check_debate(result(), make_bundle(), ACTIVE, H, [])
    assert commit_ledger(tmp_path, plan_ledger(INST, "debate-try1", AS_OF, c["synthesis"], [], job_id="job-1")) == (True, "applied")
    recs = load_ledger(tmp_path, INST)  # crash after the append, before the queue commit: the job runs again
    applied, note = commit_ledger(tmp_path, plan_ledger(INST, "debate-try2", AS_OF, c["synthesis"], recs, job_id="job-1"))
    assert not applied and "already applied" in note and len(load_ledger(tmp_path, INST)) == len(recs)


def test_d03_revise_requires_same_instrument_and_horizon(tmp_path):
    _, _, _, ids = _apply(tmp_path, AS_OF, synthesis())
    t1 = AS_OF + timedelta(minutes=5)
    priors = active_priors(load_ledger(tmp_path, INST), t1)

    def revise_to(replacement):
        return synthesis(prior_decisions=[{"hypothesis_id": ids["p3600"], "decision": "REVISE", "reason": "new forecast",
                                           "evidence_ids": ["mkt-bars-1"], "replacement_id": replacement}])

    cross = check_debate(result(synth=revise_to("p86400")), make_bundle(t1), ACTIVE, H, priors)
    assert cross["status"] == "INVALID" and any("instrument and horizon must match" in f for f in cross["failures"])
    assert check_debate(result(synth=revise_to("p3600")), make_bundle(t1), ACTIVE, H, priors)["status"] == "OK"


def test_d04_hypothesis_ids_are_unique_and_one_identity_is_used_everywhere(fixture_cfg, tmp_path):
    dup = synthesis()
    for hz in dup["horizons"]:
        hz["primary"]["hypothesis_id"] = "duplicate"
    c = check_debate(result(synth=dup), make_bundle(), ACTIVE, H, [])
    assert c["status"] == "INVALID" and any("duplicate hypothesis ids" in f for f in c["failures"])
    with pytest.raises(ValueError):  # the planner fails closed even if a caller skipped the checks
        plan_ledger(INST, "debate-dup", AS_OF, SynthesisReport.model_validate(dup), [])
    _, _, _, ids = _apply(tmp_path, AS_OF, synthesis())
    t1 = AS_OF + timedelta(minutes=5)
    priors = active_priors(load_ledger(tmp_path, INST), t1)
    reuse = synthesis()
    reuse["horizons"][0]["primary"]["hypothesis_id"] = ids["p3600"]
    keep = {"decision": "KEEP", "reason": "a", "evidence_ids": ["mkt-bars-1"]}
    twice = synthesis(prior_decisions=[{"hypothesis_id": ids["p3600"], **keep}, {"hypothesis_id": ids["p3600"], **keep}])
    rev = {"decision": "REVISE", "reason": "r", "evidence_ids": ["mkt-bars-1"], "replacement_id": "p3600"}
    shared = synthesis(prior_decisions=[{"hypothesis_id": ids["p3600"], **rev}, {"hypothesis_id": ids["o3600"], **rev}])
    for bad, msg in [(reuse, "reuse prior ids"), (twice, "more than one decision"), (shared, "several REVISE")]:
        cb = check_debate(result(synth=bad), make_bundle(t1), ACTIVE, H, priors)
        assert cb["status"] == "INVALID" and any(msg in f for f in cb["failures"]), msg
    # one identity contract: ledger rows, projected claims and the report agree per horizon
    root = tmp_path / "ident"
    be = DebateBackend(_cfg(fixture_cfg, root), project_root=ROOT, horizons=H)
    held, a = be.commit(_finish(be, AS_OF, "debate-ident"))
    rec = load_debate(root, INST, a.assessment_id)
    assert held and rec.hypothesis_id_map == {"p3600": "hyp-ident-h3600-0", "o3600": "hyp-ident-h3600-1",
                                              "p86400": "hyp-ident-h86400-0", "o86400": "hyp-ident-h86400-1"}
    assert "hyp-ident-h3600-0" in next(cl.text for cl in a.claims if cl.text.startswith("[1h"))
    summary = debate_summary(root, INST, a.assessment_id, AS_OF)
    assert [hz["primary"]["canonical_id"] for hz in summary["horizons"]] == ["hyp-ident-h3600-0", "hyp-ident-h86400-0"]
    horizon_of = {r.hypothesis_id: r.horizon_seconds for r in load_ledger(root, INST)}
    assert {horizon_of[cid] for cid in rec.hypothesis_id_map.values() if "-h3600-" in cid} == {3600}


def test_d05_budget_below_minimum_is_refused_before_any_call(fixture_cfg, tmp_path):
    cfg = _cfg(fixture_cfg, tmp_path, max_llm_calls=3)  # 2 active roles need 2 drafts + critic + synthesis = 4
    cfg.context.tradingagents.worker_python = tmp_path / "no-such-python.exe"  # proves the worker is never started
    q = JobQueue(tmp_path / "q.sqlite")
    a = DebateBackend(cfg, q, project_root=ROOT, horizons=H).assess(make_bundle(), {"job_id": "j", "idempotency_key": "k"})
    assert a.status == ContextStatus.UNSUPPORTED and "below the minimum 4" in a.status_reasons[0]
    assert q.usage_totals_today()["calls"] == 0 and not (tmp_path / "context" / "debate_runs").exists()


def _run_worker_job(work, max_calls, options):
    job = {"work_dir": str(work), "max_llm_calls": max_calls, "max_prompt_tokens": 8000, "allow_hosts": [],
           **build_prompts(make_bundle(), list(AnalystRole), H, []),  # all FOUR roles run their drafts in parallel
           "llm": {"provider": "offline_scripted", "model_id": "cap-test"}, "scripted_options": options}
    (work / "job.json").write_text(json.dumps(job), encoding="utf-8")
    subprocess.run([str(WORKER_PY), "-I", str(ROOT / "src" / "aqe" / "context" / "debate_worker.py"), "--input", str(work / "job.json"),
                    "--output", str(work / "result.json")], check=True, cwd=str(work), timeout=300, capture_output=True)
    return json.loads((work / "result.json").read_text(encoding="utf-8"))


CAP_OPTIONS = {"plain": {}, "revise+format_fix": {"revise_role": "FLOW", "bad_synthesis_first": True},
               "provider_errors": {"fail_stages": ["CHART_DRAFT", "CRITIC"]}}


@needs_worker
@pytest.mark.slow
@pytest.mark.parametrize("max_calls", [0, 1, 3, 8])
def test_d05_worker_call_cap_holds_with_four_parallel_roles(tmp_path, max_calls):
    for name, opts in CAP_OPTIONS.items():
        work = tmp_path / name
        work.mkdir()
        res = _run_worker_job(work, max_calls, opts)
        assert res["status"] == "OK", res.get("error")
        attempted = [e for e in res["events"] if e.get("attempted")]
        invoked = res["audit"]["llm_calls"]  # actual model invocations counted inside the model itself
        assert invoked <= max_calls and len(attempted) <= max_calls and res["calls"] <= max_calls, (name, invoked)
        assert invoked == len(attempted) == res["calls"], name  # every attempt counted, failed ones included
        if max_calls == 8:
            assert invoked == {"plain": 6, "revise+format_fix": 8, "provider_errors": 6}[name], (name, invoked)


_ = (RuleOutcome, Path)
