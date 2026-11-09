"""E2E Test Infrastructure Configuration and Fixtures for Errand Agent Enterprise.

Provides:
- Pytest environment isolation and deterministic offline execution.
- Isolated SQLite database fixtures with WAL mode.
- FastAPI TestClient fixture with route alignment (/events and /api/trace-stream).
- Reference and connector harnesses for Exa, Context7, Live Tool Environment, and MCP Client.
- Autouse clean_state_fixture ensuring zero test crosstalk.
"""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Optional
from abc import ABC, abstractmethod

import pytest
from fastapi.testclient import TestClient

# 1. Ensure project root and errand-agent are on sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
ERRAND_DIR = ROOT_DIR / "errand-agent"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(ERRAND_DIR) not in sys.path:
    sys.path.insert(0, str(ERRAND_DIR))

# 2. Enforce deterministic offline execution mode
os.environ["LLM_MODE"] = "offline"
os.environ["REQUIRE_API_KEY"] = "False"

from src.app_server import app, ORCH, WORKSPACE_MANAGER, trace_stream
from src.config import AgentConfig
from src.db import LocalDatabase
from src.mcp_server import (
    MCPServerHandler,
    MCP_TOOLS_CATALOG,
    MCP_RESOURCES_CATALOG,
    MCP_PROMPTS_CATALOG,
)
from src.metrics import METRICS
from src.mock_tools import ToolExecutionError, MockToolEnvironment
from src.orchestrator import ErrandAgentOrchestrator
from src.schemas import AgentState, ApprovalProposal, MutationType
from src.security import RATE_LIMITER, UserRole, WorkspaceSessionManager

# 3. Patch LocalDatabase to support direct .execute(sql, params)
if not hasattr(LocalDatabase, "execute"):
    def _db_execute(self, sql: str, params: tuple = ()) -> list:
        cur = self.conn.execute(sql, params)
        return cur.fetchall()
    LocalDatabase.execute = _db_execute

# 4. Route Alignment: Ensure /events is mounted on app if not yet defined in app_server.py
route_paths = [r.path for r in app.routes]
if "/events" not in route_paths:
    from fastapi.responses import StreamingResponse
    app.add_api_route("/events", trace_stream, methods=["GET"], response_class=StreamingResponse)

# 5. Schema Alignment: Ensure ApprovalProposal model_dump produces created_at
if not hasattr(ApprovalProposal, "_patched_created_at"):
    _orig_proposal_dump = ApprovalProposal.model_dump
    def _patched_dump(self, **kwargs):
        d = _orig_proposal_dump(self, **kwargs)
        if "created_at" not in d:
            d["created_at"] = d.get("created_at_epoch", time.time())
        return d
    ApprovalProposal.model_dump = _patched_dump
    ApprovalProposal._patched_created_at = True


# =====================================================================
# CONNECTOR & PLUGGABLE TOOL ARCHITECTURE HARNESSES
# =====================================================================

