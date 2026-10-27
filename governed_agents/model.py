"""Model-agnostic runtime interface.

The runtime only knows `Model.complete(messages, tools) -> Reply`. Adapters
translate to each provider's wire format, so switching provider is a config
change and provider differences don't leak into agents. Messages use one
neutral format: {"role": "system"|"user"|"assistant"|"tool", ...}.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Reply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


class Model(Protocol):
    name: str

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Reply: ...


class OpenAICompatible:
    """Any /chat/completions endpoint: OpenAI, vLLM, Ollama, OpenRouter, Gemini's OpenAI layer."""

    def __init__(self, model: str, base_url: str = "https://api.openai.com/v1", api_key: str = "",
                 client: httpx.Client | None = None):
        self.name = model
        self._base = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=60, headers={"Authorization": f"Bearer {api_key}"})

    def complete(self, messages, tools):
        wire = []
        for m in messages:
            if m["role"] == "tool":
                wire.append({"role": "tool", "tool_call_id": m["call_id"], "content": m["content"]})
            elif m["role"] == "assistant" and m.get("tool_calls"):
                wire.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": json.dumps(c["args"])}} for c in m["tool_calls"]]})
            else:
                wire.append({"role": m["role"], "content": m["content"]})
        body = {"model": self.name, "messages": wire,
                "tools": [{"type": "function", "function": t} for t in tools] or None}
        r = self._client.post(f"{self._base}/chat/completions", json={k: v for k, v in body.items() if v})
        r.raise_for_status()
        data = r.json()
        msg = data["choices"][0]["message"]
        calls = [ToolCall(c["id"], c["function"]["name"], _loads(c["function"]["arguments"]))
                 for c in msg.get("tool_calls") or []]
        u = data.get("usage", {})
        return Reply(msg.get("content") or "", calls,
                     {"input": u.get("prompt_tokens", 0), "output": u.get("completion_tokens", 0)})


class Anthropic:
    def __init__(self, model: str, api_key: str = "", base_url: str = "https://api.anthropic.com",
                 client: httpx.Client | None = None):
        self.name = model
        self._base = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=60, headers={
            "x-api-key": api_key, "anthropic-version": "2023-06-01"})

    def complete(self, messages, tools):
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        wire: list[dict[str, Any]] = []
        for m in messages:
            if m["role"] == "system":
                continue
            if m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["call_id"], "content": m["content"]}
                if wire and wire[-1]["role"] == "user" and isinstance(wire[-1]["content"], list):
                    wire[-1]["content"].append(block)   # parallel tool results share one user turn
                else:
                    wire.append({"role": "user", "content": [block]})
            elif m["role"] == "assistant":
                blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}
                           for c in m.get("tool_calls", [])]
                wire.append({"role": "assistant", "content": blocks})
            else:
                wire.append({"role": "user", "content": m["content"]})
        body = {"model": self.name, "max_tokens": 2048, "system": system, "messages": wire,
                "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                          for t in tools]}
        r = self._client.post(f"{self._base}/v1/messages", json=body)
        r.raise_for_status()
        data = r.json()
        text = "".join(b["text"] for b in data["content"] if b["type"] == "text")
        calls = [ToolCall(b["id"], b["name"], b["input"]) for b in data["content"] if b["type"] == "tool_use"]
        u = data.get("usage", {})
        return Reply(text, calls, {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0)})


class Scripted:
    """Replays a fixed list of replies. Used by tests and by the guardrail evals, so a run is
    deterministic and a compromised model's behaviour can be written down exactly."""

    name = "scripted"

    def __init__(self, replies: list[Reply]):
        self._replies = list(replies)
        self.seen: list[list[dict[str, Any]]] = []

    def complete(self, messages, tools):
        self.seen.append([dict(m) for m in messages])
        if not self._replies:
            return Reply(text="(script exhausted)")
        return self._replies.pop(0)


def from_env() -> Model:
    provider = os.environ.get("MODEL_PROVIDER", "anthropic")
    if provider == "anthropic":
        return Anthropic(os.environ.get("MODEL_NAME", "claude-haiku-5-5"), os.environ["ANTHROPIC_API_KEY"])
    return OpenAICompatible(os.environ.get("MODEL_NAME", "gpt-4.1-mini"),
                            os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                            os.environ["OPENAI_API_KEY"])


def _loads(s: str) -> dict[str, Any]:
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {"_raw": s}
    except json.JSONDecodeError:
        return {"_raw": s}   # rejected later by argument validation, and audited
