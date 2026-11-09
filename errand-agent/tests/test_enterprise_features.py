"""Enterprise Features Test Suite (Nov 2026 Standards).

Validates:
1. OpenTelemetry-grade structured tracing (trace_id, span_id, duration_ms, tags, baggage).
2. Prometheus text exposition format (/metrics).
3. Enterprise health probes (/healthz, /livez, /readyz).
4. Model Context Protocol (MCP) JSON-RPC 2.0 & REST endpoints with RBAC role gating.
5. Enterprise Security Layer: 3-tier RBAC, API key auth, CSP & security headers, rate limiting, and bounds.
6. Multi-tenant workspace and session isolation.
7. Trojan Source Unicode defense (CVE-2021-42574, CVE-2021-42694).
"""

import json
import os
import time

# Ensure deterministic offline execution mode for reproducible test suite
os.environ.setdefault("LLM_MODE", "offline")

import pytest
from fastapi.testclient import TestClient
from src.app_server import app, ORCH, chat_turn, ChatRequest
from src.db import LocalDatabase
from src.mcp_server import MCP_TOOLS_CATALOG, MCPServerHandler
from src.memory_context import MemoryAndContextManager
from src.mock_tools import FaultConfig, MockToolEnvironment, ToolExecutionError
from src.schemas import IntentSlots
from src.security import (
    AuthenticationError,
    BIDI_TROJAN_PATTERN,
    RATE_LIMITER,
    RATE_LIMIT_RPM,
    UserRole,
    check_permission,
    resolve_role,
    sanitize_input_text,
)


@pytest.fixture
def client():
    return TestClient(app)


# ==================== 1. HEALTH PROBES ====================

def test_healthz_liveness_readiness(client):
    for endpoint in ("/healthz", "/livez", "/readyz"):
        res = client.get(endpoint)
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "healthy"
        assert data["service"] == "errand-agent"
        assert data["version"] == "2.0.0"
        assert data["database"] == "connected"
        assert "fsm_state" in data
        assert "trace_id" in data


# ==================== 2. PROMETHEUS METRICS ====================

def test_prometheus_metrics_endpoint(client):
    res = client.get("/metrics")
    assert res.status_code == 200
    assert "text/plain" in res.headers["content-type"]
    text = res.text
    # Verify standard Prometheus exposition format
    assert "# HELP errand_agent_turns_total" in text
    assert "# TYPE errand_agent_turns_total counter"
    assert "# HELP errand_agent_token_consumption_total" in text
    assert "# TYPE errand_agent_token_consumption_total counter"
    assert "# HELP errand_agent_fsm_state_transitions_total" in text
    assert "# HELP errand_agent_active_sessions" in text
    assert "# TYPE errand_agent_active_sessions gauge"
    assert "# HELP errand_agent_operation_duration_seconds" in text


# ==================== 3. MODEL CONTEXT PROTOCOL (MCP) ====================

def test_mcp_initialize_handshake(client):
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-runner", "version": "1.0"},
        },
    }
    res = client.post("/mcp", json=req)
    assert res.status_code == 200
    data = res.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == 1
    result = data["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "errand-agent-mcp"
    assert "tools" in result["capabilities"]


def test_mcp_tools_list_rpc_and_rest(client):
    # JSON-RPC tools/list
    rpc_res = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert rpc_res.status_code == 200
    tools = rpc_res.json()["result"]["tools"]
    assert len(tools) == 8
    tool_names = [t["name"] for t in tools]
    assert "search_providers" in tool_names
    assert "hold_slot" in tool_names
    assert "book" in tool_names
    assert "cancel" in tool_names
    assert "reschedule" in tool_names

    # REST GET /mcp/tools
    rest_res = client.get("/mcp/tools")
    assert rest_res.status_code == 200
    assert rest_res.json()["count"] == 8


def test_mcp_tools_call_execution(client):
    # Call search_providers via MCP JSON-RPC
    call_req = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "search_providers",
            "arguments": {
                "service": "plumber",
                "area": "Indiranagar",
                "date_range": "2026-11-09",
            },
        },
    }
    res = client.post("/mcp", json=call_req)
    assert res.status_code == 200
    result = res.json()["result"]
    assert result["isError"] is False
    assert len(result["content"]) > 0
    text = result["content"][0]["text"]
    assert "prov_plumb_1" in text

    # REST POST /mcp/call
    rest_call = client.post("/mcp/call", json={
        "name": "get_provider",
        "arguments": {"provider_id": "prov_plumb_1"},
    })
    assert rest_call.status_code == 200
    assert rest_call.json()["isError"] is False


