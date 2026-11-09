"""Adversarial Challenge Test Suite: Memory Governance, Multi-User Isolation & Chaos Injection (Milestone 2).

Author: Challenger M2-1 (Adversarial Memory Governance & Chaos Challenger)
Purpose: Empirically stress-test hold ownership hijacking defense, memory ID namespacing & concurrency,
         partition count integrity across lifecycle operations, and chaos price drift detection.
"""

import concurrent.futures
import json
import os
import time
import pytest
from fastapi.testclient import TestClient

# Ensure deterministic offline mode
os.environ.setdefault("LLM_MODE", "offline")

from src.app_server import app, DB, WORKSPACE_MANAGER
from src.db import LocalDatabase
from src.mcp_server import MCPServerHandler
from src.memory_context import MemoryAndContextManager
from src.mock_tools import MockToolEnvironment, ToolExecutionError
from src.orchestrator import ErrandAgentOrchestrator
from src.schemas import IntentSlots, MutationType
from src.security import UserRole, WorkspaceSessionManager


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/api/reset", json={})
    yield c
    c.post("/api/reset", json={})


# ==============================================================================
# SECTION 1: CROSS-USER HOLD HIJACKING & OWNERSHIP ENFORCEMENT
# ==============================================================================

def test_adversarial_cross_user_hold_hijacking_direct_tool():
    """Adversarial test: User A acquires hold; User B attempts to book User A's hold.
    
    Verifies User B is strictly blocked with ToolExecutionError('HOLD_NOT_OWNED'),
    repeated hijacking attempts are rejected, and User A can still book their hold.
    """
    db = LocalDatabase(":memory:")
    db.seed_default_data()

    tools_alice = MockToolEnvironment(db=db, user_id="user_alice")
    tools_bob = MockToolEnvironment(db=db, user_id="user_bob")
    tools_charlie = MockToolEnvironment(db=db, user_id="user_charlie")

    # Step 1: Alice acquires hold
    hold_res = tools_alice.hold_slot("prov_plumb_1", "2026-11-09 09:00")
    hold_id = hold_res["hold_id"]
    assert hold_id.startswith("hold_")

    # Step 2: Bob attempts to hijack Alice's hold
    with pytest.raises(ToolExecutionError) as exc_bob:
        tools_bob.book(hold_id=hold_id, idempotency_key="idemp_bob_attack_01")
    assert exc_bob.value.code == "HOLD_NOT_OWNED"
    assert f"Hold '{hold_id}' does not belong to user 'user_bob'" in str(exc_bob.value)

    # Step 3: Charlie attempts to hijack Alice's hold with multiple keys
    for attempt in range(5):
        with pytest.raises(ToolExecutionError) as exc_charlie:
            tools_charlie.book(hold_id=hold_id, idempotency_key=f"idemp_charlie_{attempt}")
        assert exc_charlie.value.code == "HOLD_NOT_OWNED"
        assert f"Hold '{hold_id}' does not belong to user 'user_charlie'" in str(exc_charlie.value)

    # Step 4: Bob attempts to book with Alice's hold and a spoofed user_id in arguments
    # (MockToolEnvironment strictly uses its initialized self.user_id)
    with pytest.raises(ToolExecutionError) as exc_spoof:
        tools_bob.book(hold_id=hold_id, idempotency_key="idemp_spoof_01")
    assert exc_spoof.value.code == "HOLD_NOT_OWNED"

    # Step 5: Post-attack integrity - Alice books her own hold successfully
    tools_alice.fault_cfg.force_book_outcome = "success"
    res_alice = tools_alice.book(hold_id=hold_id, idempotency_key="idemp_alice_legit_01")
    assert res_alice["status"] == "BOOKED"
    assert res_alice["provider_id"] == "prov_plumb_1"


