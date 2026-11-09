"""
Adversarial Sentinel Gatekeeper & Material Drift Empirical Stress Test Suite.
Empirically tests and verifies:
1. Consequential mutation tool bypassing attempts (book, cancel, reschedule without approval).
2. Material drift injection (price surge, slot change, provider change).
3. Payload hash tampering and same-turn temporal affirmation attacks.
4. Deterministic RFC-4122 UUIDv5 idempotency key formulation and repeated confirmation deduplication.
5. ReAct loop code-level interception of rogue model mutation calls.
"""

import json
import time
import uuid
import pytest

from src.approval_gate import (
    ApprovalGate,
    ApprovalViolationError,
    SentinelGatekeeper,
    compute_idempotency_key,
)
from src.config import AgentConfig
from src.db import LocalDatabase
from src.mock_tools import MockToolEnvironment
from src.orchestrator import ErrandAgentOrchestrator
from src.schemas import (
    AgentState,
    AgentTurnResponse,
    ApprovalProposal,
    MutationType,
    SentinelAction,
    ToolCall,
)


# =====================================================================
# 1. DIRECT MUTATION BYPASSING TESTS
# =====================================================================

def test_sentinel_intercepts_unapproved_book_direct_call():
    """Attempt direct book invocation without active proposal."""
    decision = SentinelGatekeeper.intercept_tool_call(
        tool_name="book",
        kwargs={"hold_id": "hold_rogue_99"},
        session_id="test_adv_sess_1",
        proposal=None,
        turn_counter=1,
    )
    assert decision.action == SentinelAction.REQUIRE_APPROVAL
    assert decision.proposal is not None
    assert decision.proposal.mutation_type == MutationType.BOOK
    assert decision.proposal.hold_id == "hold_rogue_99"
    assert "intercepted" in decision.reason.lower()


def test_sentinel_intercepts_unapproved_cancel_direct_call():
    """Attempt direct cancel invocation without active proposal."""
    decision = SentinelGatekeeper.intercept_tool_call(
        tool_name="cancel",
        kwargs={"booking_id": "bk_target_001"},
        session_id="test_adv_sess_2",
        proposal=None,
        turn_counter=1,
    )
    assert decision.action == SentinelAction.REQUIRE_APPROVAL
    assert decision.proposal is not None
    assert decision.proposal.mutation_type == MutationType.CANCEL
    assert decision.proposal.booking_id == "bk_target_001"


def test_sentinel_intercepts_unapproved_reschedule_direct_call():
    """Attempt direct reschedule invocation without active proposal."""
    decision = SentinelGatekeeper.intercept_tool_call(
        tool_name="reschedule",
        kwargs={"booking_id": "bk_target_002", "new_slot": "2026-11-12 14:00"},
        session_id="test_adv_sess_3",
        proposal=None,
        turn_counter=1,
    )
    assert decision.action == SentinelAction.REQUIRE_APPROVAL
    assert decision.proposal is not None
    assert decision.proposal.mutation_type == MutationType.RESCHEDULE
    assert decision.proposal.booking_id == "bk_target_002"
    assert decision.proposal.slot == "2026-11-12 14:00"


def test_sentinel_blocks_same_turn_affirmation_spoof():
    """Proposal cannot be approved in the same turn it was proposed (turn_counter <= proposed_at_turn)."""
    prop = ApprovalGate.create_proposal(
        session_id="test_adv_sess_4",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=2,
    )
    # Attempt to claim approval at turn 2 (same turn as proposed_at_turn)
    prop.approved_at_turn = 2

    decision = SentinelGatekeeper.intercept_tool_call(
        tool_name="book",
        kwargs={"provider_id": "prov_plumb_1", "slot": "2026-11-09 09:00", "final_price": 1100},
        session_id="test_adv_sess_4",
        proposal=prop,
        turn_counter=2,
    )
    assert decision.action == SentinelAction.REQUIRE_APPROVAL, (
        "Sentinel must reject same-turn affirmation and require subsequent turn affirmation."
    )


def test_approval_gate_assert_mutation_authorized_raises_on_unapproved():
    """ApprovalGate.assert_mutation_authorized must strictly raise ApprovalViolationError when unapproved."""
    prop = ApprovalGate.create_proposal(
        session_id="test_adv_sess_5",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=1,
    )
    with pytest.raises(ApprovalViolationError) as exc_info:
        ApprovalGate.assert_mutation_authorized(
            proposal=prop,
            mutation_type=MutationType.BOOK,
            provider_id="prov_plumb_1",
            slot="2026-11-09 09:00",
            final_price=1100,
        )
    assert "subsequent turn" in str(exc_info.value).lower()


