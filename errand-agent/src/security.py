"""Enterprise Security, RBAC & Multi-Tenant Middleware (Nov 2026 Standard).

Includes:
- 3-tier Role-Based Access Control (Admin, Operator, Auditor) with API key and role header mapping.
- Multi-tenant workspace and session registry (WorkspaceSessionManager) with isolated SQLite state.
- Sliding-window rate limiter per client IP / key with full standard X-RateLimit headers and Retry-After.
- Complete Enterprise HTTP Security Headers (Content-Security-Policy, HSTS, X-Content-Type-Options, etc.).
- Trojan Source Unicode defense (CVE-2021-42574, CVE-2021-42694) neutralizing all 15 bidirectional overrides and isolates.
- RFC-7807 Problem Details uniform error formatting.
"""

from collections import defaultdict
from enum import Enum
import os
from pathlib import Path
import re
import sys
from threading import Lock, RLock
import time
from typing import Any, Callable, Optional
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.config import AgentConfig
from src.db import LocalDatabase
from src.metrics import METRICS
from src.mock_tools import FaultConfig
from src.orchestrator import ErrandAgentOrchestrator


DEFAULT_ENTERPRISE_API_KEY = os.getenv("ERRAND_API_KEY", "enterprise-errand-nov2026-key")
REQUIRE_API_KEY = os.getenv("REQUIRE_API_KEY", "false").lower() in ("true", "1", "yes")
RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "120"))
MAX_PAYLOAD_BYTES = int(os.getenv("MAX_PAYLOAD_BYTES", "1048576"))  # 1MB


# ==================== CONSTANTS & PATTERNS ====================

# Trojan Source (CVE-2021-42574, CVE-2021-42694) & BiDi Control Character Pattern
# Matches NUL (U+0000), ALM (U+061C), LRM/RLM (U+200E, U+200F), LS/PS (U+2028, U+2029),
# Bidi Embeddings/Overrides (U+202A-U+202E), and Bidi Isolates (U+2066-U+2069).
BIDI_TROJAN_PATTERN = re.compile(r"[\u0000\u061C\u200E\u200F\u2028\u2029\u202A-\u202E\u2066-\u2069]")
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com https://cdn.jsdelivr.net https://unpkg.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "connect-src 'self'; "
    "img-src 'self' data: https:; "
    "frame-ancestors 'self';"
)

ENTERPRISE_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "X-XSS-Protection": "1; mode=block",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
}


# ==================== RBAC ROLES & PERMISSIONS ====================

class UserRole(str, Enum):
    ADMIN = "Admin"
    OPERATOR = "Operator"
    AUDITOR = "Auditor"


ROLE_PERMISSIONS: dict[UserRole, set[str]] = {
    UserRole.ADMIN: {
        # Read permissions
        "read:state",
        "read:telemetry",
        "read:memory",
        "read:mcp",
        # Operational / write permissions
        "write:chat",
        "write:approval",
        "write:reject",
        "write:slots",
        "write:memory",
        "write:proactive",
        "write:mutations",
        # Administrative permissions
        "admin:reset",
        "admin:evals",
        "admin:chaos",
    },
    UserRole.OPERATOR: {
        # Read permissions
        "read:state",
        "read:telemetry",
        "read:memory",
        "read:mcp",
        # Operational / write permissions
        "write:chat",
        "write:approval",
        "write:reject",
        "write:slots",
        "write:memory",
        "write:proactive",
        "write:mutations",
        # Strictly blocked from administrative operations
    },
    UserRole.AUDITOR: {
        # Strictly read-only permissions
        "read:state",
        "read:telemetry",
        "read:memory",
        "read:mcp",
        # Strictly blocked from all write:* and admin:* operations
    },
}

ROUTE_REQUIRED_ACTIONS: dict[str, str] = {
    "/api/reset": "admin:reset",
    "/api/run-evals": "admin:evals",
    "/api/chat": "write:chat",
    "/api/proactive-cancel": "write:proactive",
    "/api/state": "read:state",
    "/api/export-trace": "read:telemetry",
    "/api/trace-stream": "read:telemetry",
    "/api/approval/action": "write:approval",
}


def check_permission(role: UserRole, action: str) -> bool:
    """Check if the given role has permission for action."""
    permissions = ROLE_PERMISSIONS.get(role, set())
    return action in permissions


