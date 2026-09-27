"""Shared types for every brain (local Ollama or Claude)."""

from dataclasses import dataclass
from typing import Protocol


class BrainError(Exception):
    """A brain could not produce an answer (offline, timeout, bad response)."""


@dataclass
class BrainReply:
    text: str
    provider: str  # "local" | "claude"
    brain: str  # local brain name ("g14", "mac", "server") or "claude"
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    stop_reason: str | None = None


class Brain(Protocol):
    name: str
    provider: str
    model: str
    label: str

    async def available(self) -> bool: ...

    async def chat(self, system: str, messages: list[dict], *, max_tokens: int,
                   effort: str | None = None, json_schema: dict | None = None) -> BrainReply: ...
