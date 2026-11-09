## Comprehensive File-by-File Analysis of errand-agent Project

I've read all 19 source files and the PRD. Here's my thorough analysis:

---

### 1. `src/config.py` (63 lines)
**Key elements:** `_load_project_env()`, `_csv()`, `AgentConfig` dataclass, `resolved_provider()`, `is_offline()`

**Issues:**
- **L10:** `.env` path computation uses `parents[2]` which assumes a fixed directory structure. Fragile if file is moved.
- **L17:** `line.split("=", 1)` doesn't handle values containing `=` signs after stripping quotes properly (e.g., base64 encoded keys with trailing `=`).
- **L23:** `_load_project_env()` called at module import time as a side effect — can cause issues in testing if imported before test env setup.
- **L52:** `max_context_tokens: int = 1200` — **PRD specifies 1800** (PRD §4.4: `max_tokens=1800`). Hardcoded at 1200 is a deviation.
- **L54:** `max_user_input_tokens: int = 350` — not mentioned in PRD.

**PRD Deviations:**
- PRD §4.4 says `ContextCompiler.build_messages(max_tokens=1800)` but code uses `max_context_tokens=1200` (L52).

---

### 2. `src/schemas.py` (92 lines)
**Key elements:** `AgentState`, `MutationType`, `MemorySource`, `IntentSlots`, `ApprovalProposal`

**Issues:**
- **L31:** `intent_type: str = "book"` — PRD says `book | reschedule | cancel | memory_op | status`, but code uses `list_bookings` instead of `status` throughout.
- **L40:** `requested_slot` field not in PRD's `IntentSlots` schema (PRD §2.1 L83-93). It's an added field.
- **L42-45:** `model_dump` override with `hasattr(super(), "model_dump")` is unnecessary — Pydantic v2 always has it (project requires `pydantic>=2.6.0`).
- **L50:** In `merge_delta`, `if v is not None and k in updated` — the `k in updated` guard prevents adding new keys, which differs from PRD's version at L96-101 that doesn't have this guard.
- **L79:** `invalidation_reason` field added to `ApprovalProposal` but not in PRD schema.

**PRD Deviations:**
- PRD `IntentSlots` doesn't include `requested_slot` or `preferred_provider_id` fields (these are extensions).
- PRD uses `status` intent type; code uses `list_bookings`.

---

### 3. `src/db.py` (387 lines)
**Key elements:** `LocalDatabase`, 8 SQLite tables, seed data with 12 providers, 6 core memories + 50 bulk, 40-turn history

**Issues:**
- **L10:** `check_same_thread=False` — necessary for FastAPI but silently disables SQLite's thread safety checks. Could cause data corruption under concurrent writes without external locking.
- **L17-112:** Creates 8 tables, but PRD specifies only 6 tables (§2.2). Extra tables: `cancel_attempts` (L56-59), `conversation_history` (L86-92), `session_state` (L104-111). These are reasonable additions but PRD deviations.
- **L130-132:** **Intentional adversarial prompt injection baked into seed data** in `prov_plumb_3`'s reviews. This is for testing EVAL-04.
- **L132:** The adversarial review in `reviews_json` is intentional test data, not a bug.
- **L199-246:** `_seed_memories_and_history` creates 56 total memories (6 core + 50 bulk). **PRD says 55 memories** (§4.4).

**Security:**
- **L10:** No connection pooling or transaction isolation for concurrent FastAPI requests.

---

### 4. `src/mock_tools.py` (335 lines)
**Key elements:** `ToolExecutionError`, `FaultConfig`, `MockToolEnvironment` with 8 tool methods, `sanitize_tool_output()`

**Issues:**
- **L161:** Uses `hashlib.md5` for hold_id generation — MD5 is cryptographically broken, though used here only for deterministic ID generation (not security-critical).
- **L151:** Missing `retriable` parameter on `ToolExecutionError` for `provider_not_found` at L151 (defaults to `False`, which is correct).
- **L206-215:** The seeded randomness uses SHA-256 hash of `seed:idempotency_key` — deterministic and matches PRD specification.
- **L224:** `self.fault_cfg.force_book_outcome = "success"` — **Mutating shared state** in `FaultConfig`. This is a side effect that persists across calls. Could cause test interference if tests share FaultConfig instances.
- **L290:** Cancel retry logic checks `self.fault_cfg.persistent_cancel_503` — good extensibility for testing.

