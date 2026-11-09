import time
from typing import Any
from src.approval_gate import ApprovalGate
from src.orchestrator import ErrandAgentOrchestrator
from src.schemas import AgentState, MutationType


class ProactiveCancellationMonitor:
    '''
    Handles asynchronous provider cancellation events without a user prompt:
    1. Detects the cancellation event.
    2. Evaluates whether and how to notify the user based on booking status and stored preferences
       (suppressing duplicate notifications if the booking was already marked PROVIDER_CANCELLED).
    3. Searches for a replacement provider matching known explicit preferences.
    4. Prepares a fresh ApprovalProposal and requires explicit user confirmation before rebooking.
    '''
    def __init__(self, orchestrator: ErrandAgentOrchestrator):
        self.orch = orchestrator

    def handle_provider_cancelled_event(self, booking_id: str) -> dict[str, Any]:
        self.orch._run_tool_calls = 0
        self.orch._run_start_ts = time.perf_counter()

        booking = self.orch.db.get_booking(booking_id)
        if not booking:
            return {"notified": False, "reason": "booking_not_found"}

        # Suppress duplicate notifications if provider cancellation was already processed
        if booking["status"] == "PROVIDER_CANCELLED":
            self.orch.trace.log_guardrail_event(
                "proactive_notification_policy",
                "suppressed_duplicate_alert",
                {"booking_id": booking_id, "should_notify": False, "reason": "already_notified"},
            )
            return {
                "notified": False,
                "reason": "duplicate_cancellation_suppressed",
                "cancelled_booking_id": booking_id,
                "replacement_provider_id": self.orch.active_proposal.provider_id if self.orch.active_proposal else None,
            }

        self.orch.turn_counter += 1

        # Mark booking as cancelled by provider in DB
        self.orch.db.update_booking_status(booking_id, "PROVIDER_CANCELLED", f"prov_cancel_{booking_id}")
        self.orch._transition(
            AgentState.PROACTIVE_RECOVERY,
            f"Detected unprompted provider cancellation for booking {booking_id}",
        )

        # Decide whether and how to notify user from confirmed explicit memories
        mems = self.orch.memory_mgr.inspect_memories(include_unconfirmed_inferred=False)
        mem_map = {m["key"]: m["value"] for m in mems}
        notify_channel = mem_map.get("cancellation_notification_channel", "immediate_chat_alert")
        preferred_area = mem_map.get("preferred_area", "Indiranagar")
        earliest_time = mem_map.get("earliest_acceptable_time", "09:00")

        self.orch.trace.log_guardrail_event(
            "proactive_notification_policy",
            "evaluated_notification_decision",
            {
                "booking_id": booking_id,
                "should_notify": True,
                "channel": notify_channel,
                "preferred_area": preferred_area,
                "earliest_acceptable_time": earliest_time,
            },
        )

        # Find replacement provider within known preferences
        search_res = self.orch._invoke_tool(
            "search_providers",
            service=booking["service"],
            area=preferred_area,
            date_range="this_week",
        )
        replacements = [
            p for p in search_res["providers"]
            if p["provider_id"] != booking["provider_id"]
        ]
        best = replacements[0] if replacements else search_res["providers"][0]

        valid_slots = [s for s in best["available_slots"] if s.split(" ")[1] >= earliest_time]
        replacement_slot = valid_slots[0] if valid_slots else best["available_slots"][0]
        replacement_price = best["price"]

        # Create fresh ApprovalProposal — never auto-book without user approval!
        self.orch.active_proposal = ApprovalGate.create_proposal(
            session_id=self.orch.session_id,
            mutation_type=MutationType.BOOK,
            provider_id=best["provider_id"],
            slot=replacement_slot,
            final_price=replacement_price,
            proposed_at_turn=self.orch.turn_counter,
        )
        self.orch._transition(
            AgentState.AWAITING_APPROVAL,
            f"Proactive replacement {best['provider_id']} proposed; awaiting fresh user approval",
        )

        proactive_message = (
            f"[Proactive Alert via {notify_channel}] Your booked provider ({booking['provider_name']}) "
            f"just cancelled booking {booking_id} for {booking['slot']}. "
            f"Based on your preferences ({preferred_area}, after {earliest_time}), I found a replacement: "
            f"{best['name']} ({best['provider_id']}) on {replacement_slot} at ₹{replacement_price}. "
            f"Please reply 'Yes, confirm' to approve rebooking."
        )
        self.orch.db.conn.execute(
            "INSERT INTO conversation_history (session_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
            (self.orch.session_id, proactive_message, time.time()),
        )
        self.orch._save_session()
        return {
            "notified": True,
            "channel": notify_channel,
            "cancelled_booking_id": booking_id,
            "replacement_provider_id": best["provider_id"],
            "replacement_slot": replacement_slot,
            "replacement_price": replacement_price,
            "message": proactive_message,
        }
