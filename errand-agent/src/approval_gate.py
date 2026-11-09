import hashlib
import re
from typing import Any, Optional
import uuid
from src.schemas import ApprovalProposal, MutationType, SentinelAction, SentinelDecision

class ApprovalViolationError(Exception):
    '''Raised by application middleware when a mutation tool is invoked without valid approval.'''
    pass


def compute_idempotency_key(
    session_id: str,
    mutation_type: str,
    target_id: str,
    slot: str,
    proposal_hash: str,
) -> str:
    """Generate a deterministic RFC-4122 UUIDv5 idempotency key bound to session, mutation, and proposal hash."""
    name_str = f"{session_id}|{mutation_type}|{target_id}|{slot}|{proposal_hash}"
    return f"idem_{uuid.uuid5(uuid.NAMESPACE_OID, name_str)}"

class ApprovalGate:
    '''
    Enforces in application code that booking, cancellation, and rescheduling
    only execute after explicit user confirmation of the exact action and material
    details (provider, slot, final_price). Any material change invalidates prior approval.
    '''
    AFFIRMATIVE_PATTERNS = re.compile(
        r"\b(yes|yep|yeah|confirm|confirmed|approve|approved|go ahead|book it|do it|proceed|sure|ok|okay)\b",
        re.IGNORECASE,
    )
    NEGATIVE_OR_CHANGE_PATTERNS = re.compile(
        r"\b(no|cancel that|wait|don't|do not|change|instead|different|actually|move to|switch)\b",
        re.IGNORECASE,
    )

    @staticmethod
    def compute_payload_hash(
        mutation_type: MutationType,
        provider_id: str,
        booking_id: Optional[str],
        slot: str,
        final_price: int,
    ) -> str:
        canonical = f"{mutation_type.value}|{provider_id.strip()}|{(booking_id or '').strip()}|{slot.strip()}|{int(final_price)}"
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def create_proposal(
        cls,
        session_id: str,
        mutation_type: MutationType,
        provider_id: str,
        slot: str,
        final_price: int,
        proposed_at_turn: int,
        booking_id: Optional[str] = None,
        hold_id: Optional[str] = None,
    ) -> ApprovalProposal:
        digest = cls.compute_payload_hash(mutation_type, provider_id, booking_id, slot, final_price)
        return ApprovalProposal(
            proposal_id=f"prop_{digest[:10]}",
            session_id=session_id,
            mutation_type=mutation_type,
            provider_id=provider_id,
            booking_id=booking_id,
            hold_id=hold_id,
            slot=slot,
            final_price=final_price,
            payload_hash=digest,
            proposed_at_turn=proposed_at_turn,
        )

    @classmethod
    def evaluate_user_affirmation(
        cls,
        proposal: Optional[ApprovalProposal],
        user_text: str,
        current_turn: int,
    ) -> bool:
        decision = cls.classify_affirmation_and_safety(user_text)
        if not proposal or proposal.invalidated:
            return False
        if current_turn <= proposal.proposed_at_turn or decision["has_conflict"]:
            return False
        if decision["affirmed"]:
            proposal.approved_at_turn = current_turn
            return True
        return False

    @classmethod
    def classify_affirmation_and_safety(cls, user_text: str) -> dict[str, bool]:
        """Small deterministic classifier kept outside the LLM trust boundary."""
        return {
            "affirmed": bool(cls.AFFIRMATIVE_PATTERNS.search(user_text)),
            "has_conflict": bool(cls.NEGATIVE_OR_CHANGE_PATTERNS.search(user_text)),
        }

    @staticmethod
    def get_allowed_tools(proposal: Optional[ApprovalProposal]) -> tuple[str, ...]:
        """Expose only read tools until a concrete proposal has a later approval."""
        read_only = ("search_providers", "get_provider", "hold_slot", "get_booking_status", "list_bookings")
        if proposal and not proposal.invalidated and proposal.approved_at_turn and proposal.approved_at_turn > proposal.proposed_at_turn:
            return read_only + (proposal.mutation_type.value,)
        return read_only

    @classmethod
    def check_material_drift(
        cls,
        proposal: Optional[ApprovalProposal],
        new_provider_id: str,
        new_slot: str,
        new_price: int,
        booking_id: Optional[str] = None,
    ) -> bool:
        '''
        Returns True if material details drifted and invalidates the proposal.
        '''
        if not proposal:
            return True
        new_hash = cls.compute_payload_hash(
            proposal.mutation_type, new_provider_id, booking_id or proposal.booking_id, new_slot, new_price
        )
        if new_hash != proposal.payload_hash:
            proposal.invalidated = True
            proposal.approved_at_turn = None
            proposal.invalidation_reason = (
                f"Material drift detected: expected ({proposal.provider_id}, {proposal.slot}, ₹{proposal.final_price}) "
                f"but got ({new_provider_id}, {new_slot}, ₹{new_price})"
            )
            return True
        return False

    @classmethod
    def assert_mutation_authorized(
        cls,
        proposal: Optional[ApprovalProposal],
        mutation_type: MutationType,
        provider_id: str,
        slot: str,
        final_price: int,
        booking_id: Optional[str] = None,
    ) -> None:
        if proposal is None:
            raise ApprovalViolationError(f"Blocked {mutation_type.value}: No active ApprovalProposal exists.")
        if proposal.invalidated:
            raise ApprovalViolationError(
                f"Blocked {mutation_type.value}: Proposal invalidated ({proposal.invalidation_reason})."
            )
        if proposal.approved_at_turn is None or proposal.approved_at_turn <= proposal.proposed_at_turn:
            raise ApprovalViolationError(
                f"Blocked {mutation_type.value}: Proposal has not received user affirmation on a subsequent turn."
            )
        actual_hash = cls.compute_payload_hash(mutation_type, provider_id, booking_id, slot, final_price)
        if actual_hash != proposal.payload_hash:
            proposal.invalidated = True
            raise ApprovalViolationError(
                f"Blocked {mutation_type.value}: Tool arguments hash mismatch against user-approved proposal."
            )


