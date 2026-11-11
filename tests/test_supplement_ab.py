"""Regression tests for the follow-up review of commit efe19f4 (supplements A and B).

A: the pre-call cost reservation must be a real upper bound — prompt size is checked before the call,
   the output cap is sent to the provider, the graph cannot retry/grow prompts outside the budget, the
   daily budget is project-wide, and a provider that ignores limits blocks further paid calls.
B: calendar/news coverage comes only from a verifiable import manifest (fetched/available time,
   coverage interval, content hash), never from the mere existence of a file.
Mock transports and temp dirs only; no paid call is made.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import timedelta

import httpx
import pytest

from aqe.context.backends import OfflineFixtureBackend, SimpleLLMBackend, reserve_budget
from aqe.context.evidence import build_bundle, calendar_events, news_items
from aqe.contracts.context import ContextStatus, SourceHealth, SourceHealthStatus, SourceKind, compute_bundle_hash
from aqe.contracts.core import InstrumentKey, RuleOutcome
from aqe.risk.engine import evaluate_risk
from aqe.storage import sha256_file
from aqe.storage.metadb import JobQueue
from tests.conftest import ROOT, utc
from tests.test_context_risk import _bundle, _ok_inputs, _rs

INST = InstrumentKey(venue="binance", market_type="usdm_perpetual", instrument_id="BTCUSDT")
AS_OF = utc(2026, 1, 10, 0, 0, 10)


# ------------------------------------------------------------------------------------------- A
def _cfg(fixture_cfg, *, cap=0.01, provider="openai", base_url=None, max_prompt=8000, max_out=2000, price=(0.0001, 0.0004)):
    cfg = fixture_cfg.model_copy(deep=True)
    cfg.context.max_cost_usd_per_day = cap
    llm = cfg.context.llm
    llm.provider, llm.model_id, llm.api_key_env, llm.base_url = provider, "m", "AQE_TEST_KEY", base_url
    llm.max_prompt_tokens_per_call, llm.max_output_tokens = max_prompt, max_out
    llm.usd_per_1k_prompt_tokens, llm.usd_per_1k_completion_tokens = price
    return cfg


def _recording_transport(calls: list, usage=(10, 5)):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        body = '{"claims":[{"text":"t","kind":"OBSERVATION","direction":"NONE","evidence_ids":["e1"]}],"counterclaims":[]}'
        return httpx.Response(200, json={"model": "m-x", "choices": [{"message": {"content": body}}],
                                          "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}})
    return httpx.MockTransport(handler)


def test_a_review_mock_case_is_refused_before_any_call(fixture_cfg, tmp_path, monkeypatch):
    """Review mock: cap 0.01, 1-token prompt/output assumptions, 1 USD/1k tokens. The prompt cannot fit in
    one token, so the job must be refused before reserving or calling (no first overspend)."""
    monkeypatch.setenv("AQE_TEST_KEY", "sk-test")
    cfg = _cfg(fixture_cfg, cap=0.01, max_prompt=1, max_out=1, price=(1.0, 1.0))
    q, calls = JobQueue(tmp_path / "q.sqlite"), []
    a = SimpleLLMBackend(cfg, q, transport=_recording_transport(calls, usage=(20, 10))).assess(_bundle(AS_OF), {"idempotency_key": "k"})
    assert a.status == ContextStatus.MISSING and "max_prompt_tokens_per_call" in a.status_reasons[0]
    assert calls == []  # provider never contacted
    u = q.usage_totals_today()
    assert u["cost_usd"] == 0 and u["reserved_usd"] == 0 and u["calls"] == 0


@pytest.mark.parametrize("provider,base_url,field", [
    ("openai", None, "max_completion_tokens"),
    ("openai_compatible", "https://paid.example.com/v1", "max_tokens"),
    ("ollama", None, "max_tokens"),
])
def test_a_output_cap_is_sent_to_the_provider(fixture_cfg, tmp_path, monkeypatch, provider, base_url, field):
    monkeypatch.setenv("AQE_TEST_KEY", "sk-test")
    cfg = _cfg(fixture_cfg, cap=1.0, provider=provider, base_url=base_url, max_out=321)
    calls = []
    SimpleLLMBackend(cfg, JobQueue(tmp_path / "q.sqlite"), transport=_recording_transport(calls)).assess(_bundle(AS_OF), {"idempotency_key": "k"})
    assert len(calls) == 1 and calls[0][field] == 321


def test_a_provider_ignoring_limits_is_booked_and_blocks_next_paid_call(fixture_cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("AQE_TEST_KEY", "sk-test")
    cfg = _cfg(fixture_cfg, cap=10.0, max_prompt=8000, max_out=100, price=(1.0, 1.0))
    q, calls = JobQueue(tmp_path / "q.sqlite"), []
    # reservation = 8.1 USD; the mocked provider reports far more output than requested (ignored max_tokens)
    a = SimpleLLMBackend(cfg, q, transport=_recording_transport(calls, usage=(7000, 5000))).assess(_bundle(AS_OF), {"idempotency_key": "k"})
    assert a.usage.cost_estimate_usd == pytest.approx(12.0)
    u = q.usage_totals_today()
    assert u["cost_usd"] == pytest.approx(12.0) and u["budget_overruns"] == 1 and u["reserved_usd"] == pytest.approx(0)
    ok, reason, _, _ = reserve_budget(cfg, q, "simple_llm", "openai", None, calls=1)
    assert ok is False and "more than their reservation" in reason


def test_a_daily_budget_is_project_wide_across_backends(fixture_cfg, tmp_path):
    cfg = _cfg(fixture_cfg, cap=0.01)
    q = JobQueue(tmp_path / "q.sqlite")
    ok1, _, r1, _ = reserve_budget(cfg, q, "simple_llm", "openai", None, calls=5)  # 5 x 0.0016 = 0.008
    ok2, reason, _, _ = reserve_budget(cfg, q, "tradingagents", "openai", None, calls=2)  # +0.0032 > 0.01
    assert ok1 and r1 == pytest.approx(0.008) and ok2 is False and "cap" in reason


def test_a_bridge_aborts_oversized_prompt_and_extra_calls():
    """The usage callback lives in the TradingAgents worker venv (langchain_core is not a core dependency)."""
    py = ROOT / ".venv-ta" / "Scripts" / "python.exe"
    if not py.exists():
        pytest.skip(".venv-ta not installed")
    code = r'''
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("b", sys.argv[1]); b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
from langchain_core.messages import HumanMessage
out = {}
h = b.usage_handler(max_calls=2, max_prompt_tokens=100)
h.on_chat_model_start({}, [[HumanMessage(content="short")]], invocation_params={"model": "m"})
try:
    h.on_chat_model_start({}, [[HumanMessage(content="x" * 500)]], invocation_params={})
    out["big_prompt"] = "allowed"
except RuntimeError as e:
    out["big_prompt"] = str(e)
h2 = b.usage_handler(max_calls=1, max_prompt_tokens=10**6)
h2.on_llm_start({}, ["p"], invocation_params={})
try:
    h2.on_llm_start({}, ["p"], invocation_params={})
    out["extra_call"] = "allowed"
except RuntimeError as e:
    out["extra_call"] = str(e)
print(json.dumps(out))
'''
    res = subprocess.run([str(py), "-I", "-c", code, str(ROOT / "src" / "aqe" / "context" / "ta_bridge.py")],
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr[-800:]
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert "max_prompt_tokens=100" in out["big_prompt"]
    assert "max_llm_calls=1" in out["extra_call"]


# ------------------------------------------------------------------------------------------- B
def _write_import(dirpath, name, content: str, manifest: dict | None, tamper: bool = False):
    dirpath.mkdir(parents=True, exist_ok=True)
    f = dirpath / name
    f.write_text(content, encoding="utf-8")
    if manifest is not None:
        m = {"content_sha256": sha256_file(f), **manifest}
        if tamper:
            m["content_sha256"] = "0" * 64
        (dirpath / (name + ".manifest.json")).write_text(json.dumps(m), encoding="utf-8")
    return f


CAL_HEADER = "event_id,name,source_id,scheduled_time,importance,revision,available_at,actual_value,actual_available_at\n"
OLD_2020 = "cpi-2020-01,US CPI,bls,2020-01-14T13:30:00Z,HIGH,0,2019-12-01T00:00:00Z,,\n"


def _cal_manifest(start, end, fetched="2026-01-01T00:00:00Z", **kw):
    return {"source_id": "bls+fed calendar (manual verified export)", "fetched_at": fetched, "coverage_start": start,
            "coverage_end": end, "revision": 1, **kw}


def test_b_csv_without_manifest_is_not_coverage(fixture_cfg):
    _write_import(fixture_cfg.paths.data_root / "imports" / "calendar", "cal.csv", CAL_HEADER + OLD_2020, None)
    events, h = calendar_events(fixture_cfg, AS_OF)
    assert h.status == SourceHealthStatus.MISSING and h.coverage_end is None and "no manifest" in h.detail


def test_b_review_mock_old_file_does_not_cover_current_period(fixture_cfg):
    """Review mock: a single 2020 event file, queried at 2026-01-10 -> must not be AVAILABLE."""
    _write_import(fixture_cfg.paths.data_root / "imports" / "calendar", "cal.csv", CAL_HEADER + OLD_2020,
                  _cal_manifest("2020-01-01T00:00:00Z", "2020-02-01T00:00:00Z", fetched="2020-01-01T00:00:00Z"))
    events, h = calendar_events(fixture_cfg, AS_OF)
    assert h.status == SourceHealthStatus.STALE and h.coverage_end is None and events == []
    bundle = build_bundle(fixture_cfg, INST, AS_OF)
    a = OfflineFixtureBackend(fixture_cfg).assess(bundle, {"idempotency_key": "k"})
    assert a.calendar_coverage_until is None and a.coverage["MACRO_CALENDAR"] == "STALE"


@pytest.mark.parametrize("change,expect", [
    ({"fetched": "2026-01-11T00:00:00Z"}, "not yet available"),  # manifest published after as_of
    ({"tamper": True}, "content hash"),
    ({"start": "2026-01-20T00:00:00Z", "end": "2026-01-10T00:00:00Z"}, "coverage_start < coverage_end"),
])
def test_b_manifest_must_be_available_consistent_and_untampered(fixture_cfg, change, expect):
    m = _cal_manifest(change.get("start", "2026-01-01T00:00:00Z"), change.get("end", "2026-01-31T00:00:00Z"),
                      fetched=change.get("fetched", "2026-01-01T00:00:00Z"))
    _write_import(fixture_cfg.paths.data_root / "imports" / "calendar", "cal.csv", CAL_HEADER, m, tamper=change.get("tamper", False))
    _, h = calendar_events(fixture_cfg, AS_OF)
    assert h.status == SourceHealthStatus.MISSING and expect in h.detail


def test_b_verified_empty_calendar_gives_manifest_coverage(fixture_cfg):
    cal_dir = fixture_cfg.paths.data_root / "imports" / "calendar"
    _write_import(cal_dir, "cal.csv", CAL_HEADER, _cal_manifest("2026-01-01T00:00:00Z", "2026-01-12T00:00:00Z"))
    news_dir = fixture_cfg.paths.data_root / "imports" / "news"
    _write_import(news_dir, "news.jsonl", "", {"source_id": "news import", "fetched_at": "2026-01-10T00:00:05Z",
                                               "coverage_start": "2026-01-07T00:00:00Z", "coverage_end": "2026-01-10T00:00:00Z"})
    events, h = calendar_events(fixture_cfg, AS_OF)
    assert events == [] and h.status == SourceHealthStatus.AVAILABLE
    assert h.coverage_end == utc(2026, 1, 12)  # from the manifest, not as_of + 3 days
    bundle = build_bundle(fixture_cfg, INST, AS_OF)
    a = OfflineFixtureBackend(fixture_cfg).assess(bundle, {"idempotency_key": "k"})
    assert a.calendar_coverage_until == utc(2026, 1, 12)
    assert a.coverage["MACRO_CALENDAR"] == "AVAILABLE" and a.coverage["NEWS"] == "AVAILABLE"


def test_b_manifest_coverage_flows_into_risk_r4(fixture_cfg, fixture_bars):
    """Calendar coverage from the manifest ends 30 min after as_of -> a 1h horizon is not covered -> R4 UNKNOWN."""
    cal_dir = fixture_cfg.paths.data_root / "imports" / "calendar"
    _write_import(cal_dir, "cal.csv", CAL_HEADER, _cal_manifest("2026-01-01T00:00:00Z", "2026-01-10T00:30:00Z"))
    _write_import(fixture_cfg.paths.data_root / "imports" / "news", "news.jsonl", "",
                  {"source_id": "news import", "fetched_at": "2026-01-10T00:00:05Z",
                   "coverage_start": "2026-01-07T00:00:00Z", "coverage_end": "2026-01-10T00:00:00Z"})
    a = OfflineFixtureBackend(fixture_cfg).assess(build_bundle(fixture_cfg, INST, AS_OF), {"idempotency_key": "k"})
    assert a.calendar_coverage_until == utc(2026, 1, 10, 0, 30)
    rs, h = _rs()
    d = evaluate_risk(_ok_inputs(fixture_bars, context=a), rs, h)
    assert [r for r in d.rules if r.rule_id == "R4_EVENT_LIQUIDITY_COST"][0].outcome == RuleOutcome.UNKNOWN


def test_b_news_coverage_must_reach_as_of(fixture_cfg):
    news_dir = fixture_cfg.paths.data_root / "imports" / "news"
    line = json.dumps({"evidence_id": "n1", "source_id": "wire", "title": "t", "excerpt": "e", "published_at": "2026-01-09T12:00:00Z",
                       "received_at": "2026-01-09T12:00:01Z", "available_at": "2026-01-09T12:00:01Z", "verification_status": "REPORTED"})
    _write_import(news_dir, "news.jsonl", line + "\n", {"source_id": "wire", "fetched_at": "2026-01-09T12:00:05Z",
                                                        "coverage_start": "2026-01-07T00:00:00Z", "coverage_end": "2026-01-09T12:00:00Z"})
    items, h = news_items(fixture_cfg, AS_OF)  # coverage ends ~12h before as_of
    assert len(items) == 1 and h.status == SourceHealthStatus.STALE


def test_b_bundle_hash_backward_compatible_without_coverage():
    health = (SourceHealth(source_kind=SourceKind.NEWS, status=SourceHealthStatus.NOT_CONFIGURED),)
    old_payload = {"instruments": [INST.slug()], "as_of": "2026-01-10T00:00:10Z", "items": [], "calendar": [],
                   "health": [("NEWS", "NOT_CONFIGURED")]}
    old = hashlib.sha256(json.dumps(old_payload, sort_keys=True).encode()).hexdigest()
    assert compute_bundle_hash((INST,), AS_OF, (), (), health) == old
    covered = (SourceHealth(source_kind=SourceKind.NEWS, status=SourceHealthStatus.AVAILABLE,
                            coverage_start=AS_OF - timedelta(days=1), coverage_end=AS_OF),)
    assert compute_bundle_hash((INST,), AS_OF, (), (), covered) != old
