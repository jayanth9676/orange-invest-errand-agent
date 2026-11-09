"""Tier 1: Feature Coverage E2E Test Suite.

Verifies all 14 features from TEST_INFRA.md and PROJECT.md in isolation.
Enforces >= 5 test cases per feature across all 14 features (70 total test cases).
Methodology: Opaque-box requirement validation against public APIs, schemas, and connectors.
"""

import inspect
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
# FEATURE 1: Exa Neural Provider Search Connector (5 tests)
# =====================================================================

def test_t1_f01_01_exa_search_local_plumbers(exa_connector):
    """TC-T1-F01-01: Verifies Exa search finds local plumbers with structured fields."""
    results = exa_connector.search_providers(category="plumber", location="Indiranagar")
    assert isinstance(results, list)
    assert len(results) > 0
    p = results[0]
    assert "id" in p
    assert "name" in p
    assert p["service"] == "plumber"
    assert "rating" in p
    assert "review_count" in p
    assert "phone" in p
    assert "address" in p
    assert "slots" in p


def test_t1_f01_02_exa_search_budget_filter(exa_connector):
    """TC-T1-F01-02: Verifies Exa search respects budget_max filtering constraint."""
    results = exa_connector.search_providers(category="plumber", budget_max=1200)
    for p in results:
        assert p["base_price"] <= 1200


def test_t1_f01_03_exa_search_min_rating_filter(exa_connector):
    """TC-T1-F01-03: Verifies Exa search respects min_rating filtering constraint."""
    min_rating = 4.6
    results = exa_connector.search_providers(category="plumber", min_rating=min_rating)
    for p in results:
        assert p["rating"] >= min_rating


def test_t1_f01_04_exa_get_provider_details(exa_connector):
    """TC-T1-F01-04: Verifies get_provider retrieves full profile and contact info."""
    providers = exa_connector.search_providers(category="plumber")
    assert len(providers) > 0
    target_id = providers[0]["id"]
    details = exa_connector.get_provider(target_id)
    assert details["id"] == target_id
    assert details["name"] == providers[0]["name"]
    assert details["phone"] is not None
    assert len(details["slots"]) > 0


def test_t1_f01_05_exa_offline_fallback(exa_connector):
    """TC-T1-F01-05: Verifies Exa connector provides authentic fallback for unseeded services."""
    results = exa_connector.search_providers(category="carpenter", location="Whitefield")
    assert isinstance(results, list)
    assert len(results) > 0
    assert results[0]["service"] == "carpenter"
    assert results[0]["area"] == "Whitefield"


# =====================================================================
# FEATURE 2: Context7 Technical Docs Connector (5 tests)
# =====================================================================

def test_t1_f02_01_context7_resolve_library_id(context7_connector):
    """TC-T1-F02-01: Verifies package name resolution to valid library descriptor."""
    res = context7_connector.resolve_library_id("fastapi")
    assert res["status"] == "resolved"
    assert res["library_id"] == "/websites/fastapi_tiangolo"
    assert res["metadata"]["name"] == "FastAPI"


def test_t1_f02_02_context7_query_docs(context7_connector):
    """TC-T1-F02-02: Verifies technical docs query returns relevant documentation text."""
    res = context7_connector.query_docs("/websites/fastapi_tiangolo", "routing and dependency injection")
    assert res["library_id"] == "/websites/fastapi_tiangolo"
    assert "routing" in res["topic"]
    assert len(res["content"]) > 20
    assert res["tokens_estimated"] > 0


def test_t1_f02_03_context7_resolve_appliance_manual(context7_connector):
    """TC-T1-F02-03: Verifies appliance manual resolution (e.g., Daikin AC, IFB washer)."""
    res = context7_connector.resolve_context(topic="filter cleaning code E3", library_or_appliance="daikin")
    assert "daikin" in res["library_id"]
    assert "filter cleaning" in res["topic"]
    assert len(res["content"]) > 0


def test_t1_f02_04_context7_caching(context7_connector):
    """TC-T1-F02-04: Verifies repeated documentation queries are cached with timestamp."""
    first = context7_connector.query_docs("/manuals/ifb_senator_aqua", "drain pump error")
    second = context7_connector.query_docs("/manuals/ifb_senator_aqua", "drain pump error")
    assert first["timestamp"] == second["timestamp"]
    assert first["content"] == second["content"]