def test_approval_gate_assert_mutation_authorized_raises_on_none():
    """ApprovalGate.assert_mutation_authorized must strictly raise ApprovalViolationError on None proposal."""
    with pytest.raises(ApprovalViolationError) as exc_info:
        ApprovalGate.assert_mutation_authorized(
            proposal=None,
            mutation_type=MutationType.BOOK,
            provider_id="prov_plumb_1",
            slot="2026-11-09 09:00",
            final_price=1100,
        )
    assert "no active approvalproposal exists" in str(exc_info.value).lower()


# =====================================================================
# 2. MATERIAL DRIFT EXPLOIT TESTS
# =====================================================================

def test_sentinel_detects_price_drift_and_invalidates_proposal():
    """When quoted price surges between proposal and confirmation, drift is flagged and new proposal generated."""
    db = LocalDatabase(":memory:")
    db.seed_default_data("user_adv", seed_50_memories=False)

    # Insert a hold with initial price 1100
    hold_id = "hold_surge_test_1"
    db.conn.execute(
        "INSERT INTO slot_holds (hold_id, user_id, provider_id, service, slot, quoted_price, expires_at_epoch, status) "
        "VALUES (?, 'user_adv', 'prov_plumb_1', 'plumber', '2026-11-09 09:00', 1100, ?, 'active')",
        (hold_id, time.time() + 600),
    )
    db.conn.commit()

    # User originally approved at price 1100
    prop = ApprovalGate.create_proposal(
        session_id="sess_drift_1",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=1,
        hold_id=hold_id,
    )
    # Approved at turn 2
    ApprovalGate.evaluate_user_affirmation(prop, "Yes, confirm", current_turn=2)
    assert prop.approved_at_turn == 2

    # Now simulate a dynamic price surge: hold price surges to 1500
    db.conn.execute("UPDATE slot_holds SET quoted_price = 1500 WHERE hold_id = ?", (hold_id,))
    db.conn.commit()

    # Intercept tool call during turn 2 confirmation
    decision = SentinelGatekeeper.intercept_tool_call(
        tool_name="book",
        kwargs={"hold_id": hold_id},
        session_id="sess_drift_1",
        proposal=prop,
        turn_counter=2,
        db=db,
    )

    # Must detect drift, invalidate old proposal, and require new approval at updated price
    assert decision.drift_detected is True
    assert decision.action == SentinelAction.REQUIRE_APPROVAL
    assert prop.invalidated is True
    assert "material drift detected" in prop.invalidation_reason.lower()
    assert decision.proposal.final_price == 1500
    assert decision.proposal.price_drift_delta == 400.0
    assert decision.proposal.original_price == 1100.0


def test_sentinel_detects_slot_drift():
    """When slot is modified during execution kwargs, drift is detected."""
    prop = ApprovalGate.create_proposal(
        session_id="sess_drift_2",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=1,
    )
    ApprovalGate.evaluate_user_affirmation(prop, "Yes, confirm", current_turn=2)

    # kwargs has drifted slot
    decision = SentinelGatekeeper.intercept_tool_call(
        tool_name="book",
        kwargs={"provider_id": "prov_plumb_1", "slot": "2026-11-09 11:00", "final_price": 1100},
        session_id="sess_drift_2",
        proposal=prop,
        turn_counter=2,
    )
    assert decision.drift_detected is True
    assert decision.action == SentinelAction.REQUIRE_APPROVAL
    assert prop.invalidated is True
    assert "material drift detected" in prop.invalidation_reason.lower()


def test_sentinel_detects_provider_drift():
    """When provider is modified, drift is detected."""
    prop = ApprovalGate.create_proposal(
        session_id="sess_drift_3",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=1,
    )
    ApprovalGate.evaluate_user_affirmation(prop, "Yes, confirm", current_turn=2)

    decision = SentinelGatekeeper.intercept_tool_call(
        tool_name="book",
        kwargs={"provider_id": "prov_plumb_2", "slot": "2026-11-09 09:00", "final_price": 1100},
        session_id="sess_drift_3",
        proposal=prop,
        turn_counter=2,
    )
    assert decision.drift_detected is True
    assert decision.action == SentinelAction.REQUIRE_APPROVAL
    assert prop.invalidated is True