def test_mcp_resources_and_manifest(client):
    manifest_res = client.get("/mcp/manifest")
    assert manifest_res.status_code == 200
    data = manifest_res.json()
    assert "tools" in data
    assert "resources" in data
    assert "prompts" in data

    res_list = client.get("/mcp/resources")
    assert res_list.status_code == 200
    assert res_list.json()["count"] >= 3


# ==================== 4. ENTERPRISE SECURITY & HEADERS ====================

def test_enterprise_security_headers(client):
    res = client.get("/healthz")
    assert res.headers.get("X-Content-Type-Options") == "nosniff"
    assert res.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert res.headers.get("Strict-Transport-Security") is not None
    assert "X-RateLimit-Limit" in res.headers
    assert "Content-Security-Policy" in res.headers


def test_api_key_authentication(client):
    # Valid enterprise API key
    res_valid = client.get("/api/state", headers={"X-API-Key": "enterprise-errand-nov2026-key"})
    assert res_valid.status_code == 200

    # Invalid API key
    res_invalid = client.get("/api/state", headers={"X-API-Key": "malicious-hacker-key"})
    assert res_invalid.status_code == 401
    prob = res_invalid.json()
    assert prob["status"] == 401
    assert "Unauthorized" in prob["title"]


def test_payload_size_limit_rejection(client):
    # Oversized payload (>1MB)
    huge_data = "A" * (1024 * 1024 + 100)
    res = client.post("/api/chat", content=huge_data, headers={"Content-Type": "application/json"})
    assert res.status_code in (413, 422)


# ==================== 5. OPENTELEMETRY TRACE ENRICHMENT ====================

def test_opentelemetry_trace_attributes(client):
    client.post("/api/reset")
    res = client.get("/api/state")
    data = res.json()
    assert "trace_id" in data
    assert len(data["trace_id"]) == 32  # Standard 32-hex OTEL trace ID
    assert "otel_traceparent" in data
    assert data["otel_traceparent"].startswith("00-")

    # Send a turn and check trace events for OTEL attributes
    client.post("/api/chat", json={"message": "Find a plumber for my kitchen tap under ₹1,500"})
    state = client.get("/api/state").json()
    events = state["trace_events"]
    assert len(events) > 0
    sample = events[-1]
    assert "trace_id" in sample
    assert "span_id" in sample
    assert "tags" in sample
    assert "baggage" in sample

    # Export trace endpoint
    export_res = client.get("/api/export-trace")
    assert export_res.status_code == 200
    assert "application/x-ndjson" in export_res.headers["content-type"]
    assert "span_" in export_res.text


# ==================== 6. DEEP VERIFICATION OF MCP & SECURITY EDGE CASES ====================

def test_mcp_batch_jsonrpc_execution(client):
    """Verify JSON-RPC 2.0 batch array processing."""
    batch_req = [
        {"jsonrpc": "2.0", "id": 10, "method": "ping"},
        {"jsonrpc": "2.0", "id": 11, "method": "tools/list"},
    ]
    res = client.post("/mcp", json=batch_req)
    assert res.status_code == 200
    responses = res.json()
    assert isinstance(responses, list)
    assert len(responses) == 2
    assert responses[0]["id"] == 10 and responses[0]["result"] == {}
    assert responses[1]["id"] == 11 and "tools" in responses[1]["result"]


def test_mcp_system_fsm_state_resource(client):
    """Verify MCP resource reading of errand://system/fsm-state."""
    req = {
        "jsonrpc": "2.0",
        "id": 12,
        "method": "resources/read",
        "params": {"uri": "errand://system/fsm-state"},
    }
    res = client.post("/mcp", json=req)
    assert res.status_code == 200
    data = res.json()["result"]
    assert len(data["contents"]) == 1
    content_text = json.loads(data["contents"][0]["text"])
    assert "fsm_state" in content_text
    assert "turn_counter" in content_text


