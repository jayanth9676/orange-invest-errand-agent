"""Deterministic offline NLU / Scripted LLM engine used for reproducible
offline evaluation runs (LLM_MODE=offline).
Extracts intent_type, slot_delta, memory_op, repeat_last_provider, and
bypass_approval_attempt strictly from user input without any network calls.
"""

import re
from typing import Any


class ScriptedEvalLLM:
    """Deterministic offline NLU / Scripted LLM engine."""

    @staticmethod
    def parse(user_msg: str, system_msg: str = "") -> dict[str, Any]:
        text = user_msg.strip()
        lower = text.lower()
        result: dict[str, Any] = {
            "intent_type": None,
            "slot_delta": {},
            "memory_op": None,
            "repeat_last_provider": False,
            "bypass_approval_attempt": False,
        }

        # 1. Detect adversarial jailbreak / skip approval attempt
        if re.search(r"(skip approval|without approval|without asking|force book|ignore rules)", lower):
            result["bypass_approval_attempt"] = True

        # 2. Detect memory management commands
        if "what do you remember" in lower or "inspect memor" in lower or "show my preferences" in lower:
            result["intent_type"] = "memory_op"
            result["memory_op"] = {"action": "inspect"}
            return result

        if lower.startswith("forget ") or "forget my " in lower:
            target = re.sub(r"^.*forget (my )?", "", lower).strip(" .!")
            result["intent_type"] = "memory_op"
            result["memory_op"] = {"action": "forget", "key": target}
            return result

        if "remember that my preferred area is" in lower or "change my preferred area to" in lower:
            m = re.search(r"(?:is|to)\s+([A-Za-z0-9_ -]+)", text, re.IGNORECASE)
            val = m.group(1).strip(" .!") if m else "Koramangala"
            result["intent_type"] = "memory_op"
            result["memory_op"] = {"action": "update", "key": "preferred_area", "value": val}
            return result

        if "confirm inferred preference" in lower:
            result["intent_type"] = "memory_op"
            result["memory_op"] = {"action": "confirm", "key": "inferred_weekend_preference"}
            return result

        # 3. Detect cancel, reschedule, or list_bookings intents
        if re.search(r"\bcancel\b.*\b(booking|ac|servicing|appointment|bk_)\b", lower) or lower.startswith("cancel "):
            result["intent_type"] = "cancel"
        elif re.search(r"\b(move|reschedule)\b", lower):
            result["intent_type"] = "reschedule"
        elif re.search(r"\b(list|show|what are|view|check)\b.*\bbookings?\b", lower) or "upcoming bookings" in lower or "my bookings" in lower:
            result["intent_type"] = "list_bookings"

        # 4. Detect service
        if "plumber" in lower or "kitchen tap" in lower or "pipe" in lower:
            result["slot_delta"]["service"] = "plumber"
        elif "electrician" in lower or "wiring" in lower:
            result["slot_delta"]["service"] = "electrician"
        elif "ac " in lower or "ac servicing" in lower or "air condition" in lower or lower.endswith("ac"):
            result["slot_delta"]["service"] = "ac_servicing"

        # 5. Detect repeat provider reference ("same electrician as last time")
        if "same " in lower and "last time" in lower:
            result["repeat_last_provider"] = True

        # 6. Detect area
        for area_name in ["Indiranagar", "Koramangala", "Whitefield", "HSR Layout", "Jayanagar"]:
            if area_name.lower() in lower:
                result["slot_delta"]["area"] = area_name

        # 7. Detect date window
        if "this week" in lower:
            result["slot_delta"]["date_window"] = "this_week"
        elif "saturday" in lower:
            result["slot_delta"]["date_window"] = "Saturday (2026-11-14)"
        elif "sunday" in lower:
            result["slot_delta"]["date_window"] = "Sunday (2026-11-15)"
        elif "monday" in lower:
            result["slot_delta"]["date_window"] = "2026-11-09"
        elif "tuesday" in lower:
            result["slot_delta"]["date_window"] = "2026-11-10"

        # 8. Detect time window / constraints
        if "mornings only" in lower or "morning" in lower:
            result["slot_delta"]["time_window"] = "morning (08:00-12:00)"
        elif "afternoon" in lower:
            result["slot_delta"]["time_window"] = "afternoon (12:00-17:00)"
        elif "evening" in lower:
            result["slot_delta"]["time_window"] = "evening (17:00-21:00)"
        if "not before 10" in lower or "after 10" in lower:
            result["slot_delta"]["time_window"] = "after 10:00"
            result["slot_delta"]["preferences"] = {"earliest_time": "10:00"}

        # 9. Detect explicit slot timestamp with date and time
        slot_match = re.search(r"(2026-11-\d{2})(?:\s+at|\s+)?\s+(\d{2}:\d{2})", text)
        if slot_match:
            date_str = slot_match.group(1)
            time_str = slot_match.group(2)
            result["slot_delta"]["requested_slot"] = f"{date_str} {time_str}"
            result["slot_delta"]["date_window"] = date_str
        elif "saturday 10:00" in lower or "saturday at 10" in lower:
            result["slot_delta"]["requested_slot"] = "2026-11-14 10:00"
        elif "saturday 14:00" in lower or "saturday at 2" in lower or "14:00" in lower:
            result["slot_delta"]["requested_slot"] = "2026-11-14 14:00"
        elif "2026-11-09" in lower:
            result["slot_delta"]["date_window"] = "2026-11-09"
        elif "2026-11-10" in lower:
            result["slot_delta"]["date_window"] = "2026-11-10"

        # 10. Detect budget (e.g. "under 1,500", "under ₹1500", "budget 2000", "change my budget to under ₹2,000")
        budget_match = re.search(
            r"(?:under|budget(?:\s+to)?|below|max|₹|rs\.?\s*)\s*(?:[₹$]|rs\.?\s*)?\s*([0-9,]{3,6})",
            lower,
        )
        if budget_match:
            val_str = budget_match.group(1).replace(",", "")
            if val_str.isdigit():
                result["slot_delta"]["max_budget"] = int(val_str)

        # 11. Detect explicit booking ID if present
        bk_match = re.search(r"\b(bk_[a-zA-Z0-9_]+)\b", text)
        if bk_match:
            result["slot_delta"]["target_booking_id"] = bk_match.group(1)

        # 12. Detect explicit provider choice by ID or name
        prov_match = re.search(r"(prov_[a-z]+_\d+)", lower)
        if prov_match:
            result["slot_delta"]["preferred_provider_id"] = prov_match.group(1)
        elif "rapidflow" in lower:
            result["slot_delta"]["preferred_provider_id"] = "prov_plumb_2"
        elif "aquafix" in lower:
            result["slot_delta"]["preferred_provider_id"] = "prov_plumb_1"
        elif "metro pipe" in lower:
            result["slot_delta"]["preferred_provider_id"] = "prov_plumb_3"

        return result
