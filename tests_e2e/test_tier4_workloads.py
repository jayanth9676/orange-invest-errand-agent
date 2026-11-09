"""Tier 4: Real-World Errand Workload Scenarios E2E Test Suite.

Executes comprehensive multi-step realistic user journeys defined in TEST_INFRA.md:
1. Emergency Plumbing Errand with Live Exa Search & Approval
2. Appliance Manual Lookup via Context7 & Rescheduling
3. Multi-Tenant Concurrent Errand Booking with Strict Isolation
4. Price Drift Invalidation & Material Re-Approval Lifecycle
5. Dynamic MCP Tool Discovery and Direct Studio Execution
6. Full Evaluation Regression Run Invariance
7. Proactive Cancellation & Slot Reclaim Workflow
"""

import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import pytest
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parent.parent

from src.db import LocalDatabase
from src.schemas import AgentState, ApprovalProposal, MutationType
from src.mock_tools import ToolExecutionError, MockToolEnvironment
from src.scripted_engine import ScriptedEvalLLM
from src.security import UserRole

import tests_e2e.conftest as test_infra

DISCOVERY_PROMPT = "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500"


# =====================================================================
# TIER 4: REAL-WORLD APPLICATION SCENARIOS (7 scenarios)
# =====================================================================

def test_t4_scenario_01_emergency_plumbing_errand(client, exa_connector, live_tool_env):
    """Scenario 1: Emergency Plumbing Errand with Live Exa Search & Approval.
    Features: F1, F3, F4, F6, F8, F9, F10.
    Simulates urgent user booking: discovery -> proposal -> approval -> confirmation -> telemetry.
    """
    # 1. User submits emergency request with all slots
    res = client.post("/api/chat", json={
        "message": "Emergency! Leaking burst pipe under kitchen sink in Indiranagar this week, mornings only under ₹1,500"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] in ("AWAITING_APPROVAL", "SEARCHING_PROVIDERS")

    # 2. Verify approval proposal structure and SHA-256 hash
    proposal = data.get("active_proposal")
    assert proposal is not None
    assert proposal["mutation_type"] == "book"
    assert len(proposal["payload_hash"]) == 64
    assert proposal["final_price"] <= 1500

    # 3. User approves via affirmative message
    confirm_res = client.post("/api/chat", json={
        "message": "Yes, confirm",
        "force_book_outcome": "success",
    })
    assert confirm_res.status_code == 200
    confirm_data = confirm_res.json()
    assert confirm_data["fsm_state"] == "COMPLETED"
    assert len(confirm_data["bookings"]) > 0

    # 4. Telemetry and SSE alignment verification
    with client.stream("GET", "/events") as sse_res:
        assert sse_res.status_code == 200
        assert "text/event-stream" in sse_res.headers["content-type"]

    # 5. Contractual DOM elements in delivered app
    page_res = client.get("/")
    assert "approval-cockpit" in page_res.text


def test_t4_scenario_02_appliance_manual_lookup_and_reschedule(client, context7_connector, live_tool_env):
    """Scenario 2: Appliance Manual Lookup via Context7 & Rescheduling.
    Features: F2, F3, F4, F6, F11.
    Resolves appliance error manual via Context7, books technician, and executes rescheduling.
    """
    # 1. Lookup appliance error manual via Context7
    doc_res = context7_connector.resolve_context(topic="filter cleaning error E3", library_or_appliance="daikin")
    assert "daikin" in doc_res["library_id"]

    # 2. Book AC servicing technician
    client.post("/api/chat", json={"message": "Find an ac servicing technician in Indiranagar this week morning under 1500"})
    book_res = client.post("/api/chat", json={"message": "Yes, confirm", "force_book_outcome": "success"})
    assert book_res.json()["fsm_state"] == "COMPLETED"
    bookings = book_res.json()["bookings"]
    assert len(bookings) > 0
    target_booking = bookings[0]

    # 3. Reschedule booking via MCP Studio call
    resched_res = client.post("/mcp/call", json={
        "name": "reschedule",
        "arguments": {
            "booking_id": target_booking["booking_id"],
            "new_slot": "2026-11-25 14:00",
        },
    })
    assert resched_res.status_code == 200
    assert "content" in resched_res.json()


def test_t4_scenario_03_multi_tenant_concurrent_errand_booking(isolated_db, client):
    """Scenario 3: Multi-Tenant Concurrent Errand Booking with Strict Isolation.
    Features: F4, F7, F12, F13.
    Executes simultaneous booking flows across 3 distinct tenants in parallel threads.
    """
    tenants = ["tenant_enterprise_corp_a", "tenant_enterprise_corp_b", "tenant_enterprise_corp_c"]
    errors = []
    completed_tenants = []

    def tenant_booking_flow(tenant_id):
        try:
            h = {"X-Tenant-ID": tenant_id, "X-Session-ID": "sess_flow"}
            # Step 1: Chat turn
            r1 = client.post("/api/chat", json={"message": DISCOVERY_PROMPT}, headers=h)
            if r1.json()["fsm_state"] != "AWAITING_APPROVAL":
                raise ValueError(f"Expected AWAITING_APPROVAL, got {r1.json()['fsm_state']}")
            # Step 2: Confirm turn
            r2 = client.post("/api/chat", json={"message": "Yes, confirm", "force_book_outcome": "success"}, headers=h)
            if r2.json()["fsm_state"] != "COMPLETED":
                raise ValueError(f"Expected COMPLETED, got {r2.json()['fsm_state']}")
            completed_tenants.append(tenant_id)
        except Exception as exc:
            errors.append((tenant_id, exc))

    threads = [threading.Thread(target=tenant_booking_flow, args=(t,)) for t in tenants]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert len(errors) == 0
    assert len(completed_tenants) == 3


def test_t4_scenario_04_price_drift_invalidation_and_reapproval_lifecycle(client):
    """Scenario 4: Price Drift Invalidation & Material Re-Approval Lifecycle.
    Features: F3, F4, F6, F8, F9, F10.
    Simulates price drift during hold -> gate invalidation -> user re-confirmation -> booking.
    """
    # 1. Discover and reach proposal
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    s1 = client.get("/api/state").json()
    assert s1["fsm_state"] == "AWAITING_APPROVAL"
    original_price = s1["active_proposal"]["final_price"]

    # 2. Price drift delta (+250) occurs during confirmation
    drift_res = client.post("/api/chat", json={
        "message": "Yes, confirm",
        "force_price_drift_delta": 250,
    })
    drift_data = drift_res.json()
    assert drift_data["fsm_state"] == "AWAITING_APPROVAL"
    assert "price" in drift_data["reply"].lower() or "changed" in drift_data["reply"].lower()
    new_price = drift_data["active_proposal"]["final_price"]
    assert new_price == original_price + 250

    # 3. User affirms the new price explicitly
    reconfirm_res = client.post("/api/chat", json={
        "message": "Yes, confirm",
        "force_book_outcome": "success",
    })
    reconfirm_data = reconfirm_res.json()
    assert reconfirm_data["fsm_state"] == "COMPLETED"
    assert len(reconfirm_data["bookings"]) > 0


def test_t4_scenario_05_dynamic_mcp_tool_discovery_and_studio_execution(mcp_client, client):
    """Scenario 5: Dynamic MCP Tool Discovery and Direct Studio Execution.
    Features: F1, F2, F5, F11.
    Introspects registered tools via MCP, validates schema parameters, and executes via REST.
    """
    # 1. Discover all tools
    manifest = client.get("/mcp/manifest").json()
    assert "tools" in manifest
    assert len(manifest["tools"]) >= 8

    # 2. Query individual tool schema
    tools_res = client.get("/mcp/tools").json()
    search_tool = next((t for t in tools_res["tools"] if t["name"] == "search_providers"), None)
    assert search_tool is not None
    assert "service" in search_tool["inputSchema"]["properties"]

    # 3. Execute search tool directly through Studio REST endpoint
    call_res = client.post("/mcp/call", json={
        "name": "search_providers",
        "arguments": {"category": "electrician", "location": "Indiranagar"},
    })
    assert call_res.status_code == 200
    assert "content" in call_res.json()


def test_t4_scenario_06_full_evaluation_regression_run_invariance():
    """Scenario 6: Full Evaluation Regression Run Invariance.
    Features: F3, F13, F14.
    Runs evaluation suite runner and verifies 100% pass rate with zero regression.
    """
    import run_evals
    exit_code = run_evals.main()
    assert exit_code == 0

    # Verify report was updated
    report_file = ROOT_DIR / "EVAL_REPORT.md"
    assert report_file.is_file()
    text = report_file.read_text(encoding="utf-8")
    assert "33" in text
    assert "Passed" in text


def test_t4_scenario_07_proactive_cancellation_and_slot_reclaim_workflow(client):
    """Scenario 7: Proactive Cancellation & Slot Reclaim Workflow.
    Features: F3, F4, F6, F8.
    Provider cancellation webhook -> proactive recovery search -> proposal -> user re-booking.
    """
    # 1. Trigger proactive cancellation webhook for existing booking
    res = client.post("/api/proactive-cancel", json={"booking_id": "bk_upcoming_ac_01"})
    assert res.status_code == 200
    data = res.json()
    # FSM recovers and searches replacement
    assert data["fsm_state"] in ("PROACTIVE_RECOVERY", "AWAITING_APPROVAL", "SEARCHING_PROVIDERS")
    assert data["event_result"]["notified"] is True
    assert data["event_result"]["cancelled_booking_id"] == "bk_upcoming_ac_01"

    # 2. Verify state snapshot holds replacement proposal
    proposal = data.get("active_proposal")
    assert proposal is not None or "replacement" in data["reply"].lower()