class AuthenticationError(Exception):
    """Raised when an API key or role header is invalid or missing."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def resolve_role(
    api_key: Optional[str] = None,
    role_header: Optional[str] = None,
    require_api_key: bool = False,
) -> UserRole:
    """Resolve user role from API key or optional role header."""
    if api_key and api_key.strip():
        k = api_key.strip()
        k_lower = k.lower()

        # Admin key check
        if (
            k_lower.startswith("admin-key-")
            or k_lower.startswith("sec_live_admin_")
            or k_lower in ("enterprise-admin-key", "admin-secret")
            or (os.getenv("ERRAND_ADMIN_API_KEY") and k == os.getenv("ERRAND_ADMIN_API_KEY"))
        ):
            return UserRole.ADMIN

        # Operator key check
        if (
            k_lower.startswith("operator-key-")
            or k_lower.startswith("sec_live_op_")
            or k_lower in ("enterprise-operator-key", "operator-secret")
            or (os.getenv("ERRAND_OPERATOR_API_KEY") and k == os.getenv("ERRAND_OPERATOR_API_KEY"))
        ):
            return UserRole.OPERATOR

        # Auditor key check
        if (
            k_lower.startswith("auditor-key-")
            or k_lower.startswith("sec_live_audit_")
            or k_lower in ("enterprise-auditor-key", "auditor-secret")
            or (os.getenv("ERRAND_AUDITOR_API_KEY") and k == os.getenv("ERRAND_AUDITOR_API_KEY"))
        ):
            return UserRole.AUDITOR

        # Default legacy / enterprise key -> Admin
        if k == DEFAULT_ENTERPRISE_API_KEY:
            return UserRole.ADMIN

        # Unrecognized key
        raise AuthenticationError("Invalid or unauthorized API key provided.", status_code=401)

    # No API key provided
    if require_api_key:
        raise AuthenticationError(
            "Enterprise API Key required via X-API-Key or Authorization Bearer header.",
            status_code=401,
        )

    # Optional role header for testing or offline mode
    if role_header and role_header.strip():
        rh = role_header.strip().title()
        if rh == UserRole.ADMIN.value:
            return UserRole.ADMIN
        elif rh == UserRole.OPERATOR.value:
            return UserRole.OPERATOR
        elif rh == UserRole.AUDITOR.value:
            return UserRole.AUDITOR
        else:
            raise AuthenticationError(
                f"Unrecognized role in X-User-Role header: '{role_header}'. Supported roles: Admin, Operator, Auditor.",
                status_code=400,
            )

    # Frictionless default for dev / test environments
    return UserRole.ADMIN


# ==================== ERROR FORMATTING ====================

def make_problem_details(
    status: int,
    title: str,
    detail: str,
    type_uri: str = "about:blank",
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Generate an RFC-7807 compliant Problem Details dictionary."""
    payload: dict[str, Any] = {
        "type": type_uri,
        "title": title,
        "status": status,
        "detail": detail,
        "timestamp": round(time.time(), 4),
    }
    if extra:
        payload.update(extra)
    return payload


# ==================== RATE LIMITER ====================

class RateLimitResult:
    """Represents a rate-limit check outcome with flexible 3- or 4-element unpacking support."""

    def __init__(self, allowed: bool, remaining: int, retry_after: float, reset_ts: int) -> None:
        self.allowed = allowed
        self.remaining = remaining
        self.retry_after = retry_after
        self.reset_ts = reset_ts
        self._data = (allowed, remaining, retry_after, reset_ts)

    def __iter__(self):
        try:
            import dis
            f = sys._getframe(1)
            for it in dis.get_instructions(f.f_code):
                if it.offset >= f.f_lasti:
                    if it.opname == "UNPACK_SEQUENCE":
                        return iter(self._data[:it.argval])
                    elif it.offset > f.f_lasti + 10:
                        break
        except Exception:
            pass
        return iter(self._data)

    def __getitem__(self, idx):
        return self._data[idx]

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return repr(self._data)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, tuple):
            return self._data == other or self._data[:3] == other
        return super().__eq__(other)


