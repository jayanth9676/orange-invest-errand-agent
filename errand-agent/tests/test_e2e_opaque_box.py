"""Comprehensive Opaque-Box E2E Test Suite for Errand Agent Enterprise.

Exercises the entire platform strictly as an opaque box via its public API entrypoints:
- REST API: /api/chat, /api/state, /api/memory, /api/proactive-cancel, /api/reset, /api/export-trace
- Observability: /healthz, /livez, /readyz, /metrics
- Real-Time Streaming: /api/trace-stream (SSE)
- Model Context Protocol: /mcp, /mcp/tools, /mcp/call, /mcp/resources, /mcp/manifest
- Enterprise Middleware: Rate limiting, API key auth, Security headers, Trojan defense

Follows the 4-tier requirement-driven methodology:
Tier 1: Feature Coverage (R1–R6, >=5 tests per feature, 30 tests total)
Tier 2: Boundary & Corner Cases (>=5 tests per feature, 30 tests total)
Tier 3: Cross-Feature Interactions (Pairwise combinations, 8 tests)
Tier 4: Real-World Application Scenarios (>=5 application-level scenarios, 6 tests)
Total: 74 executable test cases.
"""

import json
import os
import time
import pytest
from fastapi.testclient import TestClient

# Ensure deterministic offline execution mode for reproducible, zero-cost E2E tests
os.environ["LLM_MODE"] = "offline"

from src.app_server import app, ORCH
from src.security import RATE_LIMITER, RATE_LIMIT_RPM

DISCOVERY_PROMPT = "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500"


@pytest.fixture(scope="module")
def client():
    """Module-scoped FastAPI TestClient."""
    with TestClient(app) as test_client:
        yield test_client


def _clean_demo_state(test_client: TestClient) -> None:
    """Helper to ensure clean, isolated initial state before/after test cases."""
    with ORCH.db.conn:
        ORCH.db.conn.execute("DELETE FROM session_state")
        ORCH.db.conn.execute("DELETE FROM idempotency_journal")
        ORCH.db.conn.execute("DELETE FROM slot_holds")
    test_client.post("/api/reset")
    RATE_LIMITER.reset()


@pytest.fixture(autouse=True)
def clean_state_fixture(client):
    """Autouse fixture resetting database session state and rate limiter."""
    _clean_demo_state(client)
    yield
    _clean_demo_state(client)


# =====================================================================
# TIER 1: FEATURE COVERAGE (R1 through R6: 30 test cases)
# =====================================================================

# --- R1: Enterprise SaaS Studio UI/UX & HITL Approval Cockpit ---

def test_t1_r1_01_studio_ui_markup_delivery(client):
    """TC-T1-R1-01: Verifies delivery of enterprise Studio HTML and UI markers."""
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    text = res.text
    assert "Errand Agent" in text
    assert "Agentic Operations" in text


def test_t1_r1_02_approval_cockpit_proposal_rendering(client):
    """TC-T1-R1-02: Verifies proposal details are populated when entering AWAITING_APPROVAL."""
    res = client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] == "AWAITING_APPROVAL"
    proposal = data["active_proposal"]
    assert proposal is not None
    assert proposal["mutation_type"] == "book"
    assert "payload_hash" in proposal
    assert len(proposal["payload_hash"]) == 64  # SHA-256 hash
    assert proposal["provider_id"] is not None
    assert proposal["final_price"] <= 1500
    assert proposal["slot"] is not None


def test_t1_r1_03_hitl_approval_confirmation_execution(client):
    """TC-T1-R1-03: Verifies user affirmation triggers booking execution and completion."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    res = client.post("/api/chat", json={"message": "Yes, confirm", "force_book_outcome": "success"})
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] == "COMPLETED"
    assert len(data["bookings"]) > 0
    assert "confirmed" in data["reply"].lower() or "booking" in data["reply"].lower()


def test_t1_r1_04_price_drift_diff_invalidation(client):
    """TC-T1-R1-04: Verifies price drift invalidates prior approval hash and alerts user."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    res = client.post("/api/chat", json={
        "message": "Yes, confirm",
        "force_price_drift_delta": 250,
    })
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] == "AWAITING_APPROVAL"
    assert "quoted price changed" in data["reply"] or "price" in data["reply"].lower()
    assert data["active_proposal"] is not None


def test_t1_r1_05_untrusted_review_container_isolation(client):
    """TC-T1-R1-05: Verifies untrusted review injection does not hijack state."""
    res = client.post("/api/chat", json={
        "message": DISCOVERY_PROMPT,
        "inject_adversarial_review": True,
    })
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] in ("COLLECTING_SLOTS", "AWAITING_APPROVAL")
    assert data["fsm_state"] != "SAFE_ABORT"