def test_hash_tampering_attack_is_blocked():
    """If payload_hash in the proposal is tampered with, SentinelGatekeeper and assert_mutation_authorized block."""
    prop = ApprovalGate.create_proposal(
        session_id="sess_tamper_1",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=1,
    )
    ApprovalGate.evaluate_user_affirmation(prop, "Yes, confirm", current_turn=2)

    # Adversary tampers with the hash
    prop.payload_hash = "deadbeef" * 8

    # 1. Gatekeeper detects mismatch, invalidates proposal and requires fresh approval
    decision = SentinelGatekeeper.intercept_tool_call(
        tool_name="book",
        kwargs={"provider_id": "prov_plumb_1", "slot": "2026-11-09 09:00", "final_price": 1100},
        session_id="sess_tamper_1",
        proposal=prop,
        turn_counter=2,
    )
    assert decision.action == SentinelAction.REQUIRE_APPROVAL
    assert decision.drift_detected is True
    assert prop.invalidated is True

    # 2. Direct assert_mutation_authorized strictly raises ApprovalViolationError on tampered hash
    with pytest.raises(ApprovalViolationError) as exc_info:
        ApprovalGate.assert_mutation_authorized(
            proposal=prop,
            mutation_type=MutationType.BOOK,
            provider_id="prov_plumb_1",
            slot="2026-11-09 09:00",
            final_price=1100,
        )
    assert "invalidated" in str(exc_info.value).lower() or "mismatch" in str(exc_info.value).lower()


# =====================================================================
# 3. DETERMINISTIC RFC-4122 UUIDv5 IDEMPOTENCY KEY TESTS
# =====================================================================

def test_idempotency_key_is_strictly_deterministic_rfc4122_uuidv5():
    """Verify compute_idempotency_key generates deterministic RFC-4122 UUIDv5."""
    session_id = "sess_idem_test"
    mutation_type = "book"
    target_id = "prov_plumb_1"
    slot = "2026-11-09 09:00"
    proposal_hash = "abc123hash"

    key1 = compute_idempotency_key(session_id, mutation_type, target_id, slot, proposal_hash)
    key2 = compute_idempotency_key(session_id, mutation_type, target_id, slot, proposal_hash)

    # 1. Must be 100% deterministic
    assert key1 == key2
    assert key1.startswith("idem_")

    # 2. Extract UUID and verify RFC-4122 UUIDv5 properties
    raw_uuid_str = key1.replace("idem_", "")
    parsed_uuid = uuid.UUID(raw_uuid_str)
    assert parsed_uuid.version == 5, f"Expected UUIDv5, got version {parsed_uuid.version}"
    assert parsed_uuid.variant == uuid.RFC_4122, f"Expected RFC_4122 variant, got {parsed_uuid.variant}"

    # 3. Mutating any single component must generate a different key (high entropy)
    key_diff_session = compute_idempotency_key("other_sess", mutation_type, target_id, slot, proposal_hash)
    key_diff_slot = compute_idempotency_key(session_id, mutation_type, target_id, "2026-11-09 10:00", proposal_hash)
    key_diff_hash = compute_idempotency_key(session_id, mutation_type, target_id, slot, "diffhash")

    assert key_diff_session != key1
    assert key_diff_slot != key1
    assert key_diff_hash != key1


