"""Enterprise Telemetry & Prometheus Metrics for Errand Agent (Nov 2026 Standard).

Thread-safe Prometheus exposition format collector supporting counters, gauges,
and duration summaries for turns, tokens, tools, guardrails, and FSM states.
"""

from collections import defaultdict
from threading import Lock
import time
from typing import Any, Optional


class PrometheusMetricsCollector:
    """Enterprise in-memory Prometheus metrics collector."""

    def __init__(self) -> None:
        self._lock = Lock()
        self.counters: dict[str, dict[tuple[tuple[str, str], ...], float]] = defaultdict(lambda: defaultdict(float))
        self.gauges: dict[str, dict[tuple[tuple[str, str], ...], float]] = defaultdict(lambda: defaultdict(float))
        self.summaries: dict[str, dict[tuple[tuple[str, str], ...], dict[str, float]]] = defaultdict(
            lambda: defaultdict(lambda: {"count": 0.0, "sum": 0.0})
        )
        self.metadata: dict[str, tuple[str, str]] = {}
        self._init_standard_metrics()

    def _init_standard_metrics(self) -> None:
        self.register_metric("errand_agent_turns_total", "counter", "Total agent conversational turns processed")
        self.register_metric("errand_agent_token_consumption_total", "counter", "Total LLM tokens consumed by type")
        self.register_metric("errand_agent_tool_calls_total", "counter", "Total tool executions by tool and status")
        self.register_metric("errand_agent_guardrail_events_total", "counter", "Total guardrail interventions and blocks")
        self.register_metric("errand_agent_fsm_state_transitions_total", "counter", "Total FSM state transitions")
        self.register_metric("errand_agent_security_rejections_total", "counter", "Total security rejections by reason")
        self.register_metric("errand_agent_mcp_requests_total", "counter", "Total Model Context Protocol requests")
        self.register_metric("errand_agent_active_sessions", "gauge", "Number of currently active FSM sessions")
        self.register_metric("errand_agent_active_proposals", "gauge", "Number of pending approval proposals")
        self.register_metric("errand_agent_active_holds", "gauge", "Number of currently active slot holds")
        self.register_metric("errand_agent_context_tokens_used", "gauge", "Context tokens used in last LLM prompt compilation")
        self.register_metric("errand_agent_context_budget_utilization_ratio", "gauge", "Ratio of context token cap used (0.0 - 1.0)")
        self.register_metric("errand_agent_operation_duration_seconds", "summary", "Execution duration of operations in seconds")
        self.register_metric("errand_agent_http_requests_total", "counter", "Total HTTP requests received by endpoint and status")
        self.register_metric("errand_agent_errors_total", "counter", "Total operational and HTTP errors encountered across components")

    def register_metric(self, name: str, metric_type: str, description: str) -> None:
        with self._lock:
            self.metadata[name] = (metric_type, description)

    def _labels_to_key(self, labels: Optional[dict[str, Any]] = None) -> tuple[tuple[str, str], ...]:
        if not labels:
            return ()
        return tuple(sorted((k, str(v)) for k, v in labels.items()))

    def inc_counter(self, name: str, value: float = 1.0, labels: Optional[dict[str, Any]] = None) -> None:
        with self._lock:
            if name not in self.metadata:
                self.metadata[name] = ("counter", f"{name} metric")
            key = self._labels_to_key(labels)
            self.counters[name][key] += value

    def set_gauge(self, name: str, value: float, labels: Optional[dict[str, Any]] = None) -> None:
        with self._lock:
            if name not in self.metadata:
                self.metadata[name] = ("gauge", f"{name} gauge")
            key = self._labels_to_key(labels)
            self.gauges[name][key] = float(value)

    def observe_duration(self, name: str, duration_sec: float, labels: Optional[dict[str, Any]] = None) -> None:
        with self._lock:
            if name not in self.metadata:
                self.metadata[name] = ("summary", f"{name} duration summary")
            key = self._labels_to_key(labels)
            entry = self.summaries[name][key]
            entry["count"] += 1.0
            entry["sum"] += float(duration_sec)

    def render_prometheus(self) -> str:
        """Render all collected metrics in standard Prometheus exposition format 0.0.4."""
        lines: list[str] = []
        with self._lock:
            # Counters
            for name, series in self.counters.items():
                mtype, desc = self.metadata.get(name, ("counter", ""))
                lines.append(f"# HELP {name} {desc}")
                lines.append(f"# TYPE {name} {mtype}")
                if not series:
                    lines.append(f"{name} 0")
                else:
                    for key, val in series.items():
                        if key:
                            lbl_str = ",".join(f'{k}="{v}"' for k, v in key)
                            lines.append(f"{name}{{{lbl_str}}} {val}")
                        else:
                            lines.append(f"{name} {val}")

            # Gauges
            for name, series in self.gauges.items():
                mtype, desc = self.metadata.get(name, ("gauge", ""))
                lines.append(f"# HELP {name} {desc}")
                lines.append(f"# TYPE {name} {mtype}")
                if not series:
                    lines.append(f"{name} 0")
                else:
                    for key, val in series.items():
                        if key:
                            lbl_str = ",".join(f'{k}="{v}"' for k, v in key)
                            lines.append(f"{name}{{{lbl_str}}} {val}")
                        else:
                            lines.append(f"{name} {val}")

            # Summaries
            for name, series in self.summaries.items():
                mtype, desc = self.metadata.get(name, ("summary", ""))
                lines.append(f"# HELP {name} {desc}")
                lines.append(f"# TYPE {name} {mtype}")
                if not series:
                    lines.append(f"{name}_count 0")
                    lines.append(f"{name}_sum 0")
                else:
                    for key, val in series.items():
                        lbl_prefix = ""
                        if key:
                            lbl_str = ",".join(f'{k}="{v}"' for k, v in key)
                            lbl_prefix = f"{{{lbl_str}}}"
                        lines.append(f"{name}_count{lbl_prefix} {int(val['count'])}")
                        lines.append(f"{name}_sum{lbl_prefix} {round(val['sum'], 4)}")

        return "\n".join(lines) + "\n"


# Global singleton instance for enterprise application metrics
METRICS = PrometheusMetricsCollector()