def test_adversarial_cross_tenant_hold_hijacking_mcp():
    """Adversarial test: Tenant A creates a hold; Tenant B attempts to book via MCP endpoints.
    
    Verifies that calling the MCP 'book' tool from a different tenant's MCP handler
    fails with HOLD_NOT_OWNED and returns isError=True.
    """
    db = LocalDatabase(":memory:")
    db.seed_default_data()

    mgr = WorkspaceSessionManager(db=db)
    # Orchestrator and MCP for Tenant Alpha
    orch_alpha = mgr.get_orchestrator("tenant_alpha", "sess_alpha", "alice")
    mcp_alpha = mgr.get_mcp_handler("tenant_alpha", "sess_alpha")

    # Orchestrator and MCP for Tenant Beta
    orch_beta = mgr.get_orchestrator("tenant_beta", "sess_beta", "bob")
    mcp_beta = mgr.get_mcp_handler("tenant_beta", "sess_beta")

    # Alpha acquires hold
    hold_call = mcp_alpha.execute_tool(
        "hold_slot",
        {"provider_id": "prov_plumb_1", "slot": "2026-11-09 09:00"},
        role="Admin",
    )
    assert not hold_call.get("isError")
    hold_data = json.loads(hold_call["content"][0]["text"])
    hold_id = hold_data["hold_id"]

    # Beta attempts to book Alpha's hold via MCP execute_tool
    hijack_call = mcp_beta.execute_tool(
        "book",
        {"hold_id": hold_id, "idempotency_key": "idemp_beta_hijack_01"},
        role="Admin",
    )
    assert hijack_call.get("isError") is True
    err_text = hijack_call["content"][0]["text"]
    assert "HOLD_NOT_OWNED" in err_text
    assert f"Hold '{hold_id}' does not belong to user 'tenant_beta_bob'" in err_text

    # Beta attempts to book via JSON-RPC 2.0 dispatch
    jsonrpc_req = {
        "jsonrpc": "2.0",
        "id": 101,
        "method": "tools/call",
        "params": {
            "name": "book",
            "arguments": {"hold_id": hold_id, "idempotency_key": "idemp_beta_rpc_01"},
        },
    }
    rpc_res = mcp_beta.handle_jsonrpc(jsonrpc_req)
    assert rpc_res["result"].get("isError") is True
    assert "HOLD_NOT_OWNED" in rpc_res["result"]["content"][0]["text"]


def test_adversarial_hold_hijack_precedes_expiration_check():
    """Adversarial test: Bob attempts to book Alice's expired hold.
    
    Verifies that HOLD_NOT_OWNED is raised before expiration logic executes,
    preventing Bob from probing hold expiry or updating its status to EXPIRED.
    """
    db = LocalDatabase(":memory:")
    db.seed_default_data()

    tools_alice = MockToolEnvironment(db=db, user_id="user_alice")
    tools_bob = MockToolEnvironment(db=db, user_id="user_bob")

    hold_res = tools_alice.hold_slot("prov_plumb_1", "2026-11-09 09:00")
    hold_id = hold_res["hold_id"]

    # Fast-forward DB expiration epoch to past
    db.conn.execute(
        "UPDATE slot_holds SET expires_at_epoch = ? WHERE hold_id = ?",
        (time.time() - 3600.0, hold_id),
    )
    db.conn.commit()

    # Bob attempts to book Alice's expired hold -> MUST raise HOLD_NOT_OWNED, NOT return hold_expired
    with pytest.raises(ToolExecutionError) as exc_bob:
        tools_bob.book(hold_id=hold_id, idempotency_key="idemp_bob_expired_probe")
    assert exc_bob.value.code == "HOLD_NOT_OWNED"

    # Alice booking her own expired hold returns hold_expired
    res_alice = tools_alice.book(hold_id=hold_id, idempotency_key="idemp_alice_probe")
    assert res_alice.get("status") == "hold_expired"


def test_adversarial_nonexistent_hold_vs_unowned_hold():
    """Adversarial test: Distinguish between non-existent hold and unowned hold."""
    db = LocalDatabase(":memory:")
    db.seed_default_data()

    tools_alice = MockToolEnvironment(db=db, user_id="user_alice")
    tools_bob = MockToolEnvironment(db=db, user_id="user_bob")

    # Non-existent hold
    with pytest.raises(ToolExecutionError) as exc_nonexistent:
        tools_bob.book(hold_id="hold_nonexistent_9999", idempotency_key="idemp_1")
    assert exc_nonexistent.value.code == "invalid_hold"

    # Real hold owned by Alice
    hold_res = tools_alice.hold_slot("prov_plumb_1", "2026-11-09 09:00")
    with pytest.raises(ToolExecutionError) as exc_unowned:
        tools_bob.book(hold_id=hold_res["hold_id"], idempotency_key="idemp_2")
    assert exc_unowned.value.code == "HOLD_NOT_OWNED"