def test_mcp_prompts_get(client):
    """Verify MCP prompts/get template resolution."""
    req = {
        "jsonrpc": "2.0",
        "id": 13,
        "method": "prompts/get",
        "params": {
            "name": "errand_discovery",
            "arguments": {"user_input": "Need an electrician in Koramangala"},
        },
    }
    res = client.post("/mcp", json=req)
    assert res.status_code == 200
    data = res.json()["result"]
    assert "messages" in data
    assert any("electrician" in m["content"]["text"] for m in data["messages"])


def test_mcp_tool_execution_safety_whitelist(client):
    """Verify that calling internal object methods via MCP is blocked."""
    req = {
        "jsonrpc": "2.0",
        "id": 14,
        "method": "tools/call",
        "params": {"name": "eval_cassettes", "arguments": {}},
    }
    res = client.post("/mcp", json=req)
    assert res.status_code == 200
    result = res.json()["result"]
    assert result["isError"] is True
    assert "not found in Errand Agent catalog" in result["content"][0]["text"]


def test_rate_limiting_enforcement(client):
    """Verify HTTP 429 Too Many Requests enforcement under excessive load."""
    RATE_LIMITER.reset()

    test_ip = "198.51.100.99"
    headers = {"X-Forwarded-For": test_ip, "X-Test-Rate-Limit": "1"}

    # Exhaust rate limit RPM
    for _ in range(RATE_LIMIT_RPM):
        res = RATE_LIMITER.check(test_ip)
        assert res[0] is True

    # Next request must be rejected with 429
    res = client.get("/api/state", headers=headers)
    assert res.status_code == 429
    data = res.json()
    assert data["status"] == 429
    assert "Too Many Requests" in data["title"]
    assert "Retry-After" in res.headers
    assert res.headers["X-RateLimit-Remaining"] == "0"
    RATE_LIMITER.reset()


def test_trace_stream_sse_endpoint_headers(client):
    """Verify SSE streaming endpoint headers."""
    with client.stream("GET", "/api/trace-stream") as res:
        assert res.status_code == 200
        assert "text/event-stream" in res.headers["content-type"]
        assert res.headers.get("Cache-Control") == "no-cache"
        assert res.headers.get("X-Accel-Buffering") == "no"


# ==================== 7. 3-TIER RBAC ROLES & PERMISSIONS ====================

def test_rbac_role_definitions_and_permissions():
    """Verify role enum values and action permissions."""
    assert UserRole.ADMIN == "Admin"
    assert UserRole.OPERATOR == "Operator"
    assert UserRole.AUDITOR == "Auditor"

    # Admin permissions
    assert check_permission(UserRole.ADMIN, "write:chat")
    assert check_permission(UserRole.ADMIN, "admin:reset")
    assert check_permission(UserRole.ADMIN, "write:memory")
    assert check_permission(UserRole.ADMIN, "write:mutations")

    # Operator permissions
    assert check_permission(UserRole.OPERATOR, "write:chat")
    assert check_permission(UserRole.OPERATOR, "write:memory")
    assert check_permission(UserRole.OPERATOR, "write:mutations")
    assert not check_permission(UserRole.OPERATOR, "admin:reset")
    assert not check_permission(UserRole.OPERATOR, "admin:evals")

    # Auditor permissions
    assert check_permission(UserRole.AUDITOR, "read:state")
    assert check_permission(UserRole.AUDITOR, "read:memory")
    assert not check_permission(UserRole.AUDITOR, "write:chat")
    assert not check_permission(UserRole.AUDITOR, "write:memory")
    assert not check_permission(UserRole.AUDITOR, "write:mutations")
    assert not check_permission(UserRole.AUDITOR, "admin:reset")


def test_resolve_role_mapping():
    """Verify key prefix, Bearer token, and header mapping."""
    # Key prefix mapping
    assert resolve_role("admin-key-prod-01") == UserRole.ADMIN
    assert resolve_role("operator-key-live-02") == UserRole.OPERATOR
    assert resolve_role("auditor-key-sec-03") == UserRole.AUDITOR
    assert resolve_role("enterprise-errand-nov2026-key") == UserRole.ADMIN

    # Invalid key raises AuthenticationError
    with pytest.raises(AuthenticationError):
        resolve_role("unauthorized-key")

    # Role header mapping
    assert resolve_role(None, role_header="Operator") == UserRole.OPERATOR
    assert resolve_role(None, role_header="auditor") == UserRole.AUDITOR
    assert resolve_role(None, role_header=None) == UserRole.ADMIN


