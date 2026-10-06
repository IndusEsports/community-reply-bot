"""Password hashing (stdlib only, no new dependency) + signed session cookies
for the team-accounts login (replaces the old single HTTP-Basic password)."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import db
from .config import settings

COOKIE_NAME = "session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 14  # 2 weeks


def _serializer() -> URLSafeTimedSerializer:
    # Signing key: derived from DASHBOARD_PASSWORD if set, else a fixed dev
    # fallback — fine for local/offline use, you should set DASHBOARD_PASSWORD
    # (or any real secret) in production so sessions can't be forged.
    secret = settings.dashboard_password or "dev-only-insecure-secret"
    return URLSafeTimedSerializer(secret, salt="reply-bot-session")


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return digest.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, password_hash)


def ensure_bootstrap_admin() -> None:
    """If no users exist yet and DASHBOARD_PASSWORD is set, create one admin
    user named 'admin' with that password — so existing deployments keep
    working with zero manual migration steps."""
    if db.count_users() > 0:
        return
    if not settings.dashboard_password:
        return
    password_hash, salt = hash_password(settings.dashboard_password)
    db.create_user("admin", password_hash, salt, role="admin")


def create_session_cookie(username: str, role: str) -> str:
    return _serializer().dumps({"username": username, "role": role})


def read_session_cookie(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        return _serializer().loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except BadSignature:
        return None
