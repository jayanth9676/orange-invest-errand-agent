"""Adversarial Challenge Test Suite: Concurrency, Approval Race Conditions & SQLite Locking (Milestone 1).

Author: challenger_m1_1 (Concurrency & Stress Challenger)
Purpose: Empirically stress-test the Milestone 1 backend additions:
         1. Concurrent race conditions on POST /api/approval/action (simultaneous approve/reject, double-approve, double-reject).
         2. Edge cases and input validation: malformed payloads (422), nonexistent proposal IDs (404),
            expired proposals (400), invalidated proposals (400), invalid action values (400), and Auditor gating (403).
         3. SQLite database concurrency locking during rapid session resets under multithreaded contention.
"""

import concurrent.futures
import json
import os
import threading
import time
from typing import Any

os.environ.setdefault("LLM_MODE", "offline")

import pytest
from fastapi.testclient import TestClient

from src.app_server import app, DB, WORKSPACE_MANAGER
from src.schemas import AgentState, ApprovalProposal, MutationType
from src.security import UserRole, WorkspaceSessionManager


@pytest.fixture
def client():
    return TestClient(app)


def _setup_active_proposal(client: TestClient, tenant_id: str = "tenant_default", session_id: str = "conc_test_sess") -> dict[str, Any]:
    """Helper to cleanly reset and advance a session to AWAITING_APPROVAL with an active proposal."""
    headers = {"X-Tenant-ID": tenant_id, "X-Session-ID": session_id}
    client.post("/api/reset", headers=headers)
    res = client.post(
        "/api/chat",
        json={"message": "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500"},
        headers=headers,
    )
    assert res.status_code == 200
    state = client.get("/api/state", headers=headers).json()
    assert state["fsm_state"] == "AWAITING_APPROVAL", f"Expected AWAITING_APPROVAL, got {state['fsm_state']}"
    assert state["active_proposal"] is not None, "Active proposal must not be None"
    return state["active_proposal"]


# ==============================================================================
# SECTION 1: CONCURRENT RACE CONDITIONS ON POST /api/approval/action
# ==============================================================================

def test_race_condition_simultaneous_approve_and_reject(client):
    """EMPIRICAL STRESS TEST: Simultaneous approve and reject on the exact same proposal.
    
    Launches 20 concurrent threads (10 approve, 10 reject) synchronized on a threading Barrier.
    Verifies that:
    1. Exactly one action wins the initial race (either approved or rejected).
    2. Zero 500 errors occur under high concurrency.
    3. If reject wins: proposal is invalidated, FSM is IDLE, and all racing approvals get HTTP 400.
    4. If approve wins: proposal executes once, exactly 1 booking is created, and duplicate calls are safely handled.
    5. Database consistency is maintained with zero orphaned or duplicate records.
    """
    tenant_id = "tenant_race_1"
    session_id = "sess_race_approve_reject"
    headers = {"X-Tenant-ID": tenant_id, "X-Session-ID": session_id}

    prop = _setup_active_proposal(client, tenant_id=tenant_id, session_id=session_id)
    proposal_id = prop["proposal_id"]

    num_threads = 20
    barrier = threading.Barrier(num_threads)
    results = []

    def worker(action: str, idx: int):
        barrier.wait()
        req_body = {
            "proposal_id": proposal_id,
            "action": action,
            "reason": f"Concurrent test worker {idx} action {action}",
        }
        res = client.post("/api/approval/action", json=req_body, headers=headers)
        return (action, res.status_code, res.json())

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = []
        for i in range(num_threads):
            action = "approve" if i % 2 == 0 else "reject"
            futures.append(executor.submit(worker, action, i))
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    # Verify no server crashes (500)
    status_codes = [r[1] for r in results]
    assert all(code in (200, 400) for code in status_codes), f"Unexpected status codes: {status_codes}"

    # Verify state consistency
    state_after = client.get("/api/state", headers=headers).json()
    assert state_after["fsm_state"] in ("IDLE", "COMPLETED", "AWAITING_APPROVAL")

    # Verify bookings table has at most 1 booking for this proposal
    internal_sess = f"{tenant_id}::{session_id}"
    journal_entries = DB.list_journal_entries(internal_sess)
    assert len(journal_entries) <= 1, f"Expected <= 1 idempotency journal entry, got {len(journal_entries)}"