**PRD Compliance:** Implements all 8 required tools. ✓

---

### 5. `src/approval_gate.py` (148 lines)
**Key elements:** `ApprovalViolationError`, `ApprovalGate` with static/class methods for hash computation, proposal creation, affirmation evaluation, material drift detection, mutation authorization

**Issues:**
- **L16-23:** `AFFIRMATIVE_PATTERNS` and `NEGATIVE_OR_CHANGE_PATTERNS` are regex-based. The word boundary `\b` approach could have false positives (e.g., "yes" in "yesterday", though `\b` mitigates this).
- **L72:** `current_turn <= proposal.proposed_at_turn` blocks same-turn approvals — correct per PRD §4.1.
- **L83-84:** `classify_affirmation_and_safety` returns both `affirmed` and `has_conflict`. If both match (e.g., "Yes, but change..."), `has_conflict` takes precedence (L72). This is correct behavior.
- **L90:** `hold_slot` is in `read_only` tuple — this is arguably a write operation (creates a hold), but PRD treats it as pre-mutation.

**Quality:** Well-designed with deterministic, code-level enforcement outside the LLM trust boundary. ✓

---

### 6. `src/memory_context.py` (206 lines)
**Key elements:** `MemoryAndContextManager` with token estimation, memory CRUD, `compile_prompt_context()`

**Issues:**
- **L7-10:** `tiktoken` import with silent fallback — reasonable for offline mode.
- **L33:** Fallback token estimation `int(len(words) * 1.35)` — simple but may undercount for code/special chars.
- **L118:** **Critical invariant enforced correctly** — `forgotten == 1` or `(INFERRED and confirmed == 0)` excluded before relevance scoring. This was Bug #3's fix.
- **L128:** Hardcoded query terms include `["area", "time", "morning", "preferred", "electrician", "plumber", "ac_servicing", "proactive"]` — biases relevance scoring towards these keywords regardless of actual query context.
- **L134:** `is_core = 100 if not m["key"].startswith("household_note_")` — hardcoded heuristic for separating core vs bulk memories. Works but brittle.
- **L163:** `total_tokens` counts system block + user message but not the history turns added later, then history is added incrementally. Token math is correct.
- **L18:** `max_context_tokens: int = 1200` — **PRD says 1800** (deviation inherited from config.py).

**PRD Deviations:**
- Context budget is 1200, not 1800 as PRD specifies.

---

### 7. `src/trace_logger.py` (96 lines)
**Key elements:** `TraceLogger` with `_record()`, `log_state_transition()`, `log_model_call()`, `log_tool_call()`, `log_guardrail_event()`

**Issues:**
- **L23:** Uses `hashlib.md5` for span_id — fine for tracing, not security.
- **L34:** File I/O opens and closes file on every event — could be a performance issue with many events. No buffering.
- **L34:** No file locking — concurrent writes could interleave JSONL lines (though single-threaded use is typical).

**Quality:** Clean implementation, matches PRD requirements. ✓

---

### 8. `src/llm_client.py` (179 lines)
**Key elements:** `LLMProviderError`, `INTENT_SCHEMA`, `MultiProviderLLMClient` with provider routing

**Issues:**
- **L14:** `from tests.scripted_llm import ScriptedEvalLLM` — **Production code imports from test module**. This is an anti-pattern. The scripted LLM should be injected or the import should be conditional.
- **L66:** `time.sleep(self.artificial_delay_sec)` — blocks the thread, problematic for async FastAPI.
- **L97:** `provider in {"groq", "grok"}` — PRD says `GROK_API_KEY` and `api.x.ai/v1`, but code uses Groq's API endpoint (`api.groq.com`). **The PRD says xAI Grok (api.x.ai), but the code implements Groq (api.groq.com)** — these are different companies/APIs.
- **L98:** Uses `self.config.groq_api_key` — config field says `groq` but PRD says `GROK_API_KEY`.
- **L106:** Missing Anthropic provider in `_call_live_provider` even though `anthropic_api_key` and `anthropic_model` are in config. Dead config.
- **L133-139:** Uses `urllib.request` instead of `httpx` — project includes `httpx>=0.27.0` in dependencies (pyproject.toml L15) but doesn't use it.
- **L258-261:** `force_infinite_turn_loop` — dangerous debug feature left in production code (L258-261). Could cause infinite recursion if accidentally enabled.

