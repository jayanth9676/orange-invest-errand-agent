import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from src.metrics import METRICS


class TraceLogger:
    '''
    Records an OpenTelemetry-compatible structured trace for each run, including
    trace_id, span_id, parent_span_id, duration_ms, tags, baggage, state transitions,
    model calls, tool executions, context audits, and guardrail events.

    Integrates with Prometheus metrics collector to automatically update operational
    counters, latencies, and token counters.
    '''
    def __init__(
        self,
        session_id: str,
        trace_file: Optional[Path] = None,
        trace_id: Optional[str] = None,
    ):
        self.session_id = session_id
        self.trace_file = trace_file
        # Generate OpenTelemetry standard 32-hex character trace_id if not provided
        self.trace_id = trace_id or hashlib.md5(f"otel_trace:{session_id}:{uuid.uuid4()}".encode()).hexdigest()
        self.events: list[dict[str, Any]] = []
        self._span_seq = 0
        self.active_parent_span_id: Optional[str] = None
        self.baggage: dict[str, str] = {
            "session_id": session_id,
            "service.name": "errand-agent",
            "service.version": "2.0.0",
        }

    def set_baggage(self, key: str, value: str) -> None:
        self.baggage[key] = str(value)

    def get_w3c_traceparent(self, span_id: Optional[str] = None) -> str:
        """Format W3C TraceContext traceparent header: 00-{trace_id}-{span_id}-01."""
        target_span = span_id or (self.events[-1]["span_id"] if self.events else "0000000000000000")
        raw_span = target_span.replace("span_", "").zfill(16)[:16]
        return f"00-{self.trace_id}-{raw_span}-01"

    def _record(
        self,
        event_type: str,
        payload: dict[str, Any],
        duration_ms: Optional[float] = None,
        parent_span_id: Optional[str] = None,
        tags: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        self._span_seq += 1
        now = round(time.time(), 4)
        raw_span = hashlib.md5(f"{self.session_id}:{self._span_seq}:{now}".encode()).hexdigest()[:12]
        span_id = f"span_{raw_span}"

        entry_tags: dict[str, Any] = {
            "service.name": "errand-agent",
            "service.version": "2.0.0",
            "otel.library.name": "errand-agent-telemetry",
            **(tags or {}),
        }

        entry: dict[str, Any] = {
            "timestamp": now,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id or self.active_parent_span_id,
            "event_type": event_type,
            "duration_ms": duration_ms or payload.get("latency_ms", 0.0),
            "tags": entry_tags,
            "baggage": dict(self.baggage),
            **payload,
        }
        self.events.append(entry)

        if self.trace_file:
            self.trace_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.trace_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def log_state_transition(self, from_state: str, to_state: str, reason: str) -> None:
        self._record(
            "state_transition",
            {"from_state": from_state, "to_state": to_state, "reason": reason},
            tags={"fsm.from_state": from_state, "fsm.to_state": to_state},
        )
        METRICS.inc_counter(
            "errand_agent_fsm_state_transitions_total",
            labels={"from_state": from_state, "to_state": to_state},
        )

    def log_model_call(
        self,
        model_name: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        estimated_cost_usd: float,
        parsed_output: dict[str, Any],
        context_audit: Optional[dict[str, Any]] = None,
    ) -> None:
        payload: dict[str, Any] = {
            "model": model_name,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "latency_ms": round(latency_ms, 2),
            "estimated_cost_usd": round(estimated_cost_usd, 6),
            "parsed_output": parsed_output,
        }
        if context_audit:
            payload["context_budget_audit"] = {
                "included_memory_ids": context_audit.get("included_memory_ids", []),
                "excluded_memory_count": len(context_audit.get("excluded_memory_ids", [])),
                "context_tokens_used": context_audit.get("total_tokens", prompt_tokens),
                "token_budget_cap": context_audit.get("token_budget", 1200),
            }
            METRICS.set_gauge("errand_agent_context_tokens_used", context_audit.get("total_tokens", prompt_tokens))
            METRICS.set_gauge(
                "errand_agent_context_budget_utilization_ratio",
                round(context_audit.get("total_tokens", prompt_tokens) / context_audit.get("token_budget", 1200), 4),
            )

        self._record(
            "model_call",
            payload,
            duration_ms=round(latency_ms, 2),
            tags={
                "gen_ai.request.model": model_name,
                "gen_ai.usage.prompt_tokens": prompt_tokens,
                "gen_ai.usage.completion_tokens": completion_tokens,
            },
        )
        METRICS.inc_counter("errand_agent_token_consumption_total", prompt_tokens, labels={"type": "prompt", "model": model_name})
        METRICS.inc_counter("errand_agent_token_consumption_total", completion_tokens, labels={"type": "completion", "model": model_name})
        METRICS.observe_duration("errand_agent_operation_duration_seconds", latency_ms / 1000.0, labels={"operation": "model_call"})

    def log_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        sanitized_result: Any,
        latency_ms: float,
        status: str = "ok",
    ) -> None:
        self._record(
            "tool_call",
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "sanitized_result": sanitized_result,
                "latency_ms": round(latency_ms, 2),
                "status": status,
            },
            duration_ms=round(latency_ms, 2),
            tags={"tool.name": tool_name, "tool.status": status},
        )
        METRICS.inc_counter("errand_agent_tool_calls_total", labels={"tool": tool_name, "status": status})
        METRICS.observe_duration("errand_agent_operation_duration_seconds", latency_ms / 1000.0, labels={"operation": "tool_call"})

    def log_guardrail_event(self, guardrail: str, action: str, details: dict[str, Any]) -> None:
        self._record(
            "guardrail_event",
            {"guardrail": guardrail, "action": action, "details": details},
            tags={"guardrail.name": guardrail, "guardrail.action": action},
        )
        METRICS.inc_counter("errand_agent_guardrail_events_total", labels={"guardrail": guardrail, "action": action})

    def export_jsonl(self) -> str:
        """Export all recorded trace events as newline-delimited JSON string."""
        return "\n".join(json.dumps(e, ensure_ascii=False) for e in self.events)