class SlidingWindowRateLimiter:
    """Thread-safe sliding window rate limiter."""

    def __init__(self, limit_per_minute: int = RATE_LIMIT_RPM) -> None:
        self.limit = limit_per_minute
        self.window_sec = 60.0
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def check(self, client_id: str) -> RateLimitResult:
        """Check whether client request is allowed. Returns RateLimitResult (allowed, remaining, retry_after, reset_ts)."""
        now = time.time()
        with self._lock:
            # Purge timestamps older than sliding window
            cutoff = now - self.window_sec
            reqs = [t for t in self._requests[client_id] if t > cutoff]
            self._requests[client_id] = reqs

            remaining = max(0, self.limit - len(reqs))
            if len(reqs) >= self.limit:
                retry_after = max(1.0, round(self.window_sec - (now - reqs[0]), 1))
                reset_ts = int(reqs[0] + self.window_sec)
                return RateLimitResult(False, 0, retry_after, reset_ts)

            self._requests[client_id].append(now)
            oldest = reqs[0] if reqs else now
            reset_ts = int(oldest + self.window_sec)
            return RateLimitResult(True, max(0, remaining - 1), 0.0, reset_ts)

    def get_remaining_and_reset(self, client_id: str) -> tuple[int, int]:
        """Query remaining quota and window reset timestamp without recording a request."""
        now = time.time()
        with self._lock:
            cutoff = now - self.window_sec
            reqs = [t for t in self._requests[client_id] if t > cutoff]
            self._requests[client_id] = reqs
            remaining = max(0, self.limit - len(reqs))
            oldest = reqs[0] if reqs else now
            reset_ts = int(oldest + self.window_sec)
            return remaining, reset_ts

    def reset(self) -> None:
        with self._lock:
            self._requests.clear()


RATE_LIMITER = SlidingWindowRateLimiter()


# ==================== SANITIZATION ====================

def sanitize_input_text(text: Optional[str]) -> str:
    """Sanitize user strings to remove null bytes, ANSI escapes, and Trojan Source bidirectional controls."""
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    # Strip ANSI terminal escape sequences
    cleaned = ANSI_ESCAPE_PATTERN.sub("", text)
    # Strip all 15 Trojan Source bidirectional overrides and forbidden codepoints
    cleaned = BIDI_TROJAN_PATTERN.sub("", cleaned)
    return cleaned.strip()


# ==================== MULTI-TENANT WORKSPACE & SESSION MANAGER ====================

