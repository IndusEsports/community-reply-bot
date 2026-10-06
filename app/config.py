"""Loads .env + accounts.yaml into plain settings objects."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest"))
    bot_mode: str = field(default_factory=lambda: os.getenv("BOT_MODE", "review"))
    dashboard_password: str = field(default_factory=lambda: os.getenv("DASHBOARD_PASSWORD", "change-me"))
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "./bot.db"))
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))
    poll_interval_seconds: int = field(default_factory=lambda: int(os.getenv("POLL_INTERVAL_SECONDS", "90")))
    daily_reply_cap: int = field(default_factory=lambda: int(os.getenv("DAILY_REPLY_CAP", "150")))

    @property
    def auto_mode(self) -> bool:
        return self.bot_mode.strip().lower() == "auto"


def load_accounts() -> dict:
    path = ROOT / "accounts.yaml"
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_voice() -> str:
    path = ROOT / "tone" / "voice.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def resolve_platform_config(account_cfg: dict, platform: str) -> dict | None:
    """Turn the *_env references in accounts.yaml into real values from the
    environment. Returns None if the platform is disabled or missing creds."""
    plat_cfg = account_cfg.get("platforms", {}).get(platform)
    if not plat_cfg:
        return None
    resolved = {}
    for key, env_name in plat_cfg.items():
        if key == "enabled_env":
            resolved["enabled"] = _env_bool(env_name, False)
        else:
            resolved[key.removesuffix("_env")] = os.getenv(env_name, "")
    if not resolved.get("enabled"):
        return None
    return resolved


settings = Settings()
