"""X (Twitter) OAuth 1.0a three-legged flow for connecting an account from the
dashboard. Unlike the Google/Meta code flows, X's callback carries its own
oauth_token (not our `state` param), so the pending row is keyed by that
instead — see handle_callback.

One-time setup you still have to do yourself: in the X Developer Portal, set
the app's callback URL to {PUBLIC_BASE_URL}/connections/x/callback, with
User authentication settings set to OAuth 1.0a, and X_API_KEY/X_API_SECRET
set from that app's keys.
"""
from __future__ import annotations

import tweepy

from .. import db
from ..config import settings


def build_auth_url(account_slug: str) -> str:
    handler = tweepy.OAuth1UserHandler(
        settings.x_api_key,
        settings.x_api_secret,
        callback=f"{settings.public_base_url}/connections/x/callback",
    )
    auth_url = handler.get_authorization_url()
    # Keyed by oauth_token (not a random state) because that's what X's
    # callback actually gives us back.
    db.store_pending_oauth(
        handler.request_token["oauth_token"], "x", account_slug,
        extra=handler.request_token["oauth_token_secret"],
    )
    return auth_url


def handle_callback(oauth_token: str, oauth_verifier: str) -> dict:
    pending = db.pop_pending_oauth(oauth_token)
    if not pending:
        raise ValueError("OAuth request expired — please click Connect again.")

    handler = tweepy.OAuth1UserHandler(settings.x_api_key, settings.x_api_secret)
    handler.request_token = {
        "oauth_token": oauth_token,
        "oauth_token_secret": pending["extra"],
    }
    access_token, access_token_secret = handler.get_access_token(oauth_verifier)

    client = tweepy.Client(
        consumer_key=settings.x_api_key,
        consumer_secret=settings.x_api_secret,
        access_token=access_token,
        access_token_secret=access_token_secret,
    )
    me = client.get_me()

    return {
        "account_slug": pending["account_slug"],
        "access_token": access_token,
        "refresh_token": access_token_secret,  # reused field — OAuth1 has no refresh token
        "external_id": str(me.data.id),
        "handle": me.data.username,
    }
