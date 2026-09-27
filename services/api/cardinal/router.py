"""The brain router: local first, Claude only when a job needs it and the budget allows."""

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlmodel import Session

from . import usage
from .brains import Brain, BrainError, BrainReply, ClaudeBrain
from .config import Settings
from .pricing import max_call_cost

Check = Callable[[BrainReply], str | None]  # returns a failure reason, or None if the reply is fine


class NoBrainAvailable(Exception):
    pass


def basic_check(reply: BrainReply) -> str | None:
    text = reply.text.strip()
    if len(text) < 2:
        return "empty answer"
    if reply.stop_reason == "length" and len(text) < 40:
        return "answer was cut off"
    return None


@dataclass
class Attempt:
    brain: str
    model: str
    ok: bool
    reason: str | None = None


@dataclass
class RouteResult:
    reply: BrainReply
    escalated: bool = False
    reason: str | None = None
    attempts: list[Attempt] = field(default_factory=list)


class BrainRouter:
    def __init__(self, local: dict[str, Brain], order: dict[str, list[str]],
                 claude: ClaudeBrain, claude_tiers: dict[str, str],
                 routing: dict, settings: Settings):
        self.local = local
        self.order = order
        self.claude = claude
        self.claude_tiers = claude_tiers
        self.jobs = routing.get("jobs", {})
        self.essential_jobs = set(routing.get("essential_jobs", []))
        self.local_attempts = int(routing.get("local_attempts", 2))
        self.settings = settings

    def job_config(self, job: str) -> dict:
        return {"policy": "local_then_claude", "lane": "interactive", "tier": "default",
                "max_tokens": 900, **self.jobs.get(job, {})}

    async def online_local(self, lane: str) -> list[Brain]:
        names = self.order.get(lane) or list(self.local)
        return [self.local[n] for n in names if n in self.local and await self.local[n].available()]

    async def status(self) -> dict:
        return {
            "local": [
                {"name": b.name, "label": b.label, "model": b.model, "online": await b.available()}
                for b in self.local.values()
            ],
            "claude": {"configured": await self.claude.available(), "models": self.claude_tiers},
        }

    def _claude_allowed(self, session: Session, job: str, forced: bool, model: str, max_tokens: int,
                        est_input: int) -> tuple[bool, str | None]:
        if not self.claude._client:
            return False, "no API key"
        spent = usage.claude_spend_this_month(session, self.settings)
        mode = usage.budget_mode(spent, self.settings)
        if mode == usage.LOCAL_ONLY:
            return False, "monthly cap reached"
        if mode == usage.ESSENTIALS and not (forced or job in self.essential_jobs):
            return False, "past soft cap (essentials only)"
        if spent + max_call_cost(model, max_tokens, est_input) > self.settings.budget_cap_usd:
            return False, "this call could pass the monthly cap"
        return True, None

    async def run(self, session: Session, *, agent_id: str, job: str, system: str, messages: list[dict],
                  force_claude: bool = False, check: Check = basic_check,
                  json_schema: dict | None = None) -> RouteResult:
        cfg = self.job_config(job)
        policy, max_tokens, effort = cfg["policy"], int(cfg["max_tokens"]), cfg.get("effort")
        claude = self.claude.with_model(self.claude_tiers.get(cfg["tier"], self.claude_tiers["default"]))
        est_input = sum(len(m["content"]) for m in messages) // 3 + len(system) // 3
        attempts: list[Attempt] = []
        extra = {"json_schema": json_schema} if json_schema else {}

        async def try_claude(reason: str) -> RouteResult | None:
            allowed, why_not = self._claude_allowed(session, job, force_claude, claude.model, max_tokens, est_input)
            if not allowed:
                attempts.append(Attempt("claude", claude.model, False, why_not))
                return None
            try:
                reply = await claude.chat(system, messages, max_tokens=max_tokens, effort=effort, **extra)
            except BrainError as e:
                attempts.append(Attempt("claude", claude.model, False, str(e)))
                return None
            failure = check(reply)
            usage.record(session, agent_id, job, reply, ok=failure is None, reason=reason)
            attempts.append(Attempt("claude", claude.model, failure is None, failure))
            return RouteResult(reply, escalated=policy != "claude" or force_claude, reason=reason, attempts=attempts)

        if force_claude:
            result = await try_claude("you asked for Claude")
            if result:
                return result
        elif policy == "claude":
            result = await try_claude("job runs on Claude by default")
            if result:
                return result

        local_failure: str | None = None
        last_local: BrainReply | None = None
        for brain in await self.online_local(cfg["lane"]):
            for _ in range(self.local_attempts):
                try:
                    reply = await brain.chat(system, messages, max_tokens=max_tokens, **extra)
                except BrainError as e:
                    attempts.append(Attempt(brain.name, brain.model, False, str(e)))
                    local_failure = str(e)
                    break  # this brain went offline; try the next one
                last_local = reply
                failure = check(reply)
                usage.record(session, agent_id, job, reply, ok=failure is None,
                             reason=None if failure is None else f"local check failed: {failure}")
                attempts.append(Attempt(brain.name, brain.model, failure is None, failure))
                if failure is None:
                    note = None
                    if force_claude or policy == "claude":
                        note = f"ran locally: {attempts[0].reason}"
                    return RouteResult(reply, reason=note, attempts=attempts)
                local_failure = failure
            else:
                break  # the brain answered but failed its checks twice; escalate instead of shopping around

        if policy == "local_then_claude" and not force_claude:
            reason = f"local failed: {local_failure}" if local_failure else "no local brain online"
            result = await try_claude(reason)
            if result:
                return result

        if last_local is not None:
            # Best effort: a weak local answer beats no answer when Claude isn't allowed.
            return RouteResult(last_local, reason=f"local answer didn't pass checks ({local_failure})",
                               attempts=attempts)
        raise NoBrainAvailable(_explain(attempts))


def _explain(attempts: list[Attempt]) -> str:
    local_tried = [a for a in attempts if a.brain != "claude"]
    claude_tried = [a for a in attempts if a.brain == "claude"]
    if local_tried:
        msg = "Local brains didn't answer: " + "; ".join(f"{a.brain} ({a.reason})" for a in local_tried) + "."
    else:
        msg = "No local brain is online. Start Ollama on this Mac or the G14."
    if claude_tried:
        msg += f" Claude backup isn't available ({claude_tried[-1].reason})."
    return msg