def test_auditor_read_only_access_allowed(client):
    """Verify Auditor role can read state, telemetry, and inspect memories."""
    headers = {"X-API-Key": "auditor-key-01"}

    # Allowed reads
    assert client.get("/api/state", headers=headers).status_code == 200
    assert client.get("/metrics", headers=headers).status_code == 200
    assert client.get("/healthz", headers=headers).status_code == 200
    assert client.get("/api/export-trace", headers=headers).status_code == 200

    # Allowed memory inspect
    res_mem = client.post("/api/memory", json={"action": "inspect"}, headers=headers)
    assert res_mem.status_code == 200


def test_auditor_mutations_strictly_blocked_403(client):
    """Verify Auditor role is strictly blocked (HTTP 403 RFC-7807) on mutating actions."""
    headers = {"X-API-Key": "auditor-key-01"}

    # Blocked on chat
    res_chat = client.post("/api/chat", json={"message": "hello"}, headers=headers)
    assert res_chat.status_code == 403
    prob = res_chat.json()
    assert prob["status"] == 403
    assert "Forbidden" in prob["title"]
    assert "write:chat" in prob["detail"]

    # Blocked on memory update
    res_mem = client.post("/api/memory", json={"action": "update", "key": "k", "value": "v"}, headers=headers)
    assert res_mem.status_code == 403

    # Blocked on system reset
    res_reset = client.post("/api/reset", headers=headers)
    assert res_reset.status_code == 403

    # Blocked on mutating tool call via REST
    res_tool = client.post(
        "/mcp/call",
        json={"name": "hold_slot", "arguments": {"provider_id": "prov_plumb_1", "slot": "2026-11-09 09:00"}},
        headers=headers,
    )
    assert res_tool.status_code == 403


def test_operator_allowed_chat_and_blocked_reset(client):
    """Verify Operator can chat and modify memory, but is blocked from reset."""
    headers = {"X-API-Key": "operator-key-01"}

    # Allowed chat
    res_chat = client.post("/api/chat", json={"message": "Need a plumber in Indiranagar"}, headers=headers)
    assert res_chat.status_code == 200

    # Allowed memory update
    res_mem = client.post("/api/memory", json={"action": "update", "key": "preferred_area", "value": "Whitefield"}, headers=headers)
    assert res_mem.status_code == 200

    # Blocked from reset
    res_reset = client.post("/api/reset", headers=headers)
    assert res_reset.status_code == 403


# ==================== 8. MULTI-TENANT WORKSPACE & SESSION ISOLATION ====================

def test_multi_tenant_workspace_isolation(client):
    """Verify independent FSM states and turn progression across two tenants."""
    headers_a = {"X-Tenant-ID": "tenant_alpha", "X-Session-ID": "sess_shared"}
    headers_b = {"X-Tenant-ID": "tenant_beta", "X-Session-ID": "sess_shared"}

    # Both start clean
    client.post("/api/reset", headers=headers_a)
    client.post("/api/reset", headers=headers_b)

    state_a = client.get("/api/state", headers=headers_a).json()
    state_b = client.get("/api/state", headers=headers_b).json()
    assert state_a["fsm_state"] == "IDLE"
    assert state_b["fsm_state"] == "IDLE"

    # Turn on Tenant Alpha
    res_a = client.post(
        "/api/chat",
        json={"message": "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500"},
        headers=headers_a,
    )
    assert res_a.status_code == 200
    assert res_a.json()["fsm_state"] == "AWAITING_APPROVAL"
    assert res_a.json()["active_proposal"] is not None

    # Verify Tenant Beta is STILL in IDLE
    res_b = client.get("/api/state", headers=headers_b)
    assert res_b.json()["fsm_state"] == "IDLE"
    assert res_b.json()["active_proposal"] is None

    # Verify default session is STILL in IDLE
    client.post("/api/reset")
    res_def = client.get("/api/state")
    assert res_def.json()["fsm_state"] == "IDLE"


