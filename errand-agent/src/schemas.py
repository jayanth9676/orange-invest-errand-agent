from enum import Enum
import time
from typing import Any, Optional, Union
import uuid
from pydantic import BaseModel, Field


class SentinelAction(str, Enum):
    """Action outcome from Sentinel Gatekeeper interception."""
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    BLOCK = "BLOCK"
    PROCEED = "ALLOW"


class AgentState(str, Enum):
    IDLE = "IDLE"
    COLLECTING_SLOTS = "COLLECTING_SLOTS"
    SEARCHING_PROVIDERS = "SEARCHING_PROVIDERS"
    HOLDING_SLOT = "HOLDING_SLOT"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING_MUTATION = "EXECUTING_MUTATION"
    RECONCILING_TIMEOUT = "RECONCILING_TIMEOUT"
    PROACTIVE_RECOVERY = "PROACTIVE_RECOVERY"
    COMPLETED = "COMPLETED"
    SAFE_ABORT = "SAFE_ABORT"


class MutationType(str, Enum):
    BOOK = "book"
    CANCEL = "cancel"
    RESCHEDULE = "reschedule"


class MemorySource(str, Enum):
    EXPLICIT = "EXPLICIT"
    INFERRED = "INFERRED"


class IntentSlots(BaseModel):
    intent_type: str = "book"  # book | reschedule | cancel | memory_op | list_bookings | status
    service: Optional[str] = None
    area: Optional[str] = None
    date_window: Optional[str] = None
    time_window: Optional[str] = None
    max_budget: Optional[int] = None
    preferences: dict[str, Any] = Field(default_factory=dict)
    target_booking_id: Optional[str] = None
    preferred_provider_id: Optional[str] = None
    requested_slot: Optional[str] = None

    def model_dump(self, exclude_none: bool = False, **kwargs: Any) -> dict[str, Any]:
        if hasattr(super(), "model_dump"):
            return super().model_dump(exclude_none=exclude_none, **kwargs)
        return self.dict(exclude_none=exclude_none, **kwargs)

    def merge_delta(self, delta: dict[str, Any]) -> "IntentSlots":
        updated = self.model_dump()
        for k, v in delta.items():
            if v is not None and k in updated:
                if k == "preferences" and isinstance(v, dict):
                    merged_prefs = dict(updated.get("preferences") or {})
                    merged_prefs.update(v)
                    updated["preferences"] = merged_prefs
                else:
                    updated[k] = v
        return IntentSlots(**updated)

    def missing_discovery_fields(self) -> list[str]:
        if self.intent_type != "book":
            return []
        required = ["service", "area", "date_window", "time_window", "max_budget"]
        return [f for f in required if getattr(self, f) is None]


class ApprovalProposal(BaseModel):
    proposal_id: str
    session_id: str
    mutation_type: MutationType
    provider_id: str
    slot: str
    final_price: int
    payload_hash: str
    proposed_at_turn: int
    booking_id: Optional[str] = None
    hold_id: Optional[str] = None
    approved_at_turn: Optional[int] = None
    created_at_epoch: float = Field(default_factory=time.time)
    created_at: Optional[float] = None
    ttl_seconds: float = 600.0
    invalidated: bool = False
    invalidation_reason: Optional[str] = None
    provider_name: Optional[str] = None
    original_price: Optional[float] = None
    price_drift_delta: Optional[float] = None

    def __init__(self, **data: Any):
        super().__init__(**data)
        if self.created_at is None:
            self.created_at = self.created_at_epoch

    def is_expired(self, current_epoch: Optional[float] = None) -> bool:
        now = current_epoch if current_epoch is not None else time.time()
        return (now - self.created_at_epoch) > self.ttl_seconds

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        if hasattr(super(), "model_dump"):
            d = super().model_dump(**kwargs)
        else:
            d = self.dict(**kwargs)
        d["mutation_type"] = (
            self.mutation_type.value
            if isinstance(self.mutation_type, MutationType)
            else str(self.mutation_type)
        )
        if "created_at" not in d or d["created_at"] is None:
            d["created_at"] = self.created_at or self.created_at_epoch
        return d


class SentinelDecision(BaseModel):
    """Authoritative decision from SentinelGatekeeper.intercept_tool_call."""
    action: SentinelAction
    proposal: Optional[ApprovalProposal] = None
    reason: str = ""
    idempotency_key: Optional[str] = None
    drift_detected: bool = False
    tool_name: Optional[str] = None
    kwargs: dict[str, Any] = Field(default_factory=dict)
    modified_kwargs: Optional[dict[str, Any]] = None

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        if hasattr(super(), "model_dump"):
            return super().model_dump(**kwargs)
        return self.dict(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class ToolCall(BaseModel):
    """Standardized representation of a model-requested tool invocation."""
    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:8]}")
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        if hasattr(super(), "model_dump"):
            return super().model_dump(**kwargs)
        return self.dict(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class AgentTurnResponse(BaseModel):
    """Standardized response from an agentic model turn with cascade."""
    thought: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    content: Optional[str] = None
    provider_used: str = "offline"
    model_used: str = "deterministic"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    estimated_cost_usd: float = 0.0
    raw_response: Optional[dict[str, Any]] = None
    raw_message: Optional[dict[str, Any]] = None

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        if hasattr(super(), "model_dump"):
            return super().model_dump(**kwargs)
        return self.dict(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()
