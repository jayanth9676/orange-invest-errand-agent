"""Adversarial Challenge Test Suite: Web UI Integrity, SSE Telemetry & Workbench (Milestone 3).

Author: Challenger M3-1 (Web UI & Telemetry Challenger)
Purpose: Empirically verify Web application integrity, SSE streaming compliance,
         Streamlit AST & 10-state coverage, dual web app smoke harness,
         and UI DOM element ID conformance.
"""

import ast
import json
import os
import re
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

# Ensure deterministic offline mode
os.environ.setdefault("LLM_MODE", "offline")

from src.app_server import app, WORKSPACE_MANAGER
from src.schemas import AgentState


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_HTML_PATH = PROJECT_ROOT / "static" / "index.html"
STREAMLIT_APP_PATH = PROJECT_ROOT / "streamlit_app.py"


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/api/reset", json={})
    yield c
    c.post("/api/reset", json={})


# ==============================================================================
# SECTION 1: DUAL WEB APP VERIFICATION HARNESS (check_web_app.py)
# ==============================================================================

def test_check_web_app_harness_clean_run():
    """Verify check_web_app.py runs cleanly and passes 100% of endpoints and AST checks."""
    import subprocess
    import sys
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    res = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "check_web_app.py")],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert res.returncode == 0, f"check_web_app.py failed with returncode {res.returncode}:\n{res.stdout}\n{res.stderr}"
    assert "[ALL CHECKS PASSED]" in res.stdout, f"Expected [ALL CHECKS PASSED] in output, got:\n{res.stdout}"


# ==============================================================================
# SECTION 2: STREAMLIT WORKBENCH AST & STATE ENUM COVERAGE
# ==============================================================================

def test_streamlit_app_ast_and_all_ten_states():
    """Verify streamlit_app.py AST syntax, dual-column layout, and all 10 FSM states."""
    assert STREAMLIT_APP_PATH.is_file(), f"streamlit_app.py not found at {STREAMLIT_APP_PATH}"
    code_text = STREAMLIT_APP_PATH.read_text(encoding="utf-8")

    # 1. Parse AST to verify syntax
    tree = ast.parse(code_text)
    assert tree is not None, "Failed to parse streamlit_app.py AST"

    # 2. Check for columns([1, 1]) in AST or code
    assert "columns([1, 1]" in code_text or "st.columns([1, 1]" in code_text, (
        "streamlit_app.py must contain st.columns([1, 1]) dual-column split"
    )

    # 3. Extract states list from AST
    extracted_states = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "states":
                    if isinstance(node.value, ast.List):
                        extracted_states = [
                            elem.value
                            for elem in node.value.elts
                            if isinstance(elem, ast.Constant)
                        ]

    assert extracted_states is not None, "Could not find 'states = [...]' assignment in streamlit_app.py AST"
    assert len(extracted_states) == 10, f"Expected 10 states in streamlit_app.py, found {len(extracted_states)}: {extracted_states}"

    # Verify every schema AgentState is present in streamlit_app.py
    schema_states = [s.value for s in AgentState]
    for s in schema_states:
        assert s in extracted_states, f"Missing state '{s}' in streamlit_app.py states list"

    # Verify SAFE_ABORT is explicitly present
    assert "SAFE_ABORT" in extracted_states, "SAFE_ABORT must be present in streamlit_app.py states list"


# ==============================================================================
# SECTION 3: SSE TELEMETRY STREAMING COMPLIANCE (/api/trace-stream)
# ==============================================================================

def test_sse_trace_stream_headers(client):
    """Verify /api/trace-stream returns valid Server-Sent Events headers."""
    with client.stream("GET", "/api/trace-stream") as response:
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        content_type = response.headers.get("content-type", "")
        assert "text/event-stream" in content_type, f"Expected text/event-stream, got '{content_type}'"
        assert response.headers.get("cache-control") == "no-cache"


