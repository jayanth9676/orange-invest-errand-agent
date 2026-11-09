"""Adversarial Challenge Test Suite: Multi-Tenant & RBAC Verification (Milestone 1).

Author: Challenger M1-1 (Adversarial Multi-Tenant & RBAC Challenger)
Purpose: Empirically stress-test multi-tenant boundaries and RBAC privilege escalation
         assumptions in Errand Agent Enterprise.
"""

import json
import os
import pytest
from fastapi.testclient import TestClient

# Ensure deterministic offline mode
os.environ.setdefault("LLM_MODE", "offline")

from src.app_server import app, DB
from src.security import UserRole, resolve_role, REQUIRE_API_KEY


@pytest.fixture
def client():
    return TestClient(app)


# ==============================================================================
# SECTION 1: RBAC PRIVILEGE ESCALATION VULNERABILITIES
# ==============================================================================

def test_auditor_privilege_escalation_mcp_call_body_override(client):
    """REMEDIATION VERIFICATION: Auditor role body override on /mcp/call is strictly blocked.
    
    When an Auditor provides 'role': 'Admin' in the JSON body of POST /mcp/call,
    authenticated request role takes strict precedence, rejecting the mutation with 403 Forbidden.
    """
    headers = {"X-API-Key": "auditor-key-sec-01", "X-User-Role": "Auditor"}

    # 1. Standard Auditor request without body override is blocked with 403 Forbidden
    res_normal = client.post(
        "/mcp/call",
        json={
            "name": "hold_slot",
            "arguments": {"provider_id": "prov_plumb_1", "slot": "2026-11-09 09:00"},
        },
        headers=headers,
    )
    assert res_normal.status_code == 403, "Normal auditor request must be rejected with 403"

    # 2. Adversarial payload: Inject 'role': 'Admin' into request body
    res_exploit = client.post(
        "/mcp/call",
        json={
            "name": "hold_slot",
            "arguments": {"provider_id": "prov_plumb_1", "slot": "2026-11-09 09:00"},
            "role": "Admin",
        },
        headers=headers,
    )
    assert res_exploit.status_code == 403, "Body role override must NOT bypass RBAC (must return 403)"
    body = res_exploit.json()
    assert body.get("status") == 403
    assert "Forbidden" in body.get("title", "")


def test_auditor_privilege_escalation_jsonrpc_params_override(client):
    """REMEDIATION VERIFICATION: Auditor role params override on JSON-RPC /mcp is strictly blocked.
    
    In src/mcp_server.py, default_role takes strict precedence over params.role,
    rejecting mutating tool execution with AuthorizationError even when role='Admin' is in params.
    """
    headers = {"X-API-Key": "auditor-key-sec-01", "X-User-Role": "Auditor"}

    # 1. Normal JSON-RPC tools/call without role override is rejected with AuthorizationError
    req_normal = {
        "jsonrpc": "2.0",
        "id": 101,
        "method": "tools/call",
        "params": {
            "name": "hold_slot",
            "arguments": {"provider_id": "prov_plumb_1", "slot": "2026-11-09 09:00"},
        },
    }
    res_normal = client.post("/mcp", json=req_normal, headers=headers)
    assert res_normal.status_code == 200
    res_normal_json = res_normal.json()["result"]
    assert res_normal_json["isError"] is True
    assert "AuthorizationError" in res_normal_json["content"][0]["text"]

    # 2. Adversarial JSON-RPC tools/call: supply 'role': 'Admin' in params
    req_exploit = {
        "jsonrpc": "2.0",
        "id": 102,
        "method": "tools/call",
        "params": {
            "name": "hold_slot",
            "arguments": {"provider_id": "prov_plumb_1", "slot": "2026-11-09 09:00"},
            "role": "Admin",
        },
    }
    res_exploit = client.post("/mcp", json=req_exploit, headers=headers)
    assert res_exploit.status_code == 200
    res_exploit_json = res_exploit.json()["result"]
    assert res_exploit_json["isError"] is True
    assert "AuthorizationError" in res_exploit_json["content"][0]["text"]


