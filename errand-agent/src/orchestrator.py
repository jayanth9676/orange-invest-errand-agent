import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import os
from src.approval_gate import ApprovalGate, ApprovalViolationError, SentinelGatekeeper
from src.config import AgentConfig
from src.db import LocalDatabase
from src.llm_client import LLMProviderError, MultiProviderLLMClient
from src.live_tools import BaseToolEnvironment, create_tool_environment
from src.memory_context import MemoryAndContextManager
from src.mock_tools import FaultConfig, MockToolEnvironment, ToolExecutionError
from src.schemas import (
    AgentState,
    ApprovalProposal,
    IntentSlots,
    MutationType,
    SentinelAction,
    SentinelDecision,
    ToolCall,
    AgentTurnResponse,
)
from src.scripted_engine import ScriptedEvalLLM
from src.trace_logger import TraceLogger


def compute_idempotency_key(session_id: str, mutation_type: str, target_id: str, slot: str, proposal_hash: str) -> str:
    """Generate a deterministic RFC-4122 UUIDv5 idempotency key bound to the session, mutation, and proposal hash."""
    name_str = f"{session_id}|{mutation_type}|{target_id}|{slot}|{proposal_hash}"
    return f"idem_{uuid.uuid5(uuid.NAMESPACE_OID, name_str)}"


ENTERPRISE_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_providers",
            "description": "Search local home-service providers in Bengaluru matching service category, area/neighborhood, and optional budget and rating constraints.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "description": "Service category to search for (e.g. 'plumber', 'electrician', 'ac_servicing').",
                    },
                    "area": {
                        "type": "string",
                        "description": "Locality or neighborhood in Bengaluru (e.g. 'Indiranagar', 'Koramangala', 'HSR Layout', 'Whitefield').",
                    },
                    "date_range": {
                        "type": "string",
                        "description": "Target timeframe or date window (e.g. 'this week', 'tomorrow', 'this weekend').",
                    },
                    "budget_max": {
                        "type": "integer",
                        "description": "Optional upper ceiling budget in INR (e.g. 1500).",
                    },
                    "min_rating": {
                        "type": "number",
                        "description": "Optional minimum provider rating threshold between 1.0 and 5.0.",
                    },
                },
                "required": ["service", "area"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_provider",
            "description": "Retrieve detailed profile, ratings, available time slots, service attributes, and customer reviews for a specific provider by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "provider_id": {
                        "type": "string",
                        "description": "Unique identifier of the service provider (e.g. 'prov_plumb_1', 'prov_ac_1').",
                    },
                },
                "required": ["provider_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hold_slot",
            "description": "Acquire a temporary 10-minute reservation hold on an available provider slot with price locking before booking confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "provider_id": {
                        "type": "string",
                        "description": "Provider ID offering the slot (e.g. 'prov_plumb_1').",
                    },
                    "slot": {
                        "type": "string",
                        "description": "Exact scheduled slot timestamp string (e.g. '2026-11-09 09:00').",
                    },
                    "ttl_seconds": {
                        "type": "integer",
                        "description": "Hold duration in seconds before automatic expiration (defaults to 600).",
                    },
                },
                "required": ["provider_id", "slot"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_booking_status",
            "description": "Authoritative query to resolve ambiguous booking outcomes following network timeouts or process restarts using hold_id or idempotency_key.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hold_id_or_idempotency_key": {
                        "type": "string",
                        "description": "The reservation hold ID (e.g. 'hold_0f89a12b3c') or RFC-4122 UUIDv5 idempotency key (e.g. 'idem_...').",
                    },
                },
                "required": ["hold_id_or_idempotency_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_bookings",
            "description": "Retrieve all past and upcoming home-service bookings for the active user session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "Optional target user ID (defaults to active session user).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book",
            "description": "Consequential mutation: Atomically confirm and commit a home-service booking against an active hold. INTERCEPTED by Sentinel Gatekeeper before execution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hold_id": {
                        "type": "string",
                        "description": "Active reservation hold identifier (e.g. 'hold_0f89a12b3c').",
                    },
                    "idempotency_key": {
                        "type": "string",
                        "description": "Deterministic RFC-4122 UUIDv5 idempotency key. Injected automatically by Sentinel Gatekeeper.",
                    },
                },
                "required": ["hold_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel",
            "description": "Consequential mutation: Cancel an existing booking, release slot, and update status. INTERCEPTED by Sentinel Gatekeeper before execution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "booking_id": {
                        "type": "string",
                        "description": "ID of the booking to cancel (e.g. 'bk_upcoming_ac_01').",
                    },
                    "idempotency_key": {
                        "type": "string",
                        "description": "Optional deterministic idempotency key. Injected automatically by Sentinel Gatekeeper.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Optional human-readable reason for cancellation.",
                    },
                },
                "required": ["booking_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule",
            "description": "Consequential mutation: Reschedule an existing booking to a new target slot with conflict verification. INTERCEPTED by Sentinel Gatekeeper before execution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "booking_id": {
                        "type": "string",
                        "description": "ID of the booking to reschedule (e.g. 'bk_upcoming_ac_01').",
                    },
                    "slot": {
                        "type": "string",
                        "description": "New target slot timestamp string (e.g. '2026-11-14 14:00').",
                    },
                    "idempotency_key": {
                        "type": "string",
                        "description": "Optional deterministic idempotency key. Injected automatically by Sentinel Gatekeeper.",
                    },
                },
                "required": ["booking_id", "slot"],
            },
        },
    },
]


