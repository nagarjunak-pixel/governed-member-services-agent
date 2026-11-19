"""OpenAI-compatible Chat Completions client (works with Ollama, OpenAI, Gemini, etc.)."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    finish_reason: str | None = None
    raw_message: dict[str, Any] = field(default_factory=dict)


class OpenAICompatibleLLM:
    """Minimal OpenAI-compatible HTTP client supporting tool calls and cost accounting."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.0,
        timeout_s: float = 60.0,
        reasoning_effort: str | None = None,
        input_cost_per_1m: float = 0.0,
        output_cost_per_1m: float = 0.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self.input_cost_per_1m = input_cost_per_1m
        self.output_cost_per_1m = output_cost_per_1m
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout_s,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        in_cost = (prompt_tokens / 1_000_000.0) * self.input_cost_per_1m
        out_cost = (completion_tokens / 1_000_000.0) * self.output_cost_per_1m
        return round(in_cost + out_cost, 6)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": messages,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        t0 = time.monotonic()
        resp = self._http.post("/chat/completions", headers=headers, json=payload)
        latency_ms = (time.monotonic() - t0) * 1000.0
        resp.raise_for_status()
        data = resp.json()

        choice = data["choices"][0]
        msg = choice["message"]
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)

        raw_content = msg.get("content") or ""
        clean_content = THINK_BLOCK_RE.sub("", raw_content).strip()

        parsed_calls: list[ToolCall] = []
        for idx, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except ValueError:
                    args = {"_raw": raw_args}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}
            parsed_calls.append(
                ToolCall(
                    id=tc.get("id") or f"call_{idx}",
                    name=str(fn.get("name") or ""),
                    arguments=args,
                )
            )

        assistant_msg: dict[str, Any] = {"role": "assistant", "content": clean_content}
        if msg.get("tool_calls"):
            assistant_msg["tool_calls"] = msg["tool_calls"]

        return LLMResponse(
            content=clean_content,
            tool_calls=parsed_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=self._estimate_cost(prompt_tokens, completion_tokens),
            latency_ms=latency_ms,
            finish_reason=choice.get("finish_reason"),
            raw_message=assistant_msg,
        )
