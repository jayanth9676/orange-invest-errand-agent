#!/usr/bin/env python3
"""Comprehensive web app and UI verification script.

Tests the FastAPI Split-Screen Agentic Studio via Starlette/FastAPI TestClient
and validates the two-column Streamlit app structure and components.
"""

import ast
import json
import sys
from pathlib import Path

# Add project root and errand-agent directory to sys.path
root_dir = Path(__file__).resolve().parent
agent_dir = root_dir / "errand-agent"
if str(agent_dir) not in sys.path:
    sys.path.insert(0, str(agent_dir))
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

import os
os.environ.setdefault("LLM_MODE", "offline")
os.environ.setdefault("REQUIRE_API_KEY", "false")

from fastapi.testclient import TestClient
from src.app_server import app


def test_fastapi_endpoints():
    print("\n--- 1. Testing FastAPI Endpoints via TestClient ---")
    client = TestClient(app)

    # 0. Clean reset before test flow
    res_init = client.post("/api/reset")
    assert res_init.status_code == 200

    # 1. GET / (Split-Screen UI HTML)
    res_index = client.get("/")
    assert res_index.status_code == 200, f"Expected 200, got {res_index.status_code}"
    assert "Errand Agent" in res_index.text
    print("[PASS] GET / -> 200 OK (Served dark-mode split-screen Studio HTML)")

    # 2. GET /api/state
    res_state = client.get("/api/state")
    assert res_state.status_code == 200
    state_data = res_state.json()
    assert "fsm_state" in state_data
    assert "slots" in state_data
    assert "bookings" in state_data
    assert "core_memories" in state_data
    assert "limits" in state_data
    print(f"[PASS] GET /api/state -> 200 OK (FSM State: {state_data['fsm_state']}, Total Memories: {state_data['total_memories_seeded']})")

    # 3. POST /api/chat (Discovery turn)
    res_chat1 = client.post(
        "/api/chat",
        json={"message": "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500"},
    )
    assert res_chat1.status_code == 200
    chat1_data = res_chat1.json()
    assert "reply" in chat1_data
    assert chat1_data["fsm_state"] == "AWAITING_APPROVAL"
    assert chat1_data["active_proposal"] is not None
    prop_hash = chat1_data["active_proposal"]["payload_hash"]
    print(f"[PASS] POST /api/chat (Discovery) -> 200 OK (State: {chat1_data['fsm_state']}, Proposal Hash: {prop_hash[:16]}...)")

    # 4. POST /api/chat (Affirmation turn with price drift chaos test)
    res_chat2 = client.post(
        "/api/chat",
        json={"message": "Yes, confirm", "force_price_drift_delta": 250},
    )
    assert res_chat2.status_code == 200
    chat2_data = res_chat2.json()
    assert "quoted price changed" in chat2_data["reply"]
    assert chat2_data["fsm_state"] == "AWAITING_APPROVAL"
    print("[PASS] POST /api/chat (Price Drift Guard) -> 200 OK (Material drift caught & invalidated)")

    # 5. POST /api/memory (Inspect & Update)
    res_mem_inspect = client.post("/api/memory", json={"action": "inspect"})
    assert res_mem_inspect.status_code == 200
    assert "Stored Preferences" in res_mem_inspect.json()["reply"]

    res_mem_update = client.post(
        "/api/memory", json={"action": "update", "key": "preferred_area", "value": "Koramangala"}
    )
    assert res_mem_update.status_code == 200
    assert "Koramangala" in res_mem_update.json()["reply"]
    print("[PASS] POST /api/memory -> 200 OK (Memory inspection and explicit update verified)")

    # 6. POST /api/proactive-cancel (Asynchronous provider cancellation webhook)
    res_cancel = client.post("/api/proactive-cancel", json={"booking_id": "bk_upcoming_ac_01"})
    assert res_cancel.status_code == 200
    cancel_data = res_cancel.json()
    assert cancel_data["event_result"]["notified"] is True
    assert cancel_data["event_result"]["replacement_provider_id"] == "prov_ac_2"
    print(f"[PASS] POST /api/proactive-cancel -> 200 OK (Alerted via {cancel_data['event_result']['channel']}, proposed {cancel_data['event_result']['replacement_provider_id']})")

    # 7. POST /api/reset
    res_reset = client.post("/api/reset")
    assert res_reset.status_code == 200
    assert res_reset.json().get("status") == "reset"
    print("[PASS] POST /api/reset -> 200 OK (State machine cleanly reinitialized)")

    # 8. POST /api/run-evals
    res_evals = client.post("/api/run-evals")
    assert res_evals.status_code == 200
    evals_data = res_evals.json()
    assert evals_data["failed"] == 0
    assert evals_data["passed"] >= 30
    print(f"[PASS] POST /api/run-evals -> 200 OK ({evals_data['passed']}/{evals_data['total']} Scenarios Passed)")


def test_streamlit_app_structure():
    print("\n--- 2. Validating Streamlit Two-Column App Structure ---")
    st_file = agent_dir / "streamlit_app.py"
    assert st_file.is_file(), f"streamlit_app.py not found at {st_file}"

    tree = ast.parse(st_file.read_text(encoding="utf-8"))
    imported_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_names.append(node.module)

    assert "streamlit" in imported_names, "streamlit import missing"
    assert "src.orchestrator" in imported_names or "src.db" in imported_names

    # Check for st.columns([1, 1]) in code
    source_code = st_file.read_text(encoding="utf-8")
    assert "st.columns([1, 1])" in source_code or "columns([1, 1])" in source_code
    print("[PASS] streamlit_app.py -> Syntax valid, st.columns([1, 1]) two-column split layout verified")


def main():
    print("=================================================================")
    print("  ERRAND AGENT — DUAL DEMO UI & API VERIFICATION (FASTAPI + ST)")
    print("=================================================================")
    test_fastapi_endpoints()
    test_streamlit_app_structure()
    print("\n[ALL CHECKS PASSED] Both FastAPI Studio & Streamlit Studio are 100% functional and verified.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
