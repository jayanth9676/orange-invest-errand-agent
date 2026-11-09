"""Unit tests for Outbound MCP Client and Dynamic Tool Registry (Milestone 2 Feature 5)."""

import json
from pathlib import Path
import pytest
from src.db import LocalDatabase
from src.mcp_client import (
    MCPConfigLoader,
    MCPToolRegistry,
    OutboundMCPClient,
    ServerConfig,
    ToolDescriptor,
    sanitize_tool_arguments,
)
from src.mcp_server import MCPServerHandler
from src.mock_tools import MockToolEnvironment
from src.security import UserRole


@pytest.fixture
def db():
    database = LocalDatabase(":memory:")
    database.seed_default_data()
    return database


@pytest.fixture
def mcp_client(db):
    tools = MockToolEnvironment(db=db, user_id="user_1")
    handler = MCPServerHandler(tools_env=tools)
    return OutboundMCPClient(handler=handler, tools_env=tools)


# ==================== JSONC COMMENT PARSING TESTS ====================

def test_jsonc_strip_single_line_comments():
    raw = """
    {
        // This is a comment at start of line
        "mcpServers": {
            "test": {
                "serverUrl": "https://example.com/mcp" // trailing comment
            }
        }
    }
    """
    clean = MCPConfigLoader.strip_jsonc_comments(raw)
    data = json.loads(clean)
    assert "mcpServers" in data
    assert data["mcpServers"]["test"]["serverUrl"] == "https://example.com/mcp"


def test_jsonc_strip_multiline_comments():
    raw = """
    {
        /*
           Multi-line comment block
           describing something
        */
        "key": "value"
    }
    """
    clean = MCPConfigLoader.strip_jsonc_comments(raw)
    data = json.loads(clean)
    assert data["key"] == "value"


def test_jsonc_preserves_urls_with_double_slashes():
    raw = '{"url": "https://mcp.exa.ai/mcp", "url2": "http://localhost:8000/events"}'
    clean = MCPConfigLoader.strip_jsonc_comments(raw)
    data = json.loads(clean)
    assert data["url"] == "https://mcp.exa.ai/mcp"
    assert data["url2"] == "http://localhost:8000/events"


def test_jsonc_strips_trailing_commas():
    raw = """
    {
        "items": [1, 2, 3,],
        "obj": {"a": "b",},
    }
    """
    clean = MCPConfigLoader.strip_jsonc_comments(raw)
    data = json.loads(clean)
    assert data["items"] == [1, 2, 3]
    assert data["obj"] == {"a": "b"}


# ==================== REGISTRY TESTS ====================

def test_tool_registry_prepopulated_with_local_tools():
    registry = MCPToolRegistry()
    tools = registry.list_tools()
    assert len(tools) >= 8
    names = {t["name"] for t in tools}
    assert "search_providers" in names
    assert "hold_slot" in names
    assert "book" in names
    assert "get_booking_status" in names


def test_tool_registry_register_and_unregister():
    registry = MCPToolRegistry()
    desc = ToolDescriptor(
        name="custom_weather_check",
        description="Checks the weather forecast",
        input_schema={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        server_id="weather_srv",
    )
    registry.register(desc)
    assert registry.get("custom_weather_check") is not None
    assert len(registry.list_tools(server_id="weather_srv")) == 1

    registry.unregister("custom_weather_check")
    assert registry.get("custom_weather_check") is None


def test_tool_registry_llm_definitions():
    registry = MCPToolRegistry()
    llm_defs = registry.get_llm_definitions()
    assert len(llm_defs) >= 8
    first = llm_defs[0]
    assert first["type"] == "function"
    assert "name" in first["function"]
    assert "parameters" in first["function"]


def test_tool_registry_argument_validation():
    registry = MCPToolRegistry()
    ok, err = registry.validate_arguments("search_providers", {"service": "plumber", "area": "Indiranagar", "date_range": "today"})
    assert ok is True
    assert err is None

    ok_missing, err_missing = registry.validate_arguments("search_providers", {"service": "plumber"})
    assert ok_missing is False
    assert "Missing required parameter" in err_missing


# ==================== RBAC & SECURITY TESTS ====================

def test_auditor_role_blocked_from_mutating_tools(mcp_client):
    res_book = mcp_client.call_tool("book", {"hold_id": "hold_123", "idempotency_key": "idem_123"}, role="Auditor")
    assert res_book.get("result", {}).get("isError") is True
    assert "Role 'Auditor' is not authorized" in res_book["result"]["content"][0]["text"]

    res_cancel = mcp_client.call_tool("cancel", {"booking_id": "bk_123"}, role="Auditor")
    assert res_cancel.get("result", {}).get("isError") is True
    assert "Role 'Auditor' is not authorized" in res_cancel["result"]["content"][0]["text"]


def test_admin_role_allowed_for_tools(mcp_client):
    res_search = mcp_client.call_tool("search_providers", {"service": "plumber", "area": "Indiranagar", "date_range": "today"}, role="Admin")
    assert "result" in res_search
    assert not res_search.get("result", {}).get("isError", False)


def test_trojan_source_unicode_sanitization():
    # BiDi override character \u202e
    malicious_input = {"service": "plumber\u202e_reversed", "nested": {"area": "Indira\u200enagar"}}
    sanitized = sanitize_tool_arguments(malicious_input)
    assert "\u202e" not in sanitized["service"]
    assert "\u200e" not in sanitized["nested"]["area"]


# ==================== CLIENT HANDSHAKE & RESOURCES ====================

def test_mcp_client_handshake(mcp_client):
    init_res = mcp_client.initialize()
    assert init_res.get("jsonrpc") == "2.0"
    result = init_res.get("result", {})
    assert "protocolVersion" in result
    assert "serverInfo" in result
    assert result["serverInfo"]["name"] == "errand-agent-mcp"


def test_mcp_client_resources(mcp_client):
    resources = mcp_client.list_resources()
    assert len(resources) > 0
    uri = resources[0]["uri"]
    read_res = mcp_client.read_resource(uri)
    assert "result" in read_res
    contents = read_res["result"].get("contents", [])
    assert len(contents) > 0
    assert contents[0]["uri"] == uri