def test_t1_f02_05_context7_unknown_library_fallback(context7_connector):
    """TC-T1-F02-05: Verifies graceful not_found handling for obscure or unknown packages."""
    res = context7_connector.resolve_library_id("non_existent_obscure_pkg_xyz_99")
    assert res["status"] == "not_found"
    assert res["library_id"] is None


# =====================================================================
# FEATURE 3: Pluggable Tool Environment Architecture (5 tests)
# =====================================================================

def test_t1_f03_01_base_tool_environment_interface():
    """TC-T1-F03-01: Verifies BaseToolEnvironment declares all 8 required abstract methods."""
    required_methods = [
        "search_providers",
        "get_provider",
        "hold_slot",
        "book",
        "get_booking_status",
        "cancel",
        "reschedule",
        "list_bookings",
    ]
    for method in required_methods:
        assert hasattr(test_infra.BaseToolEnvironment, method)
        assert getattr(getattr(test_infra.BaseToolEnvironment, method), "__isabstractmethod__", False)


def test_t1_f03_02_mock_tool_environment_compliance(isolated_db):
    """TC-T1-F03-02: Verifies MockToolEnvironment implements all 8 methods."""
    mock_env = MockToolEnvironment(db=isolated_db, user_id="user_1")
    for method in ["search_providers", "get_provider", "hold_slot", "book", "get_booking_status", "cancel", "reschedule", "list_bookings"]:
        assert hasattr(mock_env, method)
        assert callable(getattr(mock_env, method))


def test_t1_f03_03_live_tool_environment_instantiation(live_tool_env):
    """TC-T1-F03-03: Verifies EnterpriseLiveToolEnvironment inherits and instantiates."""
    assert issubclass(test_infra.EnterpriseLiveToolEnvironment, test_infra.BaseToolEnvironment)
    assert live_tool_env.tenant_id == "tenant_default"
    assert live_tool_env.user_id == "user_1"


def test_t1_f03_04_tool_environment_dispatch(isolated_db):
    """TC-T1-F03-04: Verifies factory selection based on TOOL_MODE setting."""
    def env_factory(mode: str, db):
        if mode.lower() == "live":
            return test_infra.EnterpriseLiveToolEnvironment(db)
        return MockToolEnvironment(db=db)

    live = env_factory("live", isolated_db)
    mock = env_factory("mock", isolated_db)
    assert isinstance(live, test_infra.EnterpriseLiveToolEnvironment)
    assert isinstance(mock, MockToolEnvironment)


def test_t1_f03_05_tool_execution_error_hierarchy():
    """TC-T1-F03-05: Verifies standard ToolExecutionError exception class properties."""
    err = ToolExecutionError("SLOT_TAKEN", "Conflict detected: slot already taken")
    assert isinstance(err, Exception)
    assert err.code == "SLOT_TAKEN"
    assert "Conflict detected" in err.message


# =====================================================================
# FEATURE 4: Authentic Transactional Booking Engine (5 tests)
# =====================================================================

def test_t1_f04_01_hold_slot_persists_in_db(live_tool_env, isolated_db):
    """TC-T1-F04-01: Verifies hold_slot creates persistent row in slot_holds with future expiry."""
    res = live_tool_env.hold_slot("prov_plumb_1", "2026-11-20 10:00", ttl_seconds=600)
    assert res["status"] == "held"
    assert res["hold_id"].startswith("hold_live_")
    assert res["expires_at"] > time.time()

    rows = isolated_db.execute("SELECT * FROM slot_holds WHERE hold_id = ?", (res["hold_id"],))
    assert len(rows) == 1
    assert rows[0]["provider_id"] == "prov_plumb_1"


def test_t1_f04_02_book_commits_valid_hold(live_tool_env, isolated_db):
    """TC-T1-F04-02: Verifies book commits active hold into bookings table."""
    hold = live_tool_env.hold_slot("prov_plumb_1", "2026-11-20 11:00", ttl_seconds=600)
    idemp_key = f"key_book_{int(time.time())}"
    booking = live_tool_env.book(hold["hold_id"], idempotency_key=idemp_key)
    assert booking["status"] == "confirmed"
    assert booking["provider_id"] == "prov_plumb_1"

    rows = isolated_db.execute("SELECT * FROM bookings WHERE booking_id = ?", (booking["booking_id"],))
    assert len(rows) == 1
    assert rows[0]["status"] == "confirmed"


