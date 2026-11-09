"""Model Context Protocol (MCP) Integration for Errand Agent (Nov 2026 Standard).

Provides JSON-RPC 2.0 and REST endpoints allowing enterprise agent runners,
Claude Desktop, Cursor, and external MCP orchestrators to interact with the
Errand Agent tool catalog, state resources, and prompt templates.
"""

import json
from typing import Any, Optional
from src.metrics import METRICS
from src.mock_tools import MockToolEnvironment, ToolExecutionError


MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {
    "name": "errand-agent-mcp",
    "version": "2.0.0",
    "description": "Enterprise Model Context Protocol (MCP) server for Errand Agent home services",
}

# Complete JSON Schema Catalog for all 8 Errand Agent tools
MCP_TOOLS_CATALOG: list[dict[str, Any]] = [
    {
        "name": "search_providers",
        "description": "Search local service providers by service type, area, and optional date range. Returns verified providers with ratings, pricing, and available slots.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Category of home service (e.g., 'plumber', 'electrician', 'ac_servicing')",
                },
                "area": {
                    "type": "string",
                    "description": "Geographical locality or neighbourhood (e.g., 'Indiranagar', 'Koramangala', 'Whitefield')",
                },
                "date_range": {
                    "type": "string",
                    "description": "Target date or date range query (e.g., 'this week', '2026-11-09')",
                },
            },
            "required": ["service", "area", "date_range"],
        },
    },
    {
        "name": "get_provider",
        "description": "Fetch detailed provider profile by provider_id, including reviews, license status, pricing, and available slots.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider_id": {
                    "type": "string",
                    "description": "Unique identifier of the provider (e.g., 'prov_plumb_1')",
                },
            },
            "required": ["provider_id"],
        },
    },
    {
        "name": "hold_slot",
        "description": "Acquire a temporary reservation hold on an available provider slot with fixed price lock for 600 seconds.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider_id": {
                    "type": "string",
                    "description": "Unique identifier of the provider",
                },
                "slot": {
                    "type": "string",
                    "description": "Exact ISO slot timestamp to hold (e.g., '2026-11-09 09:00')",
                },
            },
            "required": ["provider_id", "slot"],
        },
    },
    {
        "name": "book",
        "description": "Confirm and book a reserved slot hold using an RFC-4122 deterministic idempotency key. Requires prior cryptographic approval.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hold_id": {
                    "type": "string",
                    "description": "Active reservation hold ID acquired via hold_slot",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Deterministic RFC-4122 UUIDv5 idempotency key",
                },
            },
            "required": ["hold_id", "idempotency_key"],
        },
    },
    {
        "name": "get_booking_status",
        "description": "Reconcile booking status via hold ID or idempotency key to recover from network timeouts without duplicate commits.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hold_id_or_idempotency_key": {
                    "type": "string",
                    "description": "The hold ID or idempotency key to query authoritative status for",
                },
            },
            "required": ["hold_id_or_idempotency_key"],
        },
    },
    {
        "name": "cancel",
        "description": "Cancel an existing booking with write-ahead idempotency and retry protection.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "booking_id": {
                    "type": "string",
                    "description": "Booking ID to cancel (e.g., 'bk_upcoming_ac_01')",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Deterministic idempotency key for safe retries",
                },
            },
            "required": ["booking_id", "idempotency_key"],
        },
    },
    {
        "name": "reschedule",
        "description": "Reschedule an existing booking to a new available slot with conflict checking and idempotency protection.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "booking_id": {
                    "type": "string",
                    "description": "Booking ID to reschedule",
                },
                "slot": {
                    "type": "string",
                    "description": "New target slot timestamp",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Deterministic idempotency key",
                },
            },
            "required": ["booking_id", "slot", "idempotency_key"],
        },
    },
    {
        "name": "list_bookings",
        "description": "Retrieve all confirmed and historical bookings for a user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Unique user ID (default: 'user_1')",
                },
            },
            "required": ["user_id"],
        },
    },
]

MCP_PROMPTS_CATALOG: list[dict[str, Any]] = [
    {
        "name": "errand_discovery",
        "description": "Prompt template for discovering home service intent and extracting slots.",
        "arguments": [
            {"name": "user_input", "description": "Raw user request text", "required": True},
        ],
    },
    {
        "name": "approval_confirmation",
        "description": "Prompt template for constructing human-in-the-loop cryptographic proposal confirmation.",
        "arguments": [
            {"name": "provider_name", "description": "Selected service provider", "required": True},
            {"name": "slot", "description": "Proposed appointment slot", "required": True},
            {"name": "price", "description": "Final quoted price in INR", "required": True},
        ],
    },
]