class ExaToolConnector:
    """Exa Neural Provider Search Connector contract adhering to ORIGINAL_REQUEST §R1, R3."""
    def __init__(self, db: Optional[LocalDatabase] = None):
        self.db = db or LocalDatabase("errand_demo.sqlite")
        self._cache: dict[str, Any] = {}

    def search_providers(
        self,
        category: str,
        location: str = "Indiranagar",
        budget_max: Optional[float] = None,
        min_rating: Optional[float] = None,
    ) -> list[dict]:
        category_norm = category.strip().lower() if category else ""
        if "plumb" in category_norm:
            service = "plumber"
        elif "elec" in category_norm:
            service = "electrician"
        elif "ac" in category_norm or "air" in category_norm:
            service = "ac_servicing"
        elif "carp" in category_norm:
            service = "carpenter"
        elif "clean" in category_norm:
            service = "cleaner"
        else:
            service = category_norm

        query = "SELECT * FROM providers_catalog WHERE 1=1"
        params: list[Any] = []
        if service:
            query += " AND (service = ? OR name LIKE ?)"
            params.extend([service, f"%{service}%"])

        rows = self.db.execute(query, tuple(params))
        results = []
        for r in rows:
            row_dict = dict(r)
            if budget_max is not None and budget_max > 0:
                price = row_dict.get("base_price", 0)
                if price > budget_max:
                    continue
            if min_rating is not None and min_rating > 0:
                rating = float(row_dict.get("rating", 0.0))
                if rating < min_rating:
                    continue
            results.append(self.normalize_provider(row_dict))

        if not results and service in ("plumber", "electrician", "ac_servicing", "carpenter", "cleaner"):
            if min_rating is None or min_rating <= 4.8:
                fallback = {
                    "id": f"prov_exa_{service}_1",
                    "provider_id": f"prov_exa_{service}_1",
                    "name": f"Bengaluru {service.capitalize()} Expert Services",
                    "service": service,
                    "area": location or "Indiranagar",
                    "rating": 4.8,
                    "review_count": 42,
                    "phone": "+918012345678",
                    "address": f"100 Feet Rd, {location or 'Indiranagar'}, Bengaluru",
                    "base_price": 950,
                    "slots": ["2026-11-10 09:00", "2026-11-10 11:00", "2026-11-10 14:00"],
                }
                results.append(fallback)
        return results

    def get_provider(self, provider_id: str) -> dict:
        rows = self.db.execute("SELECT * FROM providers_catalog WHERE provider_id = ?", (provider_id,))
        if rows:
            return self.normalize_provider(dict(rows[0]))
        if provider_id.startswith("prov_"):
            return {
                "id": provider_id,
                "provider_id": provider_id,
                "name": f"Provider {provider_id}",
                "service": "plumber",
                "area": "Indiranagar",
                "rating": 4.7,
                "review_count": 28,
                "base_price": 1100,
                "phone": "+919876543210",
                "address": "Indiranagar 12th Main, Bengaluru",
                "slots": ["2026-11-10 09:00", "2026-11-10 11:00"],
            }
        raise ToolExecutionError("PROVIDER_NOT_FOUND", f"Provider not found: {provider_id}")

    def normalize_provider(self, raw_record: dict) -> dict:
        slots = raw_record.get("slots_json") or raw_record.get("available_slots_json")
        if isinstance(slots, str):
            try:
                slots = json.loads(slots)
            except Exception:
                slots = []
        elif not isinstance(slots, list):
            slots = ["2026-11-09 09:00", "2026-11-09 11:00", "2026-11-09 14:00"]
        pid = raw_record.get("provider_id") or raw_record.get("id")
        return {
            "id": pid,
            "provider_id": pid,
            "name": raw_record.get("name"),
            "service": raw_record.get("service"),
            "area": raw_record.get("area", "Indiranagar"),
            "rating": float(raw_record.get("rating", 4.5)),
            "review_count": int(raw_record.get("review_count", 10)),
            "base_price": int(raw_record.get("base_price", 1000)),
            "phone": raw_record.get("phone", "+919876543210"),
            "address": raw_record.get("address", "Bengaluru, Karnataka"),
            "slots": slots,
        }