# ==============================================================================
# SECTION 2: MEMORY ID ISOLATION & CONCURRENT UPSERT STRESS
# ==============================================================================

def test_adversarial_memory_id_namespacing_across_tenants():
    """Adversarial test: Concurrent/sequential upserts of identical keys across multiple users.
    
    Verifies that distinct user_ids generate distinct namespaced memory_ids and
    do not overwrite each other in SQLite.
    """
    db = LocalDatabase(":memory:")
    db.seed_default_data()

    users = ["user_alpha", "user_beta", "tenant_1_alice", "tenant_2_bob", "user_1"]
    identical_key = "special_cleaning_instructions"

    managers = {u: MemoryAndContextManager(db=db, user_id=u) for u in users}
    results = {}

    for u in users:
        val = f"Custom value for {u} at {time.time()}"
        res = managers[u].upsert_memory(key=identical_key, value=val)
        results[u] = res

    # 1. Verify each memory_id is strictly namespaced with user_id
    for u in users:
        mem_id = results[u]["memory_id"]
        assert mem_id.startswith(f"mem_{u}_{identical_key}_")

    # 2. Verify all memory_ids are pairwise distinct
    all_ids = [results[u]["memory_id"] for u in users]
    assert len(set(all_ids)) == len(users)

    # 3. Verify SQLite contains all distinct entries
    rows = db.conn.execute(
        "SELECT user_id, memory_id, value FROM memories WHERE key = ?",
        (identical_key,),
    ).fetchall()
    assert len(rows) == len(users)
    found_users = {r["user_id"] for r in rows}
    assert found_users == set(users)


def test_adversarial_concurrent_memory_upsert_threads():
    """Adversarial stress test: Multi-threaded simultaneous upserts of identical keys.
    
    Spawns 12 worker threads each representing a unique user concurrently upserting
    the exact same key 'common_contested_key' with separate database handles into SQLite WAL.
    """
    db_file = "test_adversarial_concurrency.sqlite"
    init_db = LocalDatabase(db_file)
    init_db.seed_default_data()
    init_db.conn.close()

    num_threads = 12
    barrier_key = "common_contested_key"

    def worker_upsert(worker_id: int):
        user = f"thread_user_{worker_id:02d}"
        thread_db = LocalDatabase(db_file)
        try:
            mgr = MemoryAndContextManager(db=thread_db, user_id=user)
            # Attempt initial upsert and update on the same key
            r1 = mgr.upsert_memory(key=barrier_key, value=f"Initial value for {user}")
            r2 = mgr.upsert_memory(key=barrier_key, value=f"Updated value for {user}")
            return user, r2["memory_id"]
        finally:
            thread_db.conn.close()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker_upsert, i) for i in range(num_threads)]
            results = [f.result() for f in futures]

        assert len(results) == num_threads
        mem_ids = [r[1] for r in results]
        assert len(set(mem_ids)) == num_threads, "All memory_ids must be unique across users"

        # Verify database has exactly num_threads records for the contested key
        verify_db = LocalDatabase(db_file)
        rows = verify_db.conn.execute(
            "SELECT user_id, memory_id, value FROM memories WHERE key = ?",
            (barrier_key,),
        ).fetchall()
        assert len(rows) == num_threads
        for r in rows:
            assert r["value"] == f"Updated value for {r['user_id']}"
        verify_db.conn.close()
    finally:
        if os.path.exists(db_file):
            try:
                os.remove(db_file)
            except Exception:
                pass