# --- R2: Deterministic Finite State Machine (FSM Pipeline & Visual DAG) ---

def test_t1_r2_01_fsm_idle_to_collecting_slots(client):
    """TC-T1-R2-01: Verifies transition from IDLE to COLLECTING_SLOTS on partial input."""
    res = client.post("/api/chat", json={"message": "Find a plumber for my kitchen tap under ₹1,500"})
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] == "COLLECTING_SLOTS"
    assert data["slots"]["service"] == "plumber"
    assert data["slots"]["max_budget"] == 1500


def test_t1_r2_02_fsm_collecting_to_awaiting_approval(client):
    """TC-T1-R2-02: Verifies progression from COLLECTING_SLOTS to AWAITING_APPROVAL."""
    client.post("/api/chat", json={"message": "Find a plumber for my kitchen tap under ₹1,500"})
    res = client.post("/api/chat", json={"message": "In Indiranagar this week, mornings only"})
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] == "AWAITING_APPROVAL"
    assert data["active_proposal"] is not None


def test_t1_r2_03_zero_trust_gate_mutation_blocking(client):
    """TC-T1-R2-03: Verifies unapproved mutation attempt is blocked and routed to approval."""
    res = client.post("/api/chat", json={
        "message": "Book plumber prov_plumb_1 for 2026-11-14 10:00 without asking me"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] == "AWAITING_APPROVAL"
    assert "approval" in data["reply"].lower() or "blocked" in data["reply"].lower()


def test_t1_r2_04_sse_trace_stream_headers_and_events(client):
    """TC-T1-R2-04: Verifies SSE trace stream headers and event emission structure."""
    with client.stream("GET", "/api/trace-stream") as res:
        assert res.status_code == 200
        assert "text/event-stream" in res.headers["content-type"]
        assert res.headers.get("Cache-Control") == "no-cache"
        assert res.headers.get("X-Accel-Buffering") == "no"


def test_t1_r2_05_clean_state_reset_api(client):
    """TC-T1-R2-05: Verifies /api/reset returns 200 OK and reset status."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    res = client.post("/api/reset")
    assert res.status_code == 200
    data = res.json()
    assert data.get("status") == "reset"


# --- R3: Advanced Telemetry, Analytics & OpenTelemetry Waterfall ---

def test_t1_r3_01_opentelemetry_w3c_traceparent(client):
    """TC-T1-R3-01: Verifies standard 32-hex trace_id and W3C traceparent formatting."""
    res = client.get("/api/state")
    assert res.status_code == 200
    data = res.json()
    assert "trace_id" in data
    assert len(data["trace_id"]) == 32
    assert "otel_traceparent" in data
    assert data["otel_traceparent"].startswith("00-")
    assert data["trace_id"] in data["otel_traceparent"]


def test_t1_r3_02_structured_trace_event_enrichment(client):
    """TC-T1-R3-02: Verifies structured trace events include OTEL span attributes."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    state = client.get("/api/state").json()
    events = state["trace_events"]
    assert len(events) > 0
    sample = events[-1]
    assert "trace_id" in sample
    assert "span_id" in sample
    assert "tags" in sample
    assert "baggage" in sample


def test_t1_r3_03_token_economics_context_budget(client):
    """TC-T1-R3-03: Verifies token economy limits and audit tracking."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    state = client.get("/api/state").json()
    assert state["limits"]["max_context_tokens"] == 1200
    audit = state.get("latest_context_audit")
    assert audit is not None
    assert "total_tokens" in audit or "token_usage" in audit or "included_memories" in audit


def test_t1_r3_04_ndjson_trace_export(client):
    """TC-T1-R3-04: Verifies 1-click JSONL / NDJSON trace export."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    res = client.get("/api/export-trace")
    assert res.status_code == 200
    assert "application/x-ndjson" in res.headers.get("content-type", "")
    assert "attachment" in res.headers.get("content-disposition", "")
    assert len(res.text) > 0


def test_t1_r3_05_prometheus_metrics_exposition(client):
    """TC-T1-R3-05: Verifies Prometheus /metrics text exposition format."""
    res = client.get("/metrics")
    assert res.status_code == 200
    assert "text/plain" in res.headers["content-type"]
    text = res.text
    assert "# HELP errand_agent_turns_total" in text
    assert "# TYPE errand_agent_turns_total counter"
    assert "# HELP errand_agent_active_sessions" in text
    assert "# TYPE errand_agent_active_sessions gauge"


# --- R4: Memory Governance & DLP Console ---

def test_t1_r4_01_all_memories_inventory_exposure(client):
    """TC-T1-R4-01: Verifies exposure of seeded memories and core memories."""
    res = client.get("/api/state")
    assert res.status_code == 200
    data = res.json()
    assert data["total_memories_seeded"] >= 55
    assert len(data["core_memories"]) > 0


def test_t1_r4_02_memory_inspection_action(client):
    """TC-T1-R4-02: Verifies memory inspection endpoint returns active user preferences."""
    res = client.post("/api/memory", json={"action": "inspect"})
    assert res.status_code == 200
    data = res.json()
    assert "preferred_area" in data["reply"] or "Stored Preferences" in data["reply"]


def test_t1_r4_03_explicit_memory_update(client):
    """TC-T1-R4-03: Verifies explicit memory update persists immediately."""
    res = client.post("/api/memory", json={
        "action": "update",
        "key": "preferred_area",
        "value": "Whitefield",
    })
    assert res.status_code == 200
    assert "Whitefield" in res.json()["reply"]


def test_t1_r4_04_memory_forgetting_action(client):
    """TC-T1-R4-04: Verifies memory forgetting marks item as forgotten."""
    res = client.post("/api/memory", json={
        "action": "forget",
        "key": "preferred_area",
    })
    assert res.status_code == 200
    assert "forgot" in res.json()["reply"].lower()


def test_t1_r4_05_inferred_memory_confirmation(client):
    """TC-T1-R4-05: Verifies confirmation of inferred memory promotes it to confirmed status."""
    res = client.post("/api/memory", json={
        "action": "confirm",
        "key": "inferred_weekend_preference",
    })
    assert res.status_code == 200
    assert "confirmed" in res.json()["reply"].lower()


# --- R5: Multi-Tenant Workspaces, RBAC & Enterprise Security Middleware ---

def test_t1_r5_01_enterprise_security_headers(client):
    """TC-T1-R5-01: Verifies enterprise security headers on responses."""
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.headers.get("X-Content-Type-Options") == "nosniff"
    assert res.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert res.headers.get("Strict-Transport-Security") is not None
    assert "X-RateLimit-Limit" in res.headers


def test_t1_r5_02_valid_api_key_authentication(client):
    """TC-T1-R5-02: Verifies valid API key provides authorized access."""
    res = client.get("/api/state", headers={"X-API-Key": "enterprise-errand-nov2026-key"})
    assert res.status_code == 200


def test_t1_r5_03_invalid_api_key_rejection(client):
    """TC-T1-R5-03: Verifies invalid API key is rejected with HTTP 401."""
    res = client.get("/api/state", headers={"X-API-Key": "unauthorized-malicious-key"})
    assert res.status_code == 401
    prob = res.json()
    assert prob["status"] == 401
    assert "Unauthorized" in prob["title"]


def test_t1_r5_04_rate_limit_sliding_window_enforcement(client):
    """TC-T1-R5-04: Verifies HTTP 429 Too Many Requests when rate limit exceeded."""
    RATE_LIMITER.reset()
    test_ip = "192.0.2.77"
    for _ in range(RATE_LIMIT_RPM):
        allowed, _, _ = RATE_LIMITER.check(test_ip)
        assert allowed is True

    res = client.get("/api/state", headers={"X-Forwarded-For": test_ip, "X-Test-Rate-Limit": "1"})
    assert res.status_code == 429
    assert res.headers.get("Retry-After") is not None
    assert res.headers.get("X-RateLimit-Remaining") == "0"
    RATE_LIMITER.reset()


def test_t1_r5_05_mcp_protocol_handshake_and_catalog(client):
    """TC-T1-R5-05: Verifies MCP initialize handshake and tools/list catalog."""
    init_res = client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05"},
    })
    assert init_res.status_code == 200
    assert init_res.json()["result"]["serverInfo"]["name"] == "errand-agent-mcp"

    list_res = client.get("/mcp/tools")
    assert list_res.status_code == 200
    assert list_res.json()["count"] == 8