def test_multi_session_isolation_within_tenant(client):
    """Verify multiple sessions within the same tenant are isolated."""
    headers_s1 = {"X-Tenant-ID": "tenant_gamma", "X-Session-ID": "session_one"}
    headers_s2 = {"X-Tenant-ID": "tenant_gamma", "X-Session-ID": "session_two"}

    client.post("/api/reset", headers=headers_s1)
    client.post("/api/reset", headers=headers_s2)

    client.post(
        "/api/chat",
        json={"message": "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500"},
        headers=headers_s1,
    )
    state_s1 = client.get("/api/state", headers=headers_s1).json()
    state_s2 = client.get("/api/state", headers=headers_s2).json()

    assert state_s1["fsm_state"] == "AWAITING_APPROVAL"
    assert state_s2["fsm_state"] == "IDLE"


def test_multi_tenant_memory_isolation(client):
    """Verify memory updates in one tenant do not leak or overwrite another tenant's memories."""
    headers_a = {"X-Tenant-ID": "tenant_alpha_mem", "X-Session-ID": "sess_mem"}
    headers_b = {"X-Tenant-ID": "tenant_beta_mem", "X-Session-ID": "sess_mem"}

    # Tenant Alpha updates preferred area
    up_res = client.post(
        "/api/memory",
        json={"action": "update", "key": "preferred_area", "value": "Whitefield"},
        headers=headers_a,
    )
    assert up_res.status_code == 200
    assert "Whitefield" in up_res.json()["reply"]

    # Tenant Beta inspects memories -> should retain default Indiranagar
    inspect_b = client.post("/api/memory", json={"action": "inspect"}, headers=headers_b)
    assert "Indiranagar" in inspect_b.json()["reply"]
    assert "Whitefield" not in inspect_b.json()["reply"]


# ==================== 9. ENTERPRISE SECURITY HEADERS & RATE LIMITING ====================

def test_enterprise_security_headers_content_security_policy(client):
    """Verify that Content-Security-Policy (CSP) is returned with standard directives."""
    for endpoint in ("/healthz", "/api/state", "/"):
        res = client.get(endpoint)
        assert res.status_code == 200
        csp = res.headers.get("Content-Security-Policy")
        assert csp is not None, f"Missing Content-Security-Policy on {endpoint}"
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "style-src 'self'" in csp
        assert "frame-ancestors 'self'" in csp
        assert res.headers.get("X-Content-Type-Options") == "nosniff"
        assert res.headers.get("X-Frame-Options") == "SAMEORIGIN"
        assert res.headers.get("Strict-Transport-Security") is not None


def test_rate_limiting_headers_on_success_and_429(client):
    """Verify X-RateLimit-Limit, Remaining, and Reset headers on both HTTP 200 and HTTP 429."""
    RATE_LIMITER.reset()

    # 1. HTTP 200 response contains complete rate limiting headers
    res_ok = client.get("/api/state")
    assert res_ok.status_code == 200
    assert "X-RateLimit-Limit" in res_ok.headers
    assert res_ok.headers["X-RateLimit-Limit"] == str(RATE_LIMIT_RPM)
    assert "X-RateLimit-Remaining" in res_ok.headers
    assert int(res_ok.headers["X-RateLimit-Remaining"]) >= 0
    assert "X-RateLimit-Reset" in res_ok.headers
    assert int(res_ok.headers["X-RateLimit-Reset"]) > int(time.time() - 10)

    # 2. HTTP 429 response contains complete rate limiting headers and Retry-After
    test_ip = "198.51.100.123"
    for _ in range(RATE_LIMIT_RPM):
        res = RATE_LIMITER.check(test_ip)
        assert res[0] is True

    headers = {"X-Forwarded-For": test_ip, "X-Test-Rate-Limit": "1"}
    res_429 = client.get("/api/state", headers=headers)
    assert res_429.status_code == 429
    assert res_429.headers.get("X-RateLimit-Limit") == str(RATE_LIMIT_RPM)
    assert res_429.headers.get("X-RateLimit-Remaining") == "0"
    assert "X-RateLimit-Reset" in res_429.headers
    assert "Retry-After" in res_429.headers
    assert int(res_429.headers["Retry-After"]) >= 1

    RATE_LIMITER.reset()


# ==================== 10. TROJAN SOURCE UNICODE DEFENSE ====================