def test_adversarial_memory_cross_user_operations_isolation():
    """Adversarial test: Cross-user update, forget, and confirm mutations do NOT leak.
    
    - User A forgets a key; User B's identical key remains unforgotten.
    - User A confirms an inferred key; User B's identical key remains unconfirmed.
    - User A updates a key; User B's identical key remains unchanged.
    """
    db = LocalDatabase(":memory:")
    db.seed_default_data()

    mgr_a = MemoryAndContextManager(db=db, user_id="user_tenant_a")
    mgr_b = MemoryAndContextManager(db=db, user_id="user_tenant_b")

    # 1. Setup shared keys
    mgr_a.upsert_memory("wifi_ssid", "HomeNetworkA", source_type="EXPLICIT", confirmed=True)
    mgr_b.upsert_memory("wifi_ssid", "HomeNetworkB", source_type="EXPLICIT", confirmed=True)

    mgr_a.upsert_memory("inferred_pref", "Pref A", source_type="INFERRED", confirmed=False)
    mgr_b.upsert_memory("inferred_pref", "Pref B", source_type="INFERRED", confirmed=False)

    # 2. User A forgets 'wifi_ssid'
    forgotten = mgr_a.forget_memory("wifi_ssid")
    assert len(forgotten) == 1

    # Inspect B: wifi_ssid MUST NOT be forgotten
    mems_b = mgr_b.inspect_memories(include_unconfirmed_inferred=True)
    wifi_b = [m for m in mems_b if m["key"] == "wifi_ssid"]
    assert len(wifi_b) == 1
    assert wifi_b[0]["value"] == "HomeNetworkB"
    assert wifi_b[0]["forgotten"] == 0

    # 3. User A confirms 'inferred_pref'
    mgr_a.confirm_inferred_memory("inferred_pref")
    row_a = db.conn.execute("SELECT confirmed FROM memories WHERE user_id = 'user_tenant_a' AND key = 'inferred_pref'").fetchone()
    assert row_a["confirmed"] == 1

    # User B's inferred_pref MUST remain unconfirmed
    row_b = db.conn.execute("SELECT confirmed FROM memories WHERE user_id = 'user_tenant_b' AND key = 'inferred_pref'").fetchone()
    assert row_b["confirmed"] == 0

    # 4. User A updates 'wifi_ssid'
    mgr_a.upsert_memory("wifi_ssid", "NewNetworkA")
    row_a_new = db.conn.execute("SELECT value FROM memories WHERE user_id = 'user_tenant_a' AND key = 'wifi_ssid'").fetchone()
    row_b_same = db.conn.execute("SELECT value FROM memories WHERE user_id = 'user_tenant_b' AND key = 'wifi_ssid'").fetchone()
    assert row_a_new["value"] == "NewNetworkA"
    assert row_b_same["value"] == "HomeNetworkB"


# ==============================================================================
# SECTION 3: PARTITION COUNT INTEGRITY ON /api/state
# ==============================================================================

def test_adversarial_partition_counts_dynamic_lifecycle(client):
    """Adversarial test: Trace /api/state memory_partitions through a full lifecycle.
    
    Verifies that partition metrics dynamically adjust and maintain exact sum invariants
    through forget, confirm, and update operations.
    """
    client.post("/api/reset", json={})
    state0 = client.get("/api/state").json()
    part0 = state0["memory_partitions"]

    # Baseline 56-memory invariant
    assert part0["explicit_count"] == 36
    assert part0["confirmed_inferred_count"] == 8
    assert part0["unconfirmed_inferred_count"] == 8
    assert part0["forgotten_count"] == 4
    assert part0["total_count"] == 56
    assert (
        part0["explicit_count"]
        + part0["confirmed_inferred_count"]
        + part0["unconfirmed_inferred_count"]
        + part0["forgotten_count"]
    ) == 56

    # 1. Action: Forget an explicit memory ("preferred_area")
    res_forget1 = client.post("/api/memory", json={"action": "forget", "key": "preferred_area"})
    assert res_forget1.status_code == 200
    state1 = client.get("/api/state").json()
    part1 = state1["memory_partitions"]

    assert part1["explicit_count"] == 35, "Explicit count should decrement by 1"
    assert part1["forgotten_count"] == 5, "Forgotten count should increment by 1"
    assert part1["confirmed_inferred_count"] == 8
    assert part1["unconfirmed_inferred_count"] == 8
    assert part1["total_count"] == 56
    assert sum([part1["explicit_count"], part1["confirmed_inferred_count"], part1["unconfirmed_inferred_count"], part1["forgotten_count"]]) == 56

    # 2. Action: Confirm an unconfirmed inferred memory ("inferred_weekend_preference")
    res_conf = client.post("/api/memory", json={"action": "confirm", "key": "inferred_weekend_preference"})
    assert res_conf.status_code == 200
    state2 = client.get("/api/state").json()
    part2 = state2["memory_partitions"]

    assert part2["unconfirmed_inferred_count"] == 7, "Unconfirmed count should decrement by 1"
    assert part2["confirmed_inferred_count"] == 9, "Confirmed inferred count should increment by 1"
    assert part2["explicit_count"] == 35
    assert part2["forgotten_count"] == 5
    assert part2["total_count"] == 56
    assert sum([part2["explicit_count"], part2["confirmed_inferred_count"], part2["unconfirmed_inferred_count"], part2["forgotten_count"]]) == 56

    # 3. Action: Forget the newly confirmed inferred memory ("inferred_weekend_preference")
    res_forget2 = client.post("/api/memory", json={"action": "forget", "key": "inferred_weekend_preference"})
    assert res_forget2.status_code == 200
    state3 = client.get("/api/state").json()
    part3 = state3["memory_partitions"]

    assert part3["confirmed_inferred_count"] == 8, "Confirmed inferred count should decrement by 1"
    assert part3["forgotten_count"] == 6, "Forgotten count should increment by 1"
    assert part3["unconfirmed_inferred_count"] == 7
    assert part3["explicit_count"] == 35
    assert part3["total_count"] == 56
    assert sum([part3["explicit_count"], part3["confirmed_inferred_count"], part3["unconfirmed_inferred_count"], part3["forgotten_count"]]) == 56

    # 4. Action: Insert a completely new explicit memory via update
    res_add = client.post("/api/memory", json={"action": "update", "key": "solar_panel_capacity", "value": "10kW Enphase"})
    assert res_add.status_code == 200
    state4 = client.get("/api/state").json()
    part4 = state4["memory_partitions"]

    assert part4["explicit_count"] == 36, "Explicit count should increment by 1"
    assert part4["total_count"] == 57, "Total count should increment to 57"
    assert len(state4["all_memories"]) == 57
    assert sum([part4["explicit_count"], part4["confirmed_inferred_count"], part4["unconfirmed_inferred_count"], part4["forgotten_count"]]) == 57