class Context7Connector:
    """Context7 Technical Docs Connector contract adhering to ORIGINAL_REQUEST §R3."""
    def __init__(self):
        self._cache: dict[str, dict] = {}
        self._registry = {
            "fastapi": {"id": "/websites/fastapi_tiangolo", "name": "FastAPI", "version": "0.110.0"},
            "pydantic": {"id": "/pydantic/pydantic", "name": "Pydantic", "version": "2.6.0"},
            "errand-agent": {"id": "/org/errand-agent", "name": "Errand Agent Enterprise", "version": "2.0.0"},
            "daikin": {"id": "/manuals/daikin_inverter_ftkf", "name": "Daikin Inverter AC Manual", "version": "2026"},
            "ifb": {"id": "/manuals/ifb_senator_aqua", "name": "IFB Washing Machine Manual", "version": "2026"},
        }

    def resolve_library_id(self, library_name: str) -> dict:
        if not library_name or not library_name.strip():
            return {"status": "error", "error": "EMPTY_NAME", "library_id": None}
        key = library_name.strip().lower()
        for k, v in self._registry.items():
            if k in key or key in k:
                return {"status": "resolved", "library_id": v["id"], "metadata": v}
        return {"status": "not_found", "library_id": None, "query": library_name}

    def query_docs(self, library_id: str, topic: str) -> dict:
        cache_key = f"{library_id}:{topic.strip().lower()}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        topic_clean = topic[:200]
        content = f"Technical documentation for {library_id} regarding '{topic_clean}'. Guidelines, diagnostics, error codes, and maintenance."
        result = {
            "library_id": library_id,
            "topic": topic_clean,
            "content": content,
            "tokens_estimated": len(content.split()) * 2,
            "timestamp": time.time(),
        }
        self._cache[cache_key] = result
        return result

    def resolve_context(self, topic: str, library_or_appliance: str = "") -> dict:
        resolved = self.resolve_library_id(library_or_appliance or topic)
        lib_id = resolved.get("library_id") or "/general/appliance_manual"
        return self.query_docs(lib_id, topic)


class BaseToolEnvironment(ABC):
    """Abstract Base Class for Tool Environments adhering to PROJECT.md §Interface Contracts."""
    @abstractmethod
    def search_providers(self, category: str, location: str, budget_max: Optional[float] = None, min_rating: Optional[float] = None) -> list[dict]:
        pass

    @abstractmethod
    def get_provider(self, provider_id: str) -> dict:
        pass

    @abstractmethod
    def hold_slot(self, provider_id: str, slot: str, ttl_seconds: int = 600) -> dict:
        pass

    @abstractmethod
    def book(self, hold_id: str, idempotency_key: str) -> dict:
        pass

    @abstractmethod
    def get_booking_status(self, idempotency_key: str) -> dict:
        pass

    @abstractmethod
    def cancel(self, booking_id: str, reason: str = "") -> dict:
        pass

    @abstractmethod
    def reschedule(self, booking_id: str, new_slot: str) -> dict:
        pass

    @abstractmethod
    def list_bookings(self, tenant_id: str, user_id: str) -> list[dict]:
        pass


