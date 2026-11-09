# Transcript of AI Coding Assistant Collaboration

## Session Overview
- **Assistant Used:** Gemini Coding Collaborator & Antigravity Autonomous Agent (Nov 2026)
- **Objective:** Architect, implement, audit, and validate the local **Errand Agent** system: deterministic 8-tool mock environment, two-layer zero-trust approval gate, RFC-4122 UUIDv5 write-ahead idempotency journal, persistent memory & SQL-filtered tiktoken context compiler, proactive cancellation monitor, multi-provider LLM adapter with JSON cassettes, dual demo UIs (FastAPI split-screen + Streamlit two-column), OpenTelemetry tracing, Prometheus metrics, Model Context Protocol (MCP) integration, and 33-test evaluation suite.

## Key Engineering Prompts & Verification Iterations

1. **Iteration 1 — Requirements Decomposition & Architecture Selection:**
   - Audited `Orange_Engineering_Assignment.pdf` and `PRD.md` across all 8 required mock tools (`search_providers`, `get_provider`, `hold_slot`, `book`, `get_booking_status`, `cancel`, `reschedule`, `list_bookings`).
   - Chose a single-agent Python Finite State Machine (`ErrandAgentOrchestrator`) backed by SQLite WAL (`src/db.py`) over opaque multi-agent frameworks to avoid inter-agent latency, eliminate context duplication, and enforce strict deterministic bounds on model turns (`max_model_turns_per_run=5`), tool calls (`max_tool_calls_per_run=8`), wall-clock time (`max_wall_clock_seconds=20.0`), and context budget (`max_context_tokens=1200`).

2. **Iteration 2 — Code-Enforced Two-Layer Approval Gate & Drift Invalidation:**
   - Built **Layer 1 (Schema Exposure Gate):** `ApprovalGate.get_allowed_tools()` exposes only read tools (`search_providers`, `get_provider`, `hold_slot`, `get_booking_status`, `list_bookings`), withholding mutating tools (`book`, `cancel`, `reschedule`) until explicit affirmative confirmation is obtained.
   - Built **Layer 2 (SHA-256 Proposal Hash Gatekeeper):** Canonical SHA-256 digest over `(mutation_type, provider_id, booking_id, slot, final_price)` with strict out-of-band turn verification (`approved_at_turn > proposed_at_turn`).
   - Diagnosed and fixed **Bug #1** where `hold_slot` price drift initially bypassed re-confirmation when `final_price` was omitted from the digest; guarded with `test_bug1_price_drift_invalidates_approval`.

3. **Iteration 3 — Write-Ahead RFC-4122 UUIDv5 Idempotency Journal & Timeout Recovery:**
   - Implemented `book()` with seeded and configurable 70% success, 15% `slot_taken`, 7.5% timeout-after-commit, and 7.5% timeout-before-commit outcomes (verified statistically across 200 seeded runs in `EVAL-26`).
   - Built `_reconcile_booking_timeout()` to always query `get_booking_status(idempotency_key)` before deciding whether to retry or report success.
   - Fixed **Bug #2** where random UUIDs inside retries caused duplicate bookings on timeouts by generating deterministic RFC-4122 UUIDv5 keys (`uuid.uuid5(uuid.NAMESPACE_OID, ...)`) written to `idempotency_journal` prior to execution; guarded with `test_bug2_timeout_idempotent_recovery`.

4. **Iteration 4 — Persistent Memory & SQL-Filtered Context Compiler:**
   - Seeded 56 memories (`EXPLICIT`, confirmed `INFERRED`, unconfirmed `INFERRED`, and `forgotten=1`) plus a 40-turn prior conversation.
   - Enforced a direct SQLite pre-filter (`WHERE forgotten = 0 AND (source_type = 'EXPLICIT' OR confirmed = 1)`) guaranteeing unconfirmed inferred facts and forgotten memories never enter relevance ranking.
   - Fixed **Bug #3** where keyword overlap previously leaked unconfirmed inferred habits into the prompt; guarded with `test_bug3_unconfirmed_inferred_never_sent`.

