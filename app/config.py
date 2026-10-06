"""Loads .env + accounts.yaml into plain settings objects."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


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

    # One OAuth app per platform, shared across every connected account — the
    # per-account tokens produced by clicking "Connect" live in Supabase instead
    # (app/db.py: platform_connections), not here.
    public_base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "").rstrip("/"))
    google_oauth_client_id: str = field(default_factory=lambda: os.getenv("GOOGLE_OAUTH_CLIENT_ID", ""))
    google_oauth_client_secret: str = field(default_factory=lambda: os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", ""))
    meta_app_id: str = field(default_factory=lambda: os.getenv("META_APP_ID", ""))
    meta_app_secret: str = field(default_factory=lambda: os.getenv("META_APP_SECRET", ""))
    x_api_key: str = field(default_factory=lambda: os.getenv("X_API_KEY", ""))
    x_api_secret: str = field(default_factory=lambda: os.getenv("X_API_SECRET", ""))

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


settings = Settings()