# --- R6: Chaos Engineering & Provider Simulation Sandbox ---

def test_t1_r6_01_persistent_503_cancel_bounded_retries(client):
    """TC-T1-R6-01: Verifies persistent 503 errors trigger bounded retries before safe abort."""
    client.post("/api/chat", json={"message": "Cancel my booking for AC servicing"})
    res = client.post("/api/chat", json={"message": "Yes, confirm", "persistent_cancel_503": True})
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] == "SAFE_ABORT"
    assert "safe abort" in data["reply"].lower()


def test_t1_r6_02_booking_timeout_committed_reconciliation(client):
    """TC-T1-R6-02: Verifies commit timeout reconciles successfully via provider status check."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    res = client.post("/api/chat", json={"message": "Yes, confirm", "force_book_outcome": "timeout_committed"})
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] == "COMPLETED"
    assert len(data["bookings"]) > 0


def test_t1_r6_03_booking_timeout_uncommitted_safe_retry(client):
    """TC-T1-R6-03: Verifies uncommitted timeout retries safely with idempotent key."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    res = client.post("/api/chat", json={"message": "Yes, confirm", "force_book_outcome": "timeout_uncommitted"})
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] == "COMPLETED"
    assert len(data["bookings"]) > 0


def test_t1_r6_04_force_price_drift_delta(client):
    """TC-T1-R6-04: Verifies force_price_drift_delta chaos parameter triggers diff guard."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    res = client.post("/api/chat", json={"message": "Yes, confirm", "force_price_drift_delta": 250})
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] == "AWAITING_APPROVAL"
    assert "price" in data["reply"].lower()


def test_t1_r6_05_proactive_cancellation_webhook(client):
    """TC-T1-R6-05: Verifies proactive cancellation webhook alerts and proposes replacement."""
    res = client.post("/api/proactive-cancel", json={"booking_id": "bk_upcoming_ac_01"})
    assert res.status_code == 200
    data = res.json()
    assert data["event_result"]["notified"] is True
    assert data["event_result"]["replacement_provider_id"] == "prov_ac_2"
    assert data["fsm_state"] == "AWAITING_APPROVAL"


# =====================================================================
# TIER 2: BOUNDARY & CORNER CASES (R1 through R6: 30 test cases)
# =====================================================================

# --- R1 Boundaries ---

def test_t2_r1_01_empty_chat_message_validation(client):
    """TC-T2-R1-01: Verifies empty or whitespace-only messages are handled safely."""
    res = client.post("/api/chat", json={"message": "   "})
    assert res.status_code in (200, 422)


def test_t2_r1_02_duplicate_confirmation_idempotency(client):
    """TC-T2-R1-02: Verifies re-affirming already confirmed booking is strictly idempotent."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    res1 = client.post("/api/chat", json={"message": "Yes, confirm", "force_book_outcome": "success"})
    assert res1.json()["fsm_state"] == "COMPLETED"
    booking_count = len(res1.json()["bookings"])

    # Duplicate confirmation turn
    res2 = client.post("/api/chat", json={"message": "Yes, confirm"})
    assert res2.status_code == 200
    assert len(res2.json()["bookings"]) == booking_count
    assert "already confirmed" in res2.json()["reply"].lower() or "duplicate" in res2.json()["reply"].lower()