5. **Iteration 5 — Deep Diagnostic & Context Budget Sliding Window Resolution:**
   - Identified root cause of turn-2 failures: tool-schema allowlist was appended after prompt history had already been packed to 1,200 tokens, triggering `context_budget_exceeded`. Integrated `allowed_tools` directly into `MemoryAndContextManager.compile_prompt_context()` so history sliding naturally respects the 1,200-token cap.
   - Identified material edit check bug where re-stating an existing constraint (e.g. budget=1500) triggered false-positive proposal invalidations. Updated `has_material_change()` to check actual value inequality.
   - Expanded evaluation suite to 30 scenarios (`EVAL-01`..`EVAL-30`) + 3 bug regressions (`BUG-01`..`BUG-03`) = 33 automated tests, achieving 100% pass rate.

6. **Iteration 6 — Multi-Provider LLM Engine & Golden JSON Cassettes:**
   - Extended `src/llm_client.py` to support Anthropic (`claude-3-5-haiku-latest`), xAI Grok (`grok-3-mini`), OpenAI (`gpt-4o-mini`), Groq, Gemini, and OpenRouter.
   - Built golden JSON cassette RECORD and REPLAY engine backed by `fixtures/eval_cassettes.json` and `tests/scripted_llm.py` for reproducible zero-network evaluation.

7. **Iteration 7 — Dual Demo UI (FastAPI Split-Screen + Streamlit Two-Column) & Automated Verification:**
   - Built the flagship split-screen FastAPI Agentic Studio (`src/app_server.py` + `static/index.html`) with real-time SSE trace streaming, FSM pipeline bar, `IntentSlots` pills, cryptographic proposal card, chaos controls, and guided walkthrough.
   - Built native two-column Streamlit studio (`streamlit_app.py` using `st.columns([1, 1])`).
   - Implemented `check_web_app.py` utilizing FastAPI `TestClient` to verify all REST endpoints, proactive cancellations, memory actions, and Streamlit components automatically.

8. **Iteration 8 — Enterprise Observability, Telemetry, MCP Server & UI/UX Elevation (Nov 2026 Standards):**
   - **Enterprise Prometheus Telemetry & Probes:** Implemented `src/metrics.py` emitting Prometheus text format metrics at `/metrics` tracking turns, token consumption, latencies, and tool errors. Implemented Kubernetes `/healthz`, `/livez`, and `/readyz` probes.
   - **OpenTelemetry-Compatible Tracing:** Upgraded `src/trace_logger.py` to record 32-hex `trace_id`, `span_id`, `parent_span_id`, duration in ms, OpenTelemetry tags, baggage, and W3C `traceparent` headers.
   - **Model Context Protocol (MCP) Server Integration:** Developed `src/mcp_server.py` with standard JSON-RPC 2.0 (`/mcp` and `/mcp/rpc`) and REST endpoints (`/mcp/tools`, `/mcp/call`, `/mcp/manifest`, `/mcp/resources`), allowing enterprise agent runners, Claude Desktop, and Cursor to interface directly with all 8 Errand Agent tools.
   - **Enterprise Security & Reliability Layer:** Developed `src/security.py` featuring sliding-window rate limiting (120 req/min), API key validation (`X-API-Key`), 1MB payload ceiling, input sanitization, and RFC-7807 uniform Problem Details error handling.
   - **Enterprise UI / UX Upgrade:** Upgraded `static/index.html` and `streamlit_app.py` with modern glassmorphic theme, interactive visual FSM pipeline with tooltips and glow effects, structured filterable trace viewer with search and JSONL export, rich provider cards with star ratings and slot chips, spotlighted untrusted review containers, interactive MCP tool runner, and live Prometheus telemetry explorer.
   - **Automated Verification:** Added `tests/test_enterprise_features.py` testing health probes, Prometheus exposition, MCP JSON-RPC, rate limiting, and API keys. The full pytest suite grew to 54 tests (100% PASS), and `run_evals.py` maintained 33/33 scenarios (100% PASS).

