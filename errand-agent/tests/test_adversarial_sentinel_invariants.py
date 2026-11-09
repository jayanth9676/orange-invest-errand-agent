"""Adversarial Stress Test Suite for Milestone 1 Reviewer 2:
Sentinel HITL Gatekeeper, Cryptographic SHA-256 Invariants, Drift Detection,
Temporal Affirmation, and RFC-4122 UUIDv5 Idempotency.
"""

import hashlib
import uuid
import pytest

from src.approval_gate import (
    ApprovalGate,
    ApprovalViolationError,
    SentinelGatekeeper,
    compute_idempotency_key,
)
from src.db import LocalDatabase
from src.schemas import (
    AgentState,
    ApprovalProposal,
    MutationType,
    SentinelAction,
    SentinelDecision,
)


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "sentinel_test.sqlite"
    db = LocalDatabase(str(db_file))
    db.seed_default_data("user_1", seed_50_memories=False)
    return db


# =====================================================================
# 1. SENTINEL GATEKEEPER INTERCEPTION CONTRACT
# =====================================================================

def test_sentinel_intercepts_all_consequential_mutations(test_db):
    """Verify Sentinel intercepts book, cancel, reschedule before execution."""
    consequential = ["book", "cancel", "reschedule"]
    for tool_name in consequential:
        decision = SentinelGatekeeper.intercept_tool_call(
            tool_name=tool_name,
            kwargs={"hold_id": "hold_dummy", "booking_id": "bk_dummy", "slot": "2026-11-09 09:00"},
            session_id="sess_test",
            proposal=None,
            current_turn=1,
            db=test_db,
        )
        assert decision.action == SentinelAction.REQUIRE_APPROVAL, f"Tool {tool_name} was not intercepted!"
        assert decision.proposal is not None
        assert decision.proposal.mutation_type.value == tool_name
        assert decision.proposal.proposed_at_turn == 1


def test_sentinel_allows_all_read_and_discovery_tools(test_db):
    """Verify read/discovery tools pass through Sentinel without blocking."""
    read_tools = [
        "search_providers",
        "get_provider",
        "hold_slot",
        "get_booking_status",
        "list_bookings",
    ]
    for tool_name in read_tools:
        decision = SentinelGatekeeper.intercept_tool_call(
            tool_name=tool_name,
            kwargs={"service": "plumber", "area": "Indiranagar"},
            session_id="sess_test",
            proposal=None,
            current_turn=1,
            db=test_db,
        )
        assert decision.action == SentinelAction.ALLOW, f"Read tool {tool_name} was unexpectedly blocked!"
        assert decision.proposal is None


# =====================================================================
# 2. CANONICAL SHA-256 FORMULATION & VALIDATION MATCHES SPECIFICATION
# =====================================================================

def test_canonical_sha256_exact_specification_match():
    """Verify canonical payload hash exactly matches f'{mutation_type.value}|{provider_id.strip()}|{(booking_id or "").strip()}|{slot.strip()}|{int(final_price)}'."""
    mutation_type = MutationType.BOOK
    provider_id = "  prov_plumb_1  "
    booking_id = None
    slot = " 2026-11-09 09:00 "
    final_price = 1100

    expected_canonical = "book|prov_plumb_1||2026-11-09 09:00|1100"
    expected_digest = hashlib.sha256(expected_canonical.encode("utf-8")).hexdigest()

    computed_digest = ApprovalGate.compute_payload_hash(
        mutation_type=mutation_type,
        provider_id=provider_id,
        booking_id=booking_id,
        slot=slot,
        final_price=final_price,
    )
    assert computed_digest == expected_digest


def test_canonical_sha256_price_int_coercion():
    """Verify float or numeric prices are strictly coerced to int without loss of canonical format."""
    digest_int = ApprovalGate.compute_payload_hash(
        MutationType.BOOK, "prov_1", None, "slot_1", 1200
    )
    digest_float = ApprovalGate.compute_payload_hash(
        MutationType.BOOK, "prov_1", None, "slot_1", 1200.0  # type: ignore
    )
    assert digest_int == digest_float