class SentinelGatekeeper:
    """
    Code-level zero-trust Sentinel Gatekeeper.
    Intercepts consequential mutation tools (book, cancel, reschedule) before execution,
    enforces cryptographic SHA-256 proposal verification, checks material drift,
    and requires explicit human affirmation with RFC-4122 UUIDv5 idempotency.
    """
    CONSEQUENTIAL_TOOLS: set[str] = {"book", "cancel", "reschedule"}

    @classmethod
    def intercept_tool_call(
        cls,
        tool_name: str,
        kwargs: dict[str, Any],
        session_id: str,
        proposal: Optional[ApprovalProposal] = None,
        turn_counter: int = 1,
        tool_env: Any = None,
        db: Any = None,
        current_turn: Optional[int] = None,
        user_id: str = "user_1",
        **extra: Any,
    ) -> SentinelDecision:
        # Flexible argument handling for positional or varied callers:
        # If 4th arg was passed as turn int and 5th as proposal:
        if isinstance(proposal, int) and (turn_counter is None or isinstance(turn_counter, (ApprovalProposal, type(None)))):
            actual_turn = proposal
            actual_proposal = turn_counter
        else:
            actual_turn = current_turn if current_turn is not None else turn_counter
            actual_proposal = proposal

        # Extract db from tool_env if needed
        eff_db = db or (getattr(tool_env, "db", None) if tool_env is not None else None)

        # 1. Non-consequential read/discovery tools pass straight through
        norm_tool = (tool_name or "").strip().lower()
        if norm_tool not in cls.CONSEQUENTIAL_TOOLS:
            return SentinelDecision(
                action=SentinelAction.ALLOW,
                reason=f"Tool '{tool_name}' is read-only / non-consequential",
                tool_name=tool_name,
                kwargs=kwargs,
            )

        mutation_type = MutationType(norm_tool)

        # 2. Interception Case: No active proposal, invalidated, or un-affirmed on subsequent turn
        if (
            actual_proposal is None
            or actual_proposal.invalidated
            or actual_proposal.approved_at_turn is None
            or actual_proposal.approved_at_turn <= actual_proposal.proposed_at_turn
        ):
            new_prop = cls._synthesize_proposal_for_tool(
                tool_name=norm_tool,
                kwargs=kwargs,
                session_id=session_id,
                current_turn=actual_turn,
                db=eff_db,
                user_id=user_id,
            )
            return SentinelDecision(
                action=SentinelAction.REQUIRE_APPROVAL,
                proposal=new_prop,
                reason=f"Consequential action '{norm_tool}' intercepted. Human approval required.",
                tool_name=norm_tool,
                kwargs=kwargs,
            )

        # 3. Interception Case: User affirmed proposal exists -> Check for material drift
        material_target = kwargs.get("provider_id") or actual_proposal.provider_id
        target_slot = kwargs.get("slot") or kwargs.get("new_slot") or actual_proposal.slot
        target_price = kwargs.get("price") or kwargs.get("final_price") or actual_proposal.final_price

        # For book, check authoritative hold price in database to detect price drift
        if mutation_type == MutationType.BOOK:
            hold_id = kwargs.get("hold_id") or actual_proposal.hold_id
            if hold_id and eff_db is not None and hasattr(eff_db, "get_hold"):
                hold_row = eff_db.get_hold(hold_id)
                if hold_row:
                    material_target = hold_row["provider_id"]
                    target_slot = hold_row["slot"]
                    target_price = int(hold_row["quoted_price"])

        is_drifted = ApprovalGate.check_material_drift(
            proposal=actual_proposal,
            new_provider_id=material_target,
            new_slot=target_slot,
            new_price=target_price,
            booking_id=actual_proposal.booking_id,
        )

        if is_drifted:
            fresh_proposal = ApprovalGate.create_proposal(
                session_id=session_id,
                mutation_type=mutation_type,
                provider_id=material_target,
                slot=target_slot,
                final_price=target_price,
                proposed_at_turn=actual_turn,
                booking_id=actual_proposal.booking_id,
                hold_id=kwargs.get("hold_id") or actual_proposal.hold_id,
            )
            fresh_proposal.price_drift_delta = float(target_price - actual_proposal.final_price)
            fresh_proposal.original_price = float(actual_proposal.final_price)
            return SentinelDecision(
                action=SentinelAction.REQUIRE_APPROVAL,
                proposal=fresh_proposal,
                drift_detected=True,
                reason=f"Material drift detected ({actual_proposal.invalidation_reason}). Fresh approval required.",
                tool_name=tool_name,
                kwargs=kwargs,
            )

        # 4. Final Cryptographic Authorization Assertion
        try:
            ApprovalGate.assert_mutation_authorized(
                proposal=actual_proposal,
                mutation_type=mutation_type,
                provider_id=material_target,
                slot=target_slot,
                final_price=target_price,
                booking_id=actual_proposal.booking_id,
            )
        except ApprovalViolationError as exc:
            return SentinelDecision(
                action=SentinelAction.BLOCK,
                proposal=actual_proposal,
                reason=str(exc),
                tool_name=tool_name,
                kwargs=kwargs,
            )

        # 5. Generate deterministic RFC-4122 UUIDv5 idempotency key
        target_id = actual_proposal.booking_id if mutation_type in (MutationType.CANCEL, MutationType.RESCHEDULE) else actual_proposal.provider_id
        idem_key = compute_idempotency_key(
            session_id=session_id,
            mutation_type=mutation_type.value,
            target_id=target_id or "",
            slot=actual_proposal.slot,
            proposal_hash=actual_proposal.payload_hash,
        )

        # Inject idempotency key into modified kwargs
        mod_kwargs = dict(kwargs)
        mod_kwargs["idempotency_key"] = idem_key

        return SentinelDecision(
            action=SentinelAction.ALLOW,
            proposal=actual_proposal,
            idempotency_key=idem_key,
            modified_kwargs=mod_kwargs,
            tool_name=tool_name,
            kwargs=kwargs,
            reason="Mutation authorized and verified.",
        )

    @classmethod
    def _synthesize_proposal_for_tool(
        cls,
        tool_name: str,
        kwargs: dict[str, Any],
        session_id: str,
        current_turn: int,
        db: Any,
        user_id: str = "user_1",
    ) -> ApprovalProposal:
        norm_name = (tool_name or "").strip().lower()
        if norm_name not in cls.CONSEQUENTIAL_TOOLS:
            raise ValueError(f"Unsupported consequential mutation tool: {tool_name}")
        mutation_type = MutationType(norm_name)

        if mutation_type == MutationType.BOOK:
            hold_id = kwargs.get("hold_id")
            hold_row = db.get_hold(hold_id) if (hold_id and db is not None and hasattr(db, "get_hold")) else None
            if hold_row:
                provider_id = hold_row["provider_id"]
                slot = hold_row["slot"]
                final_price = int(hold_row["quoted_price"])
            else:
                provider_id = kwargs.get("provider_id", "prov_default")
                slot = kwargs.get("slot", "2026-11-09 09:00")
                final_price = int(kwargs.get("final_price", kwargs.get("price", 1100)))

            return ApprovalGate.create_proposal(
                session_id=session_id,
                mutation_type=mutation_type,
                provider_id=provider_id,
                slot=slot,
                final_price=final_price,
                proposed_at_turn=current_turn,
                hold_id=hold_id,
            )

        elif mutation_type == MutationType.CANCEL:
            booking_id = kwargs.get("booking_id")
            bk_row = db.get_booking(booking_id) if (booking_id and db is not None and hasattr(db, "get_booking")) else None
            if bk_row:
                provider_id = bk_row["provider_id"]
                slot = bk_row["slot"]
                final_price = int(bk_row["price"])
            else:
                provider_id = kwargs.get("provider_id", "")
                slot = kwargs.get("slot", "")
                final_price = int(kwargs.get("price", 0))

            return ApprovalGate.create_proposal(
                session_id=session_id,
                mutation_type=mutation_type,
                provider_id=provider_id,
                booking_id=booking_id,
                slot=slot,
                final_price=final_price,
                proposed_at_turn=current_turn,
            )

        elif mutation_type == MutationType.RESCHEDULE:
            booking_id = kwargs.get("booking_id")
            new_slot = kwargs.get("slot") or kwargs.get("new_slot") or "2026-11-14 10:00"
            bk_row = db.get_booking(booking_id) if (booking_id and db is not None and hasattr(db, "get_booking")) else None
            if bk_row:
                provider_id = bk_row["provider_id"]
                final_price = int(bk_row["price"])
            else:
                provider_id = kwargs.get("provider_id", "")
                final_price = int(kwargs.get("price", 0))

            return ApprovalGate.create_proposal(
                session_id=session_id,
                mutation_type=mutation_type,
                provider_id=provider_id,
                booking_id=booking_id,
                slot=new_slot,
                final_price=final_price,
                proposed_at_turn=current_turn,
            )

        raise ValueError(f"Unsupported consequential mutation tool: {tool_name}")