def test_auditor_enforcement_on_direct_endpoints(client):
    """Verify that direct endpoints (/api/chat, /api/reset, /api/memory) enforce RBAC properly."""
    headers = {"X-API-Key": "auditor-key-sec-01", "X-User-Role": "Auditor"}

    # Blocked on /api/chat
    r_chat = client.post("/api/chat", json={"message": "hello"}, headers=headers)
    assert r_chat.status_code == 403

    # Blocked on /api/reset
    r_reset = client.post("/api/reset", headers=headers)
    assert r_reset.status_code == 403

    # Blocked on /api/memory mutating action (update)
    r_mem = client.post("/api/memory", json={"action": "update", "key": "k", "value": "v"}, headers=headers)
    assert r_mem.status_code == 403

    # Allowed on /api/memory read action (inspect)
    r_inspect = client.post("/api/memory", json={"action": "inspect"}, headers=headers)
    assert r_inspect.status_code == 200


def test_unauthenticated_default_admin_elevation(client):
    """DOCUMENTATION OF RISK: Unauthenticated requests default to UserRole.ADMIN when REQUIRE_API_KEY=False."""
    # When no headers are sent and REQUIRE_API_KEY is False:
    role = resolve_role(None, None, require_api_key=False)
    assert role == UserRole.ADMIN, "Default dev role is Admin, granting full access if exposed without auth"


# ==============================================================================
# SECTION 2: MULTI-TENANT BOUNDARY CROSSING & STATE CORRUPTION
# ==============================================================================

def test_cross_tenant_booking_cancellation_by_another_tenant(client):
    """REMEDIATION VERIFICATION: Tenant A cannot cancel Tenant B's bookings via /mcp/call.
    
    MockToolEnvironment.cancel verifies that booking['user_id'] == self.user_id. Cross-tenant
    cancellation attempts are rejected with ToolExecutionError (BOOKING_NOT_OWNED).
    """
    # 1. Seed a booking owned by Tenant Beta (user_id='tenant_beta_user_1')
    target_booking_id = "bk_tenant_beta_isolated_01"
    DB.conn.execute("DELETE FROM bookings WHERE booking_id = ?", (target_booking_id,))
    DB.insert_booking(
        booking_id=target_booking_id,
        user_id="tenant_beta_user_1",
        provider_id="prov_plumb_1",
        provider_name="AquaFix Plumbing",
        service="plumber",
        slot="2026-11-09 09:00",
        price=1100,
        status="CONFIRMED",
        hold_id=None,
        idempotency_key="key_tenant_beta_orig",
    )

    # Verify Tenant Beta sees it as CONFIRMED
    state_b = client.get("/api/state", headers={"X-Tenant-ID": "tenant_beta", "X-Session-ID": "sess_b"}).json()
    b_records = [b for b in state_b["bookings"] if b["booking_id"] == target_booking_id]
    assert len(b_records) == 1
    assert b_records[0]["status"] == "CONFIRMED"

    # 2. Tenant Alpha calls cancel on Tenant Beta's booking
    headers_alpha = {
        "X-Tenant-ID": "tenant_alpha",
        "X-Session-ID": "sess_alpha",
        "X-User-Role": "Operator",
    }
    res_cancel = client.post(
        "/mcp/call",
        json={"name": "cancel", "arguments": {"booking_id": target_booking_id, "idempotency_key": "k_alpha_cancel"}},
        headers=headers_alpha,
    )
    assert res_cancel.status_code == 200
    res_data = res_cancel.json()
    assert res_data.get("isError") is True
    assert "BOOKING_NOT_OWNED" in res_data["content"][0]["text"]

    # 3. Verify Tenant Beta's booking status remains strictly CONFIRMED
    state_b_after = client.get("/api/state", headers={"X-Tenant-ID": "tenant_beta", "X-Session-ID": "sess_b"}).json()
    b_records_after = [b for b in state_b_after["bookings"] if b["booking_id"] == target_booking_id]
    assert b_records_after[0]["status"] == "CONFIRMED", "Tenant Alpha was blocked from mutating Tenant Beta's booking"


