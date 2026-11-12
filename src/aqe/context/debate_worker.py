"""AQE analysis/debate graph worker — runs INSIDE the `.venv-ta` interpreter (stdlib + langgraph + langchain).

    <venv-ta python> -I debate_worker.py --input job.json --output result.json

Flow (debate proposal §3, §6):  specialists (independent drafts, parallel) -> critic -> [one revision of
one role] -> synthesis -> [one format-only repair] ; hard cap = job["max_llm_calls"] (default 8).
Reused upstream parts: LangGraph (StateGraph) and TradingAgents' LLM client factory
(`tradingagents.llm_clients.factory.create_llm_client`). The TradingAgents debate graph itself is NOT used
here (it stays available as the separate `tradingagents` backend). Isolation/budget helpers are imported
from ta_bridge.py (network guard, usage callback). All prompts are built by AQE core and passed in;
the worker only executes the flow and returns raw texts + parsed JSON (validation happens in core).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import operator
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Annotated, Any, TypedDict

_bridge_spec = importlib.util.spec_from_file_location("aqe_ta_bridge", Path(__file__).with_name("ta_bridge.py"))
bridge = importlib.util.module_from_spec(_bridge_spec)
_bridge_spec.loader.exec_module(bridge)

EV_TAG = re.compile(r"\[ev:([A-Za-z0-9._:\-]+)\]")
PLAN_TAG = re.compile(r"\[plan:(pc-[0-9a-f]+)\] horizon (\d+)s (LONG|SHORT) selectable=(yes|no)")


def _merge(a: dict, b: dict) -> dict:
    return {**(a or {}), **(b or {})}


class DebateState(TypedDict):
    drafts: Annotated[dict, _merge]
    critique: dict | None
    revision: dict | None
    synthesis: dict | None
    calls: Annotated[int, operator.add]
    events: Annotated[list, operator.add]


def extract_json(text: str) -> Any:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            return json.loads(t[i:j + 1])
        raise


def scripted_model(options: dict):
    """Offline contract model: answers per STAGE marker with grounded JSON (no network)."""
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    state = {"synth_calls": 0, "last_synthesis": None}

    def ids_in(text: str) -> list[str]:
        return list(dict.fromkeys(EV_TAG.findall(text)))

    def respond(system: str, user: str) -> str:
        stage = re.search(r"STAGE:\s*(\w+)", system).group(1)
        if stage in options.get("fail_stages", ()):  # simulated provider error (the attempt is still counted)
            raise RuntimeError(f"scripted failure at {stage}")
        ids = ids_in(user) or ["missing-id"]
        horizons = [int(x) for x in re.findall(r"HORIZON_SECONDS:\s*(\d+)", system + "\n" + user)] or [3600]
        if stage.endswith("_DRAFT") or stage.endswith("_REVISION"):
            role = stage.split("_")[0]
            rnd = 1 if stage.endswith("_REVISION") else 0
            hyps = [{"hypothesis_id": f"{role.lower()}-h{h}", "horizon_seconds": h, "thesis_type": "DIRECTIONAL",
                     "direction": "NONE", "statement": f"{role} sees no directional edge over {h // 3600}h",
                     "evidence_ids": ids[:1], "invalidation_conditions": ["a close beyond the 24h range"],
                     "review_conditions": ["next completed bar"], "qualitative_confidence": "LOW"} for h in sorted(set(horizons))]
            ev = ids[:1] if not (options.get("hallucinate_role") == role and rnd == 0) else ["ghost-id"]
            return json.dumps({"role": role, "round": rnd,
                               "statements": [{"statement_id": f"{role.lower()}-s1", "kind": "OBSERVATION", "text": "observed value", "evidence_ids": ev},
                                              {"statement_id": f"{role.lower()}-s2", "kind": "INTERPRETATION", "text": "reading", "evidence_ids": ids[:2]}],
                               "hypotheses": hyps, "counter_evidence": [], "missing_data": ["order book not collected"] if role == "FLOW" else []})
        if stage == "CRITIC":
            target = options.get("revise_role")
            issues = [{"target_role": target, "target_ref": f"{target.lower()}-s1", "issue_type": "UNSUPPORTED", "severity": "MAJOR",
                       "explanation": "observation needs a cited value", "required_check": "cite the funding print"}] if target else []
            return json.dumps({"issues": issues, "revision_role": target})
        if stage == "SYNTHESIS":
            state["synth_calls"] += 1
            prior_ids = re.findall(r'"hypothesis_id":\s*"([^"]+)"', user.split("PRIOR_ACTIVE_HYPOTHESES:")[-1]) if "PRIOR_ACTIVE_HYPOTHESES:" in user else []
            hz = []
            for h in sorted(set(horizons)):
                hz.append({"horizon_seconds": h, "primary": {
                    "hypothesis_id": f"syn-h{h}", "horizon_seconds": h, "thesis_type": "RANGE", "direction": "NONE",
                    "statement": f"no directional edge over {h // 3600}h", "evidence_ids": ids[:2],
                    "invalidation_conditions": ["break of the 24h high or low on a completed bar"], "qualitative_confidence": "LOW"},
                    "opposing": [{"hypothesis_id": f"syn-opp-h{h}", "horizon_seconds": h, "thesis_type": "REBOUND", "direction": "UP",
                                  "statement": "short rebound possible", "evidence_ids": ids[:1],
                                  "invalidation_conditions": ["new 24h low"]}],
                    "unresolved": ["order-book evidence missing"], "changed_since_previous": "first run" if not prior_ids else "re-confirmed"})
            decisions = [{"hypothesis_id": prior_ids[0], "decision": options.get("prior_decision", "KEEP"), "reason": "still consistent",
                          "evidence_ids": ids[:1]}] if prior_ids else []
            plain = json.dumps({"horizons": hz, "prior_decisions": decisions, "limitations": ["news and cross-market roles not activated"]})
            if "PLAN_CANDIDATES" in user:  # trade-plan selection (contract test only; never presented as an AI choice)
                plan_selection(hz, user, ids, options.get("plan_select", "first"))
            good = json.dumps({"horizons": hz, "prior_decisions": decisions, "limitations": ["news and cross-market roles not activated"]})
            state["last_synthesis"] = good
            if options.get("format_fix_adds_selection") and state["synth_calls"] == 1:
                return "Here is the synthesis: " + plain[:-1] + ",}"  # broken JSON WITHOUT a selection; the repair adds one
            if options.get("bad_synthesis_first") and state["synth_calls"] == 1:
                return "Here is the synthesis: " + good[:-1] + ",}"  # same content, broken JSON
            return good
        if stage == "FORMAT_FIX":
            return state["last_synthesis"] or "{}"  # reformat only: no new content
        return "{}"

    def plan_selection(hz: list, user: str, ids: list, mode: str) -> None:
        cands = [(cid, int(h), side, sel == "yes") for cid, h, side, sel in PLAN_TAG.findall(user)]
        for block in hz:
            h = block["horizon_seconds"]
            mine = [c for c in cands if c[1] == h]
            other = [c for c in cands if c[1] != h]
            pick = next((c[0] for c in mine if c[3]), None)
            reason = "scripted selection (wiring test, not an AI judgement)"
            extra = {}
            if mode == "none":
                pick = None
            elif mode == "ghost":
                pick = "pc-000000000000deadbeef"
            elif mode == "other_horizon":
                pick = other[0][0] if other else "pc-none"
            elif mode == "unselectable":
                pick = next((c[0] for c in mine if not c[3]), "pc-none")
            elif mode == "price_field":
                extra = {"entry_price": 12345.6}
            elif mode == "price_text":
                m = re.search(r"reference close (\d+(?:\.\d+)?)", user)
                reason += f"; enter at {float(m.group(1)) * 1.013:.1f}" if m else "; enter at 1"
            block["plan_selection"] = {"candidate_id": pick, "reason": reason, "evidence_ids": ids[:1], "counter_evidence_ids": [], **extra}

    class Scripted(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "aqe-offline-scripted-debate"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            bridge.AUDIT["llm_calls"] += 1
            system = next((m.content for m in messages if m.type == "system"), "")
            user = "\n".join(m.content for m in messages if m.type != "system")
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=respond(system, user)))])

    return Scripted()


def build_graph(job: dict, llm, log: list):
    from langchain_core.messages import HumanMessage, SystemMessage
    from langgraph.graph import END, START, StateGraph

    max_calls = int(job.get("max_llm_calls", 8))
    roles = job["roles"]  # [{role, system, user}]
    active = [r["role"] for r in roles]
    # Hard cap shared by ALL nodes. Parallel draft nodes see the same per-superstep state snapshot, so the state's
    # `calls` cannot enforce the cap (review D05); each call takes a slot here atomically BEFORE invoking the model.
    # Slots are never returned, even when the call fails (a failed call may still be billed).
    slot_lock = threading.Lock()
    slots = {"used": 0}

    def take_slot() -> bool:
        with slot_lock:
            if slots["used"] >= max_calls:
                return False
            slots["used"] += 1
            return True

    def call(stage: str, system: str, user: str) -> tuple[str | None, dict]:
        if not take_slot():
            return None, {"stage": stage, "error": f"call budget {max_calls} exhausted", "attempted": False}
        t0 = time.time()
        try:
            msg = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
            text = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
            return text, {"stage": stage, "seconds": round(time.time() - t0, 3), "attempted": True,
                          "response_model": (getattr(msg, "response_metadata", {}) or {}).get("model_name")}
        except Exception as exc:  # noqa: BLE001 — recorded, the run continues (fail-soft per stage); may still be billed
            return None, {"stage": stage, "error": f"{type(exc).__name__}: {exc}"[:400], "attempted": True}

    def parsed(text):
        if text is None:
            return None, "no response"
        try:
            return extract_json(text), None
        except (json.JSONDecodeError, ValueError) as exc:
            return None, f"invalid JSON: {exc}"[:200]

    def draft_node(spec):
        def node(state: DebateState):
            # independent draft: sees only its own evidence slice, never other reports or prior synthesis
            text, ev = call(f"{spec['role']}_DRAFT", spec["system"], spec["user"])
            obj, err = parsed(text)
            return {"drafts": {spec["role"]: {"raw": text, "parsed": obj, "error": err or ev.get("error")}},
                    "calls": 1 if ev["attempted"] else 0, "events": [ev]}
        return node

    def critic(state: DebateState):
        drafts = {k: v["parsed"] for k, v in state["drafts"].items() if v.get("parsed") is not None}
        user = job["critic"]["user"].replace("{{DRAFTS_JSON}}", json.dumps(drafts, ensure_ascii=False))
        text, ev = call("CRITIC", job["critic"]["system"], user)
        obj, err = parsed(text)
        return {"critique": {"raw": text, "parsed": obj, "error": err or ev.get("error")}, "calls": 1 if ev["attempted"] else 0, "events": [ev]}

    def needs_revision(state: DebateState) -> str:
        c = (state.get("critique") or {}).get("parsed") or {}
        role = c.get("revision_role")
        budget_left = max_calls - slots["used"]
        if role in active and budget_left >= 2:  # keep one call for the synthesis
            return "revise"
        return "synthesize"

    def revise(state: DebateState):
        c = state["critique"]["parsed"]
        role = c["revision_role"]
        spec = next(r for r in roles if r["role"] == role)
        issues = [i for i in c.get("issues", []) if i.get("target_role") == role]
        user = (spec["user"] + "\n\nCRITIQUE_OF_YOUR_DRAFT (address each issue; do not add new market data):\n"
                + json.dumps(issues, ensure_ascii=False) + "\n\nYOUR_DRAFT:\n" + json.dumps(state["drafts"][role]["parsed"], ensure_ascii=False))
        text, ev = call(f"{role}_REVISION", spec["system"].replace(f"STAGE: {role}_DRAFT", f"STAGE: {role}_REVISION"), user)
        obj, err = parsed(text)
        return {"revision": {"role": role, "raw": text, "parsed": obj, "error": err or ev.get("error")},
                "calls": 1 if ev["attempted"] else 0, "events": [ev]}

    def synthesize(state: DebateState):
        drafts = {k: v["parsed"] for k, v in state["drafts"].items() if v.get("parsed") is not None}
        rev = (state.get("revision") or {}).get("parsed")
        if rev is not None:
            drafts[state["revision"]["role"]] = rev
        user = (job["synthesis"]["user"].replace("{{REPORTS_JSON}}", json.dumps(drafts, ensure_ascii=False))
                .replace("{{CRITIQUE_JSON}}", json.dumps((state.get("critique") or {}).get("parsed"), ensure_ascii=False)))
        text, ev = call("SYNTHESIS", job["synthesis"]["system"], user)
        obj, err = parsed(text)
        out = {"synthesis": {"raw": text, "parsed": obj, "error": err or ev.get("error"), "format_fixed": False},
               "calls": 1 if ev["attempted"] else 0, "events": [ev]}
        if obj is None and text is not None and slots["used"] < max_calls:
            # one format-only repair: same content, no new data, no new debate
            fix_user = job["format_fix"]["user"].replace("{{BAD_OUTPUT}}", text[:6000])
            text2, ev2 = call("FORMAT_FIX", job["format_fix"]["system"], fix_user)
            obj2, err2 = parsed(text2)
            # raw_before_fix lets core verify that the repair added nothing (e.g. no new plan selection)
            out = {"synthesis": {"raw": text2, "parsed": obj2, "error": err2 or ev2.get("error"), "format_fixed": obj2 is not None,
                                 "raw_before_fix": text},
                   "calls": (1 if ev["attempted"] else 0) + (1 if ev2["attempted"] else 0), "events": [ev, ev2]}
        return out

    g = StateGraph(DebateState)
    for spec in roles:
        g.add_node(f"draft_{spec['role']}", draft_node(spec))
        g.add_edge(START, f"draft_{spec['role']}")
    g.add_node("critic", critic)
    g.add_node("revise", revise)
    g.add_node("synthesize", synthesize)
    g.add_edge([f"draft_{r}" for r in active], "critic")
    g.add_conditional_edges("critic", needs_revision, {"revise": "revise", "synthesize": "synthesize"})
    g.add_edge("revise", "synthesize")
    g.add_edge("synthesize", END)
    return g.compile()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    a = ap.parse_args()
    job = json.loads(Path(a.input).read_text(encoding="utf-8"))
    out: dict[str, Any] = {"status": "ERROR"}
    t0 = time.time()
    work = Path(job["work_dir"])
    work.mkdir(parents=True, exist_ok=True)
    for k in list(os.environ):
        if k.startswith("TRADINGAGENTS_"):
            os.environ.pop(k)
    os.chdir(work)
    uninstall = bridge.install_network_guard(set(job.get("allow_hosts", [])))
    handler = None
    try:
        import importlib.metadata as md

        out["langgraph_version"] = md.version("langgraph")
        llm_cfg = job.get("llm", {})
        if llm_cfg.get("provider") == "offline_scripted":
            llm = scripted_model(job.get("scripted_options", {}))
        else:
            from tradingagents.llm_clients.factory import create_llm_client

            direct = json.loads(md.distribution("tradingagents").read_text("direct_url.json") or "{}")
            out["llm_factory_upstream_commit"] = direct.get("vcs_info", {}).get("commit_id")
            handler = bridge.usage_handler(int(job.get("max_llm_calls", 8)), int(job.get("max_prompt_tokens", 8000)))
            llm = create_llm_client(llm_cfg["provider"], llm_cfg["model_id"], llm_cfg.get("base_url"),
                                    temperature=llm_cfg.get("temperature", 0.0), max_tokens=int(llm_cfg.get("max_tokens", 2000)),
                                    max_retries=0, callbacks=[handler]).get_llm()
        graph = build_graph(job, llm, [])
        final = graph.invoke({"drafts": {}, "critique": None, "revision": None, "synthesis": None, "calls": 0, "events": []})
        out.update(status="OK", drafts=final["drafts"], critique=final.get("critique"), revision=final.get("revision"),
                   synthesis=final.get("synthesis"), calls=final.get("calls", 0), events=final.get("events", []))
    except Exception as exc:  # noqa: BLE001
        out.update(status="ERROR", error=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc(limit=6))
    finally:
        uninstall()
    if handler is not None:
        bridge.AUDIT["llm_calls"] = handler.calls
        bridge.AUDIT["prompt_tokens"] = handler.prompt_tokens
        bridge.AUDIT["completion_tokens"] = handler.completion_tokens
        bridge.AUDIT["tokens_complete"] = handler.calls > 0 and handler.calls_with_usage == handler.calls
    out["audit"] = bridge.AUDIT
    out["duration_seconds"] = round(time.time() - t0, 3)
    Path(a.output).write_text(json.dumps(out, ensure_ascii=False, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