def test_adversarial_partition_counts_bulk_forget(client):
    """Adversarial test: Forgetting by a common substring that matches multiple memories."""
    client.post("/api/reset", json={})
    state_before = client.get("/api/state").json()
    before_part = state_before["memory_partitions"]

    # Match multiple household notes: "household_note_1" matches household_note_1, 10, 11, ..., 19
    res_forget = client.post("/api/memory", json={"action": "forget", "key": "household_note_1"})
    assert res_forget.status_code == 200
    reply = res_forget.json()["reply"]
    assert "Forgot" in reply

    state_after = client.get("/api/state").json()
    after_part = state_after["memory_partitions"]

    # Invariant: total_count must equal sum of all 4 partitions
    assert sum([
        after_part["explicit_count"],
        after_part["confirmed_inferred_count"],
        after_part["unconfirmed_inferred_count"],
        after_part["forgotten_count"],
    ]) == after_part["total_count"]
    assert after_part["forgotten_count"] > before_part["forgotten_count"]


# ==============================================================================
# SECTION 4: CHAOS PARAMETER INJECTION & DRIFT DETECTION
# ==============================================================================

def test_adversarial_chaos_force_price_drift_delta_250_detection(client):
    """Adversarial test: Inject force_price_drift_delta=250 into /api/chat during confirmation.
    
    Verifies that:
    1. Turn 1 (Discovery): Creates proposal with search base price (e.g. ₹500), enters AWAITING_APPROVAL.
    2. Turn 2 (Confirmation with drift): Passing force_price_drift_delta=250 causes hold price to jump to ₹750.
    3. The turn orchestrator catches material drift, logs 'price_drift_invalidated_approval' guardrail event,
       invalidates the proposal, stays in AWAITING_APPROVAL, and informs user of price change.
    4. Turn 3 (Turn isolation): Subsequent turn without chaos flags resets drift delta to 0 and succeeds.
    """
    client.post("/api/reset", json={})

    # Turn 1: Discovery (supplying all required slots: service, area, date_window, time_window, budget)
    res_turn1 = client.post(
        "/api/chat",
        json={"message": "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500"},
    )
    assert res_turn1.status_code == 200
    data1 = res_turn1.json()
    assert data1["fsm_state"] == "AWAITING_APPROVAL"
    assert data1["active_proposal"] is not None
    orig_price = data1["active_proposal"]["final_price"]
    orig_hash = data1["active_proposal"]["payload_hash"]

    # Turn 2: Affirmation with chaos force_price_drift_delta=250
    res_turn2 = client.post(
        "/api/chat",
        json={"message": "Yes, confirm", "force_price_drift_delta": 250},
    )
    assert res_turn2.status_code == 200
    data2 = res_turn2.json()

    # Material drift verification
    assert data2["fsm_state"] == "AWAITING_APPROVAL", "FSM must remain in AWAITING_APPROVAL after drift"
    reply2 = data2["reply"]
    expected_drift_price = orig_price + 250
    assert f"quoted price changed from ₹{orig_price} to ₹{expected_drift_price}" in reply2
    assert "Your previous approval was automatically invalidated" in reply2

    # Active proposal must be updated with new price and new hold
    new_prop = data2["active_proposal"]
    assert new_prop is not None
    assert new_prop["final_price"] == expected_drift_price
    assert new_prop["payload_hash"] != orig_hash
    assert new_prop["hold_id"] is not None

    # Guardrail telemetry event check
    guardrail_events = [e for e in data2["trace_events"] if e.get("event_type") == "guardrail_event"]
    drift_events = [e for e in guardrail_events if e.get("action") == "price_drift_invalidated_approval"]
    assert len(drift_events) >= 1
    assert drift_events[0]["details"]["approved_price"] == orig_price
    assert drift_events[0]["details"]["quoted_price"] == expected_drift_price

    # Turn 3: Turn isolation - confirm new price WITHOUT chaos delta
    res_turn3 = client.post(
        "/api/chat",
        json={"message": "Yes, confirm"},
    )
    assert res_turn3.status_code == 200
    data3 = res_turn3.json()
    # Booking should execute with the updated hold at ₹750
    assert data3["fsm_state"] in ("COMPLETED", "EXECUTING_MUTATION", "AWAITING_APPROVAL")
    assert "confirmed" in data3["reply"].lower() or "booked" in data3["reply"].lower()