**PRD Deviations:**
- PRD §5: Says `GROK_API_KEY` with `api.x.ai/v1` endpoint. Code uses `GROQ_API_KEY` with `api.groq.com` — **different provider entirely**.
- PRD §7.3: Says to use `openai` library; code uses raw `urllib.request`.

---

### 9. `src/orchestrator.py` (818 lines)
**Key elements:** `ErrandAgentOrchestrator` — the core FSM. Methods: `_load_or_init_session`, `_save_session`, `_transition`, `_invoke_tool`, `reconcile_pending_journal_on_startup`, `handle_user_message`, `_call_model_with_limits`, `_process_turn`, `_search_compare_and_propose`, `_execute_booking_with_hold_and_recovery`, `_reconcile_booking_timeout`, `_execute_cancellation_with_retry`, `_execute_reschedule_with_conflict_handling`

**Issues:**
- **L101-106:** `_run_tool_calls` and `_run_start_ts` are set in `handle_user_message` (L179-181) and `reconcile_pending_journal_on_startup` (L132-133), but `_invoke_tool` at L101 uses them without guaranteed initialization. If `_invoke_tool` is called before these are set, `AttributeError` would occur. However, all call paths set them first.
- **L258-261:** **Infinite recursion bomb**: `if self.llm.force_infinite_turn_loop: while True: self._call_model_with_limits(user_message)` — this would cause stack overflow (recursive call). The `max_model_turns_per_run` guard at L228 should catch it, but the recursion depth could be a problem before the guard triggers.
- **L317-320:** `bypass_approval_attempt` handler uses hardcoded values `"prov_plumb_1"`, `"2026-11-09 09:00"`, `1100` — these are fallbacks when slot_delta doesn't provide values, but they're hardcoded test data in production code.
- **L544-545:** `new_slot = self.slots.requested_slot or "2026-11-14 10:00"` — hardcoded fallback slot is problematic. Should derive from available slots.
- **L562:** `assert self.active_proposal is not None` — using assert in production code. Will be stripped with `python -O`. Should raise an explicit exception.
- **L628:** Idempotency key includes `proposal.payload_hash` — correct per PRD §4.2.
- **L714:** `next_slot = alt_slots[0] if alt_slots else "2026-11-10 10:00"` — another hardcoded fallback date.

**Bugs:**
- **L258-261:** The `force_infinite_turn_loop` causes infinite **recursion** (not iteration), which will hit Python's recursion limit before the model turn guard kicks in. Should use iteration instead.

---

### 10. `src/proactive_worker.py` (122 lines)
**Key elements:** `ProactiveCancellationMonitor` with `handle_provider_cancelled_event()`

**Issues:**
- **L21-22:** Directly accesses `self.orch._run_tool_calls` and `self.orch._run_start_ts` — accessing private members of another class. Should be a public method on orchestrator.
- **L81:** `best = replacements[0] if replacements else search_res["providers"][0]` — falls back to original provider if no replacements found, which is the cancelled provider. Should handle this edge case.
- **L83:** `s.split(" ")[1] >= earliest` — string comparison of times works for "HH:MM" format but is fragile.

**Quality:** Meets PRD §4.5 requirements for proactive cancellation. ✓

---

### 11. `src/app_server.py` (177 lines)
**Key elements:** FastAPI app with endpoints: `/`, `/api/state`, `/api/chat`, `/api/proactive-cancel`, `/api/memory`, `/api/run-evals`, `/api/trace-stream`, `/api/reset`