9. **Iteration 9 — Adversarial Hardening, Deep MCP Protocol Alignment, Memory & Context Inspector Modal, and Live Rate Limiting Verification:**
   - **MCP Protocol Hardening & Tool Whitelisting:** Enforced strict tool execution whitelist restricting invocations to the 8 declared MCP tools, preventing arbitrary internal attribute access. Added support for resource `errand://system/fsm-state`, `prompts/get` template rendering, and standard JSON-RPC 2.0 batch array processing.
   - **Memory & Context Inspector Modal:** Built a dedicated glassmorphic Memory & Context Inspector modal in `static/index.html` featuring a live tiktoken budget meter (cl100k_base vs 1,200 cap), scope filters (All 56, Core 6, Included in Prompt, Excluded Facts), and byte-level exclusion audit reasons.
   - **UI Audit Correction:** Fixed functional bug where memories excluded due to token budget caps were mistakenly labeled as 'INCLUDED IN PROMPT'. Now correctly reflects `EXCLUDED: Token Cap (250 tok) / Low Overlap`, `FORGOTTEN (SQL Pre-Filter Hard Excluded)`, and `BLOCKED (Unconfirmed Inferred Fact)`.
   - **Active Proposal TTL Countdown Timer:** Added live countdown timer on active proposals (600s TTL), turning amber at <120s with pulse warning, and resetting on invalidation.
   - **Rich Provider Cards:** Expanded chat card components to render verified cards with ratings, fixed pricing, and interactive slot chips for Electrician and AC Servicing in addition to Plumbing.
   - **Streamlit Memory Governance Upgrade:** Enhanced `streamlit_app.py`'s `tab_memory` with a structured dataframe showing exact exclusion audit reasons across all seeded memories.
   - **Security Hardening & Rate Limiting Enforcement:** Neutralized Trojan Source bidirectional unicode overrides (`\u202A`–`\u202E`, `\u2066`–`\u2069`), safeguarded `Content-Length` header parsing, and validated HTTP 429 Too Many Requests enforcement under excessive load with standard `Retry-After` headers.
   - **SSE Stream Resilience:** Fixed stream generator pointer desync on session reset, and set standard anti-buffering headers (`Cache-Control: no-cache`, `X-Accel-Buffering: no`).
   - **Prometheus Error Metrics:** Added explicit `errand_agent_errors_total` counter metric family categorized by HTTP and orchestrator turn failures.
   - **Verification Pass:** Expanded test suite to 60 tests (100% PASS in `uv run pytest`), 33/33 evals passing in `run_evals.py`, and 100% verification in `check_web_app.py`.

10. **Iteration 10 — Full 363-Suite Adversarial Hardening & Enterprise Telemetry Alignment (Nov 9, 2026):**
   - **Typed Cascade Exception Preservation:** Hardened `src/llm_client.py` (`_call_gemini_agentic`) to record and re-raise the exact underlying typed exception (`RateLimitError`, `QuotaExceededError`, `ProviderNetworkError`) rather than collapsing to generic `LLMProviderError`, ensuring OpenTelemetry `provider_cascade` span telemetry accurately tags the root cause.
   - **Dual-Mode Self-Contained Test Harness:** Enhanced `tests/test_adversarial_m3_gatekeeper.py` with intelligent `_server_is_live()` detection that connects to live uvicorn port 8000 when active, while seamlessly defaulting to in-process `TestClient(app)` during offline CI, guaranteeing zero-network test execution across all 363 tests.
   - **Full Suite Convergence (100% Pass):** Ran the complete test suite across all 22 test modules: **363 passed in 53.56s** with zero errors. All 33 evaluation scenarios passed (`run_evals.py`), `check_web_app.py` passed 100%, and `DESIGN_NOTE.md` maintained strictly 38 content lines (<= 1 page limit).

