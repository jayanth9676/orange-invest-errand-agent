# Errand Agent — Architectural Design Note (Max 1 Page)

## 1. Explicit Workflow State Machine & Orchestration Trade-Offs
The system decouples **probabilistic natural language understanding** (intent classification and slot-delta extraction via LLM) from **deterministic state and side-effect enforcement** using a single-agent Python Finite State Machine (`ErrandAgentOrchestrator`).
- **Single-Agent FSM vs. Multi-Agent Graph:** A single stateful orchestrator avoids inter-agent handoff latency, eliminates redundant context serialization across sub-agents, and makes enforcing a strict 1,200-token context budget deterministic.
- **State Model:** Transitions explicitly across `IDLE`, `COLLECTING_SLOTS`, `SEARCHING_PROVIDERS`, `HOLDING_SLOT`, `AWAITING_APPROVAL`, `EXECUTING_MUTATION`, `RECONCILING_TIMEOUT`, `PROACTIVE_RECOVERY`, `COMPLETED`, and `SAFE_ABORT`.
- **Delta Slot Tracking & Bounded Execution:** `IntentSlots.merge_delta()` updates only newly supplied or edited fields while preserving unrelated context and never re-asking supplied facts. Hard per-run limits (`max_model_turns=5`, `max_tool_calls=8`, `max_wall_clock_seconds=20.0`, `max_mutation_retries=2`) plus a 350-token user-input admission limit guarantee clean exits to `SAFE_ABORT` without hanging, silent truncation, or silent failure.

## 2. Two-Layer Approval Enforcement & Untrusted Tool Output Isolation
Consequential tools (`book`, `cancel`, `reschedule`) cannot be invoked directly by LLM output:
- **Layer 1 (Schema Exposure Gate):** `ApprovalGate.get_allowed_tools()` exposes only read tools (`search_providers`, `get_provider`, `hold_slot`, `get_booking_status`, `list_bookings`) to the model until a proposal is explicitly affirmed.
- **Layer 2 (SHA-256 Proposal Hash Gatekeeper):** On proposal turn $T$, `ApprovalGate.create_proposal()` binds `(mutation_type, provider_id, booking_id, slot, final_price)`. On turn $T+k$, `assert_mutation_authorized()` blocks execution unless `approved_at_turn > proposed_at_turn`, `invalidated == False`, and runtime arguments match the approved digest. If `hold_slot` drifts price or slot, `check_material_drift()` immediately invalidates approval and returns to `AWAITING_APPROVAL`.
- **Spotlighting Untrusted Reviews:** `MockToolEnvironment.sanitize_tool_output()` wraps free-text reviews inside `<untrusted_review_data trust='none' mode='data_only'>` tags and flags imperative patterns so prompt injections remain passive data.

## 3. RFC-4122 UUIDv5 Idempotency, Crash Recovery & Timeout Reconciliation
Before calling `book`, `cancel`, or `reschedule`, the orchestrator writes a `PENDING` record to the SQLite WAL `idempotency_journal` keyed by a deterministic RFC-4122 UUIDv5 (`uuid.uuid5(uuid.NAMESPACE_OID, f"{session_id}|{mutation_type}|{target_id}|{slot}|{proposal_hash}")`).
- **Timeout-After-Success Reconciliation:** When `book` raises `booking_timeout` (15% fault rate: 7.5% committed, 7.5% uncommitted), the orchestrator transitions to `RECONCILING_TIMEOUT` and queries `get_booking_status(idempotency_key)` before deciding whether to retry or report success. If `CONFIRMED`, it marks the journal `COMPLETED` without re-calling `book`; if `NOT_FOUND`, it retries `book` once with the identical UUIDv5 key.
- **Restart & Duplicate Confirmation Safety:** On startup, `reconcile_pending_journal_on_startup()` resolves any `PENDING` journal entry via `get_booking_status`. Repeated confirmations in `COMPLETED` state return the journaled record without re-executing tools.

