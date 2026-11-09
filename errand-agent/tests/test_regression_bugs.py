'''
Regression tests guarding the 3 real engineering bugs documented in BUGS_POSTMORTEM.md.
Includes both naming conventions from PRD.md and the regression suite.
'''
import json
import unittest
from src.approval_gate import ApprovalGate, ApprovalViolationError
from src.config import AgentConfig
from src.db import LocalDatabase
from src.mock_tools import FaultConfig
from src.orchestrator import ErrandAgentOrchestrator
from src.schemas import AgentState, MutationType
from tests.conftest import record_eval_result


def test_bug1_regression_price_included_in_approval_hash():
    '''
    Bug #1: Early ApprovalGate hashed only (provider_id, slot) without final_price,
    allowing hold_slot price increases to slip past approval check.
    '''
    prop = ApprovalGate.create_proposal(
        session_id="reg_1",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_2",
        slot="2026-11-09 10:00",
        final_price=1250,
        proposed_at_turn=1,
    )
    ApprovalGate.evaluate_user_affirmation(prop, "Yes, confirm", current_turn=2)
    raised = False
    try:
        ApprovalGate.assert_mutation_authorized(
            proposal=prop,
            mutation_type=MutationType.BOOK,
            provider_id="prov_plumb_2",
            slot="2026-11-09 10:00",
            final_price=1450,  # +200 drift must raise ApprovalViolationError
        )
    except ApprovalViolationError:
        raised = True
    assert raised, "Expected ApprovalViolationError on price drift"
    record_eval_result(
        "BUG-01",
        "Regression: Price Drift Invalidates Approval Hash",
        "Raise ApprovalViolationError when final_price drifts from ₹1,250 to ₹1,450",
        "ApprovalViolationError raised deterministically on +₹200 drift",
        True,
        "Guards Bug #1 in BUGS_POSTMORTEM.md",
    )


def test_bug2_regression_deterministic_idempotency_key_across_timeout_retry():
    '''
    Bug #2: Generating a random UUID per retry attempt caused duplicate bookings
    when a timeout occurred. Verify idempotency_key is deterministic and reused.
    '''
    fault = FaultConfig(seed=902, force_book_outcome="timeout_uncommitted")
    db = LocalDatabase(":memory:")
    db.seed_default_data("user_1")
    orch = ErrandAgentOrchestrator(db=db, config=AgentConfig(llm_mode="offline"), fault_cfg=fault, session_id="reg_2")
    orch.handle_user_message("Find a plumber in Indiranagar this week, mornings only, under ₹1,500")
    orch.handle_user_message("Yes, confirm")

    book_events = [e for e in orch.trace.events if e["event_type"] == "tool_call" and e["tool_name"] == "book"]
    assert len(book_events) == 2
    assert book_events[0]["arguments"]["idempotency_key"] == book_events[1]["arguments"]["idempotency_key"]
    record_eval_result(
        "BUG-02",
        "Regression: Deterministic Idempotency Key Across Timeout Retry",
        "Both book() attempts after uncommitted timeout use identical idempotency_key",
        f"Attempt 1 & 2 shared key {book_events[0]['arguments']['idempotency_key']}",
        True,
        "Guards Bug #2 in BUGS_POSTMORTEM.md",
    )


def test_bug3_regression_unconfirmed_inferred_memory_excluded_from_high_keyword_match():
    '''
    Bug #3: Keyword scorer originally ranked all non-forgotten memories before checking
    (source_type == 'INFERRED' and confirmed == 0), leaking unconfirmed weekend habits.
    '''
    db = LocalDatabase(":memory:")
    db.seed_default_data("user_1")
    orch = ErrandAgentOrchestrator(db=db, config=AgentConfig(llm_mode="offline"), session_id="reg_3")
    # Query specifically mentions weekend/time keywords matching mem_inferred_unconfirmed
    orch.handle_user_message("Find a plumber this weekend schedule")
    audit = db.conn.execute("SELECT * FROM context_audit_log ORDER BY turn_id DESC LIMIT 1").fetchone()
    included = json.loads(audit["included_memory_ids_json"])
    assert "mem_inferred_unconfirmed" not in included
    record_eval_result(
        "BUG-03",
        "Regression: Unconfirmed Inferred Memory Excluded Under High Keyword Overlap",
        "Exclude mem_inferred_unconfirmed even when user query matches 'weekend schedule'",
        "mem_inferred_unconfirmed excluded from prompt context",
        True,
        "Guards Bug #3 in BUGS_POSTMORTEM.md",
    )


# Aliases matching PRD.md Section 7.1 exact test names
test_bug1_price_drift_invalidates_approval = test_bug1_regression_price_included_in_approval_hash
test_bug2_timeout_idempotent_recovery = test_bug2_regression_deterministic_idempotency_key_across_timeout_retry
test_bug3_unconfirmed_inferred_never_sent = test_bug3_regression_unconfirmed_inferred_memory_excluded_from_high_keyword_match


class RegressionBugsTestCase(unittest.TestCase):
    def test_all_bug_regressions(self):
        test_bug1_regression_price_included_in_approval_hash()
        test_bug2_regression_deterministic_idempotency_key_across_timeout_retry()
        test_bug3_regression_unconfirmed_inferred_memory_excluded_from_high_keyword_match()