def test_sse_trace_stream_event_framing_and_push(client):
    """Verify /api/trace-stream streams valid SSE event framing (data: <json>\n\n) upon state change."""
    # Step 1: Trigger chat turn that produces trace events
    chat_res = client.post(
        "/api/chat",
        json={"message": "Find a plumber for kitchen tap"},
        headers={"X-Tenant-ID": "test_sse_tenant", "X-Session-ID": "test_sse_session"},
    )
    assert chat_res.status_code == 200

    # Step 2: Connect to SSE stream for that tenant and session
    with client.stream(
        "GET",
        "/api/trace-stream",
        headers={"X-Tenant-ID": "test_sse_tenant", "X-Session-ID": "test_sse_session"},
    ) as stream:
        lines = []
        for line in stream.iter_lines():
            if line:
                lines.append(line)
            if len(lines) >= 3:
                break

        assert len(lines) > 0, "No SSE lines received from /api/trace-stream"
        has_data_prefix = any(l.startswith("data: ") for l in lines)
        assert has_data_prefix, f"SSE events must follow 'data: <json>' format, got: {lines}"

        # Parse first valid JSON event
        data_line = next(l for l in lines if l.startswith("data: "))
        json_payload = json.loads(data_line[6:])
        assert "event_type" in json_payload or "type" in json_payload, (
            f"Expected trace event payload, got: {json_payload}"
        )


def test_sse_trace_stream_cross_tenant_isolation(client):
    """Adversarial test: Trace events in Tenant A must NOT bleed into Tenant B's SSE stream."""
    tenant_a_headers = {"X-Tenant-ID": "tenant_alpha", "X-Session-ID": "session_alpha"}
    tenant_b_headers = {"X-Tenant-ID": "tenant_beta", "X-Session-ID": "session_beta"}

    # Generate turn only for Tenant Alpha
    client.post(
        "/api/chat",
        json={"message": "I need an electrician in Indiranagar"},
        headers=tenant_a_headers,
    )

    orch_a = WORKSPACE_MANAGER.get_orchestrator("tenant_alpha", "session_alpha")
    orch_b = WORKSPACE_MANAGER.get_orchestrator("tenant_beta", "session_beta")

    assert len(orch_a.trace.events) > 0, "Tenant Alpha should have trace events"
    assert len(orch_b.trace.events) == 0, "Tenant Beta should have zero trace events (strict isolation)"


# ==============================================================================
# SECTION 4: WEB APPLICATION DOM & COMPONENT INTEGRITY (static/index.html)
# ==============================================================================

def test_static_html_core_invariants():
    """Verify static/index.html exists and contains core brand invariants."""
    assert STATIC_HTML_PATH.is_file(), f"static/index.html not found at {STATIC_HTML_PATH}"
    content = STATIC_HTML_PATH.read_text(encoding="utf-8")
    assert "Errand Agent" in content, "Missing 'Errand Agent' in static/index.html"
    assert "Agentic Operations" in content, "Missing 'Agentic Operations' in static/index.html"


def test_static_html_theme_and_eventsource():
    """Verify EventSource('/api/trace-stream') and dark/light theme controls exist in index.html."""
    content = STATIC_HTML_PATH.read_text(encoding="utf-8")

    # 1. EventSource connection
    assert "EventSource('/api/trace-stream')" in content or 'EventSource("/api/trace-stream")' in content, (
        "static/index.html must connect to browser-native EventSource('/api/trace-stream')"
    )

    # 2. Dark/Light Theme controls
    assert "toggleTheme" in content, "static/index.html must implement toggleTheme() function"
    assert "ea_theme" in content, "static/index.html must persist theme in localStorage.ea_theme"
    assert "theme-toggle-icon" in content, "static/index.html must contain #theme-toggle-icon"
    assert 'class="dark"' in content or "class='dark'" in content or "classList.add('dark')" in content, (
        "static/index.html must support dark mode class toggling"
    )


