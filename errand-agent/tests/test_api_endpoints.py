"""Integration tests for FastAPI Errand Agent Studio Endpoints.
Verifies REST API endpoints, state snapshots, and SSE streaming.
"""

from fastapi.testclient import TestClient
from src.app_server import app


def test_api_index_html():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "Errand Agent" in response.text
    assert "Agentic Operations" in response.text


def test_api_state_snapshot():
    client = TestClient(app)
    response = client.get("/api/state")
    assert response.status_code == 200
    data = response.json()
    assert "fsm_state" in data
    assert "slots" in data
    assert "total_memories_seeded" in data
    assert data["total_memories_seeded"] >= 55


def test_api_chat_discovery_and_proposal():
    client = TestClient(app)
    client.post("/api/reset")

    res = client.post("/api/chat", json={
        "message": "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["fsm_state"] == "AWAITING_APPROVAL"
    assert data["active_proposal"] is not None
    assert data["active_proposal"]["mutation_type"] == "book"


def test_api_chat_price_drift_guard():
    client = TestClient(app)
    client.post("/api/reset")

    # Step 1: Discovery
    client.post("/api/chat", json={
        "message": "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500"
    })

    # Step 2: Inject price drift chaos
    res = client.post("/api/chat", json={
        "message": "Yes, please confirm",
        "force_price_drift_delta": 250
    })
    assert res.status_code == 200
    data = res.json()
    assert "price" in data["reply"].lower()
    assert data["fsm_state"] == "AWAITING_APPROVAL"


def test_api_memory_inspection_and_update():
    client = TestClient(app)
    inspect_res = client.post("/api/memory", json={"action": "inspect"})
    assert inspect_res.status_code == 200
    assert "preferred_area" in inspect_res.json()["reply"]

    update_res = client.post("/api/memory", json={
        "action": "update",
        "key": "preferred_area",
        "value": "Whitefield"
    })
    assert update_res.status_code == 200
    assert "Whitefield" in update_res.json()["reply"]


def test_api_proactive_cancellation():
    client = TestClient(app)
    res = client.post("/api/proactive-cancel", json={"booking_id": "bk_upcoming_ac_01"})
    assert res.status_code == 200
    data = res.json()
    assert "event_result" in data
    assert data["event_result"]["notified"] is True
    assert data["fsm_state"] == "AWAITING_APPROVAL"
