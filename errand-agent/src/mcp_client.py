"""Outbound Model Context Protocol (MCP) Client and Dynamic Tool Registry (Nov 2026).

Provides an outbound MCP client allowing the Errand Agent engine to:
- Parse comment-tolerant JSONC configuration (~/.gemini/config/mcp_config.json)
- Discover tools dynamically from remote or local MCP servers over JSON-RPC 2.0 / SSE
- Maintain a unified registry of local errand tools and external MCP capabilities
- Enforce zero-trust Role-Based Access Control (RBAC) and Trojan Source Unicode defense
- Provide seamless, non-breaking execution with offline domain fallbacks
"""

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Optional

import httpx

from src.mcp_server import (
    MCP_PROTOCOL_VERSION,
    MCP_TOOLS_CATALOG,
    MCPServerHandler,
    SERVER_INFO,
    is_mutating_tool,
)
from src.metrics import METRICS
from src.mock_tools import MockToolEnvironment, ToolExecutionError
from src.security import UserRole, sanitize_input_text

logger = logging.getLogger(__name__)


@dataclass
class ServerConfig:
    """Configuration descriptor for an MCP server endpoint."""
    server_id: str
    server_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    disabled: bool = False
    disabled_tools: list[str] = field(default_factory=list)
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 15.0


@dataclass
class ToolDescriptor:
    """Metadata descriptor for a tool registered in the MCP tool registry."""
    name: str
    description: str
    input_schema: dict[str, Any]
    server_id: str = "local"
    is_mutating: bool = False
    handler: Optional[Callable[..., Any]] = None
    metadata: dict[str, Any] = field(default_factory=dict)


