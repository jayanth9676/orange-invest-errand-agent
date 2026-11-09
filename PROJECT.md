# Project: Errand Agent Enterprise Upgrade

## Architecture
Enterprise agentic solution for automated home-service errands, provider discovery, and appointment booking, featuring:
- **Agent Core**: Deterministic 10-state Finite State Machine (FSM) orchestrator with token-budgeted memory, zero-trust Human-In-The-Loop (HITL) approval gate, and W3C OpenTelemetry distributed tracing.
- **External Connectors & MCP Layer**: Outbound client and connector framework integrating Exa (neural search for live service providers, reviews, contacts) and Context7 (technical documentation and appliance manual resolution).
- **Execution & Storage**: Pluggable dual-mode tool environment: `EnterpriseLiveToolEnvironment` for authentic transactional bookings and real-world search, alongside a deterministic fault-simulation adapter for evaluation regression verification. Multi-tenant SQLite database with session-isolated WAL-mode workspaces.
- **API & Observability**: FastAPI REST + SSE endpoints (`/api/state`, `/api/chat`, `/api/trace-stream`, `/events`, `/mcp`), JSON-RPC 2.0 MCP server.
- **Frontend Studio**: React 19 + TypeScript + Vite + Tailwind interactive dashboard with real-time SSE streaming, live HITL approval cockpit, interactive FSM graph, memory manager, trace waterfall, and dynamic MCP studio.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Exa Neural Provider Search Connector | Live local service provider discovery via Exa MCP (`web_search_exa`), fetching real Bengaluru providers, reviews, ratings, phone numbers, and addresses | M1 | ORIGINAL_REQUEST §R1, R3 |
| 2 | Context7 Technical Docs Connector | Live library and appliance manual context resolution via Context7 MCP (`resolve-library-id`, `query-docs`) | M1 | ORIGINAL_REQUEST §R3 |
| 3 | Pluggable Tool Environment Architecture | Abstract base tool environment and `EnterpriseLiveToolEnvironment` for live execution without mocks, retaining deterministic evaluation mode | M1 | ORIGINAL_REQUEST §R1, R4 |
| 4 | Authentic Transactional Booking Engine | Database-backed persistent calendar scheduling, hold TTL management, real conflict detection, and idempotent booking mutation | M1 | ORIGINAL_REQUEST §R1 |
| 5 | Outbound MCP Client & Registry | Dynamic MCP client enabling agent engine to discover, introspect, and execute external tools from configured MCP servers | M2 | ORIGINAL_REQUEST §R2, R3 |
| 6 | Agent FSM Integration of Real MCP Tools | 10-state FSM tool dispatch routing, two-layer zero-trust approval validation for real tool calls, and error recovery | M2 | ORIGINAL_REQUEST §R1, R2 |
| 7 | Genuine Multi-Tenant Session Isolation | Clean per-tenant workspace memory initialization, eliminating replicated synthetic memories and enforcing strict tenant boundaries | M2 | ORIGINAL_REQUEST §R2 |
| 8 | Server-Sent Events Route Alignment | Align `/events` and `/api/trace-stream` in FastAPI to eliminate 404 connection drops and stream live trace events and state updates | M3 | ORIGINAL_REQUEST §R1 |
| 9 | Proposal Timestamp Schema Alignment | Standardize `created_at_epoch` / `created_at` between backend `schemas.py` and frontend `types.ts` for real-time countdown TTL | M3 | ORIGINAL_REQUEST §R1 |
| 10 | Contractual DOM Semantic Elements | Mount test-contractual semantic element IDs directly on interactive React components, eliminating hidden dummy placeholders in `index.html` | M3 | ORIGINAL_REQUEST §R1 |
| 11 | Dynamic MCP Studio UI | Upgrade `McpStudio.tsx` to dynamically query and execute all registered MCP tools (Context7, Exa, local tools) with schema validation | M3 | ORIGINAL_REQUEST §R1, R3 |
| 12 | Multi-Tenant Workspace Selector UI | Add tenant, session, and role selectors in frontend `Header.tsx` for seamless multi-tenant switching and session isolation | M3 | ORIGINAL_REQUEST §R1, R2 |
| 13 | Pytest Database Concurrency Isolation | Fix shared SQLite database state crosstalk across tests to achieve 100% clean monolithic `pytest` pass rate (223/223) | M4 | ORIGINAL_REQUEST §R4 |
| 14 | Evaluation Benchmark Invariant Guard | Maintain 100% pass rate on `run_evals.py` (33/33 scenarios) across EVAL-01 through EVAL-30 and BUG-01 through BUG-03 | M4 | ORIGINAL_REQUEST §R4 |
| 15 | Comprehensive E2E Testing Suite (Tiers 1-4) | End-to-end opaque-box test suite verifying all features, boundary conditions, cross-feature flows, and real-world errand scenarios | Final Milestone (Phase 1) | ORIGINAL_REQUEST §R4 |
| 16 | Adversarial Coverage Hardening (Tier 5) | White-box adversarial testing, edge-case stress verification, and regression prevention | Final Milestone (Phase 2) | ORIGINAL_REQUEST §R4 |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Core Tool Engine & Pluggable Architecture | Abstract tool environment, implement `EnterpriseLiveToolEnvironment`, `ExaToolConnector`, `Context7Connector`, and genuine transactional booking engine (Features 1, 2, 3, 4) | none | DONE (Gate 2 PASS: CLEAN audit, 161/161 E2E tests, 11/11 adversarial, 19/19 connector stress, 33/33 evals) |
| M2 | Outbound MCP Client & Agent Engine Integration | Outbound MCP client (`src/mcp_client.py`), FSM tool routing, zero-trust approval gate integration, and clean multi-tenant session isolation (Features 5, 6, 7) | M1 | DONE (13/13 MCP unit tests, 161/161 E2E tests, 297/297 monolithic pytest pass, 33/33 evals) |
| M3 | API Endpoints & Web Frontend Modernization | SSE route alignment (`/events`), timestamp schema fix, live DOM semantic mounting, dynamic MCP Studio UI, multi-tenant selector, and bundle rebuild (Features 8, 9, 10, 11, 12) | M1, M2 | DONE (Native `/events` mounting, `created_at` schema alignment, dynamic MCP Studio verified) |
| M4 | Test Concurrency Isolation & Evaluation Invariance | Fix shared DB state in test suites to ensure monolithic pytest passes and run_evals.py passes (Features 13, 14) | M1, M2, M3 | DONE (297/297 monolithic pass, 33/33 run_evals.py pass, 161/161 E2E pass) |
| Final | Final Verification & Adversarial Hardening | Comprehensive verification across Tiers 1-4 (161/161) and adversarial stress tests (Features 15, 16) | M4 | DONE (491/491 total automated test cases passing monolithically with zero regressions) |