def test_repeated_confirmation_orchestrator_deduplication():
    """Verify orchestrator repeated confirmation deduplication produces zero duplicate bookings."""
    db = LocalDatabase(":memory:")
    db.seed_default_data("user_1", seed_50_memories=False)
    tools = MockToolEnvironment(db=db)
    config = AgentConfig()
    config.llm_mode = "offline"  # Deterministic evaluation processing
    orch = ErrandAgentOrchestrator(
        session_id="sess_dedup_test",
        user_id="user_1",
        db=db,
        tools=tools,
        config=config,
    )

    # Turn 1: Search & propose
    r1 = orch.handle_user_message("Find a plumber in Indiranagar this week, mornings only, under ₹1,500")
    assert orch.state == AgentState.AWAITING_APPROVAL
    assert orch.active_proposal is not None

    # Count bookings before confirmation
    bk_before = len(db.conn.execute("SELECT * FROM bookings WHERE user_id = 'user_1'").fetchall())

    # Turn 2: Confirm booking
    r2 = orch.handle_user_message("Yes, confirm")
    assert orch.state == AgentState.COMPLETED
    assert "confirmed" in r2.lower()

    # Verify exactly 1 new booking added to database
    bk_rows_turn2 = db.conn.execute("SELECT * FROM bookings WHERE user_id = 'user_1'").fetchall()
    assert len(bk_rows_turn2) == bk_before + 1
    # Find the new booking (the one not present in initial seed)
    new_booking = [b for b in bk_rows_turn2 if b["provider_id"] == "prov_plumb_1"][0]
    booking_id = new_booking["booking_id"]
    assert booking_id in r2

    # Turn 3: Repeated confirmation
    r3 = orch.handle_user_message("Yes, confirm")
    assert "already confirmed" in r3.lower()
    assert booking_id in r3

    # Turn 4: Repeated affirmation with alternate phrase "sure, go ahead"
    r4 = orch.handle_user_message("sure, go ahead")
    assert "already confirmed" in r4.lower()

    # Turn 5: Repeated affirmation with "ok, do it"
    r5 = orch.handle_user_message("ok, do it")
    assert "already confirmed" in r5.lower()

    # Verify count in database has NOT grown (zero duplicates created across 3 repeated confirmations)
    bk_rows_final = db.conn.execute("SELECT * FROM bookings WHERE user_id = 'user_1'").fetchall()
    assert len(bk_rows_final) == bk_before + 1
    assert [b["booking_id"] for b in bk_rows_final if b["status"] == "CONFIRMED"].count(booking_id) == 1


# =====================================================================
# 4. REACT LOOP ROGUE MUTATION INTERCEPTION
# =====================================================================

def test_react_loop_intercepts_unapproved_model_mutation():
    """
    Simulate a model emitting a book() tool call directly in ReAct mode.
    SentinelGatekeeper must intercept the call and transition to AWAITING_APPROVAL.
    """
    db = LocalDatabase(":memory:")
    db.seed_default_data("user_1", seed_50_memories=False)
    tools = MockToolEnvironment(db=db)
    config = AgentConfig()
    config.enable_react_loop = True
    config.llm_mode = "live"

    orch = ErrandAgentOrchestrator(
        session_id="sess_react_rogue",
        user_id="user_1",
        db=db,
        tools=tools,
        config=config,
    )

    bk_count_before = len(db.conn.execute("SELECT * FROM bookings").fetchall())

    # Mock llm.run_agentic_turn_with_cascade to simulate a rogue model calling book() directly
    def mock_rogue_turn(*args, **kwargs):
        return AgentTurnResponse(
            thought="I will book immediately without asking the user.",
            tool_calls=[
                ToolCall(
                    id="call_rogue_1",
                    name="book",
                    arguments={"hold_id": "hold_unapproved", "provider_id": "prov_plumb_1", "slot": "2026-11-09 09:00", "final_price": 1100},
                )
            ],
            provider_used="mock_gemini",
        )

    orch.llm.run_agentic_turn_with_cascade = mock_rogue_turn

    reply = orch.handle_user_message("Book me a plumber now.")

    # Sentinel must intercept and require approval
    assert orch.state == AgentState.AWAITING_APPROVAL
    assert orch.active_proposal is not None
    assert orch.active_proposal.mutation_type == MutationType.BOOK
    assert "please reply 'yes, confirm' to approve booking" in reply.lower()

    # Verify no new booking was created in database
    bk_rows = db.conn.execute("SELECT * FROM bookings").fetchall()
    assert len(bk_rows) == bk_count_before
    assert not any(b["hold_id"] == "hold_unapproved" for b in bk_rows)


def test_schema_gating_restricts_tools_when_no_active_proposal():
    """Verify ApprovalGate.get_allowed_tools exposes only read tools when unapproved."""
    allowed = ApprovalGate.get_allowed_tools(None)
    assert "search_providers" in allowed
    assert "get_provider" in allowed
    assert "hold_slot" in allowed
    assert "get_booking_status" in allowed
    assert "list_bookings" in allowed
    assert "book" not in allowed
    assert "cancel" not in allowed
    assert "reschedule" not in allowed