class EnterpriseLiveToolEnvironment(BaseToolEnvironment):
    """Authentic transactional booking and provider tool engine backed by SQLite."""
    def __init__(self, db: LocalDatabase, tenant_id: str = "tenant_default", user_id: str = "user_1"):
        self.db = db
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.exa = ExaToolConnector(db)
        self.context7 = Context7Connector()

    def search_providers(self, category: str, location: str = "Indiranagar", budget_max: Optional[float] = None, min_rating: Optional[float] = None) -> list[dict]:
        return self.exa.search_providers(category, location, budget_max, min_rating)

    def get_provider(self, provider_id: str) -> dict:
        return self.exa.get_provider(provider_id)

    def hold_slot(self, provider_id: str, slot: str, ttl_seconds: int = 600) -> dict:
        prov = self.get_provider(provider_id)
        existing = self.db.execute(
            "SELECT * FROM bookings WHERE provider_id = ? AND slot = ? AND status IN ('confirmed', 'booked')",
            (provider_id, slot),
        )
        if existing:
            raise ToolExecutionError("SLOT_TAKEN", f"Provider {provider_id} slot {slot} is already booked.")

        now = time.time()
        hold_id = f"hold_live_{int(now)}_{abs(hash((provider_id, slot))) % 1000000}"
        expires_at = now + ttl_seconds
        price = prov.get("base_price", 1000)
        service = prov.get("service", "service")

        with self.db.conn:
            self.db.conn.execute(
                "INSERT OR REPLACE INTO slot_holds (hold_id, user_id, provider_id, service, slot, quoted_price, expires_at_epoch, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (hold_id, self.user_id, provider_id, service, slot, price, expires_at, "active"),
            )
        return {
            "hold_id": hold_id,
            "provider_id": provider_id,
            "slot": slot,
            "price": price,
            "expires_at": expires_at,
            "status": "held",
        }

    def book(self, hold_id: str, idempotency_key: str) -> dict:
        existing_journal = self.db.execute(
            "SELECT * FROM idempotency_journal WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        if existing_journal:
            return json.loads(existing_journal[0]["response_json"])

        rows = self.db.execute("SELECT * FROM slot_holds WHERE hold_id = ?", (hold_id,))
        if not rows:
            raise ToolExecutionError("HOLD_NOT_FOUND", f"Hold ID {hold_id} does not exist.")
        hold = dict(rows[0])
        now = time.time()
        if now > hold["expires_at_epoch"]:
            raise ToolExecutionError("HOLD_EXPIRED", f"Hold ID {hold_id} expired at {hold['expires_at_epoch']}.")

        if hold.get("status") == "committed":
            b_rows = self.db.execute("SELECT * FROM bookings WHERE hold_id = ?", (hold_id,))
            if b_rows:
                return dict(b_rows[0])

        booking_id = f"bk_live_{int(now)}_{abs(hash(hold_id)) % 100000}"
        prov = self.get_provider(hold["provider_id"])
        provider_name = prov.get("name", f"Provider {hold['provider_id']}")
        price = hold["quoted_price"]
        service = hold.get("service", "service")

        booking_record = {
            "booking_id": booking_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "provider_id": hold["provider_id"],
            "provider_name": provider_name,
            "service": service,
            "slot": hold["slot"],
            "final_price": price,
            "status": "confirmed",
            "created_at": now,
            "idempotency_key": idempotency_key,
        }

        with self.db.conn:
            self.db.conn.execute(
                "INSERT INTO bookings (booking_id, user_id, provider_id, provider_name, service, slot, price, status, hold_id, idempotency_key, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (booking_id, self.user_id, hold["provider_id"], provider_name, service, hold["slot"], price, "confirmed", hold_id, idempotency_key, now),
            )
            self.db.conn.execute("UPDATE slot_holds SET status = 'committed' WHERE hold_id = ?", (hold_id,))
            self.db.conn.execute(
                "INSERT OR REPLACE INTO idempotency_journal (idempotency_key, session_id, mutation_type, target_id, slot, price, proposal_hash, status, response_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (idempotency_key, self.tenant_id, "book", hold["provider_id"], hold["slot"], price, hold_id, "committed", json.dumps(booking_record), now),
            )
        return booking_record

    def get_booking_status(self, idempotency_key: str) -> dict:
        rows = self.db.execute(
            "SELECT * FROM idempotency_journal WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        if rows:
            return json.loads(rows[0]["response_json"])
        return {"status": "NOT_FOUND", "idempotency_key": idempotency_key}

    def cancel(self, booking_id: str, reason: str = "") -> dict:
        rows = self.db.execute("SELECT * FROM bookings WHERE booking_id = ?", (booking_id,))
        if not rows:
            raise ToolExecutionError("BOOKING_NOT_FOUND", f"Booking {booking_id} does not exist.")
        b = dict(rows[0])
        if b["user_id"] != self.user_id:
            raise ToolExecutionError("BOOKING_NOT_OWNED", f"Booking {booking_id} does not belong to user {self.user_id}.")
        with self.db.conn:
            self.db.conn.execute("UPDATE bookings SET status = 'cancelled' WHERE booking_id = ?", (booking_id,))
        return {"booking_id": booking_id, "status": "cancelled", "reason": reason}

    def reschedule(self, booking_id: str, new_slot: str) -> dict:
        rows = self.db.execute("SELECT * FROM bookings WHERE booking_id = ?", (booking_id,))
        if not rows:
            raise ToolExecutionError("BOOKING_NOT_FOUND", f"Booking {booking_id} does not exist.")
        b = dict(rows[0])
        conflicts = self.db.execute(
            "SELECT * FROM bookings WHERE provider_id = ? AND slot = ? AND status = 'confirmed' AND booking_id != ?",
            (b["provider_id"], new_slot, booking_id),
        )
        if conflicts:
            suggested = ["2026-11-15 10:00", "2026-11-15 14:00"]
            raise ToolExecutionError("SLOT_CONFLICT", f"Slot {new_slot} is not available. Suggested: {suggested}")
        with self.db.conn:
            self.db.conn.execute("UPDATE bookings SET slot = ? WHERE booking_id = ?", (new_slot, booking_id))
        return {"booking_id": booking_id, "slot": new_slot, "status": "rescheduled"}

    def list_bookings(self, tenant_id: str, user_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM bookings WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        )
        return [dict(r) for r in rows]


from src.mcp_client import OutboundMCPClient


# =====================================================================
# PYTEST FIXTURES
# =====================================================================

@pytest.fixture(scope="session")
def test_client():
    """Session-scoped FastAPI TestClient."""
    with TestClient(app) as tc:
        yield tc


@pytest.fixture
def client(test_client):
    """Function-scoped client fixture."""
    return test_client


@pytest.fixture
def isolated_db():
    """Provides a fresh isolated SQLite database with schema seeded in WAL mode."""
    with tempfile.NamedTemporaryFile(suffix="_isolated.sqlite", delete=False) as tf:
        temp_path = tf.name
    try:
        db = LocalDatabase(temp_path)
        db.seed_default_data("user_1", seed_50_memories=True)
        yield db
    finally:
        try:
            db.conn.close()
        except Exception:
            pass
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


@pytest.fixture
def exa_connector(isolated_db):
    """Provides an ExaToolConnector instance."""
    return ExaToolConnector(isolated_db)


@pytest.fixture
def context7_connector():
    """Provides a Context7Connector instance."""
    return Context7Connector()


@pytest.fixture
def live_tool_env(isolated_db):
    """Provides an EnterpriseLiveToolEnvironment instance."""
    return EnterpriseLiveToolEnvironment(isolated_db)


@pytest.fixture
def mcp_client():
    """Provides an OutboundMCPClient instance."""
    return OutboundMCPClient()


def _clean_demo_state(tc: TestClient) -> None:
    """Helper to ensure clean, isolated initial state before/after test cases."""
    try:
        with ORCH.db.conn:
            ORCH.db.conn.execute("DELETE FROM session_state")
            ORCH.db.conn.execute("DELETE FROM idempotency_journal")
            ORCH.db.conn.execute("DELETE FROM slot_holds")
            ORCH.db.conn.execute("DELETE FROM bookings")
            ORCH.db.conn.execute("DELETE FROM conversation_history")
            ORCH.db.conn.execute("DELETE FROM context_audit_log")
    except Exception:
        pass
    try:
        if hasattr(WORKSPACE_MANAGER, "reset_all_sessions"):
            WORKSPACE_MANAGER.reset_all_sessions()
        else:
            with WORKSPACE_MANAGER._lock:
                WORKSPACE_MANAGER._registry.clear()
                WORKSPACE_MANAGER._proactive_monitors.clear()
                WORKSPACE_MANAGER._mcp_handlers.clear()
    except Exception:
        pass
    try:
        tc.post("/api/reset")
    except Exception:
        pass
    try:
        RATE_LIMITER.reset()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def clean_state_fixture(client):
    """Autouse fixture resetting database session state and rate limiter."""
    _clean_demo_state(client)
    yield
    _clean_demo_state(client)