**Issues:**
- **L20-31:** Global mutable state (`DB`, `ORCH`, `PROACTIVE`, `FAULT_CFG`) — not thread-safe despite `STATE_LOCK`. The `RLock` only protects individual endpoint handlers, not the SSE stream at L148.
- **L20:** `LocalDatabase("errand_demo.sqlite")` — creates a file-based database on import. Side effect at module level.
- **L15-16:** `import run_evals` and `from tests import conftest` — **importing test modules in production server code**. Anti-pattern.
- **L132:** `ORCH._handle_memory_operation(...)` — accessing private method directly.
- **L148-159:** SSE trace stream with `await asyncio.sleep(0.4)` polling loop — no cancellation mechanism. Will run indefinitely per client connection. Memory leak potential.
- **L164-176:** `reset_demo` creates new `LocalDatabase(":memory:")` but the original was file-based (`"errand_demo.sqlite"`). After reset, data is in-memory only and won't survive restarts.
- **L166:** `global DB, ORCH, PROACTIVE` — reassigning globals is thread-unsafe even with `STATE_LOCK`.

**Security:**
- No authentication on any endpoint.
- No rate limiting.
- No CORS configuration.
- `/api/run-evals` endpoint runs tests on the production server — dangerous.

---

### 12. `tests/conftest.py` (49 lines)
**Key elements:** `EVAL_RESULTS` list, `record_eval_result()`, `pytest_sessionfinish()` hook

**Issues:**
- **L3:** `EVAL_RESULTS` is a module-level mutable list — shared state across test runs.
- **L29:** `pytest_sessionfinish` needs to be in `conftest.py` at the root test directory for pytest to auto-discover it. Placement seems correct.

**Quality:** Clean, functional. ✓

---

### 13. `tests/scripted_llm.py` (125 lines)
**Key elements:** `ScriptedEvalLLM.parse()` — deterministic regex-based NLU

**Issues:**
- **L65:** `"ac " in lower` — trailing space means "ac" at end of string won't match. Edge case.
- **L92:** Overlapping time_window: `"morning"` match at L90 sets `morning (08:00-12:00)`, then L92 can override with `after 10:00` if both keywords present. The test messages are designed to avoid this conflict, but it's fragile.
- **L107:** Budget regex `r"(?:under|budget|below|max|₹|rs\.?\s*)\s*([0-9,]{3,5})"` — `{3,5}` limits to 3-5 digit strings, so budgets like ₹99 or ₹100,000 won't parse.

**Quality:** Adequate for deterministic offline eval. ✓

---

### 14. `tests/test_eval_scenarios.py` (723 lines)
**Key elements:** 29 eval scenarios (EVAL-01 through EVAL-29) + `EvalScenariosTestCase`

**Issues:**
- **L718-722:** `EvalScenariosTestCase` runs all test functions — this means pytest discovers both the standalone functions AND the TestCase methods, causing double execution.
- **PRD says 22 scenarios** (§6), but implementation has **29 scenarios** (EVAL-01 through EVAL-29) — exceeds requirements.
- **L627:** `make_agent` creates a full orchestrator per test — no shared state, which is correct for isolation.

**PRD Compliance:** Exceeds PRD's 22-scenario requirement with 29. ✓+

---

### 15. `tests/test_regression_bugs.py` (111 lines)
**Key elements:** 3 bug regression tests (BUG-01, BUG-02, BUG-03) with aliases

**Issues:**
- **L100-103:** Aliases created for PRD naming compliance — good practice.
- **L106-110:** `RegressionBugsTestCase` same double-execution issue as eval scenarios.

**Quality:** Clean, focused regression guards. ✓

---

### 16. `static/index.html` (1410 lines)
**Key elements:** Split-screen dark-mode UI with TailwindCSS CDN, local sandbox fallback, live FastAPI integration