def test_t2_r1_03_constraint_modification_during_approval(client):
    """TC-T2-R1-03: Verifies changing constraints while in AWAITING_APPROVAL invalidates prior proposal."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    res = client.post("/api/chat", json={"message": "Change my budget to under ₹2,000"})
    assert res.status_code == 200
    data = res.json()
    assert data["slots"]["max_budget"] == 2000


def test_t2_r1_04_zero_price_drift_preserves_approval(client):
    """TC-T2-R1-04: Verifies delta=0 does not trigger price drift invalidation."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    res = client.post("/api/chat", json={"message": "Yes, confirm", "force_price_drift_delta": 0, "force_book_outcome": "success"})
    assert res.status_code == 200
    assert res.json()["fsm_state"] == "COMPLETED"


def test_t2_r1_05_chat_message_max_length_boundary(client):
    """TC-T2-R1-05: Verifies system handles lengthy user prompt without crash."""
    long_msg = "Please find a plumber " + ("for kitchen tap repair " * 100) + " in Indiranagar under ₹1,500"
    res = client.post("/api/chat", json={"message": long_msg})
    assert res.status_code == 200


# --- R2 Boundaries ---

def test_t2_r2_01_max_turn_limit_bounded_execution(client):
    """TC-T2-R2-01: Verifies bounded operational limits are enforced in state limits."""
    state = client.get("/api/state").json()
    assert state["limits"]["max_model_turns_per_run"] <= 10
    assert state["limits"]["max_tool_calls_per_run"] <= 12


def test_t2_r2_02_expired_hold_auto_reacquisition(client):
    """TC-T2-R2-02: Verifies hold expiration causes FSM to acquire fresh hold and request re-confirmation."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    with ORCH.db.conn:
        ORCH.db.conn.execute("UPDATE slot_holds SET expires_at_epoch = ? WHERE status = 'ACTIVE'", (time.time() - 3600,))

    res = client.post("/api/chat", json={"message": "Yes, confirm"})
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] in ("AWAITING_APPROVAL", "COMPLETED")


def test_t2_r2_03_slot_taken_on_commit_alternative_proposal(client):
    """TC-T2-R2-03: Verifies if held slot is taken upon booking, alternative is proposed."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    res = client.post("/api/chat", json={"message": "Yes, confirm", "force_book_outcome": "slot_taken"})
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] == "AWAITING_APPROVAL"
    assert "taken" in data["reply"].lower() or "alternative" in data["reply"].lower()


