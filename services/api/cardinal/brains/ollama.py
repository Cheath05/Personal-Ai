"""Local brain: an Ollama server on the G14, the Mac, or the Proxmox VM."""

import asyncio
import re
import time

import httpx

from .base import BrainError, BrainReply

THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_thinking(text: str) -> str:
    """Drop a model's reasoning, keeping only the answer. Some models omit the opening <think> tag."""
    text = THINK_BLOCK.sub("", text)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return text.strip()


class OllamaBrain:
    provider = "local"

    def __init__(self, name: str, url: str, model: str, label: str = "",
                 timeout: float = 120.0, client: httpx.AsyncClient | None = None):
        self.name = name
        self.url = url.rstrip("/")
        self.model = model
        self.label = label or name
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._checked_at = 0.0
        self._online = False
        self._refreshing: asyncio.Task | None = None

    async def available(self) -> bool:
        """True when the server answers and has this brain's model pulled.

        The first check waits; after that the cached answer comes back immediately and a stale one is
        refreshed in the background, so an offline G14 never slows down a chat."""
        if self._checked_at == 0.0:
            return await self._refresh()
        if time.monotonic() - self._checked_at > 15 and (self._refreshing is None or self._refreshing.done()):
            self._refreshing = asyncio.create_task(self._refresh())
        return self._online

    async def _refresh(self) -> bool:
        try:
            r = await self._client.get(f"{self.url}/api/tags", timeout=1.5)
            r.raise_for_status()
            names = {m.get("name", "") for m in r.json().get("models", [])}
            wanted = self.model if ":" in self.model else f"{self.model}:latest"
            self._online = wanted in names
        except (httpx.HTTPError, ValueError):
            self._online = False
        self._checked_at = time.monotonic()
        return self._online

    def mark_offline(self) -> None:
        self._online = False
        self._checked_at = time.monotonic()

    async def chat(self, system: str, messages: list[dict], *, max_tokens: int,
                   effort: str | None = None, json_schema: dict | None = None) -> BrainReply:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": False,
            "think": False,  # Qwen3 thinking mode is slow on small GPUs; answers directly instead
            "options": {"num_predict": max_tokens},
        }
        if json_schema:
            body["format"] = json_schema  # Ollama constrains the output to this JSON schema
        start = time.monotonic()
        try:
            r = await self._client.post(f"{self.url}/api/chat", json=body)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            self.mark_offline()
            raise BrainError(f"{self.label} did not answer: {e}") from e
        text = strip_thinking(data.get("message", {}).get("content", ""))
        return BrainReply(
            text=text,
            provider=self.provider,
            brain=self.name,
            model=self.model,
            input_tokens=data.get("prompt_eval_count", 0) or 0,
            output_tokens=data.get("eval_count", 0) or 0,
            latency_ms=int((time.monotonic() - start) * 1000),
            stop_reason=data.get("done_reason"),
        )