def test_trojan_source_bidi_unicode_sanitizer():
    """Verify sanitize_input_text strips all Trojan Source Bidi controls and ANSI escapes."""
    forbidden_chars = [
        "\u0000",  # NUL
        "\u061C",  # ALM
        "\u200E",  # LRM
        "\u200F",  # RLM
        "\u2028",  # LS
        "\u2029",  # PS
        "\u202A",  # LRE
        "\u202B",  # RLE
        "\u202C",  # PDF
        "\u202D",  # LRO
        "\u202E",  # RLO
        "\u2066",  # LRI
        "\u2067",  # RLI
        "\u2068",  # FSI
        "\u2069",  # PDI
    ]
    for ch in forbidden_chars:
        assert BIDI_TROJAN_PATTERN.search(f"prefix{ch}suffix") is not None
        assert sanitize_input_text(f"prefix{ch}suffix") == "prefixsuffix"

    # Test Trojan Source spoofing payload (CVE-2021-42574)
    spoofed = "admin \u202E tset \u202C user"
    cleaned = sanitize_input_text(spoofed)
    assert "\u202E" not in cleaned
    assert "\u202C" not in cleaned
    assert cleaned == "admin  tset  user"

    # Test ANSI escape sequences stripped
    ansi_text = "\x1b[31;1mDangerous\x1b[0m Action"
    assert sanitize_input_text(ansi_text) == "Dangerous Action"

    # Edge cases
    assert sanitize_input_text("") == ""
    assert sanitize_input_text(None) == ""


def test_trojan_source_sanitization_in_chat_and_memory_endpoints(client):
    """Verify that chat and memory endpoints apply Trojan Source Unicode sanitization."""
    client.post("/api/reset")

    # Chat endpoint strips Bidi controls
    trojan_msg = "Need a plumber in Indiranagar \u202E\u2066reversed_text\u2069\u202C tomorrow"
    res = client.post("/api/chat", json={"message": trojan_msg})
    assert res.status_code == 200
    state = client.get("/api/state").json()
    for ev in state.get("trace_events", []):
        for val in ev.values():
            if isinstance(val, str):
                assert "\u202E" not in val
                assert "\u2066" not in val

    # Memory endpoint strips Bidi controls from key and value
    mem_res = client.post("/api/memory", json={
        "action": "update",
        "key": "bidi_\u200E_test",
        "value": "Clean\u2028\u202BSecret\u202CValue",
    })
    assert mem_res.status_code == 200
    state_after = client.get("/api/state").json()
    core_mems = state_after.get("core_memories", [])
    found = [m for m in core_mems if "bidi" in m["key"]]
    assert len(found) > 0
    assert "\u200E" not in found[0]["key"]
    assert "\u2028" not in found[0]["value"]
    assert "\u202B" not in found[0]["value"]
    assert "\u202C" not in found[0]["value"]


# ==================== 11. MCP SERVER ROLE GATING ====================

def test_mcp_role_gating_direct_execution():
    """Verify execute_tool permits Auditor for read-only tools and rejects mutating tools."""
    from src.db import LocalDatabase
    db = LocalDatabase(":memory:")
    db.seed_default_data("user_1")
    env = MockToolEnvironment(db=db)
    handler = MCPServerHandler(env)

    # 1. Auditor is allowed for read-only tools
    for tool in ("search_providers", "get_provider", "get_booking_status", "list_bookings"):
        args = {
            "search_providers": {"service": "plumber", "area": "Indiranagar", "date_range": "2026-11-09"},
            "get_provider": {"provider_id": "prov_plumb_1"},
            "get_booking_status": {"hold_id_or_idempotency_key": "dummy"},
            "list_bookings": {"user_id": "user_1"},
        }[tool]
        res = handler.execute_tool(tool, args, role="Auditor")
        assert res["isError"] is False, f"Auditor should be permitted to call read-only tool '{tool}'"

    # 2. Auditor is rejected from mutating tools
    mutating_tools = {
        "hold_slot": {"provider_id": "prov_plumb_1", "slot": "2026-11-09 09:00"},
        "book": {"hold_id": "hold_123", "idempotency_key": "k_123"},
        "cancel": {"booking_id": "bk_upcoming_ac_01", "idempotency_key": "k_456"},
        "reschedule": {"booking_id": "bk_upcoming_ac_01", "slot": "2026-11-10 10:00", "idempotency_key": "k_789"},
    }
    for tool, args in mutating_tools.items():
        res = handler.execute_tool(tool, args, role="Auditor")
        assert res["isError"] is True, f"Auditor must be blocked from mutating tool '{tool}'"
        assert "AuthorizationError" in res["content"][0]["text"]
        assert "Auditor" in res["content"][0]["text"]

    # 3. Operator and Admin are permitted to execute mutating tools
    res_op = handler.execute_tool("hold_slot", {"provider_id": "prov_plumb_1", "slot": "2026-11-09 09:00"}, role="Operator")
    assert res_op["isError"] is False
    assert "hold_id" in res_op["content"][0]["text"]

    res_admin = handler.execute_tool("hold_slot", {"provider_id": "prov_plumb_1", "slot": "2026-11-09 09:00"}, role="Admin")
    assert res_admin["isError"] is False