MCP_RESOURCES_CATALOG: list[dict[str, Any]] = [
    {
        "uri": "errand://system/fsm-state",
        "name": "FSM State Machine State",
        "description": "Authoritative real-time state of the Errand Agent Finite State Machine",
        "mimeType": "application/json",
    },
    {
        "uri": "errand://catalog/providers",
        "name": "Service Providers Catalog",
        "description": "Catalog of verified local home-service providers in Bengaluru",
        "mimeType": "application/json",
    },
    {
        "uri": "errand://telemetry/metrics",
        "name": "Agent Operational Metrics",
        "description": "Current Prometheus performance and reliability counters",
        "mimeType": "text/plain",
    },
]


VALID_TOOL_NAMES: set[str] = {t["name"] for t in MCP_TOOLS_CATALOG}
MUTATING_TOOLS: set[str] = {"hold_slot", "book", "cancel", "reschedule"}
READONLY_TOOLS: set[str] = {"search_providers", "get_provider", "get_booking_status", "list_bookings"}


def is_mutating_tool(tool_name: str) -> bool:
    return tool_name in MUTATING_TOOLS


class MCPServerHandler:
    """Handles JSON-RPC 2.0 requests per Model Context Protocol specification."""

    def __init__(self, tools_env: MockToolEnvironment, orchestrator: Optional[Any] = None) -> None:
        self.tools = tools_env
        self.orchestrator = orchestrator

    def handle_jsonrpc(self, request: Any, default_role: Optional[Any] = None) -> Any:
        """Dispatch a JSON-RPC 2.0 message or batch array of messages."""
        if isinstance(request, list):
            if not request:
                return {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Invalid Request: empty batch array"},
                }
            return [self._handle_single_jsonrpc(item, default_role=default_role) for item in request]
        return self._handle_single_jsonrpc(request, default_role=default_role)

    def _handle_single_jsonrpc(self, request: Any, default_role: Optional[Any] = None) -> dict[str, Any]:
        """Process a single JSON-RPC 2.0 request dictionary."""
        if not isinstance(request, dict):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request: body must be a JSON object"},
            }

        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params")

        if not isinstance(params, dict):
            params = {} if params is None else {"_raw": params}

        METRICS.inc_counter("errand_agent_mcp_requests_total", labels={"method": str(method or "unknown"), "status": "received"})

        if not method or not isinstance(method, str):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32600, "message": "Invalid Request: missing or non-string method"},
            }

        # Initialize Handshake
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                        "prompts": {"listChanged": False},
                    },
                    "serverInfo": SERVER_INFO,
                },
            }

        # Ping
        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        # Notifications (e.g. notifications/initialized) require no response
        if method.startswith("notifications/"):
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        # Tools listing
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": MCP_TOOLS_CATALOG},
            }

        # Tool execution
        if method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments")
            if tool_args is None:
                tool_args = {}
            caller_role = default_role if default_role is not None else (params.get("role") or params.get("_role"))
            res = self.execute_tool(tool_name, tool_args, role=caller_role)
            METRICS.inc_counter(
                "errand_agent_mcp_requests_total",
                labels={"method": "tools/call", "status": "ok" if not res.get("isError") else "error"},
            )
            return {"jsonrpc": "2.0", "id": req_id, "result": res}

        # Resources listing
        if method == "resources/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"resources": MCP_RESOURCES_CATALOG},
            }

        # Resources reading
        if method == "resources/read":
            uri = params.get("uri", "")
            return self._handle_resource_read(req_id, uri)

        # Prompts listing
        if method == "prompts/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"prompts": MCP_PROMPTS_CATALOG},
            }

        # Prompts retrieval
        if method == "prompts/get":
            prompt_name = params.get("name", "")
            prompt_args = params.get("arguments") or {}
            return self._handle_prompt_get(req_id, prompt_name, prompt_args)

        # Method not found
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    def execute_tool(
        self,
        tool_name: Optional[str],
        arguments: Any,
        role: Optional[Any] = None,
    ) -> dict[str, Any]:
        """Execute a tool and return an MCP-compliant call result.

        Enforces role-based permissions:
        - If role is 'Auditor', mutating tools (hold_slot, book, cancel, reschedule) are rejected with error.
        - If role is 'Operator' or 'Admin' (or None for backward compatibility), full execution is permitted.
        """
        if not tool_name:
            return {
                "content": [{"type": "text", "text": "Error: Missing tool name"}],
                "isError": True,
            }

        if tool_name not in VALID_TOOL_NAMES or not hasattr(self.tools, tool_name):
            return {
                "content": [{"type": "text", "text": f"Error: Tool '{tool_name}' not found in Errand Agent catalog"}],
                "isError": True,
            }

        # Extract role if embedded in arguments dict
        if isinstance(arguments, dict):
            if role is None:
                role = arguments.pop("_role", None) or arguments.pop("role", None)
            else:
                arguments.pop("_role", None)
                arguments.pop("role", None)

        # Enforce Auditor role mutation gating
        if role is not None and is_mutating_tool(tool_name):
            role_str = getattr(role, "value", str(role)).strip()
            from src.security import check_permission, UserRole
            try:
                user_role = UserRole(role_str.title()) if isinstance(role_str, str) else role
                has_perm = check_permission(user_role, "write:mutations")
            except ValueError:
                has_perm = (role_str.lower() != "auditor")
            if not has_perm:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"AuthorizationError: Role '{role_str}' is not authorized to execute mutating tool '{tool_name}'. Read-only access permitted.",
                        }
                    ],
                    "isError": True,
                }

        if not isinstance(arguments, dict):
            return {
                "content": [{"type": "text", "text": "Error: Tool arguments must be a JSON object (mapping)"}],
                "isError": True,
            }

        # Apply Trojan Source sanitization to string values in tool arguments
        from src.security import sanitize_input_text
        sanitized_args = {}
        for k, v in arguments.items():
            if isinstance(v, str):
                sanitized_args[k] = sanitize_input_text(v)
            else:
                sanitized_args[k] = v
        arguments = sanitized_args

        try:
            fn = getattr(self.tools, tool_name)
            result = fn(**arguments)
            return {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
                "isError": False,
            }
        except ToolExecutionError as tee:
            return {
                "content": [{"type": "text", "text": f"ToolExecutionError [{tee.code}]: {tee.message}"}],
                "isError": True,
            }
        except TypeError as te:
            return {
                "content": [{"type": "text", "text": f"ToolExecutionError [INVALID_ARGUMENTS]: {str(te)}"}],
                "isError": True,
            }
        except Exception as exc:
            return {
                "content": [{"type": "text", "text": f"Unexpected Tool Error: {str(exc)}"}],
                "isError": True,
            }

    def _handle_resource_read(self, req_id: Any, uri: str) -> dict[str, Any]:
        if uri == "errand://system/fsm-state":
            fsm_state = self.orchestrator.state.value if self.orchestrator else "IDLE"
            turn_counter = self.orchestrator.turn_counter if self.orchestrator else 0
            active_prop = (
                self.orchestrator.active_proposal.model_dump()
                if self.orchestrator and self.orchestrator.active_proposal
                else None
            )
            state_data = {
                "fsm_state": fsm_state,
                "turn_counter": turn_counter,
                "active_proposal": active_prop,
                "session_id": getattr(self.orchestrator, "session_id", "live_demo_session"),
                "total_bookings": len(self.tools.list_bookings("user_1")["bookings"]),
            }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(state_data, ensure_ascii=False, indent=2),
                        }
                    ]
                },
            }
        if uri == "errand://catalog/providers":
            providers = self.tools.search_providers("plumber", "Indiranagar", "2026-11-09")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(providers, ensure_ascii=False, indent=2),
                        }
                    ]
                },
            }
        if uri == "errand://telemetry/metrics":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "text/plain",
                            "text": METRICS.render_prometheus(),
                        }
                    ]
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": f"Unknown resource URI: {uri}"},
        }

    def _handle_prompt_get(self, req_id: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Render a prompt template with provided arguments."""
        if name == "errand_discovery":
            user_input = args.get("user_input", "")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "description": "Errand intent discovery prompt",
                    "messages": [
                        {
                            "role": "system",
                            "content": {
                                "type": "text",
                                "text": "You are Errand Agent. Extract home service intent and required slots.",
                            },
                        },
                        {
                            "role": "user",
                            "content": {"type": "text", "text": user_input},
                        },
                    ],
                },
            }
        if name == "approval_confirmation":
            prov = args.get("provider_name", "Provider")
            slot = args.get("slot", "Slot")
            price = args.get("price", "Price")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "description": "Cryptographic approval confirmation prompt",
                    "messages": [
                        {
                            "role": "user",
                            "content": {
                                "type": "text",
                                "text": f"Proposed booking with {prov} at {slot} for ₹{price}. Confirm explicit approval?",
                            },
                        }
                    ],
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": f"Unknown prompt template: {name}"},
        }