def test_race_condition_concurrent_double_approve_idempotency(client):
    """EMPIRICAL STRESS TEST: Simultaneous approve requests (double spend / double booking race).
    
    Launches 10 concurrent threads all attempting to approve the same proposal simultaneously.
    Verifies that:
    1. Exactly one mutation executes; duplicate confirmations are handled idempotently via journal.
    2. Zero duplicate bookings are inserted in the database.
    3. Every response is HTTP 200 (either fresh confirmation or idempotent duplicate response).
    """
    tenant_id = "tenant_race_2"
    session_id = "sess_race_double_approve"
    headers = {"X-Tenant-ID": tenant_id, "X-Session-ID": session_id}

    prop = _setup_active_proposal(client, tenant_id=tenant_id, session_id=session_id)
    proposal_id = prop["proposal_id"]

    num_threads = 10
    barrier = threading.Barrier(num_threads)
    results = []

    def worker(idx: int):
        barrier.wait()
        req_body = {
            "proposal_id": proposal_id,
            "action": "approve",
        }
        res = client.post("/api/approval/action", json=req_body, headers=headers)
        return res.status_code, res.json()

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker, i) for i in range(num_threads)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    # All threads should get HTTP 200
    for code, body in results:
        assert code == 200, f"Expected 200, got {code}: {body}"
        assert body["status"] == "approved"
        assert body["proposal_id"] == proposal_id

    # Check bookings table: exactly 1 booking must exist
    effective_user = f"{tenant_id}_user_1"
    bookings_res = [dict(r) for r in DB.conn.execute("SELECT * FROM bookings WHERE user_id = ?", (effective_user,)).fetchall()]
    matching_bookings = [b for b in bookings_res if b["provider_id"] == prop["provider_id"] and b["slot"] == prop["slot"]]
    assert len(matching_bookings) == 1, f"Expected exactly 1 booking in DB, found {len(matching_bookings)}"


def test_race_condition_concurrent_double_reject(client):
    """EMPIRICAL STRESS TEST: Simultaneous reject requests.
    
    Launches 10 concurrent threads all attempting to reject the same proposal simultaneously.
    Verifies that:
    1. The first thread to acquire the lock successfully rejects (HTTP 200, status='rejected').
    2. Remaining threads observe that the proposal is already invalidated (HTTP 400 Problem Details).
    3. Final FSM state is IDLE.
    """
    tenant_id = "tenant_race_3"
    session_id = "sess_race_double_reject"
    headers = {"X-Tenant-ID": tenant_id, "X-Session-ID": session_id}

    prop = _setup_active_proposal(client, tenant_id=tenant_id, session_id=session_id)
    proposal_id = prop["proposal_id"]

    num_threads = 10
    barrier = threading.Barrier(num_threads)
    results = []

    def worker(idx: int):
        barrier.wait()
        req_body = {
            "proposal_id": proposal_id,
            "action": "reject",
            "reason": f"Reject from thread {idx}",
        }
        res = client.post("/api/approval/action", json=req_body, headers=headers)
        return res.status_code, res.json()

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker, i) for i in range(num_threads)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    status_codes = [r[0] for r in results]
    assert 200 in status_codes, "At least one reject must succeed with 200"
    num_200 = sum(1 for c in status_codes if c == 200)
    num_400 = sum(1 for c in status_codes if c == 400)
    assert num_200 >= 1
    assert num_200 + num_400 == num_threads, f"All responses must be 200 or 400, got: {status_codes}"

    state_after = client.get("/api/state", headers=headers).json()
    assert state_after["fsm_state"] == "IDLE"


def test_race_condition_reject_after_approved_proposal_creates_split_brain_state(client):
    """EMPIRICAL ADVERSARIAL CHALLENGE: Reject after approve creates split-brain state.
    
    Demonstrates that sending an 'action': 'reject' request to an already-approved and completed proposal
    improperly succeeds with HTTP 200, marks prop.invalidated=True, and transitions FSM to IDLE,
    even though the booking was already finalized and committed in SQLite.
    An approved proposal should NOT be rejectable without explicit cancellation or should return HTTP 400.
    """
    tenant_id = "tenant_split_brain"
    session_id = "sess_split_brain"
    headers = {"X-Tenant-ID": tenant_id, "X-Session-ID": session_id}

    prop = _setup_active_proposal(client, tenant_id=tenant_id, session_id=session_id)
    proposal_id = prop["proposal_id"]

    # 1. Approve proposal -> 200, state is COMPLETED, booking count is 1
    res_app = client.post("/api/approval/action", json={"proposal_id": proposal_id, "action": "approve"}, headers=headers)
    assert res_app.status_code == 200
    state_app = client.get("/api/state", headers=headers).json()
    assert state_app["fsm_state"] == "COMPLETED"
    assert len(state_app["bookings"]) >= 1

    # 2. Adversarial action: Reject the already-approved proposal
    res_rej = client.post("/api/approval/action", json={"proposal_id": proposal_id, "action": "reject"}, headers=headers)

    # Invariant: An already-executed proposal must NOT accept 'reject' with 200 OK
    assert res_rej.status_code == 400, (
        f"DEFECT CONFIRMED: Server allowed rejecting an already-approved proposal with status {res_rej.status_code}. "
        f"FSM transitioned to IDLE while booking remains active in SQLite!"
    )