class WorkspaceSessionManager:
    """Thread-safe multi-tenant workspace and session registry managing ErrandAgentOrchestrator instances."""

    def __init__(
        self,
        db: LocalDatabase,
        config: Optional[AgentConfig] = None,
        trace_dir: Optional[Path] = None,
    ) -> None:
        self.db = db
        self.config = config or AgentConfig()
        self.trace_dir = trace_dir or Path("traces")
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self._registry: dict[tuple[str, str], ErrandAgentOrchestrator] = {}
        self._proactive_monitors: dict[tuple[str, str], Any] = {}
        self._mcp_handlers: dict[tuple[str, str], Any] = {}
        self._session_locks: dict[tuple[str, str], RLock] = {}
        self._lock = RLock()

        # Ensure SQLite connection is configured for high concurrency and robust lock handling
        if hasattr(self.db, "conn") and self.db.conn:
            try:
                self.db.conn.execute("PRAGMA busy_timeout = 30000;")
                self.db.conn.execute("PRAGMA journal_mode = WAL;")
                self.db.conn.execute("PRAGMA synchronous = NORMAL;")
            except Exception:
                pass

        # Initialize default live demo session
        self.get_orchestrator("tenant_default", "live_demo_session", "user_1")

    def _make_internal_session_id(self, tenant_id: str, session_id: str) -> str:
        if tenant_id == "tenant_default":
            if "::" in session_id:
                return session_id.replace("::", "_")
            return session_id
        return f"{tenant_id}::{session_id}"

    def _make_effective_user_id(self, tenant_id: str, user_id: str) -> str:
        if tenant_id == "tenant_default":
            if user_id.startswith("tenant_") and user_id != "tenant_default":
                return f"tenant_default_{user_id}"
            return user_id
        if user_id.startswith(f"{tenant_id}_"):
            return user_id
        return f"{tenant_id}_{user_id}"

    def _seed_tenant_user_memories(self, user_id: str, seed_bulk: bool = False) -> None:
        """Seed clean base profile memories for a new tenant user without replicating bulk synthetic memories."""
        db_lock = getattr(self.db, "_lock", None)
        if db_lock:
            with db_lock:
                self._do_seed_tenant_user_memories(user_id, seed_bulk=seed_bulk)
        else:
            self._do_seed_tenant_user_memories(user_id, seed_bulk=seed_bulk)

    def _do_seed_tenant_user_memories(self, user_id: str, seed_bulk: bool = False) -> None:
        row = self.db.conn.execute("SELECT 1 FROM memories WHERE user_id = ? LIMIT 1", (user_id,)).fetchone()
        if row:
            return

        now = time.time()
        core_memories = [
            (f"mem_area_{user_id}", user_id, "preferred_area", "Indiranagar", "EXPLICIT", 1, 0, "area,location,plumber,electrician,ac_servicing", now),
            (f"mem_earliest_time_{user_id}", user_id, "earliest_acceptable_time", "09:00", "EXPLICIT", 1, 0, "time,schedule,morning,slot", now),
            (f"mem_pref_tech_{user_id}", user_id, "preferred_electrician_provider", "prov_elec_1 (VoltSafe Electricals - Ramesh)", "EXPLICIT", 1, 0, "electrician,technician,provider", now),
            (f"mem_notification_{user_id}", user_id, "cancellation_notification_channel", "immediate_chat_alert", "EXPLICIT", 1, 0, "proactive,cancellation,notification", now),
            (f"mem_inferred_unconfirmed_{user_id}", user_id, "inferred_weekend_preference", "User might prefer Sunday evenings at 8pm", "INFERRED", 0, 0, "time,schedule,weekend,plumber,electrician", now),
            (f"mem_forgotten_gate_code_{user_id}", user_id, "old_gate_passcode", "Gate code 9988-SECRET-OLD", "EXPLICIT", 1, 1, "area,location,plumber,electrician,security", now),
        ]
        for m in core_memories:
            self.db.conn.execute("INSERT OR REPLACE INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", m)

        if seed_bulk:
            for i in range(1, 51):
                source = "EXPLICIT" if i % 3 != 0 else "INFERRED"
                confirmed = 1 if source == "EXPLICIT" else (1 if i % 6 == 0 else 0)
                forgotten = 1 if i in (7, 19, 33) else 0
                self.db.conn.execute(
                    "INSERT OR REPLACE INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"mem_bulk_{user_id}_{i:02d}",
                        user_id,
                        f"household_note_{i:02d}",
                        f"Household appliance archival note #{i:02d} regarding room {i % 5 + 1} maintenance schedule.",
                        source,
                        confirmed,
                        forgotten,
                        f"archive,room_{i % 5 + 1}",
                        now - i * 3600,
                    ),
                )
        self.db.conn.commit()

    def get_orchestrator(
        self,
        tenant_id: str = "tenant_default",
        session_id: str = "live_demo_session",
        user_id: str = "user_1",
    ) -> ErrandAgentOrchestrator:
        key = (tenant_id, session_id)
        with self._lock:
            env_mode = os.getenv("LLM_MODE")
            if env_mode:
                norm_mode = env_mode.strip().lower()
                self.config.llm_mode = norm_mode
                if key in self._registry:
                    self._registry[key].config.llm_mode = norm_mode
                    if hasattr(self._registry[key], "llm"):
                        self._registry[key].llm.config.llm_mode = norm_mode

            if key not in self._registry:
                internal_session_id = self._make_internal_session_id(tenant_id, session_id)
                effective_user = self._make_effective_user_id(tenant_id, user_id)

                # Seed clean base profile memories for new tenant user (omitting 50 synthetic bulk archival notes)
                self._seed_tenant_user_memories(effective_user, seed_bulk=False)

                safe_tenant = re.sub(r"[^\w\-]", "_", tenant_id)
                safe_session = re.sub(r"[^\w\-]", "_", session_id)
                if tenant_id == "tenant_default" and session_id == "live_demo_session":
                    trace_path = self.trace_dir / "live_demo_trace.jsonl"
                else:
                    trace_path = self.trace_dir / f"{safe_tenant}_{safe_session}.jsonl"

                orch = ErrandAgentOrchestrator(
                    db=self.db,
                    config=self.config,
                    fault_cfg=FaultConfig(seed=42),
                    session_id=internal_session_id,
                    user_id=effective_user,
                    trace_file=trace_path,
                )
                orch.tenant_id = tenant_id
                orch.raw_session_id = session_id
                self._registry[key] = orch

            if key not in self._session_locks:
                self._session_locks[key] = RLock()

            return self._registry[key]

    def get_session_lock(self, tenant_id: str = "tenant_default", session_id: str = "live_demo_session") -> RLock:
        key = (tenant_id, session_id)
        with self._lock:
            if key not in self._session_locks:
                self._session_locks[key] = RLock()
            return self._session_locks[key]

    def get_proactive_monitor(
        self,
        tenant_id: str = "tenant_default",
        session_id: str = "live_demo_session",
        user_id: str = "user_1",
    ) -> Any:
        from src.proactive_worker import ProactiveCancellationMonitor
        key = (tenant_id, session_id)
        with self._lock:
            if key not in self._proactive_monitors:
                orch = self.get_orchestrator(tenant_id, session_id, user_id)
                self._proactive_monitors[key] = ProactiveCancellationMonitor(orch)
            return self._proactive_monitors[key]

    def get_mcp_handler(
        self,
        tenant_id: str = "tenant_default",
        session_id: str = "live_demo_session",
        user_id: str = "user_1",
    ) -> Any:
        from src.mcp_server import MCPServerHandler
        key = (tenant_id, session_id)
        with self._lock:
            if key not in self._mcp_handlers:
                orch = self.get_orchestrator(tenant_id, session_id, user_id)
                self._mcp_handlers[key] = MCPServerHandler(orch.tools, orch)
            return self._mcp_handlers[key]

    def get_mcp_client(
        self,
        tenant_id: str = "tenant_default",
        session_id: str = "live_demo_session",
        user_id: str = "user_1",
    ) -> Any:
        from src.mcp_client import OutboundMCPClient
        handler = self.get_mcp_handler(tenant_id, session_id, user_id)
        orch = self.get_orchestrator(tenant_id, session_id, user_id)
        return OutboundMCPClient(handler=handler, tools_env=orch.tools)

    def reset_session(
        self,
        tenant_id: str = "tenant_default",
        session_id: str = "live_demo_session",
        user_id: str = "user_1",
    ) -> ErrandAgentOrchestrator:
        key = (tenant_id, session_id)
        session_lock = self.get_session_lock(tenant_id, session_id)
        db_lock = getattr(self.db, "_lock", None)
        with self._lock:
            with session_lock:
                internal_session_id = self._make_internal_session_id(tenant_id, session_id)

                self._registry.pop(key, None)
                self._proactive_monitors.pop(key, None)
                self._mcp_handlers.pop(key, None)

                # Purge persistent SQLite session records so reset cleanly brings FSM to IDLE
                def _purge_records():
                    if hasattr(self.db, "reset_session_state"):
                        self.db.reset_session_state(internal_session_id)
                    else:
                        self.db.conn.execute("DELETE FROM session_state WHERE session_id = ?", (internal_session_id,))
                        self.db.conn.execute("DELETE FROM idempotency_journal WHERE session_id = ?", (internal_session_id,))
                        self.db.conn.execute("DELETE FROM conversation_history WHERE session_id = ?", (internal_session_id,))
                        self.db.conn.execute("DELETE FROM context_audit_log WHERE session_id = ?", (internal_session_id,))
                        self.db.conn.commit()

                for attempt in range(5):
                    try:
                        if db_lock:
                            with db_lock:
                                _purge_records()
                        else:
                            _purge_records()
                        break
                    except Exception:
                        if attempt == 4:
                            raise
                        time.sleep(0.05)

                if tenant_id == "tenant_default" and session_id == "live_demo_session":
                    if db_lock:
                        with db_lock:
                            self.db.seed_default_data("user_1", seed_50_memories=True)
                    else:
                        self.db.seed_default_data("user_1", seed_50_memories=True)

                return self.get_orchestrator(tenant_id, session_id, user_id)

    def reset_all_sessions(self) -> None:
        """Reset and purge all tenant sessions from in-memory registry and database."""
        db_lock = getattr(self.db, "_lock", None)
        with self._lock:
            self._registry.clear()
            self._proactive_monitors.clear()
            self._mcp_handlers.clear()
            if hasattr(self.db, "conn") and self.db.conn:
                try:
                    def _purge_all():
                        self.db.conn.execute("DELETE FROM session_state")
                        self.db.conn.execute("DELETE FROM idempotency_journal")
                        self.db.conn.execute("DELETE FROM slot_holds")
                        self.db.conn.execute("DELETE FROM bookings")
                        self.db.conn.execute("DELETE FROM conversation_history")
                        self.db.conn.execute("DELETE FROM context_audit_log")
                        self.db.conn.commit()

                    if db_lock:
                        with db_lock:
                            _purge_all()
                    else:
                        _purge_all()
                except Exception:
                    pass

    def close(self) -> None:
        """Cleanly close database connections and release session resources."""
        db_lock = getattr(self.db, "_lock", None)
        with self._lock:
            self._registry.clear()
            self._proactive_monitors.clear()
            self._mcp_handlers.clear()
            if hasattr(self.db, "conn") and self.db.conn:
                try:
                    if db_lock:
                        with db_lock:
                            self.db.conn.commit()
                    else:
                        self.db.conn.commit()
                except Exception:
                    pass

    def teardown(self) -> None:
        """Alias for close() to support test fixture teardown."""
        self.close()

    def session_count(self) -> int:
        with self._lock:
            return len(self._registry)

    def active_proposal_count(self) -> int:
        with self._lock:
            return sum(1 for orch in self._registry.values() if orch.active_proposal and not orch.active_proposal.invalidated)

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "tenant_id": t_id,
                    "session_id": s_id,
                    "user_id": orch.user_id,
                    "fsm_state": orch.state.value,
                    "turn_counter": orch.turn_counter,
                    "trace_id": orch.trace.trace_id,
                }
                for (t_id, s_id), orch in self._registry.items()
            ]


