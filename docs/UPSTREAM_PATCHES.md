# Upstream patches — TradingAgents (B08, R07)

Pinned upstream: `TauricResearch/TradingAgents` @ `9fa1196fffbcfd9de5146d2dd2869df84ab99838` (0.7.1).
No other SHA is used. **No upstream file is modified.** All changes are runtime hooks applied inside the
isolated worker process by `src/aqe/context/ta_bridge.py`, before the graph is constructed.

Verified at install: `.venv-ta/.../tradingagents-0.7.1.dist-info/direct_url.json` → `commit_id = 9fa1196…`.
The bridge re-checks this at every run and refuses to run on a different commit.

| # | Hook | Upstream location (at pinned SHA) | Why | Verified by |
|---|---|---|---|---|
| P1 | `config["memory_log_path"] = None` (existing key) | `memory/log.py` (`TradingMemoryLog`), `trading_graph.py` `_memory_step` | Disables memory read/write, pending-decision logging, settlement and reflection for historical/replay runs (B08.3) | bridge asserts `g.memory_log._log_path is None`; `tests/test_context_risk.py::test_real_tradingagents_graph_offline_isolated` |
| P2 | `checkpoint_enabled=False`, per-job `results_dir` / `data_cache_dir` (existing keys) + `TRADINGAGENTS_*` env cleared + cwd = job dir | `default_config.py`, `graph/checkpointer.py`, `__init__.py` (`load_dotenv`) | No checkpoint/cache/state-log sharing across as-of/fold/portfolio; no `.env` leakage into config (T07) | job dir layout under `{data_root}/context/ta_runs/<job>` |
| P3 | Register vendor `aqe_evidence` in `dataflows.router.VENDOR_METHODS[*]` and select it in `data_vendors` (all categories) and `tool_vendors` (all methods) | `dataflows/router.py` | Every routed tool serves only the read-only EvidenceBundle snapshot (with `[ev:<id>]` tags) | audit `tool_calls[*].served_by == "aqe_evidence"` |
| P4 | Monkeypatch `dataflows.vendors.yahoo.snapshot.load_ohlcv` → daily OHLCV built from the bundle | `vendors/yahoo/snapshot.py` (router bypass) | `get_verified_market_snapshot` would otherwise call yfinance directly | audit entry `load_ohlcv(patched)` |
| P5 | Monkeypatch `agents.analysts.sentiment_analyst.fetch_stocktwits_messages` / `fetch_reddit_posts` → "not configured" | sentiment analyst (router bypass) | No scraping; social source is NOT_CONFIGURED | audit entry `social_feed(patched)` when the social analyst is selected |
| P6 | Monkeypatch `graph.trading_graph.resolve_instrument_identity` → fixed label | `trading_graph.py` / `agents/context.py` | Avoids yfinance identity lookup | no network attempt recorded |
| P7 | Monkeypatch `memory.settlement.get_closes` → raises `GuardViolation` | `memory/settlement.py` | Defence in depth: settlement must never fetch prices | `guard_violations` must be empty |
| P8 | Socket/DNS guard (hardened 2026-11-11, review F03): only allow-listed names resolve; `connect`/`connect_ex`/`sendto`/Windows IOCP `ConnectEx` may only reach IPs those names resolved to (direct IP literals denied even when the allow list is non-empty); loopback only if a loopback name is allow-listed; `socket.socketpair` self-pipe exempt; proxy env vars → run not proven isolated | process-wide | Any residual network path becomes a recorded denial → assessment `UNVERIFIED` (T08) | `tests/test_review_fixes.py::test_f03_*`, `tests/test_context_risk.py::test_network_guard_blocks_and_records` |
| P10 | Real-LLM mode only: LangChain callback passed via the existing `TradingAgentsGraph(callbacks=...)` argument counts LLM calls/tokens and aborts beyond `max_llm_calls_per_job`; existing config key `max_tokens` set from `context.llm.max_output_tokens` | `trading_graph.py` constructor (callbacks forwarded to LLM clients) | Budget reservation/settlement (review F04) | `tests/test_review_fixes.py::test_f04_*` (offline; not yet exercised against a real provider) |
| P9 | Offline contract mode only: monkeypatch `graph.trading_graph.create_tier_client` with a scripted chat model (same pattern as upstream `tests/test_graph_end_to_end.py`) | `llm_clients/factory.py` | Run the real graph without network/API key to verify wiring | offline run 2026-11-10: 12 scripted LLM calls, 7 tool calls all `aqe_evidence`, 0 network attempts |

Known residual limits
- `trade_date` is a date string. Point-in-time restriction comes from the AQE bundle (exact `as_of`), not from TradingAgents.
- The upstream full-state JSON log is still written (no flag exists) but only into the per-job directory.
- LLM parametric knowledge may post-date `as_of`; historical LLM evaluation is recorded with this limitation and forward (shadow) validation is required.
- Token usage is captured by the P10 callback when the provider reports it; if any call lacks usage the job cost is `unknown`, the reservation is charged and further paid calls that day are blocked until reconciled. Not yet verified against a real provider.
- Prompts are upstream's; the prompt hash records `upstream-prompts@<sha>`.
