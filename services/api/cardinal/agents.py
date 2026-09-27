"""Agent registry and system prompts."""

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import get_settings, load_agents_config

SHARED_RULES = """\
You are {name} (unit {unit}), one agent in Cardinal, a personal AI system{for_user}.

How to answer:
- Replies may be read aloud, so talk like a person: short sentences, no markdown tables, no headings.
- Keep it to 1-4 sentences unless the user asks for detail.
- If the question belongs to a teammate, answer briefly and say who handles it.

What you can and cannot do right now:
- You do not yet have access to the user's calendar, email, Blackboard, files, health data or the web.
- Never invent schedules, emails, grades, deadlines or numbers about the user. If asked, say that connection is not set up yet.
- You cannot change anything on the user's devices or accounts yet.

Your role:
{persona}

Today is {today}."""


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    unit: str
    type: str
    color: str
    voice: str
    job: str
    role: str
    persona: str
    sees: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)

    def system_prompt(self, now: datetime | None = None) -> str:
        settings = get_settings()
        now = now or datetime.now(ZoneInfo(settings.timezone))
        for_user = f" for {settings.user_name}" if settings.user_name else ""
        return SHARED_RULES.format(
            name=self.name,
            unit=self.unit,
            for_user=for_user,
            persona=self.persona.strip(),
            today=f"{now:%A}, {now.day} {now:%B %Y}",
        )

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "unit": self.unit,
            "type": self.type,
            "color": self.color,
            "voice": self.voice,
            "role": self.role,
            "sees": self.sees,
            "changes": self.changes,
        }


def load_agents() -> dict[str, Agent]:
    raw = load_agents_config()["agents"]
    return {a["id"]: Agent(**a) for a in raw}