def sanitize_tool_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Recursively sanitize string arguments to neutralize Trojan Source Unicode (CVE-2021-42574)."""
    sanitized: dict[str, Any] = {}
    for k, v in arguments.items():
        if isinstance(v, str):
            sanitized[k] = sanitize_input_text(v)
        elif isinstance(v, dict):
            sanitized[k] = sanitize_tool_arguments(v)
        elif isinstance(v, list):
            sanitized[k] = [
                sanitize_input_text(item) if isinstance(item, str)
                else (sanitize_tool_arguments(item) if isinstance(item, dict) else item)
                for item in v
            ]
        else:
            sanitized[k] = v
    return sanitized


class MCPConfigLoader:
    """Loads and normalizes MCP client configuration from JSONC files and environment variables."""

    @classmethod
    def strip_jsonc_comments(cls, raw_text: str) -> str:
        """Strip single-line and multi-line comments while preserving URLs containing double slashes."""
        # 1. Strip multi-line comments /* ... */
        no_block_comments = re.sub(r'/\*.*?\*/', '', raw_text, flags=re.DOTALL)
        # 2. Strip single-line comments // that are not part of URLs (http:// or https://)
        lines = []
        for line in no_block_comments.splitlines():
            # If line starts with optional whitespace and //, remove it entirely
            if re.match(r'^\s*//', line):
                continue
            # Remove trailing // comments if preceded by whitespace and not inside URL
            cleaned_line = re.sub(r'(?<=\s)//(?![/]).*$', '', line)
            lines.append(cleaned_line)
        cleaned_text = "\n".join(lines)
        # 3. Strip trailing commas before closing braces/brackets
        cleaned_text = re.sub(r',\s*([\]}])', r'\1', cleaned_text)
        return cleaned_text

    @classmethod
    def load_servers(cls, config_path: Optional[str | Path] = None) -> dict[str, ServerConfig]:
        """Load configured MCP servers from config file and environment variables."""
        servers: dict[str, ServerConfig] = {}

        # 1. Attempt file resolution
        path_to_try: Optional[Path] = None
        if config_path:
            path_to_try = Path(config_path)
        else:
            default_path = Path.home() / ".gemini" / "config" / "mcp_config.json"
            if default_path.is_file():
                path_to_try = default_path

        if path_to_try and path_to_try.is_file():
            try:
                raw_content = path_to_try.read_text(encoding="utf-8")
                clean_json = cls.strip_jsonc_comments(raw_content)
                data = json.loads(clean_json)
                mcp_servers = data.get("mcpServers", {})
                for srv_id, srv_data in mcp_servers.items():
                    if isinstance(srv_data, dict):
                        servers[srv_id] = ServerConfig(
                            server_id=srv_id,
                            server_url=srv_data.get("serverUrl", ""),
                            headers=srv_data.get("headers", {}),
                            disabled=srv_data.get("disabled", False),
                            disabled_tools=srv_data.get("disabledTools", []),
                            command=srv_data.get("command", ""),
                            args=srv_data.get("args", []),
                            env=srv_data.get("env", {}),
                            timeout_seconds=float(srv_data.get("timeout", 15.0)),
                        )
            except Exception as exc:
                logger.warning("Failed to parse MCP config at %s: %s", path_to_try, exc)

        # 2. Environment variable overrides / supplements
        exa_key = os.getenv("EXA_API_KEY")
        if exa_key or "exa" not in servers:
            headers = {"x-api-key": exa_key} if exa_key else {}
            if "exa" in servers and exa_key:
                servers["exa"].headers.update(headers)
            elif "exa" not in servers:
                servers["exa"] = ServerConfig(
                    server_id="exa",
                    server_url=os.getenv("EXA_MCP_URL", "https://mcp.exa.ai/mcp"),
                    headers=headers,
                )

        c7_key = os.getenv("CONTEXT7_API_KEY")
        if c7_key or "context7" not in servers:
            headers = {"x-api-key": c7_key} if c7_key else {}
            if "context7" in servers and c7_key:
                servers["context7"].headers.update(headers)
            elif "context7" not in servers:
                servers["context7"] = ServerConfig(
                    server_id="context7",
                    server_url=os.getenv("CONTEXT7_MCP_URL", "https://mcp.context7.com/mcp"),
                    headers=headers,
                )

        return servers


class MCPToolRegistry:
    """Central thread-safe registry aggregating local and remote MCP tool definitions."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tools: dict[str, ToolDescriptor] = {}
        self._init_local_tools()

    def _init_local_tools(self) -> None:
        """Initialize registry with the 8 core Errand Agent tools."""
        with self._lock:
            for tool_meta in MCP_TOOLS_CATALOG:
                name = tool_meta["name"]
                desc = tool_meta.get("description", "")
                schema = tool_meta.get("inputSchema", {})
                mutating = is_mutating_tool(name)
                self._tools[name] = ToolDescriptor(
                    name=name,
                    description=desc,
                    input_schema=schema,
                    server_id="local",
                    is_mutating=mutating,
                )

    def register(self, descriptor: ToolDescriptor) -> None:
        """Register or update a tool descriptor in the registry."""
        with self._lock:
            self._tools[descriptor.name] = descriptor

    def unregister(self, name: str) -> bool:
        """Remove a tool descriptor from the registry."""
        with self._lock:
            return self._tools.pop(name, None) is not None

    def get(self, name: str) -> Optional[ToolDescriptor]:
        """Fetch descriptor for a given tool name."""
        with self._lock:
            return self._tools.get(name)

    def list_tools(self, server_id: Optional[str] = None) -> list[dict[str, Any]]:
        """List all tools formatted according to the standard MCP tools/list JSON-RPC schema."""
        with self._lock:
            results = []
            for t in self._tools.values():
                if server_id is not None and t.server_id != server_id:
                    continue
                results.append({
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.input_schema,
                })
            return results

    def get_llm_definitions(self, server_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Generate OpenAI/Anthropic compatible function calling schemas."""
        with self._lock:
            defs = []
            for t in self._tools.values():
                if server_id is not None and t.server_id != server_id:
                    continue
                defs.append({
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                })
            return defs

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> tuple[bool, Optional[str]]:
        """Validate input arguments against the registered JSON Schema for the tool."""
        descriptor = self.get(name)
        if not descriptor:
            return False, f"Unknown tool '{name}'."

        schema = descriptor.input_schema
        required = schema.get("required", [])
        for field_name in required:
            if field_name not in arguments or arguments[field_name] is None:
                return False, f"Missing required parameter '{field_name}' for tool '{name}'."

        properties = schema.get("properties", {})
        for param, val in arguments.items():
            if param in properties:
                expected_type = properties[param].get("type")
                if expected_type == "string" and not isinstance(val, str):
                    return False, f"Parameter '{param}' must be a string, got {type(val).__name__}."
                elif expected_type in ("integer", "number") and not isinstance(val, (int, float)):
                    return False, f"Parameter '{param}' must be numeric, got {type(val).__name__}."
                elif expected_type == "boolean" and not isinstance(val, bool):
                    return False, f"Parameter '{param}' must be a boolean, got {type(val).__name__}."

        return True, None


class OutboundMCPClient:
    """Outbound Model Context Protocol (MCP) Client and Dynamic Registry."""

    def __init__(
        self,
        handler: Optional[MCPServerHandler] = None,
        tools_env: Optional[Any] = None,
        config_path: Optional[str | Path] = None,
        db_conn: Optional[Any] = None,
        auto_discover: bool = False,
        timeout: float = 15.0,
    ) -> None:
        self.timeout = timeout
        self.db_conn = db_conn
        self.registry = MCPToolRegistry()
        self.server_configs = MCPConfigLoader.load_servers(config_path)

        # Wire local handler
        if handler is None:
            if tools_env is None:
                from src.db import LocalDatabase
                db = LocalDatabase("errand_demo.sqlite")
                tools_env = MockToolEnvironment(db=db, user_id="user_1")
            handler = MCPServerHandler(tools_env=tools_env)
        self.handler = handler

        self._req_counter = 0
        self._lock = threading.RLock()
        self._http = httpx.Client(timeout=self.timeout)

        if auto_discover:
            self.discover_all()

    def _next_id(self) -> int:
        with self._lock:
            self._req_counter += 1
            return self._req_counter

    # ==================== MCP STANDARD PROTOCOL METHODS ====================

    def initialize(self) -> dict[str, Any]:
        """Perform MCP initialize handshake, returning server capabilities and metadata."""
        METRICS.inc_counter("errand_agent_mcp_client_calls_total", labels={"method": "initialize", "status": "sent"})
        return self.handler.handle_jsonrpc({
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {"clientInfo": {"name": "errand-outbound-client", "version": "2.0.0"}},
            "id": self._next_id(),
        }, default_role=UserRole.ADMIN)

    def list_tools(self, server_id: Optional[str] = None) -> list[dict[str, Any]]:
        """List registered tools from the dynamic registry."""
        METRICS.inc_counter("errand_agent_mcp_client_calls_total", labels={"method": "tools/list", "status": "sent"})
        return self.registry.list_tools(server_id=server_id)

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        role: Any = UserRole.ADMIN,
    ) -> dict[str, Any]:
        """Execute a tool via JSON-RPC 2.0 with RBAC gating and Trojan Source Unicode sanitization."""
        call_id = self._next_id()
        METRICS.inc_counter("errand_agent_mcp_client_calls_total", labels={"method": "tools/call", "tool": name, "status": "sent"})

        # 1. Normalize and resolve role
        role_enum = UserRole.ADMIN
        if role is not None:
            role_str = getattr(role, "value", str(role)).strip().lower()
            if role_str == "auditor":
                role_enum = UserRole.AUDITOR
            elif role_str == "operator":
                role_enum = UserRole.OPERATOR

        # 2. RBAC check: Auditor cannot invoke mutating tools
        descriptor = self.registry.get(name)
        is_mutating = descriptor.is_mutating if descriptor else is_mutating_tool(name)
        if role_enum == UserRole.AUDITOR and is_mutating:
            return {
                "jsonrpc": "2.0",
                "id": call_id,
                "result": {
                    "content": [{
                        "type": "text",
                        "text": f"AuthorizationError: Role 'Auditor' is not authorized to execute mutating tool '{name}'.",
                    }],
                    "isError": True,
                },
            }

        # 3. Trojan Source Unicode Sanitization
        sanitized_args = sanitize_tool_arguments(arguments)

        # 4. Dispatch to local handler if local or descriptor not remote
        if descriptor is None or descriptor.server_id == "local":
            res = self.handler.handle_jsonrpc({
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": name, "arguments": sanitized_args, "role": role_enum.value},
                "id": call_id,
            }, default_role=role_enum)
            return res

        # 5. Remote tool dispatch
        server_cfg = self.server_configs.get(descriptor.server_id)
        if server_cfg and server_cfg.server_url:
            return self._call_remote_tool(server_cfg, name, sanitized_args, call_id)

        # Fallback if remote not reachable
        return {
            "jsonrpc": "2.0",
            "id": call_id,
            "error": {"code": -32601, "message": f"Server for tool '{name}' not found or unconfigured."},
        }

    def _call_remote_tool(
        self,
        server_cfg: ServerConfig,
        tool_name: str,
        arguments: dict[str, Any],
        call_id: int,
    ) -> dict[str, Any]:
        """Dispatch a JSON-RPC 2.0 tool call to a remote HTTP/SSE MCP server."""
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
            "id": call_id,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            **server_cfg.headers,
        }
        try:
            resp = self._http.post(
                server_cfg.server_url,
                json=payload,
                headers=headers,
                timeout=server_cfg.timeout_seconds,
            )
            # Check for SSE stream
            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" in content_type or "event:" in resp.text:
                for line in resp.text.splitlines():
                    line = line.strip()
                    if line.startswith("data:"):
                        payload_str = line[5:].strip()
                        if payload_str:
                            try:
                                return json.loads(payload_str)
                            except json.JSONDecodeError:
                                continue
            return resp.json()
        except Exception as exc:
            logger.warning("Remote MCP tool execution error for '%s': %s", tool_name, exc)
            return {
                "jsonrpc": "2.0",
                "id": call_id,
                "result": {
                    "content": [{"type": "text", "text": f"Remote MCP Error: {str(exc)}"}],
                    "isError": True,
                },
            }

    def list_resources(self) -> list[dict[str, Any]]:
        """List state resources available via MCP."""
        res = self.handler.handle_jsonrpc({
            "jsonrpc": "2.0",
            "method": "resources/list",
            "params": {},
            "id": self._next_id(),
        }, default_role=UserRole.ADMIN)
        return res.get("result", {}).get("resources", [])

    def read_resource(self, uri: str) -> dict[str, Any]:
        """Read resource content by URI via MCP."""
        res = self.handler.handle_jsonrpc({
            "jsonrpc": "2.0",
            "method": "resources/read",
            "params": {"uri": uri},
            "id": self._next_id(),
        }, default_role=UserRole.ADMIN)
        return res

    # ==================== DYNAMIC DISCOVERY METHODS ====================

    def discover_server(self, server_id: str) -> list[dict[str, Any]]:
        """Introspect a remote MCP server via tools/list and register discovered tools."""
        server_cfg = self.server_configs.get(server_id)
        if not server_cfg or not server_cfg.server_url or server_cfg.disabled:
            return []

        payload = {
            "jsonrpc": "2.0",
            "method": "tools/list",
            "params": {},
            "id": self._next_id(),
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            **server_cfg.headers,
        }
        try:
            resp = self._http.post(
                server_cfg.server_url,
                json=payload,
                headers=headers,
                timeout=server_cfg.timeout_seconds,
            )
            data: dict[str, Any] = {}
            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" in content_type or "event:" in resp.text:
                for line in resp.text.splitlines():
                    line = line.strip()
                    if line.startswith("data:"):
                        payload_str = line[5:].strip()
                        if payload_str:
                            try:
                                data = json.loads(payload_str)
                                break
                            except json.JSONDecodeError:
                                continue
            else:
                data = resp.json()

            tools = data.get("result", {}).get("tools", [])
            for t in tools:
                name = t.get("name")
                if not name or name in server_cfg.disabled_tools:
                    continue
                self.registry.register(ToolDescriptor(
                    name=name,
                    description=t.get("description", ""),
                    input_schema=t.get("inputSchema", {}),
                    server_id=server_id,
                    is_mutating=is_mutating_tool(name),
                ))
            return tools
        except Exception as exc:
            logger.info("Could not dynamically connect to MCP server '%s' (%s): %s", server_id, server_cfg.server_url, exc)
            return []

    def discover_all(self) -> dict[str, list[dict[str, Any]]]:
        """Discover tools across all configured MCP servers."""
        results: dict[str, list[dict[str, Any]]] = {}
        for srv_id in list(self.server_configs.keys()):
            results[srv_id] = self.discover_server(srv_id)
        return results
