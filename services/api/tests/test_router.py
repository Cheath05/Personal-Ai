from datetime import UTC, datetime

import pytest
from sqlmodel import select

from cardinal.brains import BrainError
from cardinal.db import UsageEvent
from cardinal.router import NoBrainAvailable

from .conftest import FakeClaude, FakeLocal

MSGS = [{"role": "user", "content": "What should I focus on today?"}]


async def run(router, session, job="chat", **kw):
    return await router.run(session, agent_id="cardinal", job=job, system="sys", messages=MSGS, **kw)


def spend(session, usd):
    session.add(UsageEvent(ts=datetime.now(UTC), agent_id="delta", job="weekly_rollup", provider="claude",
                           brain="claude", model="claude-sonnet-5", cost_usd=usd))
    session.commit()


async def test_local_answer_stays_local_and_free(make_router, session):
    claude = FakeClaude()
    result = await run(make_router(claude=claude), session)
    assert result.reply.provider == "local"
    assert not result.escalated
    assert claude.calls == []
    event = session.exec(select(UsageEvent)).one()
    assert event.provider == "local" and event.cost_usd == 0


async def test_escalates_after_local_fails_checks_twice(make_router, session):
    g14 = FakeLocal("g14", replies=["", ""])
    claude = FakeClaude()
    result = await run(make_router(local={"g14": g14}, claude=claude), session)
    assert g14.calls == 2
    assert result.reply.provider == "claude"
    assert result.escalated
    assert result.reason == "local failed: empty answer"
    claude_event = session.exec(select(UsageEvent).where(UsageEvent.provider == "claude")).one()
    assert claude_event.cost_usd > 0


async def test_offline_brain_falls_through_to_next_local(make_router, session):
    g14 = FakeLocal("g14", replies=[BrainError("G14 did not answer")])
    mac = FakeLocal("mac", replies=["Answer from the Mac."])
    result = await run(make_router(local={"g14": g14, "mac": mac}), session)
    assert result.reply.brain == "mac"
    assert result.attempts[0].brain == "g14" and not result.attempts[0].ok


async def test_no_local_online_uses_claude(make_router, session):
    router = make_router(local={"g14": FakeLocal("g14", online=False)})
    result = await run(router, session)
    assert result.reply.provider == "claude"
    assert result.reason == "no local brain online"


async def test_nothing_available_raises_clear_error(make_router, session):
    router = make_router(local={"g14": FakeLocal("g14", online=False)}, claude=FakeClaude(configured=False))
    with pytest.raises(NoBrainAvailable):
        await run(router, session)


async def test_local_only_job_never_uses_claude(make_router, session):
    claude = FakeClaude()
    g14 = FakeLocal("g14", replies=["", ""])
    result = await run(make_router(local={"g14": g14}, claude=claude), session, job="quick")
    assert claude.calls == []
    assert result.reply.provider == "local"  # best-effort local answer, labeled
    assert "didn't pass checks" in result.reason


async def test_background_lane_prefers_server(make_router, session):
    local = {"g14": FakeLocal("g14"), "server": FakeLocal("server")}
    result = await run(make_router(local=local), session, job="triage")
    assert result.reply.brain == "server"


async def test_claude_job_uses_deep_tier(make_router, session):
    claude = FakeClaude()
    result = await run(make_router(claude=claude), session, job="weekly_rollup")
    assert result.reply.provider == "claude"
    assert claude.calls == ["claude-sonnet-5"]


async def test_hard_cap_forces_local(make_router, session):
    spend(session, 20.0)
    claude = FakeClaude()
    result = await run(make_router(claude=claude), session, job="weekly_rollup")
    assert claude.calls == []
    assert result.reply.provider == "local"
    assert result.reason == "ran locally: monthly cap reached"


async def test_soft_cap_blocks_escalation_but_not_essentials_or_forced(make_router, session):
    spend(session, 16.0)
    claude = FakeClaude()
    router = make_router(local={"g14": FakeLocal("g14", replies=["", ""])}, claude=claude)

    result = await run(router, session)  # chat escalation is blocked past the soft cap
    assert result.reply.provider == "local"
    assert claude.calls == []

    assert (await run(router, session, job="weekly_rollup")).reply.provider == "claude"
    forced = await run(router, session, force_claude=True)
    assert forced.reply.provider == "claude" and forced.reason == "you asked for Claude"


async def test_call_that_could_pass_cap_is_blocked(make_router, session):
    spend(session, 19.999)  # a worst-case chat call (~$0.005) would cross the $20 cap
    claude = FakeClaude()
    router = make_router(local={"g14": FakeLocal("g14", online=False)}, claude=claude)
    with pytest.raises(NoBrainAvailable):
        await run(router, session, force_claude=True)
    assert claude.calls == []
