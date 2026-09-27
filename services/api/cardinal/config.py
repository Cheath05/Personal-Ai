"""Settings loaded from the repo-root .env file and config/*.yaml."""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

API_DIR = Path(__file__).resolve().parents[1]
ROOT = API_DIR.parents[1]
CONFIG_DIR = API_DIR / "config"
WEB_DIR = ROOT / "apps" / "web"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_prefix="CARDINAL_",
        extra="ignore",
    )

    user_name: str = ""
    timezone: str = "America/New_York"
    database_url: str = f"sqlite:///{ROOT / 'data' / 'cardinal.db'}"

    # Claude is optional. Without a key, everything runs on local brains.
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")

    # Monthly Claude budget. Soft cap switches to "essentials only", hard cap to local only.
    budget_cap_usd: float = 20.0
    budget_soft_usd: float = 15.0

    brains_file: Path | None = None
    history_turns: int = 12

    # Where people open Cardinal. Google sends you back here after sign-in.
    public_url: str = "http://localhost:8000"

    # Google sign-in (Calendar + Gmail). From your Google Cloud project's OAuth client.
    google_client_id: str | None = Field(default=None, validation_alias="GOOGLE_CLIENT_ID")
    google_client_secret: str | None = Field(default=None, validation_alias="GOOGLE_CLIENT_SECRET")

    # Blackboard: Calendar → settings → "Get external calendar link". Treat it like a password.
    blackboard_ics_url: str | None = None

    # Ordinal's morning briefing (local time, HH:MM). The scheduler can be turned off for tests.
    briefing_time: str = "06:00"
    scheduler: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _load_yaml(name: str, override: Path | None = None) -> dict[str, Any]:
    """Load config/<name>.yaml, falling back to <name>.example.yaml."""
    candidates = [override] if override else []
    candidates += [CONFIG_DIR / f"{name}.yaml", CONFIG_DIR / f"{name}.example.yaml"]
    for path in candidates:
        if path and path.exists():
            return yaml.safe_load(path.read_text()) or {}
    raise FileNotFoundError(f"No config found for {name}")


def load_brains_config() -> dict[str, Any]:
    return _load_yaml("brains", get_settings().brains_file)


def load_routing_config() -> dict[str, Any]:
    return _load_yaml("routing")


def load_agents_config() -> dict[str, Any]:
    return _load_yaml("agents")