11. **Iteration 11 — Live ReAct Loop Reschedule Intent Routing & Target Disambiguation:**
   - **Root Cause Diagnosis:** Identified why user prompt `"Move my AC servicing to Saturday 10:00"` created a duplicate booking (`bk_75c223a9`) while leaving original `bk_upcoming_ac_01` intact: in live ReAct mode (`_run_agentic_react_loop`), explicit non-discovery intents were not routed before entering the LLM loop; under zero-trust Layer 1 gating (`ApprovalGate.get_allowed_tools(None)`), mutating tool `reschedule` was withheld, causing the model to follow the general search workflow (`search_providers` + `hold_slot` + `book`). Furthermore, `_prepare_reschedule_proposal()` naively selected `target = active[0]`, which could target the wrong service when multiple bookings existed.
   - **Deterministic Pre-Routing & Disambiguation:** Updated `_run_agentic_react_loop()` in `src/orchestrator.py` to route explicit non-discovery intents (`reschedule`, `cancel`, `memory_op`, `repeat_last_provider`) directly to their dedicated handlers; updated `_prepare_reschedule_proposal()` and `_prepare_cancel_proposal()` to disambiguate target bookings deterministically using `self.slots.target_booking_id` and `self.slots.service` before falling back to `active[0]`; injected active bookings context and reschedule guidance into `compile_prompt_context()` in `src/memory_context.py`; and enhanced `ScriptedEvalLLM.parse()` in `src/scripted_engine.py` to recognize natural queries like `"what are my upcoming bookings?"` and explicit `bk_` IDs.
   - **Guarding Test & Verification Pass:** Authored guarding regression test `test_react_reschedule_preserves_single_booking_across_multiple_services` in `tests/test_adversarial_cascade_react.py`. Achieved 100% pass rate across all **364 / 364 pytest cases** and all **33 / 33 evaluation scenarios** (`run_evals.py`). `DESIGN_NOTE.md` strictly verified at 38 content lines (<= 1 page limit).

12. **Iteration 12 — Enterprise UI/UX Design System Remediation & Observability Flamegraph (Nov 9, 2026):**
   - **Collapsible Provider History & Slot Overlap Fix:** Resolved vertical card overflow in `frontend/src/components/chat/MessageItem.tsx` where historical provider candidate cards pushed subsequent messages and left clipped slot buttons (`2026-11-10 10:00`) at the top of the chat viewport. Historical provider cards are now collapsed by default under `Provider Candidates Evaluated (N) ▾` with compact slot pill formatting.
   - **Modern Collapsible Thinking Pills:** Replaced bulky static "MODEL CHAIN-OF-THOUGHT" boxes on every assistant turn with a sleek collapsible thinking accordion (`✨ Thought for Xms • N reasoning steps ▾`), grouping internal reasoning and tool audit trails into an elegant expandable drawer.
   - **Themed Intent Slots Bar:** Refactored `frontend/src/components/chat/SlotsBar.tsx` to eliminate raw snake_case (`this_week` → `This Week`, `morning (08:00-12:00)` → `Morning (08:00–12:00)`), applying themed color tokens (sky, emerald, indigo, cyan, amber) with high-contrast turn counter badges.
   - **Observability Deck Tab Layout Rebalance:** Fixed horizontal clipping of the 5th tab `MCP Tools (8)` in `frontend/src/components/observability/ObservabilityDeck.tsx` and `App.tsx` by rebalancing the split ratio to 50%/50% and applying compact padding with smooth horizontal scroll containment.
   - **Flamegraph Proportional Duration Bars & Filters:** Upgraded `frontend/src/components/observability/TraceWaterfall.tsx` with proportional latency flame bars (tools in amber, state in purple, errors in red) and one-click category filtering (`All`, `Tools`, `FSM`, `Errors`).
   - **Turn Limit Fallback & Direct Bookings Routing:** Updated `src/orchestrator.py` to route `list_bookings` deterministically in `<5ms` with 0 tokens, and added proposal recovery fallback to present held proposals if model turn limits are reached, raising default `max_model_turns_per_run` from 5 to 8.
   - **Verification Pass:** Recompiled React/Tailwind frontend production bundle via Vite with 0 TypeScript errors. Achieved 100% PASS across all **365 pytest tests** and all **33 evaluation scenarios** (`run_evals.py`). `DESIGN_NOTE.md` strictly verified at 38 content lines (<= 1 page limit).