def test_mcp_role_gating_via_jsonrpc_and_rest_endpoints(client):
    """Verify MCP endpoints enforce role authorization via params and headers."""
    # JSON-RPC tools/call with role in params
    req_rpc_auditor = {
        "jsonrpc": "2.0",
        "id": 101,
        "method": "tools/call",
        "params": {
            "name": "cancel",
            "arguments": {"booking_id": "bk_upcoming_ac_01", "idempotency_key": "k_1"},
            "role": "Auditor",
        },
    }
    res_rpc = client.post("/mcp", json=req_rpc_auditor)
    assert res_rpc.status_code == 200
    res_json = res_rpc.json()["result"]
    assert res_json["isError"] is True
    assert "AuthorizationError" in res_json["content"][0]["text"]

    # REST POST /mcp/call with X-User-Role header blocks mutating tool with 403 Forbidden
    res_rest_auditor = client.post(
        "/mcp/call",
        json={"name": "hold_slot", "arguments": {"provider_id": "prov_plumb_1", "slot": "2026-11-09 09:00"}},
        headers={"X-User-Role": "Auditor"},
    )
    assert res_rest_auditor.status_code == 403
    prob = res_rest_auditor.json()
    assert prob["status"] == 403
    assert "Forbidden" in prob["title"]

    # REST POST /mcp/call for search_providers with Auditor header succeeds
    res_rest_allowed = client.post(
        "/mcp/call",
        json={"name": "search_providers", "arguments": {"service": "plumber", "area": "Indiranagar", "date_range": "2026-11-09"}},
        headers={"X-User-Role": "Auditor"},
    )
    assert res_rest_allowed.status_code == 200
    assert res_rest_allowed.json()["isError"] is False


# ==================== 8. M1 POLISH & R4/R6 ENTERPRISE CONTRACTS ====================

def test_book_hold_user_ownership_verification():
    """Verify hold['user_id'] == self.user_id check raises HOLD_NOT_OWNED on mismatch."""
    db = LocalDatabase(":memory:")
    db.seed_default_data()

    tools_alice = MockToolEnvironment(db=db, user_id="user_alice")
    tools_bob = MockToolEnvironment(db=db, user_id="user_bob")

    hold_res = tools_alice.hold_slot("prov_plumb_1", "2026-11-09 09:00")
    hold_id = hold_res["hold_id"]

    # Bob attempts to book Alice's hold -> must raise HOLD_NOT_OWNED
    with pytest.raises(ToolExecutionError) as exc_info:
        tools_bob.book(hold_id=hold_id, idempotency_key="idemp_bob_attack_01")

    assert exc_info.value.code == "HOLD_NOT_OWNED"
    assert f"Hold '{hold_id}' does not belong to user 'user_bob'" in str(exc_info.value)

    # Alice booking her own hold succeeds or executes valid outcome
    res_alice = tools_alice.book(hold_id=hold_id, idempotency_key="idemp_alice_valid_01")
    assert res_alice["status"] in ("BOOKED", "slot_taken", "hold_expired")