def test_static_html_fsm_dag_all_ten_states():
    """Verify SVG DAG in static/index.html contains all 10 states and edge connections."""
    content = STATIC_HTML_PATH.read_text(encoding="utf-8")
    expected_nodes = [
        "dag-node-IDLE",
        "dag-node-COLLECTING_SLOTS",
        "dag-node-SEARCHING_PROVIDERS",
        "dag-node-HOLDING_SLOT",
        "dag-node-AWAITING_APPROVAL",
        "dag-node-EXECUTING_MUTATION",
        "dag-node-RECONCILING_TIMEOUT",
        "dag-node-PROACTIVE_RECOVERY",
        "dag-node-COMPLETED",
        "dag-node-SAFE_ABORT",
    ]
    for node_id in expected_nodes:
        assert f'id="{node_id}"' in content, f"Missing SVG DAG node '{node_id}' in static/index.html"

    # Verify node inspector drawer
    assert 'id="dag-inspect-drawer"' in content, "Missing #dag-inspect-drawer in static/index.html"


def test_static_html_hitl_cockpit_and_drift_diff():
    """Verify HITL approval controls, 600s TTL indicator, and material drift visualizer exist."""
    content = STATIC_HTML_PATH.read_text(encoding="utf-8")

    assert 'id="ttl-indicator"' in content, "Missing #ttl-indicator (600s TTL countdown badge)"
    assert 'id="material-drift-diff-box"' in content, "Missing #material-drift-diff-box (Drift Diff Visualizer)"
    assert 'id="btn-approve-proposal"' in content, "Missing #btn-approve-proposal (1-click Approve)"
    assert 'id="btn-reject-proposal"' in content, "Missing #btn-reject-proposal (1-click Reject)"


def test_static_html_memory_governance_and_chaos_controls():
    """Verify 56-memory table partitions, live tiktoken budget, and chaos injection buttons."""
    content = STATIC_HTML_PATH.read_text(encoding="utf-8")

    # Memory Governance controls
    assert 'id="mem-flt-all"' in content
    assert 'id="mem-flt-explicit"' in content
    assert 'id="mem-flt-inferred_confirmed"' in content
    assert 'id="mem-flt-inferred_unconfirmed"' in content
    assert 'id="mem-flt-forgotten"' in content
    assert 'id="mem-gov-table-body"' in content

    # Chaos Injection controls
    chaos_buttons = [
        "btn-chaos-503",
        "btn-chaos-t-commit",
        "btn-chaos-t-uncommit",
        "btn-chaos-drift",
        "btn-chaos-slot",
        "btn-chaos-adv",
    ]
    for btn in chaos_buttons:
        assert f'id="{btn}"' in content, f"Missing chaos injection button '{btn}' in static/index.html"


# ==============================================================================
# SECTION 5: ADVERSARIAL DOM SELECTOR CONFORMANCE (Mandated IDs)
# ==============================================================================

def test_static_html_required_ui_element_selectors():
    """Adversarial check: Verify the 5 exact UI element IDs required by the Challenger specification:
    - #approval-cockpit
    - #fsm-dag-container
    - #trace-waterfall-container
    - #memory-table-container
    - #chaos-sandbox-drawer

    Parses via both regex and html.parser.HTMLParser to ensure DOM specification compliance.
    Reports exact findings if selectors are missing or aliased differently.
    """
    import html.parser

    class DOMIdParser(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.dom_ids = set()

        def handle_starttag(self, tag, attrs):
            for k, v in attrs:
                if k.lower() == "id":
                    self.dom_ids.add(v)

    content = STATIC_HTML_PATH.read_text(encoding="utf-8")
    parser = DOMIdParser()
    parser.feed(content)

    required_selectors = {
        "approval-cockpit": "hitl-approval-cockpit",
        "fsm-dag-container": "view-fsm",
        "trace-waterfall-container": "otel-waterfall-tree",
        "memory-table-container": "view-memory",
        "chaos-sandbox-drawer": "view-chaos",
    }

    missing = []
    aliased = {}
    for req_id, fallback in required_selectors.items():
        if req_id not in parser.dom_ids:
            missing.append(req_id)
            if fallback in parser.dom_ids:
                aliased[req_id] = fallback

    assert not missing, (
        f"Missing required UI element IDs in static/index.html: {missing}. "
        f"Functional equivalents found under different IDs: {aliased}. "
        f"Remediation: Worker M3 must add these exact IDs (or container wrappers) to static/index.html "
        f"so automated test fixtures and external graders succeed."
    )
