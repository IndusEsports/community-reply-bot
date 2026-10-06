"""Google OAuth2 code flow for connecting a YouTube channel from the dashboard,
replacing the old standalone scripts/youtube_login.py for new connections.

One-time setup you still have to do yourself (same as before, just once, not
per-channel): in Google Cloud Console, add
  {PUBLIC_BASE_URL}/connections/youtube/callback
as an authorized redirect URI on the OAuth client whose ID/secret go into
GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET.
"""
from __future__ import annotations

import secrets
import urllib.parse

import requests

from .. import db
from ..config import settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/youtube/v3"
SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"


def build_auth_url(account_slug: str) -> str:
    state = secrets.token_urlsafe(24)
    db.store_pending_oauth(state, "youtube", account_slug)
    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": f"{settings.public_base_url}/connections/youtube/callback",
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def handle_callback(code: str, state: str) -> dict:
    pending = db.pop_pending_oauth(state)
    if not pending:
        raise ValueError("OAuth state not found or expired — please click Connect again.")

    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "code": code,
            "redirect_uri": f"{settings.public_base_url}/connections/youtube/callback",
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    resp.raise_for_status()
    tokens = resp.json()

    channel_resp = requests.get(
        f"{API_BASE}/channels",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        params={"part": "snippet", "mine": "true"},
        timeout=20,
    )
    channel_resp.raise_for_status()
    items = channel_resp.json().get("items", [])
    channel_id = items[0]["id"] if items else None
    channel_title = items[0]["snippet"]["title"] if items else None

    return {
        "account_slug": pending["account_slug"],
        "access_token": tokens.get("access_token"),
        # Google only returns a refresh_token on first-ever consent for this
        # account+client; if this is a reconnect, keep the existing one.
        "refresh_token": tokens.get("refresh_token"),
        "external_id": channel_id,
        "handle": channel_title,
    }