def test_react_loop_repeated_confirmation_deduplication():
    """Verify ReAct loop deduplicates repeated confirmation when COMPLETED with active proposal."""
    db = LocalDatabase(":memory:")
    db.seed_default_data("user_1", seed_50_memories=False)
    tools = MockToolEnvironment(db=db)
    config = AgentConfig()
    config.enable_react_loop = True
    config.llm_mode = "live"

    orch = ErrandAgentOrchestrator(
        session_id="sess_react_dedup",
        user_id="user_1",
        db=db,
        tools=tools,
        config=config,
    )

    # Establish an active proposal and completed journal entry
    prop = ApprovalGate.create_proposal(
        session_id="sess_react_dedup",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=1,
    )
    orch.active_proposal = prop
    orch.state = AgentState.COMPLETED

    # Insert idempotency journal entry representing the committed booking
    idem_key = compute_idempotency_key(
        "sess_react_dedup", "book", "prov_plumb_1", "2026-11-09 09:00", prop.payload_hash
    )
    db.begin_mutation(
        idempotency_key=idem_key,
        session_id="sess_react_dedup",
        mutation_type="book",
        target_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        price=1100,
        proposal_hash=prop.payload_hash,
    )
    db.complete_mutation(idem_key, {"booking_id": "bk_dedup_001", "status": "CONFIRMED"})

    # Send repeated confirmation in ReAct mode
    reply1 = orch.handle_user_message("Yes, confirm")
    assert "already confirmed" in reply1.lower()
    assert "bk_dedup_001" in reply1

    reply2 = orch.handle_user_message("Confirm please")
    assert "already confirmed" in reply2.lower()
    assert "bk_dedup_001" in reply2


def test_repeated_confirmation_cancel_deduplication():
    """Verify cancellation repeated confirmations are cleanly deduplicated."""
    db = LocalDatabase(":memory:")
    db.seed_default_data("user_1", seed_50_memories=False)
    tools = MockToolEnvironment(db=db)
    config = AgentConfig()
    orch = ErrandAgentOrchestrator(
        session_id="sess_cancel_dedup",
        user_id="user_1",
        db=db,
        tools=tools,
        config=config,
    )

    prop = ApprovalGate.create_proposal(
        session_id="sess_cancel_dedup",
        mutation_type=MutationType.CANCEL,
        provider_id="prov_ac_1",
        slot="2026-11-14 10:00",
        final_price=1800,
        proposed_at_turn=1,
        booking_id="bk_upcoming_ac_01",
    )
    orch.active_proposal = prop
    orch.state = AgentState.COMPLETED

    idem_key = compute_idempotency_key(
        "sess_cancel_dedup", "cancel", "bk_upcoming_ac_01", "2026-11-14 10:00", prop.payload_hash
    )
    db.begin_mutation(
        idempotency_key=idem_key,
        session_id="sess_cancel_dedup",
        mutation_type="cancel",
        target_id="bk_upcoming_ac_01",
        slot="2026-11-14 10:00",
        price=1800,
        proposal_hash=prop.payload_hash,
    )
    db.complete_mutation(idem_key, {"booking_id": "bk_upcoming_ac_01", "status": "CANCELLED"})

    reply = orch.handle_user_message("Yes, confirm cancel")
    assert "already cancelled" in reply.lower()
    assert "bk_upcoming_ac_01" in reply


def test_repeated_confirmation_reschedule_deduplication():
    """Verify rescheduling repeated confirmations are cleanly deduplicated."""
    db = LocalDatabase(":memory:")
    db.seed_default_data("user_1", seed_50_memories=False)
    tools = MockToolEnvironment(db=db)
    config = AgentConfig()
    orch = ErrandAgentOrchestrator(
        session_id="sess_resched_dedup",
        user_id="user_1",
        db=db,
        tools=tools,
        config=config,
    )

    prop = ApprovalGate.create_proposal(
        session_id="sess_resched_dedup",
        mutation_type=MutationType.RESCHEDULE,
        provider_id="prov_ac_1",
        slot="2026-11-16 11:00",
        final_price=1800,
        proposed_at_turn=1,
        booking_id="bk_upcoming_ac_01",
    )
    orch.active_proposal = prop
    orch.state = AgentState.COMPLETED

    idem_key = compute_idempotency_key(
        "sess_resched_dedup", "reschedule", "bk_upcoming_ac_01", "2026-11-16 11:00", prop.payload_hash
    )
    db.begin_mutation(
        idempotency_key=idem_key,
        session_id="sess_resched_dedup",
        mutation_type="reschedule",
        target_id="bk_upcoming_ac_01",
        slot="2026-11-16 11:00",
        price=1800,
        proposal_hash=prop.payload_hash,
    )
    db.complete_mutation(idem_key, {"booking_id": "bk_upcoming_ac_01", "new_slot": "2026-11-16 11:00", "status": "CONFIRMED"})

    reply = orch.handle_user_message("Yes, confirm")
    assert "already rescheduled" in reply.lower()
    assert "bk_upcoming_ac_01" in reply