## 4. Persistent Memory, Token Budget & Nov 2026 Enterprise Capabilities
- **SQL Pre-Filter & Token Budget:** Cross-session preferences are tagged as `EXPLICIT` or `INFERRED` alongside `confirmed` and `forgotten` flags. Seeded with 56 memories and 40-turn history, `MemoryAndContextManager.compile_prompt_context()` enforces a hard 1,200-token cap via `tiktoken` (`cl100k_base`), executes a SQL pre-filter (`WHERE forgotten = 0 AND (source_type = 'EXPLICIT' OR confirmed = 1)`), and logs exact included/excluded memory IDs to `context_audit_log`.
- **OpenTelemetry-Grade Tracing:** `TraceLogger` injects 32-hex `trace_id`, `span_id`, `parent_span_id`, duration in ms, OpenTelemetry semantic tags, baggage, and W3C `traceparent` headers, exportable to JSONL via `/api/export-trace`.
- **Prometheus Telemetry (/metrics) & Health Probes (/healthz):** Thread-safe metrics collector emits standard Prometheus exposition metrics tracking turns, token consumption, latencies, and tool errors. `/healthz`, `/livez`, and `/readyz` provide cloud-native liveness/readiness verification.
- **Model Context Protocol (MCP) Server:** Implements standard JSON-RPC 2.0 (`/mcp`) and REST catalog endpoints exposing all 8 tool contracts with full JSON schemas, resources, and prompt templates for external agents and MCP clients.
- **Enterprise Security Middleware:** Sliding-window rate limiter (120 req/min), API key validation (`X-API-Key`), 1MB payload limits, and RFC-7807 uniform Problem Details error responses.

## 5. Annotated Timeout-After-Success Trace Excerpt (`EVAL-08`)
```json
// 1. User confirms proposal -> Hold acquired at approved price (₹1,100)
{"trace_id": "c4b967e89104e1bc2a94f61b0a884f1a", "span_id": "span_60d3e18634d7", "event_type": "tool_call", "tool_name": "hold_slot", "arguments": {"provider_id": "prov_plumb_1", "slot": "2026-11-09 09:00"}, "sanitized_result": {"hold_id": "hold_0f8976792a", "quoted_price": 1100, "price_changed": false}, "status": "ok"}
// 2. Write-ahead UUIDv5 journal written -> book() times out after backend committed booking
{"trace_id": "c4b967e89104e1bc2a94f61b0a884f1a", "span_id": "span_129774c78c0a", "event_type": "tool_call", "tool_name": "book", "arguments": {"hold_id": "hold_0f8976792a", "idempotency_key": "idem_328de9ad-5ebf-5801-84fc-e7354c8a8e50"}, "sanitized_result": {"error_code": "booking_timeout", "message": "Network timeout after backend committed booking"}, "status": "error"}
// 3. State transitions to RECONCILING_TIMEOUT -> Queries authoritative get_booking_status before retrying
{"trace_id": "c4b967e89104e1bc2a94f61b0a884f1a", "span_id": "span_7b5fefac88dc", "event_type": "state_transition", "from_state": "EXECUTING_MUTATION", "to_state": "RECONCILING_TIMEOUT", "reason": "book() timed out; querying get_booking_status"}
{"trace_id": "c4b967e89104e1bc2a94f61b0a884f1a", "span_id": "span_cf2ac9b08304", "event_type": "tool_call", "tool_name": "get_booking_status", "arguments": {"hold_id_or_idempotency_key": "idem_328de9ad-5ebf-5801-84fc-e7354c8a8e50"}, "sanitized_result": {"status": "CONFIRMED", "booking_id": "bk_d9e0c667", "slot": "2026-11-09 09:00", "price": 1100}, "status": "ok"}
// 4. Verified CONFIRMED -> Transitions to COMPLETED without duplicate book() call
{"trace_id": "c4b967e89104e1bc2a94f61b0a884f1a", "span_id": "span_303fd2554c5c", "event_type": "state_transition", "from_state": "RECONCILING_TIMEOUT", "to_state": "COMPLETED", "reason": "Timeout resolved via get_booking_status: booking bk_d9e0c667 CONFIRMED"}
```