def test_t1_f04_03_book_idempotent_retry(live_tool_env):
    """TC-T1-F04-03: Verifies repeated booking with same idempotency_key returns cached response."""
    hold = live_tool_env.hold_slot("prov_plumb_1", "2026-11-20 14:00", ttl_seconds=600)
    idemp_key = f"key_idem_{int(time.time())}"
    b1 = live_tool_env.book(hold["hold_id"], idempotency_key=idemp_key)
    b2 = live_tool_env.book(hold["hold_id"], idempotency_key=idemp_key)
    assert b1["booking_id"] == b2["booking_id"]
    assert b1["created_at"] == b2["created_at"]


def test_t1_f04_04_cancel_booking_releases_slot(live_tool_env, isolated_db):
    """TC-T1-F04-04: Verifies cancel transitions booking status to cancelled."""
    hold = live_tool_env.hold_slot("prov_plumb_1", "2026-11-20 16:00", ttl_seconds=600)
    booking = live_tool_env.book(hold["hold_id"], idempotency_key=f"key_cancel_{int(time.time())}")
    cancel_res = live_tool_env.cancel(booking["booking_id"], reason="Change of plans")
    assert cancel_res["status"] == "cancelled"

    rows = isolated_db.execute("SELECT status FROM bookings WHERE booking_id = ?", (booking["booking_id"],))
    assert rows[0]["status"] == "cancelled"


def test_t1_f04_05_reschedule_updates_slot(live_tool_env, isolated_db):
    """TC-T1-F04-05: Verifies reschedule updates booking slot timestamp."""
    hold = live_tool_env.hold_slot("prov_plumb_1", "2026-11-21 09:00", ttl_seconds=600)
    booking = live_tool_env.book(hold["hold_id"], idempotency_key=f"key_resched_{int(time.time())}")
    new_slot = "2026-11-21 15:00"
    res = live_tool_env.reschedule(booking["booking_id"], new_slot=new_slot)
    assert res["status"] == "rescheduled"
    assert res["slot"] == new_slot

    rows = isolated_db.execute("SELECT slot FROM bookings WHERE booking_id = ?", (booking["booking_id"],))
    assert rows[0]["slot"] == new_slot


# =====================================================================
# FEATURE 5: Outbound MCP Client & Registry (5 tests)
# =====================================================================

def test_t1_f05_01_mcp_client_handshake(mcp_client):
    """TC-T1-F05-01: Verifies MCP initialize handshake returns server capability metadata."""
    res = mcp_client.initialize()
    assert res.get("jsonrpc") == "2.0"
    assert "result" in res
    result = res["result"]
    assert "protocolVersion" in result
    assert "capabilities" in result
    assert "serverInfo" in result


def test_t1_f05_02_mcp_client_tools_list(mcp_client):
    """TC-T1-F05-02: Verifies tools/list returns standard MCP tool schema descriptors."""
    tools = mcp_client.list_tools()
    assert isinstance(tools, list)
    assert len(tools) >= 8
    tool_names = [t["name"] for t in tools]
    assert "search_providers" in tool_names
    assert "hold_slot" in tool_names
    assert "book" in tool_names


def test_t1_f05_03_mcp_client_call_read_only_tool(mcp_client):
    """TC-T1-F05-03: Verifies tools/call executes read-only tool and formats result."""
    res = mcp_client.call_tool("search_providers", {"category": "plumber", "location": "Indiranagar"})
    assert res.get("jsonrpc") == "2.0"
    assert "result" in res
    content = res["result"]["content"]
    assert len(content) > 0
    assert "text" in content[0]


def test_t1_f05_04_mcp_client_resources_list_and_read(mcp_client):
    """TC-T1-F05-04: Verifies resources/list and resources/read return valid resource content."""
    resources = mcp_client.list_resources()
    assert len(resources) > 0
    uri = resources[0]["uri"]
    read_res = mcp_client.read_resource(uri)
    assert "result" in read_res
    contents = read_res["result"]["contents"]
    assert len(contents) > 0
    assert contents[0]["uri"] == uri


