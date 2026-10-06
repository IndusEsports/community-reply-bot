"""Meta OAuth2 code flow (Instagram API with Instagram Login) for connecting an
Instagram Business/Creator account from the dashboard.

One-time setup you still have to do yourself: in your Meta app's "API setup with
Instagram login" product, add
  {PUBLIC_BASE_URL}/connections/instagram/callback
as a valid OAuth redirect URI, and set META_APP_ID / META_APP_SECRET.
"""
from __future__ import annotations

import secrets
import urllib.parse

import requests

from .. import db
from ..config import settings

AUTH_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
EXCHANGE_URL = "https://graph.instagram.com/access_token"
ME_URL = "https://graph.instagram.com/me"
SCOPE = "instagram_business_basic,instagram_business_manage_comments,instagram_business_manage_messages"


def build_auth_url(account_slug: str) -> str:
    state = secrets.token_urlsafe(24)
    db.store_pending_oauth(state, "instagram", account_slug)
    params = {
        "client_id": settings.meta_app_id,
        "redirect_uri": f"{settings.public_base_url}/connections/instagram/callback",
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def handle_callback(code: str, state: str) -> dict:
    pending = db.pop_pending_oauth(state)
    if not pending:
        raise ValueError("OAuth state not found or expired — please click Connect again.")

    short_resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": f"{settings.public_base_url}/connections/instagram/callback",
            "code": code,
        },
        timeout=20,
    )
    short_resp.raise_for_status()
    short_data = short_resp.json()
    user_id = str(short_data["user_id"])

    long_resp = requests.get(
        EXCHANGE_URL,
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": settings.meta_app_secret,
            "access_token": short_data["access_token"],
        },
        timeout=20,
    )
    long_resp.raise_for_status()
    long_data = long_resp.json()

    me_resp = requests.get(ME_URL, params={"fields": "username", "access_token": long_data["access_token"]}, timeout=20)
    me_resp.raise_for_status()
    username = me_resp.json().get("username")

    import datetime

    expires_at = None
    if "expires_in" in long_data:
        expires_at = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=long_data["expires_in"])
        ).isoformat()

    return {
        "account_slug": pending["account_slug"],
        "access_token": long_data["access_token"],
        "external_id": user_id,
        "handle": username,
        "expires_at": expires_at,
    }
