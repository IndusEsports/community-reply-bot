"""Facebook OAuth2 code flow for connecting a Page from the dashboard. Uses the
same META_APP_ID/META_APP_SECRET as Instagram (one Meta app covers both products).

Facebook's API needs a Page Access Token, not the user token the OAuth flow
first returns — so the callback does one extra step: list the user's pages via
/me/accounts and grab that page's own access token.

One-time setup you still have to do yourself: in your Meta app, add
  {PUBLIC_BASE_URL}/connections/facebook/callback
as a valid OAuth redirect URI, and enable the Facebook Login product with scopes
pages_show_list, pages_read_engagement, pages_manage_posts, pages_manage_engagement.
"""
from __future__ import annotations

import secrets
import urllib.parse

import requests

from .. import db
from ..config import settings

AUTH_URL = "https://www.facebook.com/v21.0/dialog/oauth"
TOKEN_URL = "https://graph.facebook.com/v21.0/oauth/access_token"
ACCOUNTS_URL = "https://graph.facebook.com/v21.0/me/accounts"
SCOPE = "pages_show_list,pages_read_engagement,pages_manage_posts,pages_manage_engagement"


def build_auth_url(account_slug: str) -> str:
    state = secrets.token_urlsafe(24)
    db.store_pending_oauth(state, "facebook", account_slug)
    params = {
        "client_id": settings.meta_app_id,
        "redirect_uri": f"{settings.public_base_url}/connections/facebook/callback",
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def handle_callback(code: str, state: str) -> dict:
    pending = db.pop_pending_oauth(state)
    if not pending:
        raise ValueError("OAuth state not found or expired — please click Connect again.")

    token_resp = requests.get(
        TOKEN_URL,
        params={
            "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret,
            "redirect_uri": f"{settings.public_base_url}/connections/facebook/callback",
            "code": code,
        },
        timeout=20,
    )
    token_resp.raise_for_status()
    user_access_token = token_resp.json()["access_token"]

    pages_resp = requests.get(
        ACCOUNTS_URL,
        params={"access_token": user_access_token},
        timeout=20,
    )
    pages_resp.raise_for_status()
    pages = pages_resp.json().get("data", [])
    if not pages:
        raise ValueError("No Facebook Pages found for this account — you need to manage at least one Page.")

    # First page the user manages. If they manage several and want a specific
    # one, that's a future "pick a page" step — out of scope for this pass.
    page = pages[0]

    return {
        "account_slug": pending["account_slug"],
        "access_token": page["access_token"],
        "external_id": page["id"],
        "handle": page.get("name"),
    }