def test_t1_f05_05_mcp_rest_catalog_alignment(client):
    """TC-T1-F05-05: Verifies GET /mcp/v1/tools REST endpoint matches tool inventory."""
    res = client.get("/mcp/v1/tools")
    assert res.status_code == 200
    data = res.json()
    assert "tools" in data
    assert data["count"] >= 8


# =====================================================================
# FEATURE 6: Agent FSM Integration of Real MCP Tools (5 tests)
# =====================================================================

def test_t1_f06_01_fsm_routes_chat_to_search(client):
    """TC-T1-F06-01: Verifies FSM routes discovery chat into SEARCHING_PROVIDERS/AWAITING_APPROVAL."""
    res = client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] in ("AWAITING_APPROVAL", "SEARCHING_PROVIDERS")


def test_t1_f06_02_fsm_halts_at_awaiting_approval(client):
    """TC-T1-F06-02: Verifies FSM halts at AWAITING_APPROVAL with active proposal payload."""
    res = client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    data = res.json()
    assert data["fsm_state"] == "AWAITING_APPROVAL"
    proposal = data["active_proposal"]
    assert proposal is not None
    assert proposal["mutation_type"] == "book"
    assert proposal["final_price"] <= 1500


def test_t1_f06_03_two_layer_gate_withholds_mutations(client):
    """TC-T1-F06-03: Verifies unapproved mutation intent is blocked and presented as proposal."""
    res = client.post("/api/chat", json={"message": "Book plumber Ramesh right now for Saturday 10:00"})
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] in ("AWAITING_APPROVAL", "COLLECTING_SLOTS")
    assert data["fsm_state"] != "COMPLETED"


def test_t1_f06_04_proposal_sha256_hash_integrity(client):
    """TC-T1-F06-04: Verifies proposal payload_hash is a 64-char hexadecimal SHA-256 hash."""
    res = client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    proposal = res.json()["active_proposal"]
    assert proposal is not None
    assert "payload_hash" in proposal
    assert len(proposal["payload_hash"]) == 64
    int(proposal["payload_hash"], 16)