def test_adversarial_chaos_negative_price_drift_delta(client):
    """Adversarial test: Price decreases by ₹100 (force_price_drift_delta = -100).
    
    Verifies cryptographic hash detects any price discrepancy (even downward price movement)
    as material drift, requiring explicit confirmation of the new price.
    """
    client.post("/api/reset", json={})

    # Turn 1: Discovery
    res_turn1 = client.post(
        "/api/chat",
        json={"message": "Find an electrician for my wiring this week, mornings only, under ₹1,500"},
    )
    assert res_turn1.status_code == 200
    data1 = res_turn1.json()
    assert data1["fsm_state"] == "AWAITING_APPROVAL"
    orig_price = data1["active_proposal"]["final_price"]

    # Turn 2: Affirmation with negative drift (-100)
    res_turn2 = client.post(
        "/api/chat",
        json={"message": "Yes, confirm", "force_price_drift_delta": -100},
    )
    assert res_turn2.status_code == 200
    data2 = res_turn2.json()

    # Material drift must be caught even when price drops!
    assert data2["fsm_state"] == "AWAITING_APPROVAL"
    assert f"quoted price changed from ₹{orig_price} to ₹{orig_price - 100}" in data2["reply"]


def test_adversarial_chaos_cross_session_isolation(client):
    """Adversarial test: Chaos injection in Session A does NOT pollute Session B."""
    client.post("/api/reset", json={})

    headers_sess_a = {"X-Session-ID": "session_chaos_alpha"}
    headers_sess_b = {"X-Session-ID": "session_clean_beta"}

    # Setup Session A and Session B into AWAITING_APPROVAL
    client.post("/api/chat", json={"message": "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500"}, headers=headers_sess_a)
    client.post("/api/chat", json={"message": "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500"}, headers=headers_sess_b)

    # Inject chaos into Session A only
    res_a = client.post(
        "/api/chat",
        json={"message": "Yes, confirm", "force_price_drift_delta": 250},
        headers=headers_sess_a,
    )
    assert "quoted price changed" in res_a.json()["reply"]

    # Session B confirms cleanly WITHOUT chaos delta -> must NOT experience price drift
    res_b = client.post(
        "/api/chat",
        json={"message": "Yes, confirm"},
        headers=headers_sess_b,
    )
    reply_b = res_b.json()["reply"]
    assert "quoted price changed" not in reply_b
    assert "confirmed" in reply_b.lower() or "booked" in reply_b.lower()