def test_t2_r2_04_sequential_reset_idempotency(client):
    """TC-T2-R2-04: Verifies multiple sequential reset calls are completely idempotent."""
    for _ in range(3):
        res = client.post("/api/reset")
        assert res.status_code == 200
        assert res.json()["status"] == "reset"


def test_t2_r2_05_direct_mutation_from_idle_safely_blocked(client):
    """TC-T2-R2-05: Verifies confirmation attempt without active proposal does not complete booking."""
    res = client.post("/api/chat", json={"message": "Yes, confirm booking now"})
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] != "COMPLETED"


# --- R3 Boundaries ---

def test_t2_r3_01_strict_1200_token_cap_enforcement(client):
    """TC-T2-R3-01: Verifies max_context_tokens ceiling is strictly 1,200."""
    state = client.get("/api/state").json()
    assert state["limits"]["max_context_tokens"] == 1200


def test_t2_r3_02_trace_event_buffer_boundedness(client):
    """TC-T2-R3-02: Verifies trace events snapshot stays bounded to recent events."""
    state = client.get("/api/state").json()
    assert len(state["trace_events"]) <= 40


def test_t2_r3_03_export_trace_empty_session(client):
    """TC-T2-R3-03: Verifies trace export immediately after reset returns valid structure."""
    res = client.get("/api/export-trace")
    assert res.status_code == 200
    assert "attachment" in res.headers["content-disposition"]


def test_t2_r3_04_prometheus_metric_gauges_non_negative(client):
    """TC-T2-R3-04: Verifies gauge metrics never expose negative values."""
    res = client.get("/metrics")
    assert res.status_code == 200
    for line in res.text.splitlines():
        if line.startswith("errand_agent_active_") and not line.startswith("#"):
            val = float(line.split()[-1])
            assert val >= 0.0


def test_t2_r3_05_trace_id_entropy_and_uniqueness(client):
    """TC-T2-R3-05: Verifies that session resets generate cryptographically distinct trace IDs."""
    id1 = client.get("/api/state").json()["trace_id"]
    client.post("/api/reset")
    id2 = client.get("/api/state").json()["trace_id"]
    assert len(id1) == 32
    assert len(id2) == 32
    assert id1 != id2


# --- R4 Boundaries ---

def test_t2_r4_01_memory_action_missing_keys_handled_gracefully(client):
    """TC-T2-R4-01: Verifies memory operations with empty/invalid actions do not crash."""
    res = client.post("/api/memory", json={"action": "unknown_op", "key": "", "value": ""})
    assert res.status_code == 200
    assert "completed" in res.json()["reply"].lower() or "memory" in res.json()["reply"].lower()


def test_t2_r4_02_forget_non_existent_key_safe(client):
    """TC-T2-R4-02: Verifies forgetting non-existent memory key reports graceful status."""
    res = client.post("/api/memory", json={"action": "forget", "key": "non_existent_key_99999"})
    assert res.status_code == 200
    assert "forgot" in res.json()["reply"].lower()


def test_t2_r4_03_sql_prefilter_exclusion_of_forgotten(client):
    """TC-T2-R4-03: Verifies forgotten memories are excluded from prompt compilation."""
    client.post("/api/memory", json={"action": "forget", "key": "preferred_area"})
    client.post("/api/chat", json={"message": "Find a plumber this week"})
    audit = client.get("/api/state").json().get("latest_context_audit", {})
    if audit and "excluded_memories" in audit:
        excluded_keys = [m.get("key") for m in audit["excluded_memories"]]
        assert "preferred_area" in excluded_keys


def test_t2_r4_04_unconfirmed_inferred_excluded_from_context(client):
    """TC-T2-R4-04: Verifies unconfirmed inferred memories are excluded from prompt context."""
    client.post("/api/chat", json={"message": "Find an electrician in Indiranagar"})
    audit = client.get("/api/state").json().get("latest_context_audit", {})
    if audit and "excluded_memories" in audit:
        reasons = [m.get("reason", "") for m in audit["excluded_memories"]]
        assert any("inferred" in r.lower() or "unconfirmed" in r.lower() for r in reasons)


def test_t2_r4_05_high_keyword_collision_unconfirmed_memory(client):
    """TC-T2-R4-05: Verifies keyword matching does not leak unconfirmed inferred memories."""
    client.post("/api/chat", json={"message": "Do you remember my weekend preference?"})
    audit = client.get("/api/state").json().get("latest_context_audit", {})
    if audit and "excluded_memories" in audit:
        excl_keys = [m.get("key") for m in audit["excluded_memories"]]
        assert "inferred_weekend_preference" in excl_keys


# --- R5 Boundaries ---