def test_mutation_authorization_blocks_tampered_payload():
    """Verify ApprovalViolationError is raised when tool arguments hash differs from approved proposal."""
    proposal = ApprovalGate.create_proposal(
        session_id="sess_sec",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=1,
    )
    proposal.approved_at_turn = 2

    # Attempt mutation with modified price (₹1,150 instead of ₹1,100)
    with pytest.raises(ApprovalViolationError) as exc_info:
        ApprovalGate.assert_mutation_authorized(
            proposal=proposal,
            mutation_type=MutationType.BOOK,
            provider_id="prov_plumb_1",
            slot="2026-11-09 09:00",
            final_price=1150,
        )
    assert "hash mismatch" in str(exc_info.value).lower()
    assert proposal.invalidated is True


# =====================================================================
# 3. MATERIAL PRICE AND SLOT DRIFT DETECTION
# =====================================================================

def test_price_surge_invalidates_proposal():
    """Verify price increase invalidates proposal and resets approval."""
    proposal = ApprovalGate.create_proposal(
        session_id="sess_sec",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=1,
    )
    proposal.approved_at_turn = 2

    drifted = ApprovalGate.check_material_drift(
        proposal=proposal,
        new_provider_id="prov_plumb_1",
        new_slot="2026-11-09 09:00",
        new_price=1400,
    )
    assert drifted is True
    assert proposal.invalidated is True
    assert proposal.approved_at_turn is None
    assert "Material drift detected" in (proposal.invalidation_reason or "")


def test_price_drop_also_invalidates_proposal():
    """Verify ANY material price deviation (even downward) invalidates approval to enforce zero-trust."""
    proposal = ApprovalGate.create_proposal(
        session_id="sess_sec",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=1,
    )
    proposal.approved_at_turn = 2

    drifted = ApprovalGate.check_material_drift(
        proposal=proposal,
        new_provider_id="prov_plumb_1",
        new_slot="2026-11-09 09:00",
        new_price=900,
    )
    assert drifted is True
    assert proposal.invalidated is True
    assert proposal.approved_at_turn is None


def test_slot_drift_invalidates_proposal():
    """Verify slot deviation invalidates proposal."""
    proposal = ApprovalGate.create_proposal(
        session_id="sess_sec",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=1,
    )
    proposal.approved_at_turn = 2

    drifted = ApprovalGate.check_material_drift(
        proposal=proposal,
        new_provider_id="prov_plumb_1",
        new_slot="2026-11-09 11:00",
        new_price=1100,
    )
    assert drifted is True
    assert proposal.invalidated is True
    assert proposal.approved_at_turn is None


def test_provider_drift_invalidates_proposal():
    """Verify provider swap invalidates proposal."""
    proposal = ApprovalGate.create_proposal(
        session_id="sess_sec",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=1,
    )
    proposal.approved_at_turn = 2

    drifted = ApprovalGate.check_material_drift(
        proposal=proposal,
        new_provider_id="prov_plumb_2",
        new_slot="2026-11-09 09:00",
        new_price=1100,
    )
    assert drifted is True
    assert proposal.invalidated is True
    assert proposal.approved_at_turn is None


# =====================================================================
# 4. TEMPORAL AFFIRMATION (approved_at_turn > proposed_at_turn)
# =====================================================================

def test_same_turn_affirmation_strictly_rejected():
    """Verify user cannot affirm a proposal on the same turn it was proposed."""
    proposal = ApprovalGate.create_proposal(
        session_id="sess_sec",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=1,
    )
    # Attempt affirmation on turn 1
    affirmed = ApprovalGate.evaluate_user_affirmation(proposal, "Yes, confirm", current_turn=1)
    assert affirmed is False
    assert proposal.approved_at_turn is None


def test_earlier_turn_affirmation_strictly_rejected():
    """Verify affirmation on an earlier turn than proposed_at_turn is rejected."""
    proposal = ApprovalGate.create_proposal(
        session_id="sess_sec",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=3,
    )
    affirmed = ApprovalGate.evaluate_user_affirmation(proposal, "Yes, confirm", current_turn=2)
    assert affirmed is False
    assert proposal.approved_at_turn is None