**Issues:**
- **L7:** TailwindCSS loaded from CDN (`cdn.tailwindcss.com/3.4.17`) — external dependency, won't work offline.
- **L329-1407:** ~1000 lines of inline JavaScript — no separation of concerns, no minification.
- **L1186-1218:** `sendMsg()` function catches fetch errors and shows "BACKEND UNAVAILABLE" — good degradation.
- **L1212-1217:** When backend is unavailable, the code shows an error message instead of running local sandbox. The local sandbox logic at L690+ only runs when the API succeeds with local fallback, but the `sendMsg` function at L1214-1216 doesn't fall back to local simulation. However, there's dead code at L1242-1283 (after a `return` statement) that was meant to handle local proactive cancel simulation.
- **L1242-1283:** **Dead code** — after `return;` at L1241, the local simulation code is unreachable.
- **L339-369:** Eval suite data is **hardcoded** in the HTML — not dynamically fetched. These results are stale and will not reflect actual test execution.

**Security:**
- **L7:** Loading scripts from CDN without integrity hashes (no SRI).
- No CSP headers configured.
- innerHTML used extensively without sanitization (L618-626, L1314-1322) — XSS risk if trace data or memory values contain malicious HTML.

---

### 17. `pyproject.toml` (24 lines)
**Issues:**
- **L15:** Lists `httpx>=0.27.0` as dependency but it's never used in the codebase. `urllib.request` is used instead.
- **L11:** Missing `openai` package — PRD §7.3 says to include it.
- **L10:** `requires-python = ">=3.11"` — PRD says Python 3.12+.

---

### 18. `BUGS_POSTMORTEM.md` (20 lines)
**Quality:** Well-written, documents 3 real bugs with root cause, fix, and regression test references. Meets PRD §7.1. ✓

---

### 19. `run_evals.py` (39 lines)
**Issues:**
- **L19:** Filters functions with `name.startswith("test_")` — will also pick up aliases in `test_regression_bugs.py` (L100-103), but `seen_funcs` set (L15) prevents double execution.
- **L29:** Calls `pytest_sessionfinish(None, ...)` — passing `None` as session parameter works because the function only uses `EVAL_RESULTS`.

**Quality:** Clean runner script. ✓

---

## Summary of Critical Issues

### Code Quality / Anti-Patterns
1. **Production imports test code** — `llm_client.py:14`, `app_server.py:15-16`
2. **Hardcoded fallback values** — orchestrator.py:317-320, 544-545, 714
3. **assert in production** — orchestrator.py:562
4. **Infinite recursion bomb** — orchestrator.py:258-261
5. **Dead code** — index.html:1242-1283
6. **Global mutable state** — app_server.py:20-31

### PRD Deviations
1. **Context token budget 1200 vs PRD's 1800** — config.py:52, memory_context.py:18
2. **Groq vs Grok provider confusion** — llm_client.py:97-98 uses Groq API but PRD says xAI Grok
3. **56 memories seeded vs PRD's 55** — db.py:199-233
4. **`list_bookings` intent vs PRD's `status` intent** — schemas.py:31
5. **Missing `openai` dependency** — pyproject.toml
6. **Python >=3.11 vs PRD's 3.12+** — pyproject.toml:10
7. **`httpx` listed but unused** — pyproject.toml:15
8. **29 eval scenarios vs PRD's 22** — exceeds requirement (positive deviation)

### Security Vulnerabilities
1. **No auth on API endpoints** — app_server.py
2. **No CORS configuration** — app_server.py
3. **innerHTML without sanitization** — index.html (XSS risk)
4. **No SRI on CDN script** — index.html:7
5. **Test execution endpoint on prod** — app_server.py:136-144
6. **SQLite `check_same_thread=False`** — db.py:10

### Performance Bottlenecks
1. **File I/O per trace event** — trace_logger.py:34 (no buffering)
2. **Thread-blocking `time.sleep`** — llm_client.py:66
3. **SSE polling loop without cleanup** — app_server.py:148-159
4. **No database connection pooling** — db.py

### Test Coverage Gaps
1. **No tests for app_server.py endpoints** (API integration tests missing)
2. **No tests for Anthropic provider path** (config has it, no tests)
3. **No negative tests for ScriptedEvalLLM** edge cases
4. **No tests for concurrent access** / thread safety
5. **No tests for session_state persistence/recovery** across actual process restarts
6. **Double-execution risk** from TestCase classes wrapping standalone test functions
