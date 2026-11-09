# E2E Test Infra: Errand Agent Enterprise

## Test Philosophy
- Opaque-box, requirement-driven. Derived strictly from `ORIGINAL_REQUEST.md` and user-facing specifications.
- Methodology: Category-Partition + Boundary Value Analysis (BVA) + Pairwise Combinatorial Testing + Real-World Workload Testing.
- Progressive testability: Tests run against public APIs, CLI, or HTTP endpoints without internal module dependencies.

## Feature Inventory
| # | Feature | Source (requirement) | Tier 1 | Tier 2 | Tier 3 |
|---|---------|---------------------|:------:|:------:|:------:|
| 1 | Exa Neural Provider Search Connector | ORIGINAL_REQUEST §R1, R3 | 5 | 5 | ✓ |
| 2 | Context7 Technical Docs Connector | ORIGINAL_REQUEST §R3 | 5 | 5 | ✓ |
| 3 | Pluggable Tool Environment Architecture | ORIGINAL_REQUEST §R1, R4 | 5 | 5 | ✓ |
| 4 | Authentic Transactional Booking Engine | ORIGINAL_REQUEST §R1 | 5 | 5 | ✓ |
| 5 | Outbound MCP Client & Registry | ORIGINAL_REQUEST §R2, R3 | 5 | 5 | ✓ |
| 6 | Agent FSM Integration of Real MCP Tools | ORIGINAL_REQUEST §R1, R2 | 5 | 5 | ✓ |
| 7 | Genuine Multi-Tenant Session Isolation | ORIGINAL_REQUEST §R2 | 5 | 5 | ✓ |
| 8 | Server-Sent Events Route Alignment | ORIGINAL_REQUEST §R1 | 5 | 5 | ✓ |
| 9 | Proposal Timestamp Schema Alignment | ORIGINAL_REQUEST §R1 | 5 | 5 | ✓ |
| 10 | Contractual DOM Semantic Elements | ORIGINAL_REQUEST §R1 | 5 | 5 | ✓ |
| 11 | Dynamic MCP Studio UI | ORIGINAL_REQUEST §R1, R3 | 5 | 5 | ✓ |
| 12 | Multi-Tenant Workspace Selector UI | ORIGINAL_REQUEST §R1, R2 | 5 | 5 | ✓ |
| 13 | Pytest Database Concurrency Isolation | ORIGINAL_REQUEST §R4 | 5 | 5 | ✓ |
| 14 | Evaluation Benchmark Invariant Guard | ORIGINAL_REQUEST §R4 | 5 | 5 | ✓ |

## Test Architecture
- **Test Runner**: Pytest-based E2E runner (`pytest tests_e2e -v`) and verification harness.
- **Test Suite Location**: `tests_e2e/`
  - `tests_e2e/test_tier1_features.py`: Happy-path feature verification in isolation (>= 5 per feature).
  - `tests_e2e/test_tier2_boundaries.py`: Extreme values, TTL expiration, invalid inputs, edge cases (>= 5 per feature).
  - `tests_e2e/test_tier3_pairwise.py`: Cross-feature combinatorial interactions (search + book, MCP + approval, multi-tenancy + SSE).
  - `tests_e2e/test_tier4_workloads.py`: End-to-end real-world user errand scenarios.
- **Pass/Fail Semantics**: All test suites must exit with return code 0, 0 failures, 0 regressions.

## Real-World Application Scenarios (Tier 4)
| # | Scenario | Features Exercised | Complexity |
|---|----------|--------------------|------------|
| 1 | Emergency Plumbing Errand with Live Exa Search & Approval | F1, F3, F4, F6, F8, F9, F10 | High |
| 2 | Appliance Manual Lookup via Context7 & Rescheduling | F2, F3, F4, F6, F11 | High |
| 3 | Multi-Tenant Concurrent Errand Booking with Strict Isolation | F4, F7, F12, F13 | High |
| 4 | Price Drift Invalidation & Material Re-Approval Lifecycle | F3, F4, F6, F8, F9, F10 | High |
| 5 | Dynamic MCP Tool Discovery and Direct Studio Execution | F1, F2, F5, F11 | Medium |
| 6 | Full Evaluation Regression Run Invariance | F3, F13, F14 | Medium |
| 7 | Proactive Cancellation & Slot Reclaim Workflow | F3, F4, F6, F8 | Medium |

## Coverage Thresholds
- Tier 1: ≥5 per feature (14 features × 5 = 70 test cases)
- Tier 2: ≥5 per feature (14 features × 5 = 70 test cases)
- Tier 3: pairwise coverage of major feature pairs (≥ 14 test cases)
- Tier 4: ≥7 realistic application scenarios
- **Total Minimum Target: ~161 E2E test cases**