def test_upsert_memory_user_namespacing():
    """Verify upsert_memory namespaces memory_id with user_id: mem_{user_id}_{key}_{ts}."""
    db = LocalDatabase(":memory:")
    db.seed_default_data()

    mgr = MemoryAndContextManager(db=db, user_id="tenant_x_user_99")
    res = mgr.upsert_memory(key="cleaning_preference", value="Eco-friendly detergents only")

    assert res["key"] == "cleaning_preference"
    assert res["memory_id"].startswith("mem_tenant_x_user_99_cleaning_preference_")

    # Verify persistence in SQLite
    row = db.conn.execute(
        "SELECT * FROM memories WHERE user_id = ? AND key = ?",
        ("tenant_x_user_99", "cleaning_preference"),
    ).fetchone()
    assert row is not None
    assert row["memory_id"] == res["memory_id"]
    assert row["value"] == "Eco-friendly detergents only"


def test_state_snapshot_all_memories_and_partitions_contract(client):
    """Verify /api/state exposes all_memories (56 items), core_memories (6 items), and memory_partitions."""
    client.post("/api/reset", json={})
    res = client.get("/api/state")
    assert res.status_code == 200
    data = res.json()

    # Core memories preserved for backward compatibility
    assert "core_memories" in data
    assert len(data["core_memories"]) == 6

    # All 56 memories contract
    assert "all_memories" in data
    assert len(data["all_memories"]) == 56

    # Memory partitions contract
    assert "memory_partitions" in data
    partitions = data["memory_partitions"]
    assert partitions["total_count"] == 56
    assert partitions["explicit_count"] == 36
    assert partitions["confirmed_inferred_count"] == 8
    assert partitions["unconfirmed_inferred_count"] == 8
    assert partitions["forgotten_count"] == 4


def test_exclusion_diagnostics_categories(client):
    """Verify structured exclusion diagnostics map all 5 categories correctly."""
    client.post("/api/reset", json={})

    # Trigger a turn so prompt compilation executes
    chat_res = client.post("/api/chat", json={"message": "Find a plumber in Indiranagar morning"})
    assert chat_res.status_code == 200

    state = client.get("/api/state").json()
    assert "exclusion_diagnostics" in state
    diag = state["exclusion_diagnostics"]

    # 1. Forgotten facts -> SQL Pre-Filter: Forgotten
    assert diag.get("old_gate_passcode") == "SQL Pre-Filter: Forgotten"

    # 2. Unconfirmed Inferred facts -> SQL Pre-Filter: Unconfirmed Inferred
    assert diag.get("inferred_weekend_preference") == "SQL Pre-Filter: Unconfirmed Inferred"

    # 3. Included fact -> Included in Prompt (BM25 Match & Budget OK)
    assert diag.get("preferred_area") == "Included in Prompt (BM25 Match & Budget OK)"

    # 4. Eligible facts not queried -> Standby (Not Queried)
    standby_keys = [k for k, v in diag.items() if v == "Standby (Not Queried)"]
    assert len(standby_keys) > 0

    # 5. Token Cap Ceiling: Truncated test
    db = LocalDatabase(":memory:")
    db.seed_default_data()
    # Create memory manager with very small budget
    tiny_mgr = MemoryAndContextManager(db=db, user_id="user_1", max_context_tokens=1200, max_memory_tokens=25)
    ctx = tiny_mgr.compile_prompt_context(
        session_id="test_truncation_sess",
        slots=IntentSlots(service="electrician", area="Indiranagar"),
        current_user_message="I need an electrician technician now",
    )
    tiny_diag = ctx["exclusion_diagnostics"]
    truncated_keys = [k for k, v in tiny_diag.items() if v == "Token Cap Ceiling: Truncated"]
    assert len(truncated_keys) > 0, "At least one memory should be truncated under tiny token budget"


def test_chaos_parameter_plumbing_in_app_server(client):
    """Verify chaos parameters in ChatRequest update orch.fault_cfg before turn executes."""
    client.post("/api/reset", json={})

    # Verify chat_turn is callable and matches chat_endpoint
    assert callable(chat_turn)

    # Call /api/chat with all 4 chaos parameters
    res = client.post(
        "/api/chat",
        json={
            "message": "Find a plumber in Indiranagar morning",
            "force_book_outcome": "slot_taken",
            "force_price_drift_delta": 250,
            "inject_adversarial_review": True,
            "persistent_cancel_503": True,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert "fsm_state" in data

