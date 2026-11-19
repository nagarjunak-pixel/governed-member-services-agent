"""PHI-safe per-conversation structured trace for auditability and governance."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from member_agent.redaction import redact_phi_for_logs


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TraceEvent:
    timestamp: str
    event_type: str  # "turn" | "model_call" | "gateway_decision" | "budget_alert"
    turn_index: int
    payload: dict[str, Any]


@dataclass
class ConversationTrace:
    session_id: str
    tenant: str
    member_id: str
    started_at: str = field(default_factory=_utc_now)
    events: list[TraceEvent] = field(default_factory=list)
    steps_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    total_latency_ms: float = 0.0
    _start_monotonic: float = field(default_factory=time.monotonic, repr=False)

    def elapsed_ms(self) -> float:
        return round((time.monotonic() - self._start_monotonic) * 1000.0, 2)

    def record_turn(self, turn_index: int, role: str, text: str, latency_ms: float | None = None) -> None:
        payload: dict[str, Any] = {
            "role": role,
            "text": text,
        }
        if latency_ms is not None:
            payload["latency_ms"] = round(latency_ms, 2)
        self.events.append(
            TraceEvent(
                timestamp=_utc_now(),
                event_type="turn",
                turn_index=turn_index,
                payload=redact_phi_for_logs(payload),
            )
        )

    def record_model_call(
        self,
        *,
        turn_index: int,
        model: str,
        latency_ms: float,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        tool_calls_requested: list[str],
        finish_reason: str | None = None,
    ) -> None:
        self.steps_used += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.estimated_cost_usd = round(self.estimated_cost_usd + cost_usd, 6)
        self.total_latency_ms = round(self.total_latency_ms + latency_ms, 2)
        self.events.append(
            TraceEvent(
                timestamp=_utc_now(),
                event_type="model_call",
                turn_index=turn_index,
                payload={
                    "model": model,
                    "latency_ms": round(latency_ms, 2),
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "cost_usd": round(cost_usd, 6),
                    "tool_calls_requested": tool_calls_requested,
                    "finish_reason": finish_reason,
                },
            )
        )

    def record_gateway_decision(
        self,
        *,
        turn_index: int,
        tool_name: str,
        decision: str,
        reason: str,
        arguments: dict[str, Any],
        latency_ms: float = 0.0,
        cache_hit: bool = False,
        upstream_status: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.steps_used += 1
        self.total_latency_ms = round(self.total_latency_ms + latency_ms, 2)
        payload: dict[str, Any] = {
            "tool": tool_name,
            "decision": decision,
            "reason": reason,
            "arguments": arguments,
            "latency_ms": round(latency_ms, 2),
            "cache_hit": cache_hit,
        }
        if upstream_status is not None:
            payload["upstream_status"] = upstream_status
        if extra:
            payload.update(extra)
        self.events.append(
            TraceEvent(
                timestamp=_utc_now(),
                event_type="gateway_decision",
                turn_index=turn_index,
                payload=redact_phi_for_logs(payload),
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return redact_phi_for_logs(
            {
                "session_id": self.session_id,
                "tenant": self.tenant,
                "member_id": self.member_id,
                "started_at": self.started_at,
                "summary": {
                    "steps_used": self.steps_used,
                    "prompt_tokens": self.prompt_tokens,
                    "completion_tokens": self.completion_tokens,
                    "estimated_cost_usd": self.estimated_cost_usd,
                    "total_latency_ms": self.total_latency_ms,
                    "elapsed_wall_ms": self.elapsed_ms(),
                },
                "events": [
                    {
                        "timestamp": e.timestamp,
                        "event_type": e.event_type,
                        "turn_index": e.turn_index,
                        "payload": e.payload,
                    }
                    for e in self.events
                ],
            }
        )

    def save_jsonl(self, trace_dir: Path) -> Path:
        """Append this conversation trace to a tenant-partitioned JSONL file."""
        tenant_dir = trace_dir / self.tenant
        tenant_dir.mkdir(parents=True, exist_ok=True)
        out_path = tenant_dir / f"{self.session_id}.json"
        out_path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return out_path