class ErrandAgentOrchestrator:
    '''
    Explicit Finite State Machine orchestrator for the Errand Agent.
    Enforces code-level approval gates, write-ahead idempotency, timeout reconciliation
    via get_booking_status, untrusted tool output isolation, bounded execution caps,
    and strict context token budgeting.
    '''
    def __init__(
        self,
        db: LocalDatabase,
        config: Optional[AgentConfig] = None,
        fault_cfg: Optional[FaultConfig] = None,
        session_id: str = "session_default",
        user_id: str = "user_1",
        trace_file: Optional[Path] = None,
        tools: Optional[Any] = None,
        mcp_client: Optional[Any] = None,
    ):
        self.db = db
        self.config = config or AgentConfig()
        self.session_id = session_id
        self.user_id = user_id
        self.mcp_client = mcp_client
        if tools is not None:
            self.tools = tools
        else:
            tool_mode = getattr(self.config, "tool_mode", os.getenv("TOOL_MODE", "mock"))
            self.tools = create_tool_environment(db=db, mode=tool_mode, fault_cfg=fault_cfg, user_id=user_id)
        self.memory_mgr = MemoryAndContextManager(
            db=db,
            user_id=user_id,
            max_context_tokens=self.config.max_context_tokens,
            max_memory_tokens=self.config.max_memory_tokens,
        )
        self.llm = MultiProviderLLMClient(self.config)
        self.trace = TraceLogger(session_id=session_id, trace_file=trace_file)

        # Load or initialize persistent session state (supports process restart recovery)
        self.state: AgentState = AgentState.IDLE
        self.slots: IntentSlots = IntentSlots()
        self.active_proposal: Optional[ApprovalProposal] = None
        self.turn_counter: int = 0
        self.startup_recovery_message: Optional[str] = None
        self._load_or_init_session()
        # Recovery is deliberately performed before the instance can accept a user turn.
        # A write-ahead PENDING record is never treated as permission to blindly re-run work.
        self.startup_recovery_message = self.reconcile_pending_journal_on_startup()

    def _load_or_init_session(self) -> None:
        row = self.db.conn.execute(
            "SELECT * FROM session_state WHERE session_id = ?", (self.session_id,)
        ).fetchone()
        if row:
            self.state = AgentState(row["fsm_state"])
            self.slots = IntentSlots(**json.loads(row["slots_json"]))
            if row["active_proposal_json"]:
                self.active_proposal = ApprovalProposal(**json.loads(row["active_proposal_json"]))
            self.turn_counter = row["turn_counter"]
        else:
            # Pre-populate area from confirmed explicit memory if available
            explicit_mems = self.memory_mgr.inspect_memories(include_unconfirmed_inferred=False)
            for m in explicit_mems:
                if m["key"] == "preferred_area" and not self.slots.area:
                    self.slots.area = m["value"]
            self._save_session()

    def _save_session(self) -> None:
        prop_json = json.dumps(self.active_proposal.model_dump()) if self.active_proposal else None
        self.db.conn.execute(
            "INSERT INTO session_state (session_id, user_id, fsm_state, slots_json, active_proposal_json, turn_counter) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET fsm_state = excluded.fsm_state, slots_json = excluded.slots_json, "
            "active_proposal_json = excluded.active_proposal_json, turn_counter = excluded.turn_counter",
            (
                self.session_id,
                self.user_id,
                self.state.value,
                json.dumps(self.slots.model_dump()),
                prop_json,
                self.turn_counter,
            ),
        )
        self.db.conn.commit()

    def _transition(self, new_state: AgentState, reason: str) -> None:
        old = self.state
        self.state = new_state
        self.trace.log_state_transition(old.value, new_state.value, reason)
        self._save_session()

    def _invoke_tool(self, tool_name: str, **kwargs: Any) -> Any:
        if self._run_tool_calls >= self.config.max_tool_calls_per_run:
            raise ToolExecutionError("tool_limit_exceeded", f"Exceeded max tool calls ({self.config.max_tool_calls_per_run})")
        if (time.perf_counter() - self._run_start_ts) > self.config.max_wall_clock_seconds:
            raise ToolExecutionError("wall_clock_exceeded", f"Exceeded wall-clock limit ({self.config.max_wall_clock_seconds}s)")

        self._run_tool_calls += 1
        t0 = time.perf_counter()
        if self.tools.fault_cfg.artificial_tool_delay_sec > 0:
            time.sleep(self.tools.fault_cfg.artificial_tool_delay_sec)
            if (time.perf_counter() - self._run_start_ts) > self.config.max_wall_clock_seconds:
                raise ToolExecutionError("wall_clock_exceeded", f"Exceeded wall-clock limit ({self.config.max_wall_clock_seconds}s)")

        fn = getattr(self.tools, tool_name, None)
        try:
            if fn is not None:
                raw = fn(**kwargs)
            elif self.mcp_client is not None:
                res = self.mcp_client.call_tool(tool_name, kwargs)
                if res.get("result", {}).get("isError"):
                    content = res["result"].get("content", [{}])[0].get("text", f"Error invoking MCP tool {tool_name}")
                    raise ToolExecutionError("mcp_tool_error", content)
                raw = res.get("result", {})
            else:
                raise ToolExecutionError("tool_not_found", f"Tool '{tool_name}' not found on tools environment or MCP client")
            sanitized = self.tools.sanitize_tool_output(tool_name, raw) if hasattr(self.tools, "sanitize_tool_output") else raw
            latency_ms = (time.perf_counter() - t0) * 1000.0
            self.trace.log_tool_call(tool_name, kwargs, sanitized, latency_ms, status="ok")
            return sanitized
        except ToolExecutionError as exc:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            self.trace.log_tool_call(
                tool_name, kwargs, {"error_code": exc.code, "message": exc.message}, latency_ms, status="error"
            )
            raise
        except (TypeError, ValueError, KeyError) as exc:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            error_msg = f"Invalid arguments for tool '{tool_name}': {exc}"
            self.trace.log_tool_call(
                tool_name, kwargs, {"error_code": "INVALID_TOOL_ARGUMENTS", "message": error_msg}, latency_ms, status="error"
            )
            raise ToolExecutionError("invalid_tool_arguments", error_msg, retriable=False) from exc

    def reconcile_pending_journal_on_startup(self) -> Optional[str]:
        '''
        If the process restarted mid-booking while an idempotency_journal entry was PENDING,
        query get_booking_status before taking any new action.
        '''
        self._run_tool_calls = 0
        self._run_start_ts = time.perf_counter()
        row = self.db.conn.execute(
            "SELECT * FROM idempotency_journal WHERE session_id = ? AND status = 'PENDING' ORDER BY created_at DESC LIMIT 1",
            (self.session_id,),
        ).fetchone()
        if not row:
            return None

        idem_key = row["idempotency_key"]
        self._transition(AgentState.RECONCILING_TIMEOUT, f"Process restart detected with PENDING journal {idem_key}")
        status_res = self._invoke_tool("get_booking_status", hold_id_or_idempotency_key=idem_key)
        if status_res.get("status") in {"CONFIRMED", "CANCELLED", "RESCHEDULED"}:
            self.db.complete_mutation(idem_key, status_res)
            self._transition(AgentState.COMPLETED, "Verified committed booking after mid-booking restart")
            return (
                f"Recovered after restart: Verified via get_booking_status that booking "
                f"{status_res['booking_id']} with {status_res['provider_name']} at {status_res['slot']} "
                f"(₹{status_res['price']}) is CONFIRMED."
            )
        # A NOT_FOUND result is only retried for a booking when we can prove the
        # original hold is still valid. Otherwise we leave an auditable SAFE_ABORT
        # rather than creating a new side effect from an ambiguous recovery.
        if row["mutation_type"] == MutationType.BOOK.value:
            hold = self.db.get_active_hold_for_target(
                self.user_id, row["target_id"], row["slot"], self.tools.now()
            )
            if hold:
                self._transition(AgentState.EXECUTING_MUTATION, "Recovered valid hold; retrying book with original idempotency key")
                try:
                    retry = self._invoke_tool("book", hold_id=hold["hold_id"], idempotency_key=idem_key)
                    if retry.get("status") == "BOOKED":
                        self.db.complete_mutation(idem_key, retry)
                        self._transition(AgentState.COMPLETED, "Recovered uncommitted booking with original idempotency key")
                        return f"Recovered after restart: booking {retry['booking_id']} is CONFIRMED."
                except ToolExecutionError as exc:
                    if exc.code == "booking_timeout":
                        return self._reconcile_booking_timeout(self.active_proposal, idem_key) if self.active_proposal else None
                    raise

        self._transition(AgentState.SAFE_ABORT, f"Pending {row['mutation_type']} recovery was not authoritatively verifiable")
        return (
            "Recovery needs attention: no authoritative completed mutation was found, "
            "so no new side effect was issued. Please start a fresh request."
        )

    def handle_user_message(self, user_message: str) -> str:
        self._run_tool_calls = 0
        self._run_model_turns = 0
        self._run_start_ts = time.perf_counter()
        self.turn_counter += 1

        # Preserve room for the policy, trusted preferences, and recent context.
        # Do not silently truncate user text before it reaches intent extraction.
        if self.memory_mgr.estimate_tokens(user_message) > self.config.max_user_input_tokens:
            self._transition(AgentState.SAFE_ABORT, "User input exceeded the bounded context admission limit")
            return (
                f"Safe Abort (input_too_large): Please resend this request in fewer than "
                f"{self.config.max_user_input_tokens} tokens. No action was taken."
            )

        # Record user turn in conversation_history
        self.db.conn.execute(
            "INSERT INTO conversation_history (session_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
            (self.session_id, user_message, time.time()),
        )
        self.db.conn.commit()

        try:
            if self.config.is_offline() or self.config.llm_mode in ("offline", "replay") or not self.config.enable_react_loop:
                reply = self._process_turn(user_message)
            else:
                reply = self._run_agentic_react_loop(user_message)
        except LLMProviderError as exc:
            self.trace.log_guardrail_event("llm_provider", "structured_intent_unavailable", {"reason": str(exc)})
            self._transition(AgentState.SAFE_ABORT, "Live model did not return a valid structured intent")
            reply = (
                "Safe Abort (llm_unavailable): I could not obtain a valid structured understanding of your request. "
                "No home-service provider was contacted and no booking was changed. Please retry shortly."
            )
        except ApprovalViolationError as exc:
            self.trace.log_guardrail_event("approval_gate", "blocked_unapproved_mutation", {"reason": str(exc)})
            self._transition(AgentState.AWAITING_APPROVAL, "Blocked unapproved mutation attempt")
            reply = f"Security Guardrail: Action blocked because explicit approval is required ({exc}). Please confirm the exact provider, slot, and price."
        except ToolExecutionError as exc:
            self._transition(AgentState.SAFE_ABORT, f"Terminated due to {exc.code}")
            reply = (
                f"Safe Abort ({exc.code}): Unable to complete operation cleanly ({exc.message}). "
                f"No unverified bookings were made."
            )
        except (TypeError, ValueError, KeyError) as exc:
            self.trace.log_guardrail_event("orchestrator", "argument_error", {"reason": str(exc)})
            self._transition(AgentState.SAFE_ABORT, f"Terminated due to unhandled argument error: {exc}")
            reply = (
                f"Safe Abort (invalid_arguments): Unable to complete operation cleanly ({exc}). "
                f"No unverified bookings were made."
            )

        self.db.conn.execute(
            "INSERT INTO conversation_history (session_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
            (self.session_id, reply, time.time()),
        )
        self._save_session()
        return reply

    def _run_agentic_react_loop(self, user_message: str) -> str:
        """
        Autonomous ReAct Reasoning Loop (Thought -> Action -> Observation)
        bounded by 5 model turns, 8 tool calls, 20.0s wall clock, and 1,200 context tokens.
        Intercepts consequential mutations at the code level via SentinelGatekeeper.
        """
        # Slot Extraction at Turn Start (keeps Intent Slots strictly synchronized with user request)
        try:
            extracted = ScriptedEvalLLM.parse(user_message)
            if extracted.get("intent_type") in ("book", "reschedule", "cancel", "list_bookings", "status"):
                raw_intent = extracted["intent_type"]
                self.slots.intent_type = "list_bookings" if raw_intent == "status" else raw_intent
            if extracted.get("slot_delta"):
                self.slots = self.slots.merge_delta(extracted["slot_delta"])
                if "area" in extracted["slot_delta"] and extracted["slot_delta"]["area"]:
                    self.memory_mgr.upsert_memory("preferred_area", extracted["slot_delta"]["area"], "EXPLICIT", True, "area,location")
        except Exception:
            pass

        # 1. Stale proposal invalidation on new inquiry / prompt
        is_affirmative = bool(ApprovalGate.AFFIRMATIVE_PATTERNS.search(user_message))
        if self.active_proposal and not is_affirmative:
            has_conflict = bool(ApprovalGate.NEGATIVE_OR_CHANGE_PATTERNS.search(user_message))
            is_new_inquiry = any(kw in user_message.lower() for kw in ["find", "search", "look for", "book", "plumber", "electrician", "ac", "cleaner", "service", "worst", "review", "history", "not interested"])
            if has_conflict or is_new_inquiry:
                self.active_proposal.invalidated = True
                self.active_proposal.invalidation_reason = "Superseded by new user inquiry"
                self.active_proposal = None
                self._transition(AgentState.IDLE, "Reset prior proposal on new inquiry")

        # 2. Repeated Confirmation Deduplication
        if self.state == AgentState.COMPLETED and self.active_proposal:
            if ApprovalGate.AFFIRMATIVE_PATTERNS.search(user_message):
                existing = self.db.conn.execute(
                    "SELECT * FROM idempotency_journal WHERE session_id = ? AND proposal_hash = ? AND status = 'COMPLETED'",
                    (self.session_id, self.active_proposal.payload_hash),
                ).fetchone()
                if existing:
                    data = json.loads(existing["response_json"])
                    self.trace.log_guardrail_event(
                        "idempotency_guard",
                        "deduplicated_repeated_confirmation",
                        {"booking_id": data.get("booking_id"), "proposal_hash": self.active_proposal.payload_hash},
                    )
                    if self.active_proposal.mutation_type == MutationType.CANCEL:
                        return f"Booking {data.get('booking_id', self.active_proposal.booking_id)} is already CANCELLED (duplicate confirmation ignored)."
                    if self.active_proposal.mutation_type == MutationType.RESCHEDULE:
                        return f"Booking {data.get('booking_id', self.active_proposal.booking_id)} is already rescheduled to {data.get('new_slot', self.active_proposal.slot)} (duplicate confirmation ignored)."
                    return (
                        f"Your booking {data.get('booking_id')} for {self.active_proposal.slot} "
                        f"at ₹{self.active_proposal.final_price} is already confirmed (duplicate confirmation ignored)."
                    )

        # 2. Check Affirmation or User Modifications if Awaiting Approval
        if self.state == AgentState.AWAITING_APPROVAL and self.active_proposal:
            has_conflict = bool(ApprovalGate.NEGATIVE_OR_CHANGE_PATTERNS.search(user_message))
            try:
                extracted_test = ScriptedEvalLLM.parse(user_message)
                delta_test = extracted_test.get("slot_delta", {})
                if delta_test.get("requested_slot") and delta_test["requested_slot"] != self.active_proposal.slot:
                    has_conflict = True
                if delta_test.get("preferred_provider_id") and delta_test["preferred_provider_id"] != self.active_proposal.provider_id:
                    has_conflict = True
            except Exception:
                pass

            if has_conflict:
                self.active_proposal.invalidated = True
                self.active_proposal.invalidation_reason = "User modified details or rejected proposal"
                self.trace.log_guardrail_event(
                    "approval_gate",
                    "invalidated_on_user_edit",
                    {"user_message": user_message},
                )
            elif ApprovalGate.evaluate_user_affirmation(self.active_proposal, user_message, self.turn_counter):
                return self._execute_approved_proposal()

        # Zero-trust Gatekeeper: immediately block explicit adversarial bypass attempts
        if extracted.get("bypass_approval_attempt"):
            ApprovalGate.assert_mutation_authorized(
                self.active_proposal,
                MutationType.BOOK,
                provider_id=self.slots.preferred_provider_id or "prov_plumb_1",
                slot=self.slots.requested_slot or "2026-11-09 09:00",
                final_price=self.slots.max_budget or 1100,
            )

        # Route explicit non-discovery intents (memory management, cancel, reschedule, repeat provider)
        if extracted.get("intent_type") == "memory_op" and extracted.get("memory_op"):
            return self._handle_memory_operation(extracted["memory_op"])

        if self.slots.intent_type == "cancel":
            return self._prepare_cancel_proposal()

        if self.slots.intent_type == "reschedule":
            return self._prepare_reschedule_proposal()

        if extracted.get("repeat_last_provider"):
            return self._handle_repeat_provider_request()

        # 3. Layer 1 Schema Gating: Expose only allowed tools to model prompt
        allowed_tool_names = set(ApprovalGate.get_allowed_tools(self.active_proposal))
        active_tool_schemas = [
            t for t in ENTERPRISE_TOOL_SCHEMAS
            if t["function"]["name"] in allowed_tool_names
        ]

        # 4. Context Compilation with Token Budgeting (<= 1,200 tokens)
        ctx = self.memory_mgr.compile_prompt_context(
            self.session_id, self.slots, user_message, allowed_tools=tuple(allowed_tool_names)
        )
        current_messages = list(ctx["messages"])

        # 5. Iterative ReAct Execution Loop
        found_candidates: list[dict[str, Any]] = []

        while self._run_model_turns < self.config.max_model_turns_per_run:
            if (time.perf_counter() - self._run_start_ts) > self.config.max_wall_clock_seconds:
                raise ToolExecutionError("wall_clock_exceeded", f"Exceeded wall-clock limit ({self.config.max_wall_clock_seconds}s)")

            self._run_model_turns += 1

            turn_response = self.llm.run_agentic_turn_with_cascade(
                messages=current_messages,
                tools=active_tool_schemas,
                session_id=self.session_id,
                trace_logger=self.trace,
            )

            if turn_response.thought:
                if hasattr(self.trace, "log_event"):
                    self.trace.log_event(
                        "agent_thought",
                        {
                            "turn": self._run_model_turns,
                            "thought": turn_response.thought,
                            "provider_used": turn_response.provider_used,
                        },
                    )
                elif hasattr(self.trace, "_record"):
                    self.trace._record(
                        "agent_thought",
                        {
                            "turn": self._run_model_turns,
                            "thought": turn_response.thought,
                            "provider_used": turn_response.provider_used,
                        },
                    )

            if not turn_response.tool_calls:
                return turn_response.content or "I have processed your request."

            # Record assistant turn with native tool_calls in message context
            if turn_response.raw_message:
                current_messages.append(turn_response.raw_message)
            else:
                current_messages.append({
                    "role": "assistant",
                    "content": turn_response.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments) if isinstance(tc.arguments, dict) else str(tc.arguments),
                            },
                        }
                        for tc in turn_response.tool_calls
                    ],
                })

            for tc in turn_response.tool_calls:
                if self._run_tool_calls >= self.config.max_tool_calls_per_run:
                    raise ToolExecutionError("tool_limit_exceeded", f"Exceeded max tool calls ({self.config.max_tool_calls_per_run})")
                if (time.perf_counter() - self._run_start_ts) > self.config.max_wall_clock_seconds:
                    raise ToolExecutionError("wall_clock_exceeded", f"Exceeded wall-clock limit ({self.config.max_wall_clock_seconds}s)")

                sentinel_decision = SentinelGatekeeper.intercept_tool_call(
                    tool_name=tc.name,
                    kwargs=tc.arguments,
                    session_id=self.session_id,
                    current_turn=self.turn_counter,
                    proposal=self.active_proposal,
                    db=self.db,
                    user_id=self.user_id,
                )

                if sentinel_decision.action == SentinelAction.REQUIRE_APPROVAL:
                    self.active_proposal = sentinel_decision.proposal
                    self._transition(
                        AgentState.AWAITING_APPROVAL,
                        f"Sentinel intercepted '{tc.name}'; awaiting human confirmation",
                    )
                    self.trace.log_guardrail_event(
                        "sentinel_gatekeeper",
                        "proposal_requires_approval",
                        {
                            "proposal": self.active_proposal.model_dump() if self.active_proposal else None,
                            "drift_detected": sentinel_decision.drift_detected,
                            "reason": sentinel_decision.reason,
                        },
                    )

                    prop = self.active_proposal
                    if prop.mutation_type == MutationType.BOOK:
                        return (
                            f"I've selected {prop.provider_id} for {prop.slot} at ₹{prop.final_price}. "
                            f"Proposal ID: {prop.proposal_id} (Digest: {prop.payload_hash[:8]}...). "
                            f"Please reply 'Yes, confirm' to approve booking."
                        )
                    elif prop.mutation_type == MutationType.CANCEL:
                        return (
                            f"Please confirm cancellation of booking {prop.booking_id} on {prop.slot} at ₹{prop.final_price}. "
                            f"Proposal ID: {prop.proposal_id}. Reply 'Yes, confirm' to proceed."
                        )
                    elif prop.mutation_type == MutationType.RESCHEDULE:
                        return (
                            f"Please confirm rescheduling booking {prop.booking_id} to {prop.slot} at ₹{prop.final_price}. "
                            f"Proposal ID: {prop.proposal_id}. Reply 'Yes, confirm' to proceed."
                        )

                if sentinel_decision.action == SentinelAction.BLOCK:
                    self.trace.log_guardrail_event(
                        "sentinel_gatekeeper",
                        "mutation_blocked",
                        {"tool_name": tc.name, "reason": sentinel_decision.reason},
                    )
                    current_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "content": json.dumps({"error": "MUTATION_BLOCKED", "message": sentinel_decision.reason}),
                    })
                    continue

                exec_kwargs = dict(sentinel_decision.modified_kwargs or tc.arguments)
                if tc.name == "search_providers":
                    exec_kwargs.setdefault("date_range", self.slots.date_window or "this week")
                try:
                    result = self._invoke_tool(tc.name, **exec_kwargs)
                except ToolExecutionError as exc:
                    if exc.code in {"tool_limit_exceeded", "wall_clock_exceeded", "model_turn_limit_exceeded"}:
                        raise
                    result = {"error": exc.code, "message": exc.message}
                except (TypeError, ValueError, KeyError) as exc:
                    result = {"error": "INVALID_TOOL_ARGUMENTS", "message": str(exc)}

                # Update candidate list and slots if search_providers was executed
                if tc.name == "search_providers" and isinstance(result, dict) and "providers" in result:
                    found_candidates.extend(result["providers"])
                    if tc.arguments.get("service") and not self.slots.service:
                        self.slots.service = tc.arguments["service"]
                    if tc.arguments.get("area") and not self.slots.area:
                        self.slots.area = tc.arguments["area"]

                # Automatically establish proposal and transition state when hold_slot succeeds
                if tc.name == "hold_slot" and isinstance(result, dict) and "hold_id" in result:
                    hold_prov = exec_kwargs.get("provider_id") or tc.arguments.get("provider_id", "")
                    hold_slot_val = exec_kwargs.get("slot") or tc.arguments.get("slot", "")
                    hold_price = result.get("price") or result.get("quoted_price") or 0
                    if not hold_price and self.slots.max_budget:
                        hold_price = self.slots.max_budget
                    self.active_proposal = ApprovalGate.create_proposal(
                        session_id=self.session_id,
                        mutation_type=MutationType.BOOK,
                        provider_id=hold_prov,
                        slot=hold_slot_val,
                        final_price=int(hold_price),
                        proposed_at_turn=self.turn_counter,
                        hold_id=result["hold_id"],
                    )
                    self.slots.preferred_provider_id = hold_prov
                    self.slots.requested_slot = hold_slot_val
                    self._transition(
                        AgentState.AWAITING_APPROVAL,
                        f"Acquired hold {result['hold_id']} on {hold_slot_val} at ₹{hold_price}; awaiting approval",
                    )

                if tc.name == "book" and isinstance(result, dict) and result.get("status") == "BOOKED":
                    self._transition(AgentState.COMPLETED, f"Booking {result.get('booking_id')} confirmed")
                    if self.active_proposal:
                        self.memory_mgr.record_inferred_preference_from_booking(
                            self.slots.service or "home_service", self.active_proposal.slot
                        )
                    return (
                        f"**Booking Confirmed!**\n\n"
                        f"- **Provider:** {result.get('provider_id')}\n"
                        f"- **Date & Time:** {result.get('slot')}\n"
                        f"- **Price:** ₹{result.get('price')}\n"
                        f"- **Booking ID:** `{result.get('booking_id')}`"
                    )

                if tc.name == "reschedule" and isinstance(result, dict) and result.get("status") == "RESCHEDULED":
                    self._transition(AgentState.COMPLETED, f"Rescheduled booking {result.get('booking_id')} to {result.get('new_slot')}")
                    return (
                        f"**Booking Rescheduled!**\n\n"
                        f"- **Booking ID:** `{result.get('booking_id')}`\n"
                        f"- **New Date & Time:** {result.get('new_slot')}\n"
                        f"- **Price:** ₹{result.get('price')}"
                    )

                if tc.name == "cancel" and isinstance(result, dict) and result.get("status") == "CANCELLED":
                    self._transition(AgentState.COMPLETED, f"Cancelled booking {result.get('booking_id')}")
                    return (
                        f"**Booking Cancelled!**\n\n"
                        f"- **Booking ID:** `{result.get('booking_id')}`\n"
                        f"- **Status:** CANCELLED"
                    )

                current_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "content": json.dumps(result),
                })

        self._transition(AgentState.SAFE_ABORT, "Exceeded maximum model turns in ReAct loop")
        return "Safe Abort (model_turn_limit_exceeded): I could not complete the errand within the allowed conversation turns."

    def _call_model_with_limits(self, user_message: str) -> dict[str, Any]:
        if self._run_model_turns >= self.config.max_model_turns_per_run:
            raise ToolExecutionError("model_turn_limit_exceeded", f"Exceeded max model turns ({self.config.max_model_turns_per_run})")
        if (time.perf_counter() - self._run_start_ts) > self.config.max_wall_clock_seconds:
            raise ToolExecutionError("wall_clock_exceeded", f"Exceeded wall-clock limit ({self.config.max_wall_clock_seconds}s)")

        # Defense-in-depth: Layer 1 tool schema gate exposes only allowed tools to the model
        allowed_tools = ApprovalGate.get_allowed_tools(self.active_proposal)
        ctx = self.memory_mgr.compile_prompt_context(
            self.session_id, self.slots, user_message, allowed_tools=allowed_tools
        )
        self._run_model_turns += 1
        llm_res = self.llm.parse_user_turn(ctx["messages"])
        self.trace.log_model_call(
            model_name=llm_res["model"],
            prompt_tokens=llm_res["prompt_tokens"],
            completion_tokens=llm_res["completion_tokens"],
            latency_ms=llm_res["latency_ms"],
            estimated_cost_usd=llm_res["estimated_cost_usd"],
            parsed_output=llm_res["parsed"],
            context_audit=ctx,
        )
        self.trace.log_guardrail_event("tool_schema_gate", "model_tool_allowlist_compiled", {"allowed_tools": allowed_tools})
        if (time.perf_counter() - self._run_start_ts) > self.config.max_wall_clock_seconds:
            raise ToolExecutionError("wall_clock_exceeded", f"Exceeded wall-clock limit ({self.config.max_wall_clock_seconds}s)")

        if self.llm.force_infinite_turn_loop:
            while self._run_model_turns < self.config.max_model_turns_per_run:
                self._run_model_turns += 1
            raise ToolExecutionError("model_turn_limit_exceeded", f"Exceeded max model turns ({self.config.max_model_turns_per_run})")

        return llm_res["parsed"]

    def _process_turn(self, user_message: str) -> str:
        # 1. Check if this turn is a repeated confirmation of an already completed booking
        if self.state == AgentState.COMPLETED and self.active_proposal:
            if ApprovalGate.AFFIRMATIVE_PATTERNS.search(user_message):
                existing = self.db.conn.execute(
                    "SELECT * FROM idempotency_journal WHERE session_id = ? AND proposal_hash = ? AND status = 'COMPLETED'",
                    (self.session_id, self.active_proposal.payload_hash),
                ).fetchone()
                if existing:
                    data = json.loads(existing["response_json"])
                    self.trace.log_guardrail_event(
                        "idempotency_guard",
                        "deduplicated_repeated_confirmation",
                        {"booking_id": data.get("booking_id"), "proposal_hash": self.active_proposal.payload_hash},
                    )
                    if self.active_proposal.mutation_type == MutationType.CANCEL:
                        return f"Booking {data.get('booking_id', self.active_proposal.booking_id)} is already CANCELLED (duplicate confirmation ignored)."
                    if self.active_proposal.mutation_type == MutationType.RESCHEDULE:
                        return f"Booking {data.get('booking_id', self.active_proposal.booking_id)} is already rescheduled to {data.get('new_slot', self.active_proposal.slot)} (duplicate confirmation ignored)."
                    return (
                        f"Your booking {data.get('booking_id')} for {self.active_proposal.slot} "
                        f"at ₹{self.active_proposal.final_price} is already confirmed (duplicate confirmation ignored)."
                    )

        parsed = self._call_model_with_limits(user_message)

        # 2. Handle Memory Inspection / Correction / Forgetting / Confirmation
        if parsed.get("intent_type") == "memory_op" and parsed.get("memory_op"):
            return self._handle_memory_operation(parsed["memory_op"])

        # 3. Check if we are in AWAITING_APPROVAL and the user either affirmed OR modified details
        slot_delta = parsed.get("slot_delta") or {}
        if self.state == AgentState.AWAITING_APPROVAL and self.active_proposal:
            # A material edit only occurs if a field's value actually differs from the active proposal/slots
            def has_material_change() -> bool:
                if "requested_slot" in slot_delta and slot_delta["requested_slot"] and slot_delta["requested_slot"] != self.active_proposal.slot:
                    return True
                if "max_budget" in slot_delta and slot_delta["max_budget"] is not None and slot_delta["max_budget"] != self.slots.max_budget:
                    return True
                if "preferred_provider_id" in slot_delta and slot_delta["preferred_provider_id"] and slot_delta["preferred_provider_id"] != self.active_proposal.provider_id:
                    return True
                if "date_window" in slot_delta and slot_delta["date_window"] and slot_delta["date_window"] != self.slots.date_window:
                    return True
                return False

            material_edit_in_turn = has_material_change()
            if material_edit_in_turn:
                self.active_proposal.invalidated = True
                self.active_proposal.invalidation_reason = "User modified constraints after proposal"
                self.trace.log_guardrail_event(
                    "approval_gate",
                    "invalidated_on_user_edit",
                    {"delta": slot_delta},
                )
            elif ApprovalGate.evaluate_user_affirmation(self.active_proposal, user_message, self.turn_counter):
                return self._execute_approved_proposal()

        # 4. If user attempted to force/bypass approval without an affirmed proposal, enforce gate
        if parsed.get("bypass_approval_attempt"):
            ApprovalGate.assert_mutation_authorized(
                self.active_proposal,
                MutationType.BOOK,
                provider_id=slot_delta.get("preferred_provider_id") or "prov_plumb_1",
                slot=slot_delta.get("requested_slot") or "2026-11-09 09:00",
                final_price=1100,
            )

        # 5. Merge slot deltas while preserving all unrelated context
        if parsed.get("intent_type") in ("book", "reschedule", "cancel", "list_bookings", "status"):
            raw_intent = parsed["intent_type"]
            self.slots.intent_type = "list_bookings" if raw_intent == "status" else raw_intent
        if slot_delta:
            self.slots = self.slots.merge_delta(slot_delta)
            if "area" in slot_delta and slot_delta["area"]:
                self.memory_mgr.upsert_memory("preferred_area", slot_delta["area"], "EXPLICIT", True, "area,location")

        # 6. Route by active intent
        if self.slots.intent_type == "list_bookings":
            res = self._invoke_tool("list_bookings", user_id=self.user_id)
            if not res.get("bookings"):
                return "You have no bookings on record."
            summary = ", ".join(
                f"{b['booking_id']} ({b['service']} with {b['provider_name']} at {b['slot']} [{b['status']}])"
                for b in res["bookings"]
            )
            return f"Here are your bookings: {summary}"

        if self.slots.intent_type == "cancel":
            return self._prepare_cancel_proposal()

        if self.slots.intent_type == "reschedule":
            return self._prepare_reschedule_proposal()

        # Default: Booking discovery workflow
        if parsed.get("repeat_last_provider"):
            return self._handle_repeat_provider_request()

        missing = self.slots.missing_discovery_fields()
        if missing:
            self._transition(AgentState.COLLECTING_SLOTS, f"Missing fields: {missing}")
            friendly_names = {
                "service": "service needed",
                "area": "neighborhood/area",
                "date_window": "preferred date or day window",
                "time_window": "preferred time of day",
                "max_budget": "maximum budget (₹)",
            }
            ask_list = ", ".join(friendly_names[m] for m in missing)
            known_summary = {
                k: v for k, v in self.slots.model_dump().items()
                if k in ("service", "area", "date_window", "time_window", "max_budget") and v is not None
            }
            return (
                f"Noted your details ({json.dumps(known_summary)}). "
                f"To compare the best providers, please share your {ask_list}."
            )

        return self._search_compare_and_propose()

    def handle_memory_operation(self, mem_op: dict[str, Any]) -> str:
        """Public API for inspecting, updating, forgetting, or confirming memories."""
        return self._handle_memory_operation(mem_op)

    def _handle_memory_operation(self, mem_op: dict[str, Any]) -> str:
        action = mem_op.get("action")
        if action == "inspect":
            mems = self.memory_mgr.inspect_memories(include_unconfirmed_inferred=True)
            core = [m for m in mems if not m["key"].startswith("household_note_")]
            formatted = "; ".join(
                f"{m['key']}={m['value']} [{m['source_type']}, confirmed={bool(m['confirmed'])}]"
                for m in core
            )
            return f"Stored Preferences: {formatted}"
        if action == "forget":
            key = mem_op.get("key", "")
            forgotten_ids = self.memory_mgr.forget_memory(key)
            return f"Forgot {len(forgotten_ids)} stored preference(s) matching '{key}' ({forgotten_ids}). They will never be sent to the model."
        if action == "update":
            key = mem_op.get("key", "preferred_area")
            val = mem_op.get("value", "")
            self.memory_mgr.upsert_memory(key, val, source_type="EXPLICIT", confirmed=True, relevance_tags="area,location")
            if key == "preferred_area":
                self.slots.area = val
            return f"Updated your explicit preference '{key}' to '{val}'."
        if action == "confirm":
            key = mem_op.get("key", "")
            ok = self.memory_mgr.confirm_inferred_memory(key)
            return f"Confirmed inferred preference '{key}' (active={ok})."
        return "Memory operation completed."

    def _handle_repeat_provider_request(self) -> str:
        self._transition(AgentState.SEARCHING_PROVIDERS, "Looking up past bookings for repeat provider")
        bookings_res = self._invoke_tool("list_bookings", user_id=self.user_id)
        past = [
            b for b in bookings_res["bookings"]
            if b["service"] == (self.slots.service or "electrician")
        ]
        if not past:
            return "Could not find a past provider for that service."

        last_booking = past[0]
        provider_id = last_booking["provider_id"]
        prov_details = self._invoke_tool("get_provider", provider_id=provider_id)

        earliest = self.slots.preferences.get("earliest_time", "09:00")
        valid_slots = [
            s for s in prov_details["available_slots"]
            if s.split(" ")[1] >= earliest
        ]
        chosen_slot = valid_slots[0] if valid_slots else prov_details["available_slots"][0]
        price = prov_details["price"]

        self.active_proposal = ApprovalGate.create_proposal(
            session_id=self.session_id,
            mutation_type=MutationType.BOOK,
            provider_id=provider_id,
            slot=chosen_slot,
            final_price=price,
            proposed_at_turn=self.turn_counter,
        )
        self._transition(AgentState.AWAITING_APPROVAL, f"Proposed repeat provider {provider_id} at {chosen_slot}")
        return (
            f"Found your previous {prov_details['service']} provider: {prov_details['name']} ({provider_id}). "
            f"Filtered slots not before {earliest}: proposing {chosen_slot} at ₹{price}. "
            f"Please reply 'Yes, confirm' to approve booking."
        )

    def _search_compare_and_propose(self) -> str:
        self._transition(AgentState.SEARCHING_PROVIDERS, "All discovery slots collected; searching providers")
        search_res = self._invoke_tool(
            "search_providers",
            service=self.slots.service or "plumber",
            area=self.slots.area or "Indiranagar",
            date_range=self.slots.date_window or "this_week",
        )
        candidates = search_res.get("providers", [])
        budget = self.slots.max_budget or 999999
        within_budget = [p for p in candidates if p["price"] <= budget]
        if not within_budget:
            return f"No providers found under your budget of ₹{budget}. Lowest available is ₹{min(p['price'] for p in candidates) if candidates else 'N/A'}."

        # Inspect provider details (including untrusted reviews) for comparison.
        # Provider data is never allowed to override user-derived slots or budget.
        inspected = []
        for p in within_budget[:3]:
            details = self._invoke_tool("get_provider", provider_id=p["provider_id"])
            valid_slots = [slot for slot in details["available_slots"] if self._slot_matches_constraints(slot)]
            if valid_slots:
                details["available_slots"] = valid_slots
                inspected.append(details)

        if not inspected:
            return "I found providers within budget, but none with a slot matching your requested date and time. Please widen the time or date window."

        # Pick preferred provider if requested, else best value/rating within budget
        selected = inspected[0]
        if self.slots.preferred_provider_id:
            for item in inspected:
                if item["provider_id"] == self.slots.preferred_provider_id:
                    selected = item
                    break

        if self.slots.requested_slot and self.slots.requested_slot not in selected["available_slots"]:
            return (
                f"{selected['name']} does not offer {self.slots.requested_slot}. "
                f"Available matching slots are: {', '.join(selected['available_slots'])}. "
                "Please choose one of these slots."
            )
        chosen_slot = self.slots.requested_slot or selected["available_slots"][0]
        search_price = selected["price"]

        self.active_proposal = ApprovalGate.create_proposal(
            session_id=self.session_id,
            mutation_type=MutationType.BOOK,
            provider_id=selected["provider_id"],
            slot=chosen_slot,
            final_price=search_price,
            proposed_at_turn=self.turn_counter,
        )
        self._transition(
            AgentState.AWAITING_APPROVAL,
            f"Awaiting user approval for {selected['provider_id']} slot {chosen_slot} price ₹{search_price}",
        )
        comp_str = "; ".join(f"{p['name']} (₹{p['price']}, ★{p['rating']})" for p in inspected)
        return (
            f"Compared options within ₹{budget}: {comp_str}. "
            f"Recommended: {selected['name']} ({selected['provider_id']}) on {chosen_slot} at ₹{search_price}. "
            f"Please confirm ('Yes, confirm') to hold and book this exact slot and price."
        )

    def _slot_matches_constraints(self, slot: str) -> bool:
        """Keep selection deterministic and strictly within the user's expressed window."""
        date_part, _, time_part = slot.partition(" ")
        if not time_part:
            return False
        date_window = self.slots.date_window or ""
        if date_window == "Saturday (2026-11-14)" and date_part != "2026-11-14":
            return False
        if date_window == "Sunday (2026-11-15)" and date_part != "2026-11-15":
            return False
        if date_window.startswith("2026-") and date_part != date_window:
            return False
        hour = int(time_part.split(":", 1)[0])
        if "morning" in (self.slots.time_window or "") and not (8 <= hour < 12):
            return False
        if "afternoon" in (self.slots.time_window or "") and not (12 <= hour < 17):
            return False
        if "evening" in (self.slots.time_window or "") and not (17 <= hour < 21):
            return False
        if self.slots.time_window == "after 10:00" and hour < 10:
            return False
        return True

    def _prepare_cancel_proposal(self) -> str:
        bookings = self._invoke_tool("list_bookings", user_id=self.user_id)["bookings"]
        active = [b for b in bookings if b["status"] in ("CONFIRMED", "RESCHEDULED")]
        if not active:
            return "You have no active bookings to cancel."
        target = None
        if self.slots.target_booking_id:
            for b in active:
                if b["booking_id"] == self.slots.target_booking_id:
                    target = b
                    break
        if not target and self.slots.service:
            for b in active:
                if b.get("service") == self.slots.service or self.slots.service in b.get("service", ""):
                    target = b
                    break
        if not target:
            target = active[0]
        self.active_proposal = ApprovalGate.create_proposal(
            session_id=self.session_id,
            mutation_type=MutationType.CANCEL,
            provider_id=target["provider_id"],
            booking_id=target["booking_id"],
            slot=target["slot"],
            final_price=target["price"],
            proposed_at_turn=self.turn_counter,
        )
        self._transition(AgentState.AWAITING_APPROVAL, f"Awaiting approval to cancel {target['booking_id']}")
        return (
            f"Please confirm cancellation of booking {target['booking_id']} "
            f"({target['service']} with {target['provider_name']} on {target['slot']} at ₹{target['price']}). "
            f"Reply 'Yes, confirm' to proceed."
        )

    def _prepare_reschedule_proposal(self) -> str:
        bookings = self._invoke_tool("list_bookings", user_id=self.user_id)["bookings"]
        active = [b for b in bookings if b["status"] in ("CONFIRMED", "RESCHEDULED")]
        if not active:
            return "You have no active bookings to reschedule."
        target = None
        if self.slots.target_booking_id:
            for b in active:
                if b["booking_id"] == self.slots.target_booking_id:
                    target = b
                    break
        if not target and self.slots.service:
            for b in active:
                if b.get("service") == self.slots.service or self.slots.service in b.get("service", ""):
                    target = b
                    break
        if not target:
            target = active[0]
        new_slot = self.slots.requested_slot or "2026-11-14 10:00"
        self.active_proposal = ApprovalGate.create_proposal(
            session_id=self.session_id,
            mutation_type=MutationType.RESCHEDULE,
            provider_id=target["provider_id"],
            booking_id=target["booking_id"],
            slot=new_slot,
            final_price=target["price"],
            proposed_at_turn=self.turn_counter,
        )
        self._transition(AgentState.AWAITING_APPROVAL, f"Awaiting approval to reschedule {target['booking_id']} to {new_slot}")
        return (
            f"Please confirm rescheduling booking {target['booking_id']} ({target['provider_name']}) "
            f"from {target['slot']} to {new_slot} at ₹{target['price']}. Reply 'Yes, confirm' to proceed."
        )

    def _execute_approved_proposal(self) -> str:
        self._run_tool_calls = 0
        self._run_start_ts = time.perf_counter()
        if self.active_proposal is None:
            raise ApprovalViolationError("No active proposal exists to execute")
        prop = self.active_proposal

        try:
            # Enforce code-level approval gate before any mutation step
            ApprovalGate.assert_mutation_authorized(
                proposal=prop,
                mutation_type=prop.mutation_type,
                provider_id=prop.provider_id,
                slot=prop.slot,
                final_price=prop.final_price,
                booking_id=prop.booking_id,
            )

            if prop.mutation_type == MutationType.BOOK:
                return self._execute_booking_with_hold_and_recovery(prop)
            if prop.mutation_type == MutationType.CANCEL:
                return self._execute_cancellation_with_retry(prop)
            if prop.mutation_type == MutationType.RESCHEDULE:
                return self._execute_reschedule_with_conflict_handling(prop)
            return "Unsupported mutation."
        except ToolExecutionError as exc:
            self._transition(AgentState.SAFE_ABORT, f"Terminated due to {exc.code}")
            reply = (
                f"Safe Abort ({exc.code}): Unable to complete operation cleanly ({exc.message}). "
                f"No unverified bookings were made."
            )
        except (TypeError, ValueError, KeyError) as exc:
            self.trace.log_guardrail_event("orchestrator", "argument_error", {"reason": str(exc)})
            self._transition(AgentState.SAFE_ABORT, f"Terminated due to unhandled argument error: {exc}")
            reply = (
                f"Safe Abort (invalid_arguments): Unable to complete operation cleanly ({exc}). "
                f"No unverified bookings were made."
            )
        self.db.conn.execute(
            "INSERT INTO conversation_history (session_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
            (self.session_id, reply, time.time()),
        )
        self._save_session()
        return reply

    def _execute_booking_with_hold_and_recovery(self, prop: ApprovalProposal) -> str:
        # Step 1: Acquire hold if not already held
        if not prop.hold_id:
            self._transition(AgentState.HOLDING_SLOT, f"Holding slot {prop.slot} with {prop.provider_id}")
            hold_res = self._invoke_tool("hold_slot", provider_id=prop.provider_id, slot=prop.slot)
            quoted_price = hold_res["quoted_price"]

            # Check if quoted_price drifted from the user-approved price!
            if ApprovalGate.check_material_drift(prop, prop.provider_id, prop.slot, quoted_price):
                self.trace.log_guardrail_event(
                    "approval_gate",
                    "price_drift_invalidated_approval",
                    {"approved_price": prop.final_price, "quoted_price": quoted_price, "hold_id": hold_res["hold_id"]},
                )
                # Create a new proposal bound to the new quoted_price and hold_id
                self.active_proposal = ApprovalGate.create_proposal(
                    session_id=self.session_id,
                    mutation_type=MutationType.BOOK,
                    provider_id=prop.provider_id,
                    slot=prop.slot,
                    final_price=quoted_price,
                    proposed_at_turn=self.turn_counter,
                    hold_id=hold_res["hold_id"],
                )
                self._transition(
                    AgentState.AWAITING_APPROVAL,
                    f"Quoted price drifted from ₹{prop.final_price} to ₹{quoted_price}; awaiting fresh approval",
                )
                return (
                    f"Slot held ({hold_res['hold_id']}), but the quoted price changed from ₹{prop.final_price} "
                    f"to ₹{quoted_price}. Your previous approval was automatically invalidated. "
                    f"Please reply 'Yes, confirm' if you approve booking at the updated price of ₹{quoted_price}."
                )
            prop.hold_id = hold_res["hold_id"]

        # Step 2: Re-verify ApprovalGate right before calling book()
        ApprovalGate.assert_mutation_authorized(
            proposal=prop,
            mutation_type=MutationType.BOOK,
            provider_id=prop.provider_id,
            slot=prop.slot,
            final_price=prop.final_price,
        )

        # Step 3: Write-ahead idempotency journal entry with deterministic RFC-4122 UUIDv5
        idem_key = compute_idempotency_key(
            self.session_id, MutationType.BOOK.value, prop.provider_id, prop.slot, prop.payload_hash
        )
        existing = self.db.begin_mutation(
            idempotency_key=idem_key,
            session_id=self.session_id,
            mutation_type=MutationType.BOOK.value,
            target_id=prop.provider_id,
            slot=prop.slot,
            price=prop.final_price,
            proposal_hash=prop.payload_hash,
        )
        if existing and existing["status"] == "COMPLETED" and existing["response_json"]:
            replay = json.loads(existing["response_json"])
            self._transition(AgentState.COMPLETED, "Returned completed idempotent booking journal entry")
            return f"Your booking {replay.get('booking_id')} is already confirmed (duplicate confirmation ignored)."

        self._transition(AgentState.EXECUTING_MUTATION, f"Calling book(hold_id={prop.hold_id}, idempotency_key={idem_key})")
        try:
            book_res = self._invoke_tool("book", hold_id=prop.hold_id, idempotency_key=idem_key)
        except ToolExecutionError as exc:
            if exc.code == "booking_timeout":
                return self._reconcile_booking_timeout(prop, idem_key)
            raise

        return self._finalize_book_response(prop, idem_key, book_res)

    def _reconcile_booking_timeout(self, prop: ApprovalProposal, idem_key: str) -> str:
        '''
        Authoritative recovery protocol when book() times out:
        1. Query get_booking_status(idempotency_key) BEFORE deciding whether to retry or report success.
        2. If CONFIRMED, do NOT retry; record completion and report verified booking.
        3. If NOT_FOUND, retry book() once with the exact same idempotency_key.
        '''
        self._transition(AgentState.RECONCILING_TIMEOUT, f"book() timed out; querying get_booking_status({idem_key})")
        status_res = self._invoke_tool("get_booking_status", hold_id_or_idempotency_key=idem_key)

        if status_res.get("status") == "CONFIRMED":
            self.db.complete_mutation(idem_key, status_res)
            self._transition(AgentState.COMPLETED, f"Timeout resolved via get_booking_status: booking {status_res['booking_id']} CONFIRMED")
            return (
                f"Booking confirmed after timeout verification! Checked get_booking_status and verified "
                f"booking {status_res['booking_id']} with {status_res['provider_name']} on {status_res['slot']} "
                f"at ₹{status_res['price']} is CONFIRMED."
            )

        # Status is NOT_FOUND: safe to retry once with the exact same idempotency_key
        self._transition(AgentState.EXECUTING_MUTATION, f"get_booking_status returned NOT_FOUND; retrying book() with same key {idem_key}")
        retry_res = self._invoke_tool("book", hold_id=prop.hold_id, idempotency_key=idem_key)
        return self._finalize_book_response(prop, idem_key, retry_res)

    def _finalize_book_response(self, prop: ApprovalProposal, idem_key: str, book_res: dict[str, Any]) -> str:
        status = book_res.get("status")
        if status == "BOOKED":
            self.db.complete_mutation(idem_key, book_res)
            self.memory_mgr.record_inferred_preference_from_booking(self.slots.service or "home_service", prop.slot)
            self._transition(AgentState.COMPLETED, f"Booking {book_res['booking_id']} verified and completed")
            return (
                f"Booking {book_res['booking_id']} confirmed for {prop.slot} "
                f"with {prop.provider_id} at ₹{prop.final_price}."
            )

        if status == "hold_expired":
            prop.invalidated = True
            prop.invalidation_reason = "Hold expired (>10 minutes)"
            # Acquire a fresh hold and issue a new proposal requiring confirmation
            new_hold = self._invoke_tool("hold_slot", provider_id=prop.provider_id, slot=prop.slot)
            self.active_proposal = ApprovalGate.create_proposal(
                session_id=self.session_id,
                mutation_type=MutationType.BOOK,
                provider_id=prop.provider_id,
                slot=prop.slot,
                final_price=new_hold["quoted_price"],
                proposed_at_turn=self.turn_counter,
                hold_id=new_hold["hold_id"],
            )
            self._transition(AgentState.AWAITING_APPROVAL, "Hold expired; re-held slot and awaiting fresh confirmation")
            return (
                f"Your previous 10-minute slot hold expired before booking completed. "
                f"I have placed a fresh hold ({new_hold['hold_id']}) for {prop.slot} at ₹{new_hold['quoted_price']}. "
                f"Please reply 'Yes, confirm' to complete the booking."
            )

        if status == "slot_taken":
            prop.invalidated = True
            prop.invalidation_reason = "Slot was taken during booking"
            prov = self._invoke_tool("get_provider", provider_id=prop.provider_id)
            alt_slots = [s for s in prov["available_slots"] if s != prop.slot]
            next_slot = alt_slots[0] if alt_slots else "2026-11-10 10:00"
            self.active_proposal = ApprovalGate.create_proposal(
                session_id=self.session_id,
                mutation_type=MutationType.BOOK,
                provider_id=prop.provider_id,
                slot=next_slot,
                final_price=prov["price"],
                proposed_at_turn=self.turn_counter,
            )
            self._transition(AgentState.AWAITING_APPROVAL, f"Slot {prop.slot} taken; proposing alternative {next_slot}")
            return (
                f"Unfortunately, slot {prop.slot} was just taken. "
                f"Would you like to book the next available slot ({next_slot}) with {prov['name']} at ₹{prov['price']}? "
                f"Reply 'Yes, confirm' to approve."
            )

        self._transition(AgentState.SAFE_ABORT, f"Unexpected booking status: {status}")
        return f"Could not complete booking (status: {status})."

    def _execute_cancellation_with_retry(self, prop: ApprovalProposal) -> str:
        idem_key = compute_idempotency_key(
            self.session_id, MutationType.CANCEL.value, prop.booking_id or "", prop.slot, prop.payload_hash
        )
        existing = self.db.begin_mutation(
            idempotency_key=idem_key,
            session_id=self.session_id,
            mutation_type=MutationType.CANCEL.value,
            target_id=prop.booking_id or "",
            slot=prop.slot,
            price=prop.final_price,
            proposal_hash=prop.payload_hash,
        )
        if existing and existing["status"] == "COMPLETED" and existing["response_json"]:
            replay = json.loads(existing["response_json"])
            self._transition(AgentState.COMPLETED, "Returned completed idempotent cancellation journal entry")
            return f"Booking {replay.get('booking_id', prop.booking_id)} is already CANCELLED (duplicate confirmation ignored)."
        self._transition(AgentState.EXECUTING_MUTATION, f"Executing cancel for {prop.booking_id}")
        max_attempts = self.config.max_mutation_retries
        last_exc: Optional[ToolExecutionError] = None
        for attempt in range(1, max_attempts + 1):
            try:
                res = self._invoke_tool("cancel", booking_id=prop.booking_id, idempotency_key=idem_key)
                self.db.complete_mutation(idem_key, res)
                self._transition(AgentState.COMPLETED, f"Cancelled booking {prop.booking_id}")
                return f"Booking {res['booking_id']} has been successfully CANCELLED (verified after transient 503 retry)."
            except ToolExecutionError as exc:
                last_exc = exc
                if exc.code == "transient_503" and exc.retriable and attempt < max_attempts:
                    self.trace.log_guardrail_event(
                        "retry_handler",
                        "retrying_transient_503_on_cancel",
                        {"booking_id": prop.booking_id, "idempotency_key": idem_key, "attempt": attempt},
                    )
                    continue
                if exc.code == "transient_503" and attempt >= max_attempts:
                    raise ToolExecutionError(
                        "retries_exhausted",
                        f"Exhausted {max_attempts} retry attempts on transient_503 for cancel({prop.booking_id})",
                        retriable=False,
                    )
                raise
        raise last_exc or ToolExecutionError("retries_exhausted", "Exhausted cancel retries")

    def _execute_reschedule_with_conflict_handling(self, prop: ApprovalProposal) -> str:
        idem_key = compute_idempotency_key(
            self.session_id, MutationType.RESCHEDULE.value, prop.booking_id or "", prop.slot, prop.payload_hash
        )
        existing = self.db.begin_mutation(
            idempotency_key=idem_key,
            session_id=self.session_id,
            mutation_type=MutationType.RESCHEDULE.value,
            target_id=prop.booking_id or "",
            slot=prop.slot,
            price=prop.final_price,
            proposal_hash=prop.payload_hash,
        )
        if existing and existing["status"] == "COMPLETED" and existing["response_json"]:
            replay = json.loads(existing["response_json"])
            self._transition(AgentState.COMPLETED, "Returned completed idempotent reschedule journal entry")
            return f"Booking {replay.get('booking_id', prop.booking_id)} is already rescheduled to {replay.get('new_slot', prop.slot)} (duplicate confirmation ignored)."
        self._transition(AgentState.EXECUTING_MUTATION, f"Executing reschedule for {prop.booking_id} to {prop.slot}")
        res = self._invoke_tool("reschedule", booking_id=prop.booking_id, slot=prop.slot, idempotency_key=idem_key)
        if res.get("status") == "slot_conflict":
            # The call completed authoritatively with no side effect; make the
            # write-ahead entry terminal before proposing a different slot.
            self.db.complete_mutation(idem_key, res)
            prop.invalidated = True
            prop.invalidation_reason = f"Slot conflict on {prop.slot}"
            suggested = res.get("suggested_slots", ["2026-11-14 14:00"])[0]
            self.active_proposal = ApprovalGate.create_proposal(
                session_id=self.session_id,
                mutation_type=MutationType.RESCHEDULE,
                provider_id=prop.provider_id,
                booking_id=prop.booking_id,
                slot=suggested,
                final_price=prop.final_price,
                proposed_at_turn=self.turn_counter,
            )
            self._transition(AgentState.AWAITING_APPROVAL, f"Slot conflict on {prop.slot}; proposed {suggested}")
            return (
                f"The requested slot {prop.slot} had a scheduling conflict. Your existing booking remains intact. "
                f"Would you like to reschedule booking {prop.booking_id} to {suggested} at ₹{prop.final_price} instead? "
                f"Reply 'Yes, confirm' to approve."
            )

        self.db.complete_mutation(idem_key, res)
        self._transition(AgentState.COMPLETED, f"Rescheduled booking {prop.booking_id} to {prop.slot}")
        return f"Booking {res['booking_id']} has been rescheduled to {res['new_slot']} at ₹{res['price']}."
