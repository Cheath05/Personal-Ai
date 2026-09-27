"""Claude brain, through the Anthropic API. Only used when the router decides to escalate."""

import time

import anthropic

from ..pricing import claude_cost
from .base import BrainError, BrainReply


class ClaudeBrain:
    provider = "claude"
    name = "claude"

    def __init__(self, api_key: str | None, model: str, label: str = ""):
        self.model = model
        self.label = label or model
        self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2) if api_key else None

    def with_model(self, model: str) -> "ClaudeBrain":
        clone = ClaudeBrain.__new__(ClaudeBrain)
        clone.model, clone.label, clone._client = model, model, self._client
        return clone

    async def available(self) -> bool:
        return self._client is not None

    async def chat(self, system: str, messages: list[dict], *, max_tokens: int,
                   effort: str | None = None) -> BrainReply:
        if self._client is None:
            raise BrainError("No Anthropic API key is set.")
        extra = {}
        # Haiku 4.5 rejects the effort setting; Sonnet 5 and Opus accept it.
        if effort and not self.model.startswith("claude-haiku"):
            extra["output_config"] = {"effort": effort}
        start = time.monotonic()
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                cache_control={"type": "ephemeral"},
                **extra,
            )
        except anthropic.RateLimitError as e:
            raise BrainError("Claude is rate limited right now.") from e
        except anthropic.AuthenticationError as e:
            raise BrainError("The Anthropic API key was rejected.") from e
        except anthropic.APIStatusError as e:
            raise BrainError(f"Claude returned an error ({e.status_code}).") from e
        except anthropic.APIConnectionError as e:
            raise BrainError("Could not reach Claude. Check the internet connection.") from e

        if response.stop_reason == "refusal":
            raise BrainError("Claude declined this request.")
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        usage = response.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        return BrainReply(
            text=text,
            provider=self.provider,
            brain=self.name,
            model=self.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cost_usd=claude_cost(self.model, usage.input_tokens, usage.output_tokens, cache_read, cache_write),
            latency_ms=int((time.monotonic() - start) * 1000),
            stop_reason=response.stop_reason,
        )