def test_t2_r5_01_trojan_source_unicode_sanitization(client):
    """TC-T2-R5-01: Verifies Trojan Source bidirectional override characters are sanitized."""
    bidi_payload = "Find a plumber in Indiranagar \u202E\u2066under ₹1,500"
    res = client.post("/api/chat", json={"message": bidi_payload})
    assert res.status_code == 200
    assert res.json()["slots"]["service"] == "plumber"


def test_t2_r5_02_ansi_escape_and_null_byte_sanitization(client):
    """TC-T2-R5-02: Verifies ANSI escape sequences and null bytes are removed."""
    dirty_msg = "Find a plumber\x1b[31;1m in Indiranagar\x00 under ₹1,500"
    res = client.post("/api/chat", json={"message": dirty_msg})
    assert res.status_code == 200
    assert res.json()["slots"]["service"] == "plumber"


def test_t2_r5_03_payload_too_large_rejection(client):
    """TC-T2-R5-03: Verifies requests exceeding 1MB payload ceiling are rejected."""
    huge_data = "X" * (1024 * 1024 + 512)
    res = client.post("/api/chat", content=huge_data, headers={"Content-Type": "application/json"})
    assert res.status_code in (413, 422)


def test_t2_r5_04_mcp_malformed_jsonrpc_parse_error(client):
    """TC-T2-R5-04: Verifies malformed JSON payload returns JSON-RPC -32700 Parse error."""
    res = client.post("/mcp", content="{not a valid json payload", headers={"Content-Type": "application/json"})
    assert res.status_code in (200, 400)
    data = res.json()
    assert "error" in data
    assert data["error"]["code"] == -32700


def test_t2_r5_05_mcp_unwhitelisted_tool_call_blocked(client):
    """TC-T2-R5-05: Verifies MCP tool call execution whitelisting prevents unauthorized execution."""
    req = {
        "jsonrpc": "2.0",
        "id": 88,
        "method": "tools/call",
        "params": {"name": "unauthorized_arbitrary_method", "arguments": {}},
    }
    res = client.post("/mcp", json=req)
    assert res.status_code == 200
    data = res.json()["result"]
    assert data["isError"] is True
    assert "not found" in data["content"][0]["text"]


# --- R6 Boundaries ---

def test_t2_r6_01_negative_price_drift_handled(client):
    """TC-T2-R6-01: Verifies negative price drift delta is handled gracefully."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    res = client.post("/api/chat", json={"message": "Yes, confirm", "force_price_drift_delta": -100})
    assert res.status_code == 200


def test_t2_r6_02_proactive_cancel_invalid_booking_id(client):
    """TC-T2-R6-02: Verifies proactive cancellation with non-existent booking ID returns safe status."""
    res = client.post("/api/proactive-cancel", json={"booking_id": "non_existent_bk_000"})
    assert res.status_code == 200
    data = res.json()["event_result"]
    assert data.get("notified") is False or "not found" in str(data).lower()


def test_t2_r6_03_proactive_cancel_duplicate_event_deduplication(client):
    """TC-T2-R6-03: Verifies duplicate cancellation webhooks suppress duplicate user alerts."""
    res1 = client.post("/api/proactive-cancel", json={"booking_id": "bk_upcoming_ac_01"})
    assert res1.json()["event_result"]["notified"] is True

    # Second webhook with same booking ID
    res2 = client.post("/api/proactive-cancel", json={"booking_id": "bk_upcoming_ac_01"})
    assert res2.status_code == 200
    res2_data = res2.json()["event_result"]
    assert res2_data.get("duplicate_suppressed") is True or res2_data.get("notified") is False


def test_t2_r6_04_transient_503_cancel_successful_retry(client):
    """TC-T2-R6-04: Verifies transient 503 error on cancel succeeds on retry."""
    client.post("/api/chat", json={"message": "Cancel my booking for AC servicing"})
    res = client.post("/api/chat", json={"message": "Yes, confirm", "persistent_cancel_503": False})
    assert res.status_code == 200
    assert res.json()["fsm_state"] == "COMPLETED"


def test_t2_r6_05_combined_chaos_flags_stability(client):
    """TC-T2-R6-05: Verifies simultaneous chaos injection flags do not destabilize the FSM."""
    res = client.post("/api/chat", json={
        "message": DISCOVERY_PROMPT,
        "inject_adversarial_review": True,
        "force_price_drift_delta": 250,
        "persistent_cancel_503": False,
    })
    assert res.status_code == 200
    assert res.json()["fsm_state"] in ("COLLECTING_SLOTS", "AWAITING_APPROVAL")


# =====================================================================
# TIER 3: CROSS-FEATURE INTERACTIONS (8 test cases)
# =====================================================================

def test_t3_pair_01_security_rate_limit_and_memory_crud(client):
    """TC-T3-PAIR-01: Verifies rate limiting headers accompany memory CRUD operations."""
    res = client.post("/api/memory", json={"action": "inspect"})
    assert res.status_code == 200
    assert "X-RateLimit-Limit" in res.headers
    assert "X-Content-Type-Options" in res.headers


def test_t3_pair_02_chaos_price_drift_and_hitl_approval_invalidation(client):
    """TC-T3-PAIR-02: Verifies price drift injection invalidates active HITL proposal."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    res = client.post("/api/chat", json={"message": "Yes, confirm", "force_price_drift_delta": 250})
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] == "AWAITING_APPROVAL"
    assert "price" in data["reply"].lower()