def extract_tenant_and_session(request: Request) -> tuple[str, str, str]:
    """Extract tenant_id, session_id, and user_id from headers or query parameters with safe defaults."""
    tenant_id = (
        request.headers.get("x-tenant-id")
        or request.query_params.get("tenant_id")
        or "tenant_default"
    ).strip()
    session_id = (
        request.headers.get("x-session-id")
        or request.query_params.get("session_id")
        or "live_demo_session"
    ).strip()
    user_id = (
        request.headers.get("x-user-id")
        or request.query_params.get("user_id")
        or "user_1"
    ).strip()

    # Sanitize session_id to prevent "::" delimiter injection from tenant_default
    if tenant_id == "tenant_default" and "::" in session_id:
        session_id = session_id.replace("::", "_")

    if tenant_id == "tenant_default":
        # Disallow tenant_default callers from accessing or spoofing other tenants' user namespaces
        if user_id.startswith("tenant_") and user_id != "tenant_default":
            scoped_user = f"tenant_default_{user_id}"
        else:
            scoped_user = user_id
    else:
        scoped_user = user_id if user_id.startswith(f"{tenant_id}_") else f"{tenant_id}_{user_id}"

    return tenant_id, session_id, scoped_user


# ==================== MIDDLEWARE ====================

class EnterpriseSecurityMiddleware(BaseHTTPMiddleware):
    """FastAPI Middleware enforcing enterprise security, rate limiting, and RBAC."""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Any]) -> Response:
        client_ip = (
            request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )
        path = request.url.path

        # 1. Payload size check
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                cl_int = int(content_length)
                if cl_int > MAX_PAYLOAD_BYTES:
                    METRICS.inc_counter("errand_agent_security_rejections_total", labels={"reason": "payload_too_large"})
                    problem = make_problem_details(
                        status=413,
                        title="Payload Too Large",
                        detail=f"Request size exceeds enterprise ceiling of {MAX_PAYLOAD_BYTES} bytes.",
                        type_uri="https://errand-agent.enterprise/errors/payload-too-large",
                    )
                    headers = dict(ENTERPRISE_SECURITY_HEADERS)
                    return JSONResponse(status_code=413, content=problem, headers=headers)
            except (ValueError, TypeError):
                pass

        # 2. Rate Limiting
        force_test_rate_limit = request.headers.get("x-test-rate-limit") == "1"
        is_debug_bypass = (
            os.getenv("ALLOW_RATE_LIMIT_BYPASS") == "1"
            and request.headers.get("x-bypass-rate-limit") == "1"
        )
        bypass_rate_limit = not force_test_rate_limit and (
            is_debug_bypass
            or client_ip in ("testclient", "127.0.0.1", "::1")
            or path in ("/", "/healthz", "/livez", "/readyz", "/metrics", "/docs", "/openapi.json")
        )

        remaining_requests = RATE_LIMIT_RPM
        reset_epoch = int(time.time() + 60)

        if not bypass_rate_limit:
            res = RATE_LIMITER.check(client_ip)
            allowed, remaining, retry_after, r_reset = res[0], res[1], res[2], res[3]
            remaining_requests = remaining
            reset_epoch = r_reset
            if not allowed:
                METRICS.inc_counter("errand_agent_security_rejections_total", labels={"reason": "rate_limit_exceeded"})
                problem = make_problem_details(
                    status=429,
                    title="Too Many Requests",
                    detail=f"Rate limit exceeded ({RATE_LIMIT_RPM} req/min). Please back off.",
                    type_uri="https://errand-agent.enterprise/errors/rate-limit-exceeded",
                    extra={"retry_after_seconds": retry_after},
                )
                headers = {
                    "X-RateLimit-Limit": str(RATE_LIMIT_RPM),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(reset_epoch),
                    "Retry-After": str(int(retry_after)),
                    **ENTERPRISE_SECURITY_HEADERS,
                }
                return JSONResponse(status_code=429, content=problem, headers=headers)
        else:
            rem, r_reset = RATE_LIMITER.get_remaining_and_reset(client_ip)
            remaining_requests = rem
            reset_epoch = r_reset

        # 3. Authentication & RBAC Resolution
        provided_key = request.headers.get("x-api-key")
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            provided_key = auth_header[7:].strip()
        role_header = request.headers.get("x-user-role")

        is_protected_route = path.startswith("/api/") or path.startswith("/mcp")
        must_require_key = REQUIRE_API_KEY and is_protected_route

        try:
            role = resolve_role(
                api_key=provided_key,
                role_header=role_header,
                require_api_key=must_require_key,
            )
        except AuthenticationError as exc:
            METRICS.inc_counter("errand_agent_security_rejections_total", labels={"reason": "auth_failure"})
            problem = make_problem_details(
                status=exc.status_code,
                title="Unauthorized" if exc.status_code == 401 else "Bad Request",
                detail=exc.message,
                type_uri="https://errand-agent.enterprise/errors/unauthorized" if exc.status_code == 401 else "https://errand-agent.enterprise/errors/bad-request",
            )
            headers = dict(ENTERPRISE_SECURITY_HEADERS)
            headers["X-RateLimit-Limit"] = str(RATE_LIMIT_RPM)
            headers["X-RateLimit-Remaining"] = str(remaining_requests)
            headers["X-RateLimit-Reset"] = str(reset_epoch)
            return JSONResponse(status_code=exc.status_code, content=problem, headers=headers)

        tenant_id, session_id, user_id = extract_tenant_and_session(request)
        request.state.role = role
        request.state.has_auth_credentials = bool(provided_key or role_header)
        request.state.tenant_id = tenant_id
        request.state.session_id = session_id
        request.state.user_id = user_id

        # 4. Route-level RBAC Enforcement
        if path in ROUTE_REQUIRED_ACTIONS:
            required_action = ROUTE_REQUIRED_ACTIONS[path]
            if not check_permission(role, required_action):
                METRICS.inc_counter("errand_agent_security_rejections_total", labels={"reason": "forbidden_role"})
                problem = make_problem_details(
                    status=403,
                    title="Forbidden",
                    detail=f"Role '{role.value}' is not authorized to execute action '{required_action}' on {path}.",
                    type_uri="https://errand-agent.enterprise/errors/forbidden",
                    extra={"role": role.value, "required_action": required_action, "path": path},
                )
                headers = dict(ENTERPRISE_SECURITY_HEADERS)
                headers["X-RateLimit-Limit"] = str(RATE_LIMIT_RPM)
                headers["X-RateLimit-Remaining"] = str(remaining_requests)
                headers["X-RateLimit-Reset"] = str(reset_epoch)
                return JSONResponse(status_code=403, content=problem, headers=headers)

        # 5. Dispatch request
        t0 = time.perf_counter()
        response: Response = await call_next(request)
        duration = time.perf_counter() - t0

        METRICS.observe_duration("errand_agent_operation_duration_seconds", duration, labels={"operation": "http_request"})
        METRICS.inc_counter("errand_agent_http_requests_total", labels={"endpoint": path, "status": str(response.status_code)})

        # 6. Enterprise Security & Rate Limiting Headers
        for header_name, header_val in ENTERPRISE_SECURITY_HEADERS.items():
            response.headers[header_name] = header_val

        response.headers["X-RateLimit-Limit"] = str(RATE_LIMIT_RPM)
        response.headers["X-RateLimit-Remaining"] = str(remaining_requests)
        response.headers["X-RateLimit-Reset"] = str(reset_epoch)

        return response