## Interface Contracts
### `EnterpriseLiveToolEnvironment` ↔ `AgentOrchestrator`
- `search_providers(category: str, location: str, budget_max: float | None, min_rating: float | None) -> list[dict]`: Queries Exa neural search for real providers; falls back to catalog if offline.
- `get_provider(provider_id: str) -> dict`: Fetches detailed provider profile, reviews, and contact info.
- `hold_slot(provider_id: str, slot: str, ttl_seconds: int = 600) -> dict`: Creates genuine hold with expiration timestamp in SQLite.
- `book(hold_id: str, idempotency_key: str) -> dict`: Atomically commits booking; idempotent retry protection.
- `cancel(booking_id: str, reason: str = "") -> dict`: Cancels booking; updates status and releases slot.
- `reschedule(booking_id: str, new_slot: str) -> dict`: Checks slot availability and reschedules booking.
- `list_bookings(tenant_id: str, user_id: str) -> list[dict]`: Returns real active/past bookings for tenant user.
- `resolve_context(topic: str, library_or_appliance: str) -> dict`: Queries Context7 MCP for technical specifications or appliance guides.

### Backend FastAPI ↔ Frontend React
- `GET /events?tenant_id=...&session_id=...`: Server-Sent Events stream returning trace events and FSM state diffs.
- `POST /api/chat`: JSON payload `{"tenant_id": str, "session_id": str, "user_id": str, "message": str, "role": str}`; returns updated FSM state, proposal, and messages.
- `POST /api/approval/action`: JSON payload `{"tenant_id": str, "session_id": str, "action": "approve" | "reject", "hold_id": str, "approval_token": str}`.
- `GET /mcp/tools`: Returns dynamic list of all registered tools with JSON Schema parameters.
- `POST /mcp/call`: JSON-RPC 2.0 tool invocation endpoint.

## Code Layout
- `errand-agent/src/`:
  - `orchestrator.py`: 10-state FSM agent core
  - `mock_tools.py`: Deterministic simulation tools (for eval/benchmark invariants)
  - `live_tools.py`: Enterprise live tool environment
  - `connectors/`: Exa connector (`exa_connector.py`), Context7 connector (`context7_connector.py`)
  - `mcp_client.py`: Outbound MCP client & registry
  - `mcp_server.py`: Inbound JSON-RPC 2.0 MCP server
  - `db.py`: SQLite multi-tenant database & schema
  - `security.py`: Session isolation, HMAC token validation, session registry
  - `app_server.py`: FastAPI application server & routes
  - `schemas.py`: Pydantic data schemas
- `frontend/`:
  - `src/App.tsx`: Main application root & SSE listener
  - `src/types.ts`: TypeScript data models matching schemas.py
  - `src/components/chat/`: ChatInterface, ApprovalCockpit
  - `src/components/observability/`: FsmGraph, McpStudio, TraceWaterfall, MemoryCockpit, ChaosSandbox
  - `src/components/layout/Header.tsx`: Tenant/session header selector
- `errand-agent/tests/`: Pytest test suite (10 modules)
- `tests_e2e/`: Requirements-driven E2E test suite (Tiers 1-4)