def test_subsequent_turn_affirmation_succeeds():
    """Verify affirmation on turn > proposed_at_turn succeeds."""
    proposal = ApprovalGate.create_proposal(
        session_id="sess_sec",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=1,
    )
    affirmed = ApprovalGate.evaluate_user_affirmation(proposal, "Yes, confirm please", current_turn=2)
    assert affirmed is True
    assert proposal.approved_at_turn == 2


def test_affirmation_with_conflicting_intent_rejected():
    """Verify affirmative words followed by modification intent do NOT affirm."""
    proposal = ApprovalGate.create_proposal(
        session_id="sess_sec",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=1,
    )
    # User says yes but wants a different time
    affirmed = ApprovalGate.evaluate_user_affirmation(
        proposal, "Yes confirm, but change to tomorrow instead", current_turn=2
    )
    assert affirmed is False
    assert proposal.approved_at_turn is None


def test_affirmation_on_invalidated_proposal_rejected():
    """Verify invalidated proposals cannot be affirmed."""
    proposal = ApprovalGate.create_proposal(
        session_id="sess_sec",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=1,
    )
    proposal.invalidated = True
    proposal.invalidation_reason = "Expired or drifted"

    affirmed = ApprovalGate.evaluate_user_affirmation(proposal, "Yes, confirm", current_turn=2)
    assert affirmed is False
    assert proposal.approved_at_turn is None


# =====================================================================
# 5. RFC-4122 UUIDv5 IDEMPOTENCY KEY INVARIANTS
# =====================================================================

def test_uuidv5_idempotency_key_rfc4122_compliance():
    """Verify compute_idempotency_key produces RFC-4122 UUIDv5 prefixed with idem_."""
    session_id = "sess_idem_1"
    mutation_type = "book"
    target_id = "prov_plumb_1"
    slot = "2026-11-09 09:00"
    proposal_hash = "abc123hash"

    key = compute_idempotency_key(session_id, mutation_type, target_id, slot, proposal_hash)
    assert key.startswith("idem_")

    raw_uuid_str = key.replace("idem_", "")
    parsed_uuid = uuid.UUID(raw_uuid_str)
    assert parsed_uuid.version == 5, f"Expected UUID version 5, got version {parsed_uuid.version}"


def test_uuidv5_idempotency_key_determinism():
    """Verify same parameters deterministically produce identical idempotency key across calls."""
    key1 = compute_idempotency_key("sess_1", "book", "prov_1", "slot_1", "hash_1")
    key2 = compute_idempotency_key("sess_1", "book", "prov_1", "slot_1", "hash_1")
    assert key1 == key2


def test_uuidv5_idempotency_key_sensitivity_to_all_parameters():
    """Verify mutating any parameter changes the idempotency key (zero collisions)."""
    base = compute_idempotency_key("sess_1", "book", "prov_1", "slot_1", "hash_1")

    assert compute_idempotency_key("sess_2", "book", "prov_1", "slot_1", "hash_1") != base
    assert compute_idempotency_key("sess_1", "cancel", "prov_1", "slot_1", "hash_1") != base
    assert compute_idempotency_key("sess_1", "book", "prov_2", "slot_1", "hash_1") != base
    assert compute_idempotency_key("sess_1", "book", "prov_1", "slot_2", "hash_1") != base
    assert compute_idempotency_key("sess_1", "book", "prov_1", "slot_1", "hash_2") != base


def test_sentinel_decision_injects_uuidv5_into_modified_kwargs(test_db):
    """Verify SentinelGatekeeper injects the generated UUIDv5 idempotency key into modified_kwargs."""
    proposal = ApprovalGate.create_proposal(
        session_id="sess_sec",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=1,
    )
    proposal.approved_at_turn = 2

    decision = SentinelGatekeeper.intercept_tool_call(
        tool_name="book",
        kwargs={"hold_id": "hold_1", "provider_id": "prov_plumb_1", "slot": "2026-11-09 09:00", "price": 1100},
        session_id="sess_sec",
        proposal=proposal,
        current_turn=2,
        db=test_db,
    )

    assert decision.action == SentinelAction.ALLOW
    assert decision.idempotency_key is not None
    assert decision.idempotency_key.startswith("idem_")
    assert decision.modified_kwargs is not None
    assert decision.modified_kwargs["idempotency_key"] == decision.idempotency_key