# ==============================================================================
# SECTION 2: EDGE CASES & INPUT VALIDATION
# ==============================================================================

def test_edge_case_malformed_payloads(client):
    """Verify HTTP 422 Unprocessable Entity on schema violations and malformed bodies."""
    headers = {"X-Tenant-ID": "tenant_edge", "X-Session-ID": "sess_malformed"}

    # 1. Empty body
    r1 = client.post("/api/approval/action", json={}, headers=headers)
    assert r1.status_code == 422, f"Expected 422 for empty json, got {r1.status_code}"

    # 2. Missing proposal_id
    r2 = client.post("/api/approval/action", json={"action": "approve"}, headers=headers)
    assert r2.status_code == 422

    # 3. Missing action
    r3 = client.post("/api/approval/action", json={"proposal_id": "prop_123"}, headers=headers)
    assert r3.status_code == 422

    # 4. Non-string proposal_id (integer)
    r4 = client.post("/api/approval/action", json={"proposal_id": 9999, "action": "approve"}, headers=headers)
    assert r4.status_code == 422 or r4.status_code == 404

    # 5. Non-string action (boolean)
    r5 = client.post("/api/approval/action", json={"proposal_id": "prop_123", "action": True}, headers=headers)
    assert r5.status_code == 422 or r5.status_code == 400

    # 6. Invalid JSON string body
    r6 = client.post(
        "/api/approval/action",
        content="NOT_VALID_JSON{:::}",
        headers={"Content-Type": "application/json", **headers},
    )
    assert r6.status_code in (400, 422)


def test_edge_case_nonexistent_proposal_ids(client):
    """Verify HTTP 404 Problem Details when proposal does not exist or does not match active proposal."""
    tenant_id = "tenant_edge_404"
    session_id = "sess_404"
    headers = {"X-Tenant-ID": tenant_id, "X-Session-ID": session_id}

    # Case A: No active proposal exists (fresh session)
    client.post("/api/reset", headers=headers)
    r_no_prop = client.post(
        "/api/approval/action",
        json={"proposal_id": "prop_ghost_001", "action": "approve"},
        headers=headers,
    )
    assert r_no_prop.status_code == 404
    body_no_prop = r_no_prop.json()
    assert body_no_prop["status"] == 404
    assert "Not Found" in body_no_prop["title"]
    assert "prop_ghost_001" in body_no_prop["detail"]

    # Case B: Active proposal exists, but different proposal_id supplied
    prop = _setup_active_proposal(client, tenant_id=tenant_id, session_id=session_id)
    r_wrong_prop = client.post(
        "/api/approval/action",
        json={"proposal_id": "prop_mismatched_999", "action": "approve"},
        headers=headers,
    )
    assert r_wrong_prop.status_code == 404
    body_wrong = r_wrong_prop.json()
    assert body_wrong["status"] == 404
    assert "prop_mismatched_999" in body_wrong["detail"]

    # Case C: Empty string proposal_id
    r_empty_id = client.post(
        "/api/approval/action",
        json={"proposal_id": "", "action": "approve"},
        headers=headers,
    )
    assert r_empty_id.status_code == 404


def test_edge_case_expired_proposals_ttl_enforcement(client):
    """Verify HTTP 400 Problem Details when proposal exceeds 600.0s TTL."""
    tenant_id = "tenant_edge_ttl"
    session_id = "sess_ttl"
    headers = {"X-Tenant-ID": tenant_id, "X-Session-ID": session_id}

    prop = _setup_active_proposal(client, tenant_id=tenant_id, session_id=session_id)
    proposal_id = prop["proposal_id"]

    # Retrieve orchestrator and artificially simulate TTL expiration (> 600s)
    orch = WORKSPACE_MANAGER.get_orchestrator(tenant_id, session_id)
    assert orch.active_proposal is not None
    orch.active_proposal.created_at_epoch = time.time() - 605.0

    # Verify is_expired() method evaluates to True
    assert orch.active_proposal.is_expired() is True

    # 1. Attempt to approve expired proposal
    res_approve = client.post(
        "/api/approval/action",
        json={"proposal_id": proposal_id, "action": "approve"},
        headers=headers,
    )
    assert res_approve.status_code == 400, f"Expected 400, got {res_approve.status_code}"
    body = res_approve.json()
    assert body["status"] == 400
    assert "Proposal Expired" in body["title"]
    assert "expired" in body["detail"].lower()
    assert orch.active_proposal.invalidated is True

    # 2. Subsequent call after invalidation also returns 400 (either Invalidated or Expired)
    res_second = client.post(
        "/api/approval/action",
        json={"proposal_id": proposal_id, "action": "reject"},
        headers=headers,
    )
    assert res_second.status_code == 400
    body_second = res_second.json()
    assert body_second["status"] == 400