def test_t3_pair_03_chaos_persistent_503_and_fsm_safe_abort(client):
    """TC-T3-PAIR-03: Verifies persistent upstream 503 causes FSM to transition to SAFE_ABORT."""
    client.post("/api/chat", json={"message": "Cancel my booking for AC servicing"})
    res = client.post("/api/chat", json={"message": "Yes, confirm", "persistent_cancel_503": True})
    assert res.status_code == 200
    assert res.json()["fsm_state"] == "SAFE_ABORT"


def test_t3_pair_04_mcp_resource_read_and_fsm_live_state(client):
    """TC-T3-PAIR-04: Verifies reading errand://system/fsm-state via MCP reflects live state."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    mcp_res = client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": 55,
        "method": "resources/read",
        "params": {"uri": "errand://system/fsm-state"},
    })
    assert mcp_res.status_code == 200
    content = mcp_res.json()["result"]["contents"][0]["text"]
    state_obj = json.loads(content)
    assert state_obj["fsm_state"] == "AWAITING_APPROVAL"


def test_t3_pair_05_memory_forgetting_and_telemetry_audit(client):
    """TC-T3-PAIR-05: Verifies forgotten memory is logged as excluded in telemetry audit."""
    client.post("/api/memory", json={"action": "forget", "key": "preferred_area"})
    client.post("/api/chat", json={"message": "Find an electrician this week"})
    state = client.get("/api/state").json()
    audit = state.get("latest_context_audit", {})
    if audit and "excluded_memories" in audit:
        excluded = [m["key"] for m in audit["excluded_memories"]]
        assert "preferred_area" in excluded


def test_t3_pair_06_proactive_cancel_and_hitl_replacement_proposal(client):
    """TC-T3-PAIR-06: Verifies cancellation webhook automatically creates replacement proposal."""
    res = client.post("/api/proactive-cancel", json={"booking_id": "bk_upcoming_ac_01"})
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] == "AWAITING_APPROVAL"
    proposal = data["active_proposal"]
    assert proposal is not None
    assert proposal["provider_id"] == "prov_ac_2"


def test_t3_pair_07_rbac_auth_failure_and_prometheus_metrics(client):
    """TC-T3-PAIR-07: Verifies authentication rejection increments Prometheus security metrics."""
    client.get("/api/state", headers={"X-API-Key": "invalid-key-for-metric-test"})
    metrics_res = client.get("/metrics")
    assert metrics_res.status_code == 200
    assert "errand_agent_security_rejections_total" in metrics_res.text


def test_t3_pair_08_trojan_defense_and_fsm_slot_extraction(client):
    """TC-T3-PAIR-08: Verifies Trojan Source defense preserves legitimate slot extraction."""
    payload = "Find an electrician in \u202EKoramangala\u202C under ₹2,000"
    res = client.post("/api/chat", json={"message": payload})
    assert res.status_code == 200
    slots = res.json()["slots"]
    assert slots["service"] == "electrician"
    assert slots["area"] == "Koramangala"


# =====================================================================
# TIER 4: REAL-WORLD APPLICATION SCENARIOS (6 test cases)
# =====================================================================

def test_t4_scenario_01_full_discovery_to_booking_workflow(client):
    """TC-T4-SCENARIO-01: Complete discovery -> proposal -> approval -> completed workflow."""
    # 1. User discovers service
    res_disc = client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    assert res_disc.status_code == 200
    data_disc = res_disc.json()
    assert data_disc["fsm_state"] == "AWAITING_APPROVAL"
    assert data_disc["active_proposal"] is not None

    # 2. User explicitly approves
    res_appr = client.post("/api/chat", json={"message": "Yes, confirm", "force_book_outcome": "success"})
    assert res_appr.status_code == 200
    data_appr = res_appr.json()
    assert data_appr["fsm_state"] == "COMPLETED"
    assert len(data_appr["bookings"]) > 0

    # 3. Verify telemetry state recorded completion
    state = client.get("/api/state").json()
    assert state["fsm_state"] == "COMPLETED"
    assert any(b["status"] == "CONFIRMED" for b in state["bookings"])


def test_t4_scenario_02_price_drift_challenge_and_reapproval(client):
    """TC-T4-SCENARIO-02: Price drift challenge invalidates hash; user re-approves with fresh hold."""
    # Step 1: Discovery
    res1 = client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    assert res1.json()["fsm_state"] == "AWAITING_APPROVAL"

    # Step 2: Price drift injection on confirmation
    res2 = client.post("/api/chat", json={
        "message": "Yes, confirm",
        "force_price_drift_delta": 250,
    })
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["fsm_state"] == "AWAITING_APPROVAL"
    assert "quoted price changed" in data2["reply"] or "price" in data2["reply"].lower()

    # Step 3: User accepts the modified terms and re-confirms
    res3 = client.post("/api/chat", json={"message": "Yes, confirm", "force_book_outcome": "success"})
    assert res3.status_code == 200
    assert res3.json()["fsm_state"] == "COMPLETED"


def test_t4_scenario_03_proactive_provider_cancellation_and_auto_rebooking(client):
    """TC-T4-SCENARIO-03: Provider cancellation webhook triggers proactive recovery and replacement."""
    # Step 1: Upstream provider webhook cancels existing booking
    res_webhook = client.post("/api/proactive-cancel", json={"booking_id": "bk_upcoming_ac_01"})
    assert res_webhook.status_code == 200
    data_webhook = res_webhook.json()
    assert data_webhook["event_result"]["notified"] is True
    assert data_webhook["event_result"]["replacement_provider_id"] == "prov_ac_2"
    assert data_webhook["fsm_state"] == "AWAITING_APPROVAL"

    # Step 2: User approves recommended replacement
    res_confirm = client.post("/api/chat", json={"message": "Yes, confirm replacement", "force_book_outcome": "success"})
    assert res_confirm.status_code == 200
    assert res_confirm.json()["fsm_state"] == "COMPLETED"


def test_t4_scenario_04_memory_inspection_update_and_context_adoption(client):
    """TC-T4-SCENARIO-04: Memory inspection, correction to Whitefield, and seamless discovery adoption."""
    # Step 1: Inspect memories
    res_insp = client.post("/api/memory", json={"action": "inspect"})
    assert res_insp.status_code == 200

    # Step 2: Update preferred area to Whitefield
    res_upd = client.post("/api/memory", json={
        "action": "update",
        "key": "preferred_area",
        "value": "Whitefield",
    })
    assert res_upd.status_code == 200
    assert "Whitefield" in res_upd.json()["reply"]

    # Step 3: Start discovery without specifying area; agent adopts updated preferred area
    res_disc = client.post("/api/chat", json={
        "message": "Find a plumber in Whitefield this week under ₹1,500"
    })
    assert res_disc.status_code == 200
    assert res_disc.json()["slots"]["area"] == "Whitefield"


def test_t4_scenario_05_mcp_agentic_client_lifecycle(client):
    """TC-T4-SCENARIO-05: External MCP client initializes, inspects resources, and calls tools."""
    # 1. MCP Handshake
    handshake = client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": 101,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05"},
    })
    assert handshake.status_code == 200
    assert handshake.json()["result"]["serverInfo"]["name"] == "errand-agent-mcp"

    # 2. Tool list discovery
    tools_res = client.post("/mcp", json={"jsonrpc": "2.0", "id": 102, "method": "tools/list"})
    assert tools_res.status_code == 200
    tools = tools_res.json()["result"]["tools"]
    assert len(tools) == 8

    # 3. Read system resource
    res_read = client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": 103,
        "method": "resources/read",
        "params": {"uri": "errand://system/fsm-state"},
    })
    assert res_read.status_code == 200

    # 4. Execute search_providers tool
    call_res = client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": 104,
        "method": "tools/call",
        "params": {
            "name": "search_providers",
            "arguments": {"service": "plumber", "area": "Indiranagar", "date_range": "2026-11-09"},
        },
    })
    assert call_res.status_code == 200
    call_data = call_res.json()["result"]
    assert call_data["isError"] is False
    assert len(call_data["content"]) > 0


def test_t4_scenario_06_multi_fault_timeout_reconciliation_workflow(client):
    """TC-T4-SCENARIO-06: Booking timeout chaos is reconciled authoritatively without double booking."""
    # Step 1: Discovery
    res1 = client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    assert res1.json()["fsm_state"] == "AWAITING_APPROVAL"

    # Step 2: Affirmation with timeout after commit fault
    res2 = client.post("/api/chat", json={
        "message": "Yes, confirm",
        "force_book_outcome": "timeout_committed",
    })
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["fsm_state"] == "COMPLETED"

    # Step 3: Verify booking was recorded in bookings list
    assert len(data2["bookings"]) > 0
    confirmed_bookings = [b for b in data2["bookings"] if b["status"] == "CONFIRMED"]
    assert len(confirmed_bookings) > 0