def test_cross_tenant_booking_listing_information_disclosure(client):
    """REMEDIATION VERIFICATION: Tenant Alpha cannot list bookings belonging to default user_1 or other tenants.
    
    The list_bookings tool restricts the query to the caller's tenant-prefixed user.
    """
    headers_alpha = {
        "X-Tenant-ID": "tenant_alpha",
        "X-Session-ID": "sess_alpha",
        "X-User-Role": "Auditor",
    }
    # Tenant Alpha queries user_1's bookings
    res = client.post(
        "/mcp/call",
        json={"name": "list_bookings", "arguments": {"user_id": "user_1"}},
        headers=headers_alpha,
    )
    assert res.status_code == 200
    bookings = json.loads(res.json()["content"][0]["text"])["bookings"]
    booking_ids = [b["booking_id"] for b in bookings]
    # Default user_1 bookings are isolated and not disclosed to Tenant Alpha
    assert "bk_upcoming_ac_01" not in booking_ids and "bk_past_elec_01" not in booking_ids


def test_cross_tenant_session_corruption_via_session_id_collision(client):
    """REMEDIATION VERIFICATION: Attacker in tenant_default cannot wipe Tenant Victim's SQLite session records.
    
    session_id delimiter injection ('::') from tenant_default is sanitized, preventing collision with namespaced sessions.
    """
    h_victim = {"X-Tenant-ID": "tenant_victim", "X-Session-ID": "sess_victim"}
    client.post("/api/reset", headers=h_victim)

    # 1. Tenant Victim executes a turn and enters AWAITING_APPROVAL with active proposal
    res_chat = client.post(
        "/api/chat",
        json={"message": "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500"},
        headers=h_victim,
    )
    assert res_chat.status_code == 200
    state_victim = client.get("/api/state", headers=h_victim).json()
    assert state_victim["fsm_state"] == "AWAITING_APPROVAL"
    assert state_victim["active_proposal"] is not None

    # Verify Victim has persistent conversation history and session_state in DB
    int_id = "tenant_victim::sess_victim"
    hist_count = DB.conn.execute("SELECT COUNT(*) FROM conversation_history WHERE session_id = ?", (int_id,)).fetchone()[0]
    assert hist_count > 0, "Victim must have conversation history in DB"

    # 2. Attacker in tenant_default targets the victim's internal session ID
    h_attacker = {"X-Tenant-ID": "tenant_default", "X-Session-ID": "tenant_victim::sess_victim"}
    r_reset = client.post("/api/reset", headers=h_attacker)
    assert r_reset.status_code == 200

    # 3. Verify Victim's DB records were NOT wiped by Attacker's reset call
    hist_count_after = DB.conn.execute("SELECT COUNT(*) FROM conversation_history WHERE session_id = ?", (int_id,)).fetchone()[0]
    assert hist_count_after > 0, "Victim's conversation history was protected from cross-tenant reset"

    db_state = DB.conn.execute("SELECT * FROM session_state WHERE session_id = ?", (int_id,)).fetchone()
    assert db_state["fsm_state"] == "AWAITING_APPROVAL", "Victim's persisted DB state was protected from cross-tenant reset"


def test_cross_tenant_memory_access_via_tenant_default(client):
    """REMEDIATION VERIFICATION: Un-namespaced tenant_default cannot access or spoof another tenant's memories.
    
    WorkspaceSessionManager prevents tenant_default callers from accessing user IDs starting with tenant prefixes.
    """
    # 1. Tenant Target stores a secret memory
    h_target = {"X-Tenant-ID": "tenant_target", "X-Session-ID": "sess_t", "X-User-Role": "Admin"}
    client.post(
        "/api/memory",
        json={"action": "update", "key": "secret_bank_pin", "value": "PIN_9988_SECRET"},
        headers=h_target,
    )

    # 2. Attacker in tenant_default supplies X-User-ID: tenant_target_user_1
    h_attacker = {
        "X-Tenant-ID": "tenant_default",
        "X-Session-ID": "sess_attack",
        "X-User-ID": "tenant_target_user_1",
        "X-User-Role": "Admin",
    }
    res_inspect = client.post("/api/memory", json={"action": "inspect"}, headers=h_attacker)
    assert res_inspect.status_code == 200
    reply = res_inspect.json().get("reply", "")
    assert "PIN_9988_SECRET" not in reply, "Tenant memory isolation prevented cross-tenant memory disclosure"