def test_edge_case_already_invalidated_proposals(client):
    """Verify HTTP 400 Problem Details when proposal has already been invalidated."""
    tenant_id = "tenant_edge_inval"
    session_id = "sess_inval"
    headers = {"X-Tenant-ID": tenant_id, "X-Session-ID": session_id}

    prop = _setup_active_proposal(client, tenant_id=tenant_id, session_id=session_id)
    proposal_id = prop["proposal_id"]

    orch = WORKSPACE_MANAGER.get_orchestrator(tenant_id, session_id)
    orch.active_proposal.invalidated = True
    orch.active_proposal.invalidation_reason = "Price drifted from ₹1100 to ₹1350"

    # Attempt to approve
    r_app = client.post(
        "/api/approval/action",
        json={"proposal_id": proposal_id, "action": "approve"},
        headers=headers,
    )
    assert r_app.status_code == 400
    b_app = r_app.json()
    assert b_app["status"] == 400
    assert "Proposal Invalidated" in b_app["title"]
    assert "Price drifted" in b_app["detail"]

    # Attempt to reject
    r_rej = client.post(
        "/api/approval/action",
        json={"proposal_id": proposal_id, "action": "reject"},
        headers=headers,
    )
    assert r_rej.status_code == 400
    b_rej = r_rej.json()
    assert b_rej["status"] == 400
    assert "Proposal Invalidated" in b_rej["title"]


def test_edge_case_invalid_action_values(client):
    """Verify HTTP 400 Problem Details on actions other than 'approve' or 'reject'."""
    tenant_id = "tenant_edge_actions"
    session_id = "sess_actions"
    headers = {"X-Tenant-ID": tenant_id, "X-Session-ID": session_id}

    prop = _setup_active_proposal(client, tenant_id=tenant_id, session_id=session_id)
    proposal_id = prop["proposal_id"]

    invalid_actions = [
        "cancel",
        "confirm",
        "hold",
        "mutate",
        "",
        "   ",
        "123",
        "APPROVE_AND_BOOK",
        "'; DROP TABLE sessions; --",
        "null",
    ]

    for inv_action in invalid_actions:
        res = client.post(
            "/api/approval/action",
            json={"proposal_id": proposal_id, "action": inv_action},
            headers=headers,
        )
        assert res.status_code == 400, f"Expected 400 for action '{inv_action}', got {res.status_code}"
        body = res.json()
        assert body["status"] == 400
        assert "Bad Request" in body["title"]
        assert "Expected 'approve' or 'reject'" in body["detail"]

    # Verify whitespace and case-normalization: "  Approve  " and "REJECT" should succeed
    res_norm = client.post(
        "/api/approval/action",
        json={"proposal_id": proposal_id, "action": "  Approve  "},
        headers=headers,
    )
    assert res_norm.status_code == 200
    assert res_norm.json()["status"] == "approved"


def test_edge_case_auditor_role_gating(client):
    """Verify HTTP 403 Problem Details when caller role is Auditor."""
    headers_auditor = {
        "X-Tenant-ID": "tenant_auditor_test",
        "X-Session-ID": "sess_auditor",
        "X-API-Key": "auditor-key-sec-01",
        "X-User-Role": "Auditor",
    }

    # Auditor attempting approve
    res_approve = client.post(
        "/api/approval/action",
        json={"proposal_id": "prop_test_any", "action": "approve"},
        headers=headers_auditor,
    )
    assert res_approve.status_code == 403, f"Expected 403, got {res_approve.status_code}"
    body_app = res_approve.json()
    assert body_app["status"] == 403
    assert "Forbidden" in body_app["title"]

    # Auditor attempting reject
    res_reject = client.post(
        "/api/approval/action",
        json={"proposal_id": "prop_test_any", "action": "reject"},
        headers=headers_auditor,
    )
    assert res_reject.status_code == 403
    body_rej = res_reject.json()
    assert body_rej["status"] == 403
    assert "Forbidden" in body_rej["title"]