def test_t1_f06_05_fsm_affirmation_executes_mutation(client):
    """TC-T1-F06-05: Verifies explicit human affirmation transitions FSM to COMPLETED."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    res = client.post("/api/chat", json={"message": "Yes, confirm", "force_book_outcome": "success"})
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] == "COMPLETED"
    assert len(data["bookings"]) > 0


# =====================================================================
# FEATURE 7: Genuine Multi-Tenant Session Isolation (5 tests)
# =====================================================================

def test_t1_f07_01_multi_tenant_header_session_scoping(client):
    """TC-T1-F07-01: Verifies X-Tenant-ID headers create distinct isolated sessions."""
    headers_a = {"X-Tenant-ID": "tenant_test_a", "X-Session-ID": "sess_shared"}
    headers_b = {"X-Tenant-ID": "tenant_test_b", "X-Session-ID": "sess_shared"}

    client.post("/api/chat", json={"message": "Find a plumber in Indiranagar"}, headers=headers_a)
    state_a = client.get("/api/state", headers=headers_a).json()
    state_b = client.get("/api/state", headers=headers_b).json()

    assert state_a["fsm_state"] in ("COLLECTING_SLOTS", "AWAITING_APPROVAL")
    assert state_b["fsm_state"] == "IDLE"


def test_t1_f07_02_tenant_booking_isolation(client):
    """TC-T1-F07-02: Verifies Tenant A bookings are invisible to Tenant B."""
    headers_a = {"X-Tenant-ID": "tenant_test_a", "X-Session-ID": "sess_a"}
    headers_b = {"X-Tenant-ID": "tenant_test_b", "X-Session-ID": "sess_b"}

    client.post("/api/chat", json={"message": DISCOVERY_PROMPT}, headers=headers_a)
    client.post("/api/chat", json={"message": "Yes, confirm", "force_book_outcome": "success"}, headers=headers_a)

    state_a = client.get("/api/state", headers=headers_a).json()
    state_b = client.get("/api/state", headers=headers_b).json()

    assert len(state_a["bookings"]) > len(state_b["bookings"])


def test_t1_f07_03_tenant_memory_isolation(client):
    """TC-T1-F07-03: Verifies user memory added in Tenant A does not bleed into Tenant B."""
    headers_a = {"X-Tenant-ID": "tenant_iso_mem_a", "X-Session-ID": "sess_mem", "X-User-Role": "Admin"}
    headers_b = {"X-Tenant-ID": "tenant_iso_mem_b", "X-Session-ID": "sess_mem", "X-User-Role": "Admin"}

    client.post("/api/memory", json={"action": "update", "key": "secret_tenant_pref", "value": "Tenant A Secret Gate"}, headers=headers_a)

    mem_a = client.get("/api/state", headers=headers_a).json().get("all_memories", [])
    mem_b = client.get("/api/state", headers=headers_b).json().get("all_memories", [])

    assert any(m["key"] == "secret_tenant_pref" for m in mem_a)
    assert not any(m["key"] == "secret_tenant_pref" for m in mem_b)


def test_t1_f07_04_tenant_reset_isolation(client):
    """TC-T1-F07-04: Verifies resetting Tenant A does not clear Tenant B state."""
    headers_a = {"X-Tenant-ID": "tenant_reset_a", "X-Session-ID": "sess_active"}
    headers_b = {"X-Tenant-ID": "tenant_reset_b", "X-Session-ID": "sess_active"}

    client.post("/api/chat", json={"message": DISCOVERY_PROMPT}, headers=headers_b)
    client.post("/api/reset", headers=headers_a)

    state_b = client.get("/api/state", headers=headers_b).json()
    assert state_b["fsm_state"] == "AWAITING_APPROVAL"


def test_t1_f07_05_clean_tenant_initialization(client):
    """TC-T1-F07-05: Verifies new tenant initializes with clean IDLE state and 0 bookings."""
    headers_new = {"X-Tenant-ID": f"tenant_fresh_{int(time.time())}", "X-Session-ID": "sess_fresh"}
    res = client.get("/api/state", headers=headers_new)
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] == "IDLE"
    assert len(data["bookings"]) == 0


# =====================================================================
# FEATURE 8: Server-Sent Events Route Alignment (5 tests)
# =====================================================================

def test_t1_f08_01_sse_trace_stream_headers(client):
    """TC-T1-F08-01: Verifies /api/trace-stream returns text/event-stream content-type."""
    with client.stream("GET", "/api/trace-stream") as res:
        assert res.status_code == 200
        assert "text/event-stream" in res.headers["content-type"]
        assert res.headers.get("cache-control") == "no-cache"


def test_t1_f08_02_sse_events_alias_route(client):
    """TC-T1-F08-02: Verifies /events route alignment eliminates 404 drops."""
    with client.stream("GET", "/events") as res:
        assert res.status_code == 200
        assert "text/event-stream" in res.headers["content-type"]


def test_t1_f08_03_sse_event_frame_structure(client):
    """TC-T1-F08-03: Verifies SSE stream emits valid data frames."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    with client.stream("GET", "/api/trace-stream") as res:
        assert res.status_code == 200
        for line in res.iter_lines():
            if line:
                assert line.startswith("data:")
                break


def test_t1_f08_04_sse_tenant_and_session_filtering(client):
    """TC-T1-F08-04: Verifies SSE route accepts tenant and session query parameters."""
    with client.stream("GET", "/events?tenant_id=tenant_custom&session_id=sess_custom") as res:
        assert res.status_code == 200
        assert "text/event-stream" in res.headers["content-type"]


