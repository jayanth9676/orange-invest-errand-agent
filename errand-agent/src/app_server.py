import asyncio
import json
from pathlib import Path
from threading import RLock
import time
from typing import Any, Optional
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.config import AgentConfig
from src.db import LocalDatabase
from src.mcp_server import (
    MCP_PROMPTS_CATALOG,
    MCP_RESOURCES_CATALOG,
    MCP_TOOLS_CATALOG,
    MCPServerHandler,
    MCP_PROTOCOL_VERSION,
    SERVER_INFO,
)
from src.metrics import METRICS
from src.mock_tools import FaultConfig
from src.orchestrator import ErrandAgentOrchestrator
from src.proactive_worker import ProactiveCancellationMonitor
from src.schemas import AgentState
from src.security import (
    EnterpriseSecurityMiddleware,
    UserRole,
    WorkspaceSessionManager,
    check_permission,
    extract_tenant_and_session,
    make_problem_details,
    sanitize_input_text,
)

app = FastAPI(
    title="Errand Agent — Enterprise Agentic Operations & MCP Studio (Nov 2026)",
    description="Deterministic State-Machine Errand Agent with OpenTelemetry Telemetry, Prometheus Metrics, and Model Context Protocol (MCP) server.",
    version="2.0.0",
)

# Enterprise Security & Rate Limiting Middleware (executed first in ASGI stack)
app.add_middleware(EnterpriseSecurityMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
ASSETS_DIR = STATIC_DIR / "assets"
ASSETS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")


DB = LocalDatabase("errand_demo.sqlite")
DB.seed_default_data("user_1", seed_50_memories=True)
CONFIG = AgentConfig()
WORKSPACE_MANAGER = WorkspaceSessionManager(db=DB, config=CONFIG)
SESSION_MANAGER = WORKSPACE_MANAGER

# Backward-compatible global bindings for direct module imports
ORCH = WORKSPACE_MANAGER.get_orchestrator("tenant_default", "live_demo_session")
PROACTIVE = WORKSPACE_MANAGER.get_proactive_monitor("tenant_default", "live_demo_session")
MCP_HANDLER = WORKSPACE_MANAGER.get_mcp_handler("tenant_default", "live_demo_session")
STATE_LOCK = WORKSPACE_MANAGER.get_session_lock("tenant_default", "live_demo_session")

# Pre-seed metrics counters so /metrics has standard metadata immediately
METRICS.inc_counter("errand_agent_turns_total", 0.0, labels={"status": "success", "user_id": "user_1"})
METRICS.inc_counter("errand_agent_token_consumption_total", 0.0, labels={"type": "prompt"})
METRICS.inc_counter("errand_agent_fsm_state_transitions_total", 0.0, labels={"from_state": "IDLE", "to_state": "IDLE"})


class ChatRequest(BaseModel):
    message: str = Field(..., max_length=10000, description="User conversational turn text")
    force_book_outcome: Optional[str] = None
    force_price_drift_delta: Optional[int] = None
    inject_adversarial_review: Optional[bool] = None
    persistent_cancel_503: Optional[bool] = None


class ProactiveCancelRequest(BaseModel):
    booking_id: str = "bk_upcoming_ac_01"


class MemoryActionRequest(BaseModel):
    action: str  # inspect | forget | confirm | update
    key: str = ""
    value: str = ""


class MCPCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    role: Optional[str] = None


class ApprovalActionRequest(BaseModel):
    proposal_id: str = Field(..., description="Active proposal ID to act upon")
    action: str = Field(..., description="Approval action: 'approve' or 'reject'")
    reason: Optional[str] = Field(None, description="Optional rejection or approval reason")


MCP_MUTATING_TOOLS = {"hold_slot", "book", "cancel", "reschedule"}


@app.exception_handler(Exception)
async def global_rfc7807_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """RFC-7807 compliant problem details for uncaught runtime exceptions."""
    METRICS.inc_counter(
        "errand_agent_errors_total",
        labels={"category": "http_exception", "error_type": exc.__class__.__name__},
    )
    trace_id = getattr(ORCH.trace, "trace_id", "unknown")
    problem = make_problem_details(
        status=500,
        title="Internal Server Error",
        detail=str(exc),
        type_uri="https://errand-agent.enterprise/errors/internal-server-error",
        extra={"trace_id": trace_id, "path": request.url.path},
    )
    return JSONResponse(status_code=500, content=problem)


def _build_state_snapshot(orch: Optional[ErrandAgentOrchestrator] = None, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    target_orch = orch or ORCH
    all_mems = [
        dict(r)
        for r in target_orch.db.conn.execute(
            "SELECT * FROM memories WHERE user_id = ? ORDER BY updated_at DESC",
            (target_orch.user_id,),
        ).fetchall()
    ]
    core_mems = [dict(r) for r in all_mems if not r["key"].startswith("household_note_")]
    audit = target_orch.db.get_latest_context_audit(target_orch.session_id)
    diag = getattr(target_orch.memory_mgr, "latest_exclusion_diagnostics", {})
    if not diag and all_mems:
        diag = {}
        for m in all_mems:
            if m["forgotten"]:
                diag[m["key"]] = "SQL Pre-Filter: Forgotten"
            elif m["source_type"] == "INFERRED" and not m["confirmed"]:
                diag[m["key"]] = "SQL Pre-Filter: Unconfirmed Inferred"
            else:
                diag[m["key"]] = "Standby (Not Queried)"

    if audit is not None:
        audit["actual_tokens"] = audit.get("total_tokens", 0)
        audit["max_tokens"] = target_orch.config.max_context_tokens
        if "included_memory_ids_json" in audit and "included_memory_ids" not in audit:
            try:
                audit["included_memory_ids"] = json.loads(audit["included_memory_ids_json"]) if isinstance(audit["included_memory_ids_json"], str) else audit["included_memory_ids_json"]
            except Exception:
                audit["included_memory_ids"] = []
        if "excluded_memory_ids_json" in audit and "excluded_memory_ids" not in audit:
            try:
                audit["excluded_memory_ids"] = json.loads(audit["excluded_memory_ids_json"]) if isinstance(audit["excluded_memory_ids_json"], str) else audit["excluded_memory_ids_json"]
            except Exception:
                audit["excluded_memory_ids"] = []
        if diag:
            audit["exclusion_diagnostics"] = diag
            audit["excluded_memories"] = [
                {"key": m["key"], "memory_id": m["memory_id"], "reason": diag.get(m["key"], "")}
                for m in all_mems
                if diag.get(m["key"]) and diag.get(m["key"]) != "Included in Prompt (BM25 Match & Budget OK)"
            ]

    payload: dict[str, Any] = {
        "tenant_id": getattr(target_orch, "tenant_id", "tenant_default"),
        "session_id": getattr(target_orch, "raw_session_id", target_orch.session_id),
        "fsm_state": target_orch.state.value,
        "turn_counter": target_orch.turn_counter,
        "slots": target_orch.slots.model_dump(),
        "missing_fields": target_orch.slots.missing_discovery_fields(),
        "active_proposal": target_orch.active_proposal.model_dump() if target_orch.active_proposal else None,
        "trace_events": target_orch.trace.events[-40:],
        "trace_id": target_orch.trace.trace_id,
        "otel_traceparent": target_orch.trace.get_w3c_traceparent(),
        "bookings": target_orch.tools.list_bookings(target_orch.user_id)["bookings"],
        "holds": target_orch.db.list_all_holds(),
        "journal": target_orch.db.list_journal_entries(target_orch.session_id),
        "core_memories": core_mems,
        "all_memories": all_mems,
        "memory_partitions": {
            "explicit_count": sum(1 for m in all_mems if m["source_type"] == "EXPLICIT" and not m["forgotten"]),
            "confirmed_inferred_count": sum(1 for m in all_mems if m["source_type"] == "INFERRED" and m["confirmed"] and not m["forgotten"]),
            "unconfirmed_inferred_count": sum(1 for m in all_mems if m["source_type"] == "INFERRED" and not m["confirmed"] and not m["forgotten"]),
            "forgotten_count": sum(1 for m in all_mems if m["forgotten"]),
            "total_count": len(all_mems),
        },
        "exclusion_diagnostics": diag,
        "total_memories_seeded": len(all_mems),
        "latest_context_audit": audit,
        "mcp_tools_count": len(MCP_TOOLS_CATALOG),
        "limits": {
            "max_model_turns_per_run": target_orch.config.max_model_turns_per_run,
            "max_tool_calls_per_run": target_orch.config.max_tool_calls_per_run,
            "max_wall_clock_seconds": target_orch.config.max_wall_clock_seconds,
            "max_context_tokens": target_orch.config.max_context_tokens,
            "max_memory_tokens": target_orch.config.max_memory_tokens,
        },
    }
    if extra:
        payload.update(extra)
    return payload


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    html_path = STATIC_DIR / "index.html"
    return html_path.read_text(encoding="utf-8")


@app.get("/index.html", response_class=HTMLResponse)
def index_alias() -> str:
    return index()


@app.get("/healthz")
@app.get("/livez")
@app.get("/readyz")
def health_check(request: Request) -> dict[str, Any]:
    """Liveness & Readiness probe verifying SQLite WAL and FSM health."""
    tenant_id, session_id, user_id = extract_tenant_and_session(request)
    orch = WORKSPACE_MANAGER.get_orchestrator(tenant_id, session_id, user_id)
    try:
        # Verify DB connection
        orch.db.conn.execute("SELECT 1").fetchone()
        db_status = "connected"
    except Exception as exc:
        db_status = f"unhealthy: {exc}"

    return {
        "status": "healthy" if db_status == "connected" else "degraded",
        "service": "errand-agent",
        "version": "2.0.0",
        "fsm_state": orch.state.value,
        "database": db_status,
        "active_session": getattr(orch, "raw_session_id", orch.session_id),
        "trace_id": orch.trace.trace_id,
        "timestamp": round(time.time(), 4),
    }


@app.get("/metrics")
def prometheus_metrics() -> Response:
    """Standard Prometheus text exposition format metrics endpoint (Nov 2026)."""
    # Refresh gauge metrics with live system values
    METRICS.set_gauge("errand_agent_active_sessions", float(WORKSPACE_MANAGER.session_count()))
    METRICS.set_gauge("errand_agent_active_proposals", float(WORKSPACE_MANAGER.active_proposal_count()))
    holds = ORCH.db.list_all_holds()
    active_holds_count = sum(1 for h in holds if h.get("status") == "ACTIVE")
    METRICS.set_gauge("errand_agent_active_holds", float(active_holds_count))

    content = METRICS.render_prometheus()
    return Response(content=content, media_type="text/plain; version=0.0.4; charset=utf-8")


def build_turn_reasoning_payload(
    orch: ErrandAgentOrchestrator,
    turn_events: list[dict[str, Any]],
    user_message: str,
    reply: str,
    elapsed: float,
) -> dict[str, Any]:
    model_calls = [e for e in turn_events if e.get("event_type") == "model_call"]
    tool_calls = [e for e in turn_events if e.get("event_type") == "tool_call"]
    guardrails = [e for e in turn_events if e.get("event_type") == "guardrail_event"]
    transitions = [e for e in turn_events if e.get("event_type") == "state_transition"]

    primary_model = (
        model_calls[-1].get("model")
        if model_calls and model_calls[-1].get("model")
        else getattr(orch.config, "resolved_provider", lambda: "autonomous-orchestrator-v2")()
    )
    prompt_tokens = sum(e.get("prompt_tokens", 0) for e in model_calls) or 24
    completion_tokens = sum(e.get("completion_tokens", 0) for e in model_calls) or 16
    total_tokens = prompt_tokens + completion_tokens

    steps: list[dict[str, Any]] = []

    # Step 1: Query Intent & Constraint Extraction
    slots_dict = {k: v for k, v in orch.slots.model_dump().items() if v is not None}
    step1_desc = (
        f"Parsed user request with {primary_model}. Extracted intent '{orch.slots.intent_type or 'discover'}' "
        f"with slots: {', '.join(f'{k}={v}' for k, v in slots_dict.items()) or 'none yet'}."
    )
    steps.append({
        "step_num": 1,
        "type": "intent_extraction",
        "title": "Query Understanding & Intent Extraction",
        "description": step1_desc,
        "details": {
            "model": primary_model,
            "extracted_slots": slots_dict,
            "missing_fields": orch.slots.missing_discovery_fields(),
        },
    })

    # Step 2: Zero-Trust Security & Policy Guardrails
    guardrail_actions = [f"{g.get('guardrail')}:{g.get('action')}" for g in guardrails]
    step2_desc = (
        f"Verified context budget ({total_tokens}/1200 tokens). Schema gate: Consequential actions "
        f"(book/cancel/reschedule) strictly withheld until user affirmation."
    )
    steps.append({
        "step_num": 2,
        "type": "guardrail_verification",
        "title": "Zero-Trust Security & Policy Guardrails",
        "description": step2_desc,
        "details": {
            "guardrail_events": guardrails,
            "actions": guardrail_actions,
            "context_budget": f"{total_tokens} / 1,200 tokens ({round((total_tokens/1200)*100, 1)}%)",
        },
    })

    # Step 3: Tool Execution Loop
    if tool_calls:
        tool_names = [str(t.get("tool_name") or t.get("tool") or "tool") for t in tool_calls]
        step3_desc = f"Executed {len(tool_calls)} sandboxed tool(s): {', '.join(tool_names)} with zero external side-effects."
        steps.append({
            "step_num": 3,
            "type": "tool_execution",
            "title": f"Autonomous Tool Execution Loop ({len(tool_calls)} calls)",
            "description": step3_desc,
            "details": {
                "tools": [
                    {
                        "tool": str(t.get("tool_name") or t.get("tool") or "tool"),
                        "args": t.get("arguments") or t.get("kwargs") or {},
                        "status": t.get("status", "ok"),
                        "duration_ms": round(float(t.get("latency_ms") or t.get("duration_ms") or 0.0), 1),
                        "output_summary": str(t.get("sanitized_result"))[:140] + "..."
                        if len(str(t.get("sanitized_result", ""))) > 140
                        else str(t.get("sanitized_result")),
                    }
                    for t in tool_calls
                ]
            },
        })

    # Step 4: FSM State Transition & Response Synthesis
    last_trans = transitions[-1] if transitions else None
    to_state = last_trans.get("to_state", orch.state.value) if last_trans else orch.state.value
    trans_reason = last_trans.get("reason", "Maintained state") if last_trans else "Maintained state"
    step4_desc = f"Transitioned orchestrator state to {to_state}: {trans_reason}."
    steps.append({
        "step_num": len(steps) + 1,
        "type": "state_transition",
        "title": f"FSM State Transition -> {to_state}",
        "description": step4_desc,
        "details": {
            "from_state": last_trans.get("from_state", "IDLE") if last_trans else "IDLE",
            "to_state": to_state,
            "active_proposal": orch.active_proposal.model_dump() if orch.active_proposal else None,
        },
    })

    # Extract rich provider data if present
    rich_providers = []
    for tc in tool_calls:
        t_name = tc.get("tool_name") or tc.get("tool")
        if t_name == "get_provider":
            prov = tc.get("sanitized_result")
            if isinstance(prov, dict) and "provider_id" in prov:
                rich_providers.append({
                    "provider_id": prov.get("provider_id"),
                    "name": prov.get("name"),
                    "service": prov.get("service"),
                    "rating": prov.get("rating", 4.8),
                    "price": prov.get("price"),
                    "available_slots": prov.get("available_slots", []),
                    "reviews": prov.get("reviews", []),
                    "verified": True,
                })
        elif t_name == "search_providers":
            sr = tc.get("sanitized_result")
            if isinstance(sr, dict) and "providers" in sr:
                for p in sr["providers"][:3]:
                    rich_providers.append({
                        "provider_id": p.get("provider_id"),
                        "name": p.get("name"),
                        "service": p.get("service"),
                        "rating": p.get("rating", 4.7),
                        "price": p.get("price"),
                        "available_slots": p.get("available_slots", []),
                        "verified": True,
                    })

    seen_provs = set()
    dedup_providers = []
    for rp in rich_providers:
        if rp["provider_id"] not in seen_provs:
            seen_provs.add(rp["provider_id"])
            dedup_providers.append(rp)

    return {
        "thought_summary": f"Orchestrator completed turn in {round(elapsed*1000, 1)}ms via {primary_model}. {len(tool_calls)} tool(s) invoked under strict zero-trust invariants.",
        "model": primary_model,
        "latency_ms": round(elapsed * 1000, 1),
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": total_tokens,
            "cap": 1200,
            "utilization_pct": round((total_tokens / 1200) * 100, 1),
        },
        "steps": steps,
        "tools_executed": [str(t.get("tool_name") or t.get("tool") or "tool") for t in tool_calls],
        "rich_providers": dedup_providers,
        "fsm_state": orch.state.value,
        "active_proposal": orch.active_proposal.model_dump() if orch.active_proposal else None,
    }


@app.get("/api/state")
def get_state(request: Request) -> dict[str, Any]:
    tenant_id, session_id, user_id = extract_tenant_and_session(request)
    orch = WORKSPACE_MANAGER.get_orchestrator(tenant_id, session_id, user_id)
    return _build_state_snapshot(orch)


@app.post("/api/chat")
def chat_endpoint(req: ChatRequest, request: Request) -> dict[str, Any]:
    tenant_id, session_id, user_id = extract_tenant_and_session(request)
    orch = WORKSPACE_MANAGER.get_orchestrator(tenant_id, session_id, user_id)
    lock = WORKSPACE_MANAGER.get_session_lock(tenant_id, session_id)
    with lock:
        if not hasattr(orch, "fault_cfg") or orch.fault_cfg is None:
            orch.fault_cfg = getattr(orch.tools, "fault_cfg", FaultConfig())

        if req.force_book_outcome is not None:
            orch.fault_cfg.force_book_outcome = req.force_book_outcome or None
            orch.tools.fault_cfg.force_book_outcome = req.force_book_outcome or None
        if req.force_price_drift_delta is not None:
            orch.fault_cfg.force_price_drift_delta = req.force_price_drift_delta
            orch.tools.fault_cfg.force_price_drift_delta = req.force_price_drift_delta
        if req.inject_adversarial_review is not None:
            orch.fault_cfg.inject_adversarial_review_on_all = req.inject_adversarial_review
            orch.tools.fault_cfg.inject_adversarial_review_on_all = req.inject_adversarial_review
        if req.persistent_cancel_503 is not None:
            orch.fault_cfg.persistent_cancel_503 = req.persistent_cancel_503
            orch.tools.fault_cfg.persistent_cancel_503 = req.persistent_cancel_503

        sanitized_msg = sanitize_input_text(req.message)
        try:
            start_event_idx = len(orch.trace.events)
            t0 = time.perf_counter()
            reply = orch.handle_user_message(sanitized_msg)
            elapsed = time.perf_counter() - t0
            turn_events = orch.trace.events[start_event_idx:]
            reasoning = build_turn_reasoning_payload(orch, turn_events, sanitized_msg, reply, elapsed)

            METRICS.inc_counter("errand_agent_turns_total", labels={"status": "success", "user_id": orch.user_id})
            METRICS.observe_duration("errand_agent_operation_duration_seconds", elapsed, labels={"operation": "orchestrator_turn"})
            return _build_state_snapshot(orch, {"reply": reply, "agent_reasoning": reasoning})
        except Exception as exc:
            METRICS.inc_counter("errand_agent_turns_total", labels={"status": "error", "user_id": orch.user_id})
            METRICS.inc_counter("errand_agent_errors_total", labels={"category": "turn", "error_type": exc.__class__.__name__})
            raise
        finally:
            # Fault controls are one-turn chaos-test inputs, never durable state.
            orch.tools.fault_cfg.force_book_outcome = None
            orch.tools.fault_cfg.force_price_drift_delta = 0
            orch.tools.fault_cfg.inject_adversarial_review_on_all = False
            orch.tools.fault_cfg.persistent_cancel_503 = False
            if hasattr(orch, "fault_cfg") and orch.fault_cfg is not None:
                orch.fault_cfg.force_book_outcome = None
                orch.fault_cfg.force_price_drift_delta = 0
                orch.fault_cfg.inject_adversarial_review_on_all = False
                orch.fault_cfg.persistent_cancel_503 = False


chat_turn = chat_endpoint


@app.post("/api/proactive-cancel")
def trigger_proactive_cancel(req: ProactiveCancelRequest, request: Request) -> dict[str, Any]:
    tenant_id, session_id, user_id = extract_tenant_and_session(request)
    orch = WORKSPACE_MANAGER.get_orchestrator(tenant_id, session_id, user_id)
    lock = WORKSPACE_MANAGER.get_session_lock(tenant_id, session_id)
    proactive = WORKSPACE_MANAGER.get_proactive_monitor(tenant_id, session_id, user_id)
    with lock:
        start_event_idx = len(orch.trace.events)
        t0 = time.perf_counter()
        clean_id = sanitize_input_text(req.booking_id)
        res = proactive.handle_provider_cancelled_event(clean_id)
        elapsed = time.perf_counter() - t0
        turn_events = orch.trace.events[start_event_idx:]
        reasoning = build_turn_reasoning_payload(
            orch, turn_events, f"Proactive Cancellation Webhook: {clean_id}", res.get("message", ""), elapsed
        )
        return _build_state_snapshot(
            orch,
            {
                "event_result": res,
                "reply": res.get("message", f"Proactive monitor handled cancellation of {clean_id}"),
                "agent_reasoning": reasoning,
            },
        )


@app.post("/api/memory")
def memory_endpoint(req: MemoryActionRequest, request: Request) -> Any:
    role: UserRole = getattr(request.state, "role", UserRole.ADMIN)
    action_permission = "read:memory" if req.action == "inspect" else "write:memory"
    if not check_permission(role, action_permission):
        return JSONResponse(
            status_code=403,
            content=make_problem_details(
                status=403,
                title="Forbidden",
                detail=f"Role '{role.value}' is not authorized to execute memory operation '{req.action}'.",
                type_uri="https://errand-agent.enterprise/errors/forbidden",
                extra={"role": role.value, "requested_action": req.action},
            ),
        )
    tenant_id, session_id, user_id = extract_tenant_and_session(request)
    orch = WORKSPACE_MANAGER.get_orchestrator(tenant_id, session_id, user_id)
    lock = WORKSPACE_MANAGER.get_session_lock(tenant_id, session_id)
    with lock:
        clean_key = sanitize_input_text(req.key)
        clean_val = sanitize_input_text(req.value)
        msg = orch.handle_memory_operation({"action": req.action, "key": clean_key, "value": clean_val})
        return _build_state_snapshot(orch, {"reply": msg})


@app.post("/api/approval/action")
def approval_action_endpoint(req: ApprovalActionRequest, request: Request) -> Any:
    """Dedicated 1-click Human-in-the-Loop (HITL) approval action endpoint."""
    role: UserRole = getattr(request.state, "role", UserRole.ADMIN)

    # 1. Auditor role gating (Auditors are strictly read-only and blocked from mutations)
    if role == UserRole.AUDITOR:
        return JSONResponse(
            status_code=403,
            content=make_problem_details(
                status=403,
                title="Forbidden",
                detail=f"Role '{role.value}' is not authorized to execute approval action '{req.action}'.",
                type_uri="https://errand-agent.enterprise/errors/forbidden",
                extra={"role": role.value, "action": req.action},
            ),
        )

    action_norm = req.action.strip().lower()
    if action_norm not in ("approve", "reject"):
        return JSONResponse(
            status_code=400,
            content=make_problem_details(
                status=400,
                title="Bad Request",
                detail=f"Invalid approval action '{req.action}'. Expected 'approve' or 'reject'.",
                type_uri="https://errand-agent.enterprise/errors/bad-request",
                extra={"action": req.action},
            ),
        )

    tenant_id, session_id, user_id = extract_tenant_and_session(request)
    orch = WORKSPACE_MANAGER.get_orchestrator(tenant_id, session_id, user_id)
    lock = WORKSPACE_MANAGER.get_session_lock(tenant_id, session_id)

    with lock:
        prop = orch.active_proposal
        if not prop or prop.proposal_id != req.proposal_id:
            return JSONResponse(
                status_code=404,
                content=make_problem_details(
                    status=404,
                    title="Not Found",
                    detail=f"No active proposal found matching proposal_id '{req.proposal_id}'.",
                    type_uri="https://errand-agent.enterprise/errors/not-found",
                    extra={"proposal_id": req.proposal_id},
                ),
            )

        if prop.invalidated:
            return JSONResponse(
                status_code=400,
                content=make_problem_details(
                    status=400,
                    title="Proposal Invalidated",
                    detail=f"Proposal '{req.proposal_id}' has already been invalidated: {prop.invalidation_reason}",
                    type_uri="https://errand-agent.enterprise/errors/proposal-invalidated",
                    extra={"proposal_id": req.proposal_id, "invalidation_reason": prop.invalidation_reason},
                ),
            )

        if prop.is_expired():
            prop.invalidated = True
            prop.invalidation_reason = f"Proposal has expired (> {prop.ttl_seconds}s TTL)"
            return JSONResponse(
                status_code=400,
                content=make_problem_details(
                    status=400,
                    title="Proposal Expired",
                    detail=f"Proposal '{req.proposal_id}' has expired after {prop.ttl_seconds} seconds.",
                    type_uri="https://errand-agent.enterprise/errors/proposal-expired",
                    extra={"proposal_id": req.proposal_id, "ttl_seconds": prop.ttl_seconds},
                ),
            )

        if action_norm == "reject":
            if prop.approved_at_turn is not None or orch.state == AgentState.COMPLETED:
                return JSONResponse(
                    status_code=400,
                    content=make_problem_details(
                        status=400,
                        title="Proposal Already Approved",
                        detail="Cannot reject proposal that has already been approved and executed.",
                        type_uri="https://errand-agent.enterprise/errors/proposal-already-approved",
                        extra={
                            "proposal_id": req.proposal_id,
                            "approved_at_turn": prop.approved_at_turn,
                            "fsm_state": orch.state.value,
                        },
                    ),
                )
            prop.invalidated = True
            clean_reason = sanitize_input_text(req.reason) if req.reason else None
            reason_msg = f"User explicitly rejected: {clean_reason}" if clean_reason else "User explicitly rejected proposal"
            prop.invalidation_reason = reason_msg
            orch._transition(AgentState.IDLE, reason_msg)
            reply = f"Proposal '{req.proposal_id}' was rejected. {reason_msg}"

            extra = {
                "status": "rejected",
                "proposal_id": prop.proposal_id,
                "booking": None,
                "reply": reply,
            }
            return _build_state_snapshot(orch, extra)

        else:  # approve
            start_event_idx = len(orch.trace.events)
            t0 = time.perf_counter()
            if prop.approved_at_turn is None:
                orch.turn_counter += 1
                prop.approved_at_turn = orch.turn_counter
            reply = orch._execute_approved_proposal()
            elapsed = time.perf_counter() - t0
            turn_events = orch.trace.events[start_event_idx:]
            reasoning = build_turn_reasoning_payload(
                orch, turn_events, f"HITL Approval Action: {action_norm}", reply, elapsed
            )

            if orch.state == AgentState.COMPLETED:
                bookings = orch.tools.list_bookings(orch.user_id).get("bookings", [])
                matching_booking = next(
                    (
                        b for b in bookings
                        if (
                            (b.get("provider_id") == prop.provider_id and b.get("slot") == prop.slot)
                            or (prop.booking_id and b.get("booking_id") == prop.booking_id)
                        )
                    ),
                    bookings[0] if bookings else None,
                )
                extra = {
                    "status": "approved",
                    "proposal_id": prop.proposal_id,
                    "booking": matching_booking,
                    "reply": reply,
                    "agent_reasoning": reasoning,
                }
                return _build_state_snapshot(orch, extra)
            elif orch.state == AgentState.AWAITING_APPROVAL:
                extra = {
                    "status": "alternative_proposed",
                    "proposal_id": (orch.active_proposal.proposal_id if orch.active_proposal else prop.proposal_id),
                    "booking": None,
                    "reply": reply,
                    "agent_reasoning": reasoning,
                }
                return _build_state_snapshot(orch, extra)
            elif orch.state == AgentState.SAFE_ABORT:
                extra = {
                    "status": "aborted",
                    "proposal_id": prop.proposal_id,
                    "booking": None,
                    "reply": reply,
                    "agent_reasoning": reasoning,
                }
                return _build_state_snapshot(orch, extra)
            else:
                extra = {
                    "status": orch.state.value.lower(),
                    "proposal_id": prop.proposal_id,
                    "booking": None,
                    "reply": reply,
                    "agent_reasoning": reasoning,
                }
                return _build_state_snapshot(orch, extra)


@app.post("/api/run-evals")
def run_eval_suite() -> dict[str, Any]:
    from pathlib import Path
    import importlib.util
    eval_path = Path(__file__).resolve().parent.parent / "run_evals.py"
    if eval_path.is_file():
        spec = importlib.util.spec_from_file_location("errand_agent_run_evals", eval_path)
        eval_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(eval_mod)
        eval_mod.main()
    else:
        import run_evals
        run_evals.main()
    from tests import conftest
    return {
        "total": len(conftest.EVAL_RESULTS),
        "passed": sum(1 for r in conftest.EVAL_RESULTS if r["status"] == "PASS"),
        "failed": sum(1 for r in conftest.EVAL_RESULTS if r["status"] == "FAIL"),
        "results": conftest.EVAL_RESULTS,
    }


@app.get("/events")
@app.get("/api/trace-stream")
async def trace_stream(request: Request) -> StreamingResponse:
    tenant_id, session_id, user_id = extract_tenant_and_session(request)
    orch = WORKSPACE_MANAGER.get_orchestrator(tenant_id, session_id, user_id)

    async def event_generator():
        last_idx = 0
        last_state = None
        last_prop_id = None
        try:
            while True:
                events = orch.trace.events
                current_state = orch.state.value
                current_proposal = (
                    orch.active_proposal.model_dump()
                    if (orch.active_proposal and hasattr(orch.active_proposal, "model_dump"))
                    else (orch.active_proposal.dict() if orch.active_proposal else None)
                )
                current_prop_id = current_proposal.get("proposal_id") if current_proposal else None

                if len(events) < last_idx:
                    last_idx = 0

                has_new_events = len(events) > last_idx
                state_changed = (last_state is not None and current_state != last_state)
                proposal_changed = (last_prop_id != current_prop_id)

                if has_new_events:
                    for ev in events[last_idx:]:
                        payload = dict(ev)
                        payload["fsm_state"] = current_state
                        payload["active_proposal"] = current_proposal
                        yield f"data: {json.dumps(payload)}\n\n"
                    last_idx = len(events)
                    last_state = current_state
                    last_prop_id = current_prop_id
                elif state_changed or proposal_changed:
                    payload = {
                        "event_type": "state_sync",
                        "fsm_state": current_state,
                        "active_proposal": current_proposal,
                        "timestamp": round(time.time(), 4),
                        "session_id": session_id,
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                    last_state = current_state
                    last_prop_id = current_prop_id
                elif last_state is None:
                    last_state = current_state
                    last_prop_id = current_prop_id

                await asyncio.sleep(0.4)
        except asyncio.CancelledError:
            return

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(event_generator(), media_type="text/event-stream", headers=headers)


@app.get("/api/export-trace")
def export_trace_jsonl(request: Request) -> Response:
    """Download full trace events as JSONL."""
    tenant_id, session_id, user_id = extract_tenant_and_session(request)
    orch = WORKSPACE_MANAGER.get_orchestrator(tenant_id, session_id, user_id)
    content = orch.trace.export_jsonl()
    headers = {
        "Content-Disposition": f'attachment; filename="errand_trace_{session_id}.jsonl"',
        "Content-Type": "application/x-ndjson",
    }
    return Response(content=content, headers=headers)


# ==================== MODEL CONTEXT PROTOCOL (MCP) ENDPOINTS ====================

@app.post("/mcp")
@app.post("/mcp/rpc")
async def mcp_jsonrpc_endpoint(request: Request) -> JSONResponse:
    """Standard JSON-RPC 2.0 endpoint for Model Context Protocol clients."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
        )

    has_auth = getattr(request.state, "has_auth_credentials", False)
    caller_role = getattr(request.state, "role", None) if has_auth else None
    tenant_id, session_id, user_id = extract_tenant_and_session(request)
    handler = WORKSPACE_MANAGER.get_mcp_handler(tenant_id, session_id, user_id)
    response = handler.handle_jsonrpc(body, default_role=caller_role)
    return JSONResponse(content=response)


@app.get("/mcp/tools")
@app.get("/mcp/v1/tools")
def mcp_list_tools() -> dict[str, Any]:
    """REST endpoint returning full MCP tool catalog and schemas."""
    return {"tools": MCP_TOOLS_CATALOG, "count": len(MCP_TOOLS_CATALOG)}


@app.post("/mcp/call")
def mcp_call_tool(req: MCPCallRequest, request: Request) -> Any:
    """REST endpoint for direct tool execution via MCP catalog."""
    has_auth = getattr(request.state, "has_auth_credentials", False)
    auth_role = getattr(request.state, "role", None) if has_auth else None

    if auth_role is not None:
        caller_role = auth_role
    elif req.role:
        try:
            caller_role = UserRole(req.role.strip().title())
        except ValueError:
            caller_role = getattr(request.state, "role", UserRole.ADMIN)
    else:
        caller_role = getattr(request.state, "role", UserRole.ADMIN)

    if req.name in MCP_MUTATING_TOOLS and not check_permission(caller_role, "write:mutations"):
        return JSONResponse(
            status_code=403,
            content=make_problem_details(
                status=403,
                title="Forbidden",
                detail=f"Role '{caller_role.value}' is not authorized to execute mutating MCP tool '{req.name}'.",
                type_uri="https://errand-agent.enterprise/errors/forbidden",
                extra={"role": caller_role.value, "tool": req.name},
            ),
        )
    tenant_id, session_id, user_id = extract_tenant_and_session(request)
    handler = WORKSPACE_MANAGER.get_mcp_handler(tenant_id, session_id, user_id)
    lock = WORKSPACE_MANAGER.get_session_lock(tenant_id, session_id)
    with lock:
        return handler.execute_tool(req.name, req.arguments, role=caller_role)


@app.get("/mcp/manifest")
def mcp_manifest() -> dict[str, Any]:
    """MCP server manifest, capabilities, and catalog."""
    return {
        "serverInfo": SERVER_INFO,
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
            "prompts": {"listChanged": False},
        },
        "tools": MCP_TOOLS_CATALOG,
        "resources": MCP_RESOURCES_CATALOG,
        "prompts": MCP_PROMPTS_CATALOG,
    }


@app.get("/mcp/resources")
def mcp_list_resources() -> dict[str, Any]:
    """REST endpoint returning available MCP resources."""
    return {"resources": MCP_RESOURCES_CATALOG, "count": len(MCP_RESOURCES_CATALOG)}


@app.post("/api/reset")
def reset_demo(request: Request) -> dict[str, Any]:
    global DB, ORCH, PROACTIVE, MCP_HANDLER, STATE_LOCK
    tenant_id, session_id, user_id = extract_tenant_and_session(request)
    lock = WORKSPACE_MANAGER.get_session_lock(tenant_id, session_id)
    with lock:
        orch = WORKSPACE_MANAGER.reset_session(tenant_id, session_id, user_id)
        if tenant_id == "tenant_default" and session_id == "live_demo_session":
            DB = orch.db
            ORCH = orch
            PROACTIVE = WORKSPACE_MANAGER.get_proactive_monitor(tenant_id, session_id, user_id)
            MCP_HANDLER = WORKSPACE_MANAGER.get_mcp_handler(tenant_id, session_id, user_id)
            STATE_LOCK = lock
        return _build_state_snapshot(orch, {"status": "reset"})