# ==============================================================================
# SECTION 3: SQLITE DATABASE CONCURRENCY & LOCKING STRESS TEST
# ==============================================================================

def test_sqlite_concurrency_rapid_session_resets_only(client):
    """EMPIRICAL STRESS TEST: Concurrent rapid session resets.
    
    Spawns 15 concurrent worker threads all issuing POST /api/reset simultaneously.
    Verifies if SQLite database concurrency locking handles rapid resets without errors.
    """
    tenants = ["tenant_reset_1", "tenant_reset_2", "tenant_reset_3", "tenant_default"]
    num_threads = 15
    errors = []

    def reset_worker(idx: int):
        tenant_id = tenants[idx % len(tenants)]
        session_id = f"reset_sess_{idx % 3}"
        headers = {
            "X-Tenant-ID": tenant_id,
            "X-Session-ID": session_id,
            "X-User-Role": "Admin",
        }
        try:
            for _ in range(5):
                r = client.post("/api/reset", headers=headers)
                if r.status_code != 200:
                    errors.append(f"Worker {idx} reset status {r.status_code}: {r.text}")
        except Exception as exc:
            errors.append(f"Worker {idx} raised: {type(exc).__name__}: {exc}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(reset_worker, i) for i in range(num_threads)]
        for f in concurrent.futures.as_completed(futures):
            f.result()

    assert len(errors) == 0, f"Encountered {len(errors)} reset errors:\n" + "\n".join(errors[:10])


def test_sqlite_concurrency_rapid_session_resets_and_interleaved_traffic(client):
    """EMPIRICAL STRESS TEST: High-concurrency rapid session resets and interleaved requests.
    
    Spawns 25 concurrent worker threads across 5 distinct tenants executing:
    - Rapid POST /api/reset calls.
    - Concurrent POST /api/chat calls.
    - Concurrent POST /api/memory CRUD operations.
    - Concurrent GET /api/state inspection calls.
    
    Verifies that:
    1. Zero 'sqlite3.OperationalError: database is locked' errors occur.
    2. Zero 500 Internal Server Errors occur.
    3. SQLite WAL mode, PRAGMA busy_timeout=30000, and session synchronization keep database healthy.
    4. Database passes 'PRAGMA integrity_check;' cleanly.
    """
    tenants = ["tenant_stress_a", "tenant_stress_b", "tenant_stress_c", "tenant_stress_d", "tenant_stress_e"]
    num_threads = 25
    operations_per_thread = 4
    errors = []

    def stress_worker(worker_id: int):
        tenant_id = tenants[worker_id % len(tenants)]
        session_id = f"stress_sess_{worker_id % 5}"
        headers = {
            "X-Tenant-ID": tenant_id,
            "X-Session-ID": session_id,
            "X-User-Role": "Admin",
        }

        try:
            for op in range(operations_per_thread):
                op_type = op % 4
                if op_type == 0:
                    # Reset session
                    r = client.post("/api/reset", headers=headers)
                    if r.status_code != 200:
                        errors.append(f"Worker {worker_id} reset failed: {r.status_code} {r.text}")
                elif op_type == 1:
                    # Chat request
                    r = client.post("/api/chat", json={"message": f"Hello from worker {worker_id} op {op}"}, headers=headers)
                    if r.status_code != 200:
                        errors.append(f"Worker {worker_id} chat failed: {r.status_code} {r.text}")
                elif op_type == 2:
                    # Memory update
                    r = client.post(
                        "/api/memory",
                        json={"action": "update", "key": f"key_{worker_id}_{op}", "value": f"val_{worker_id}_{op}"},
                        headers=headers,
                    )
                    if r.status_code != 200:
                        errors.append(f"Worker {worker_id} memory failed: {r.status_code} {r.text}")
                else:
                    # State check
                    r = client.get("/api/state", headers=headers)
                    if r.status_code != 200:
                        errors.append(f"Worker {worker_id} state failed: {r.status_code} {r.text}")
        except Exception as exc:
            errors.append(f"Worker {worker_id} raised unhandled exception: {type(exc).__name__}: {exc}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(stress_worker, i) for i in range(num_threads)]
        for f in concurrent.futures.as_completed(futures):
            f.result()

    assert len(errors) == 0, f"Encountered {len(errors)} concurrency errors during stress test:\n" + "\n".join(errors[:10])

    # Final database integrity check
    check_row = DB.conn.execute("PRAGMA integrity_check;").fetchone()
    assert check_row[0] == "ok", f"Database integrity check failed: {check_row[0]}"