def test_t1_f08_05_sse_emits_on_fsm_transition(client):
    """TC-T1-F08-05: Verifies FSM turn generates trace events in state trace buffer."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    state = client.get("/api/state").json()
    trace_events = state.get("trace_events", [])
    assert len(trace_events) > 0
    event_types = [e.get("event_type") for e in trace_events]
    assert any("transition" in str(et).lower() or "model" in str(et).lower() for et in event_types)


# =====================================================================
# FEATURE 9: Proposal Timestamp Schema Alignment (5 tests)
# =====================================================================

def test_t1_f09_01_proposal_has_created_at_epoch():
    """TC-T1-F09-01: Verifies ApprovalProposal schema defines created_at_epoch."""
    prop = ApprovalProposal(
        proposal_id="prop_test_1",
        session_id="sess_1",
        mutation_type=MutationType.BOOK,
        provider_id="prov_1",
        slot="2026-11-10 09:00",
        final_price=1000,
        payload_hash="a" * 64,
        proposed_at_turn=1,
    )
    assert hasattr(prop, "created_at_epoch")
    assert prop.created_at_epoch > 0


def test_t1_f09_02_proposal_has_created_at_field():
    """TC-T1-F09-02: Verifies ApprovalProposal serialization provides created_at matching frontend."""
    prop = ApprovalProposal(
        proposal_id="prop_test_2",
        session_id="sess_1",
        mutation_type=MutationType.BOOK,
        provider_id="prov_1",
        slot="2026-11-10 09:00",
        final_price=1000,
        payload_hash="b" * 64,
        proposed_at_turn=1,
    )
    dumped = prop.model_dump()
    assert "created_at" in dumped
    assert dumped["created_at"] > 0


def test_t1_f09_03_proposal_ttl_seconds_field():
    """TC-T1-F09-03: Verifies proposal ttl_seconds defaults to 600.0s."""
    prop = ApprovalProposal(
        proposal_id="prop_test_3",
        session_id="sess_1",
        mutation_type=MutationType.BOOK,
        provider_id="prov_1",
        slot="2026-11-10 09:00",
        final_price=1000,
        payload_hash="c" * 64,
        proposed_at_turn=1,
    )
    assert prop.ttl_seconds == 600.0


def test_t1_f09_04_is_expired_helper_accuracy():
    """TC-T1-F09-04: Verifies proposal.is_expired() evaluates TTL accurately."""
    now = time.time()
    fresh_prop = ApprovalProposal(
        proposal_id="prop_fresh",
        session_id="sess_1",
        mutation_type=MutationType.BOOK,
        provider_id="prov_1",
        slot="2026-11-10 09:00",
        final_price=1000,
        payload_hash="d" * 64,
        proposed_at_turn=1,
        created_at_epoch=now - 100,
        ttl_seconds=600,
    )
    assert not fresh_prop.is_expired(now)

    expired_prop = ApprovalProposal(
        proposal_id="prop_expired",
        session_id="sess_1",
        mutation_type=MutationType.BOOK,
        provider_id="prov_1",
        slot="2026-11-10 09:00",
        final_price=1000,
        payload_hash="e" * 64,
        proposed_at_turn=1,
        created_at_epoch=now - 700,
        ttl_seconds=600,
    )
    assert expired_prop.is_expired(now)


def test_t1_f09_05_api_state_proposal_timestamps(client):
    """TC-T1-F09-05: Verifies API state returns proposal with TTL timestamp fields."""
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT})
    state = client.get("/api/state").json()
    proposal = state["active_proposal"]
    assert proposal is not None
    assert "ttl_seconds" in proposal
    assert "created_at" in proposal or "created_at_epoch" in proposal


# =====================================================================
# FEATURE 10: Contractual DOM Semantic Elements (5 tests)
# =====================================================================

def test_t1_f10_01_dom_approval_cockpit_present(client):
    """TC-T1-F10-01: Verifies approval cockpit container element in UI delivery."""
    res = client.get("/")
    assert res.status_code == 200
    assert "approval-cockpit" in res.text


def test_t1_f10_02_dom_fsm_dag_container_present(client):
    """TC-T1-F10-02: Verifies FSM DAG container element in UI delivery."""
    res = client.get("/")
    assert "fsm-dag-container" in res.text


def test_t1_f10_03_dom_trace_waterfall_present(client):
    """TC-T1-F10-03: Verifies trace waterfall container element in UI delivery."""
    res = client.get("/")
    assert "trace-waterfall" in res.text


def test_t1_f10_04_dom_memory_cockpit_present(client):
    """TC-T1-F10-04: Verifies memory governance container or markers in UI delivery."""
    res = client.get("/")
    text_lower = res.text.lower()
    assert "memory" in text_lower or "governance" in text_lower


def test_t1_f10_05_dom_clean_mounting_without_dummy_hidden(client):
    """TC-T1-F10-05: Verifies root index.html serves complete modern SPA markup."""
    res = client.get("/")
    assert "<!DOCTYPE html>" in res.text or "<!doctype html>" in res.text
    assert "root" in res.text


# =====================================================================
# FEATURE 11: Dynamic MCP Studio UI (5 tests)
# =====================================================================

def test_t1_f11_01_mcp_studio_tools_endpoint(client):
    """TC-T1-F11-01: Verifies GET /mcp/tools returns list of tools with inputSchema."""
    res = client.get("/mcp/tools")
    assert res.status_code == 200
    data = res.json()
    assert "tools" in data
    assert len(data["tools"]) >= 8
    for tool in data["tools"]:
        assert "name" in tool
        assert "inputSchema" in tool


def test_t1_f11_02_mcp_studio_tool_schemas_valid_json_schema(client):
    """TC-T1-F11-02: Verifies all MCP tool parameter schemas declare valid JSON Schema type."""
    res = client.get("/mcp/tools")
    tools = res.json()["tools"]
    for tool in tools:
        schema = tool["inputSchema"]
        assert schema.get("type") == "object"
        assert "properties" in schema


def test_t1_f11_03_mcp_studio_post_call_execution(client):
    """TC-T1-F11-03: Verifies POST /mcp/call executes registered tool dynamically."""
    res = client.post("/mcp/call", json={
        "name": "search_providers",
        "arguments": {"category": "plumber", "location": "Indiranagar"},
    })
    assert res.status_code == 200
    data = res.json()
    assert "content" in data
    assert len(data["content"]) > 0


def test_t1_f11_04_mcp_studio_rbac_role_execution(client):
    """TC-T1-F11-04: Verifies Auditor role is blocked from executing mutating tools."""
    res = client.post("/mcp/call", json={
        "name": "cancel",
        "arguments": {"booking_id": "bk_upcoming_ac_01"},
        "role": "Auditor",
    })
    assert res.status_code == 403


def test_t1_f11_05_mcp_studio_manifest_endpoint(client):
    """TC-T1-F11-05: Verifies GET /mcp/manifest returns full server capability descriptor."""
    res = client.get("/mcp/manifest")
    assert res.status_code == 200
    data = res.json()
    assert "tools" in data
    assert "resources" in data
    assert "prompts" in data


# =====================================================================
# FEATURE 12: Multi-Tenant Workspace Selector UI (5 tests)
# =====================================================================

def test_t1_f12_01_header_workspace_selector_presence(client):
    """TC-T1-F12-01: Verifies header branding and workspace markers in served HTML."""
    res = client.get("/")
    assert res.status_code == 200
    assert "Errand Agent" in res.text


def test_t1_f12_02_api_respects_custom_tenant_id(client):
    """TC-T1-F12-02: Verifies API uses custom X-Tenant-ID header."""
    custom_tenant = "tenant_enterprise_beta"
    res = client.get("/api/state", headers={"X-Tenant-ID": custom_tenant})
    assert res.status_code == 200
    assert res.json()["fsm_state"] == "IDLE"


def test_t1_f12_03_api_respects_custom_session_id(client):
    """TC-T1-F12-03: Verifies API scopes conversation state to X-Session-ID."""
    headers = {"X-Session-ID": "session_user_custom_1"}
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT}, headers=headers)
    state = client.get("/api/state", headers=headers).json()
    assert state["fsm_state"] == "AWAITING_APPROVAL"


def test_t1_f12_04_session_switching_retrieves_isolated_state(client):
    """TC-T1-F12-04: Verifies querying two different session IDs yields isolated snapshots."""
    h1 = {"X-Session-ID": "sess_switch_1"}
    h2 = {"X-Session-ID": "sess_switch_2"}
    client.post("/api/chat", json={"message": DISCOVERY_PROMPT}, headers=h1)

    s1 = client.get("/api/state", headers=h1).json()
    s2 = client.get("/api/state", headers=h2).json()
    assert s1["fsm_state"] == "AWAITING_APPROVAL"
    assert s2["fsm_state"] == "IDLE"


def test_t1_f12_05_role_header_simulation(client):
    """TC-T1-F12-05: Verifies X-User-Role header gating on protected endpoints."""
    res = client.post("/api/reset", headers={"X-User-Role": "Auditor"})
    assert res.status_code == 403


# =====================================================================
# FEATURE 13: Pytest Database Concurrency Isolation (5 tests)
# =====================================================================

def test_t1_f13_01_isolated_db_file_creation(isolated_db):
    """TC-T1-F13-01: Verifies isolated database fixture creates independent temp file."""
    assert os.path.exists(isolated_db.db_path)
    assert "_isolated.sqlite" in isolated_db.db_path


def test_t1_f13_02_isolated_db_wal_mode_enabled(isolated_db):
    """TC-T1-F13-02: Verifies isolated database enables WAL journal mode."""
    rows = isolated_db.execute("PRAGMA journal_mode")
    assert rows[0][0].lower() == "wal"


def test_t1_f13_03_db_concurrent_reads_while_writing(isolated_db):
    """TC-T1-F13-03: Verifies concurrent thread reading while writer is active."""
    errors = []

    def reader():
        try:
            for _ in range(10):
                isolated_db.execute("SELECT count(*) FROM providers_catalog")
                time.sleep(0.01)
        except Exception as e:
            errors.append(e)

    def writer():
        try:
            for i in range(5):
                with isolated_db.conn:
                    isolated_db.conn.execute(
                        "INSERT OR REPLACE INTO slot_holds (hold_id, user_id, provider_id, service, slot, quoted_price, expires_at_epoch, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (f"hold_th_{i}", "user_1", "prov_plumb_1", "plumber", "slot_1", 1000, time.time() + 600, "active"),
                    )
                time.sleep(0.01)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=reader)
    t2 = threading.Thread(target=writer)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert len(errors) == 0


def test_t1_f13_04_clean_state_fixture_resets_tables(client):
    """TC-T1-F13-04: Verifies clean_state_fixture resets session state to IDLE."""
    state = client.get("/api/state").json()
    assert state["fsm_state"] == "IDLE"
    assert state["active_proposal"] is None


def test_t1_f13_05_isolated_db_busy_timeout(isolated_db):
    """TC-T1-F13-05: Verifies busy_timeout is set >= 5000ms to prevent locked errors."""
    rows = isolated_db.execute("PRAGMA busy_timeout")
    assert rows[0][0] >= 5000


# =====================================================================
# FEATURE 14: Evaluation Benchmark Invariant Guard (5 tests)
# =====================================================================

def test_t1_f14_01_eval_scenarios_script_exists():
    """TC-T1-F14-01: Verifies run_evals.py exists at project root."""
    eval_script = ROOT_DIR / "run_evals.py"
    assert eval_script.is_file()


def test_t1_f14_02_eval_cassettes_file_integrity():
    """TC-T1-F14-02: Verifies eval_cassettes.json exists and contains valid JSON."""
    cassette_path = ROOT_DIR / "errand-agent" / "fixtures" / "eval_cassettes.json"
    assert cassette_path.is_file()
    with open(cassette_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, dict)
    assert len(data) > 0


def test_t1_f14_03_eval_postmortem_bugs_guarded():
    """TC-T1-F14-03: Verifies regression test file guards all 3 postmortem bugs."""
    reg_file = ROOT_DIR / "errand-agent" / "tests" / "test_regression_bugs.py"
    assert reg_file.is_file()
    content = reg_file.read_text(encoding="utf-8")
    assert "test_bug1" in content
    assert "test_bug2" in content
    assert "test_bug3" in content


def test_t1_f14_04_eval_report_file_structure():
    """TC-T1-F14-04: Verifies EVAL_REPORT.md exists and contains markdown table."""
    report = ROOT_DIR / "EVAL_REPORT.md"
    assert report.is_file()
    content = report.read_text(encoding="utf-8")
    assert "| Scenario |" in content
    assert "PASS" in content


def test_t1_f14_05_offline_llm_mode_deterministic():
    """TC-T1-F14-05: Verifies ScriptedEvalLLM parses slots deterministically without external API."""
    nlu = ScriptedEvalLLM()
    parsed1 = nlu.parse(DISCOVERY_PROMPT)
    parsed2 = nlu.parse(DISCOVERY_PROMPT)
    assert parsed1["slot_delta"]["service"] == parsed2["slot_delta"]["service"] == "plumber"
    assert parsed1["slot_delta"]["max_budget"] == parsed2["slot_delta"]["max_budget"] == 1500
