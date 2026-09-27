import json

import httpx
import pytest

from cardinal.brains import BrainError, OllamaBrain


def brain_with(handler, model="qwen3:4b"):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaBrain("mac", "http://mac:11434", model, "M3 MacBook Air", client=client)


async def test_available_requires_model_to_be_pulled():
    def handler(request):
        return httpx.Response(200, json={"models": [{"name": "qwen3:4b"}, {"name": "gemma3:4b"}]})
    assert await brain_with(handler).available()
    assert not await brain_with(handler, model="qwen3:8b").available()


async def test_unreachable_server_is_offline():
    def handler(request):
        raise httpx.ConnectError("refused")
    assert not await brain_with(handler).available()


async def test_chat_sends_system_prompt_and_strips_think_blocks():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={
            "message": {"role": "assistant", "content": "<think>planning</think>\nRun easy today."},
            "prompt_eval_count": 210, "eval_count": 12, "done_reason": "stop",
        })

    reply = await brain_with(handler).chat("You are Vector.", [{"role": "user", "content": "Run today?"}], max_tokens=300)
    assert seen["messages"][0] == {"role": "system", "content": "You are Vector."}
    assert seen["options"]["num_predict"] == 300 and seen["stream"] is False
    assert reply.text == "Run easy today."
    assert (reply.input_tokens, reply.output_tokens, reply.cost_usd) == (210, 12, 0.0)


async def test_chat_error_raises_brain_error_and_marks_offline():
    def handler(request):
        return httpx.Response(500, text="model crashed")
    brain = brain_with(handler)
    with pytest.raises(BrainError):
        await brain.chat("sys", [{"role": "user", "content": "hi"}], max_tokens=10)
    assert not await brain.available()  # cached as offline right after the failure


def test_strip_thinking_handles_missing_open_tag():
    from cardinal.brains.ollama import strip_thinking
    assert strip_thinking("Okay, the user asks...\n</think>\n\nEasy runs build your base.") == "Easy runs build your base."
    assert strip_thinking("<think>hmm</think>Answer.") == "Answer."
    assert strip_thinking("Just an answer.") == "Just an answer."