# =====================================================================
# 6. ADVERSARIAL EDGE CASES & SECURITY BOUNDARY STRESS-TESTS
# =====================================================================

def test_tampered_payload_hash_intercepted_and_requires_fresh_approval(test_db):
    """Verify that if a proposal's payload_hash is tampered with, SentinelGatekeeper intercepts and requires fresh approval."""
    proposal = ApprovalGate.create_proposal(
        session_id="sess_sec",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=1,
    )
    proposal.approved_at_turn = 2
    # Adversary tampers with the hash
    proposal.payload_hash = "forged_sha256_hash_value"

    decision = SentinelGatekeeper.intercept_tool_call(
        tool_name="book",
        kwargs={"hold_id": "hold_1", "provider_id": "prov_plumb_1", "slot": "2026-11-09 09:00", "price": 1100},
        session_id="sess_sec",
        proposal=proposal,
        current_turn=2,
        db=test_db,
    )
    # Drift detector catches hash mismatch, invalidates approval, and requires fresh approval
    assert decision.action == SentinelAction.REQUIRE_APPROVAL
    assert decision.drift_detected is True
    assert decision.proposal.approved_at_turn is None

    # Directly asserting mutation authorization on the tampered proposal strictly raises ApprovalViolationError
    with pytest.raises(ApprovalViolationError):
        ApprovalGate.assert_mutation_authorized(
            proposal=proposal,
            mutation_type=MutationType.BOOK,
            provider_id="prov_plumb_1",
            slot="2026-11-09 09:00",
            final_price=1100,
        )


def test_material_drift_detection_clears_approval_state(test_db):
    """Verify that when drift is detected during tool interception, old approval is wiped and fresh approval required."""
    proposal = ApprovalGate.create_proposal(
        session_id="sess_sec",
        mutation_type=MutationType.BOOK,
        provider_id="prov_plumb_1",
        slot="2026-11-09 09:00",
        final_price=1100,
        proposed_at_turn=1,
    )
    proposal.approved_at_turn = 2

    # Tool is invoked with price 1300 instead of approved 1100
    decision = SentinelGatekeeper.intercept_tool_call(
        tool_name="book",
        kwargs={"hold_id": "hold_1", "provider_id": "prov_plumb_1", "slot": "2026-11-09 09:00", "price": 1300},
        session_id="sess_sec",
        proposal=proposal,
        current_turn=2,
        db=test_db,
    )
    assert decision.action == SentinelAction.REQUIRE_APPROVAL
    assert decision.drift_detected is True
    assert decision.proposal is not None
    assert decision.proposal.approved_at_turn is None
    assert decision.proposal.final_price == 1300
    assert decision.proposal.original_price == 1100.0
    assert decision.proposal.price_drift_delta == 200.0


def test_uuidv5_idempotency_key_survives_process_restart():
    """Verify idempotency key generation is purely algorithmic and stateless (identical across process restarts)."""
    # Key generated with identical parameters at any time/process must be identical
    key_run1 = compute_idempotency_key("sess_restart", "cancel", "bk_123", "2026-11-14 10:00", "hash_abc")
    key_run2 = compute_idempotency_key("sess_restart", "cancel", "bk_123", "2026-11-14 10:00", "hash_abc")
    assert key_run1 == key_run2
    assert key_run1 == "idem_" + str(uuid.uuid5(uuid.NAMESPACE_OID, "sess_restart|cancel|bk_123|2026-11-14 10:00|hash_abc"))


def test_tool_name_casing_and_whitespace_normalization_gap(test_db):
    """Adversarial check: Does casing variation ('Book', 'Cancel', 'book ') bypass interception?
    Verifies DEF-M1-SENTINEL-01: SentinelGatekeeper checks tool_name with .strip().lower() normalization.
    """
    decision_book = SentinelGatekeeper.intercept_tool_call(
        tool_name="Book",
        kwargs={},
        session_id="sess_casing",
        proposal=None,
        current_turn=1,
        db=test_db,
    )
    assert decision_book.action == SentinelAction.REQUIRE_APPROVAL, "DEF-M1-SENTINEL-01: 'Book' must be normalized and intercepted"
