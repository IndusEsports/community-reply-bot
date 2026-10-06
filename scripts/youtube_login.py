"""Run once per YouTube channel to get a refresh token (README step 3).

    pip install requests
    python scripts/youtube_login.py

Prompts for your OAuth Client ID + Secret (from Google Cloud Console credentials),
opens the consent flow in your browser, and prints the refresh token to paste into
.env as *_YT_REFRESH_TOKEN.
"""
from __future__ import annotations

import urllib.parse
import webbrowser

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob:auto"  # out-of-band / manual code entry


def main() -> None:
    client_id = input("OAuth Client ID: ").strip()
    client_secret = input("OAuth Client Secret: ").strip()

    params = {
        "client_id": client_id,
        "redirect_uri": "http://localhost",
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print("\nOpening your browser to sign in with the Google account that owns the channel...")
    print(f"If it doesn't open automatically, visit:\n{url}\n")
    webbrowser.open(url)

    print("After approving, your browser will redirect to a localhost URL that fails to load.")
    print("Copy the 'code=' value from that URL's address bar and paste it below.")
    code = input("code=").strip()

    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": "http://localhost",
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        print("No refresh_token returned. Make sure the OAuth consent screen is Published "
              "(not just in Testing), and that you used access_type=offline + prompt=consent "
              "(this script already does both) — Google only issues a refresh token the first "
              "time an app is authorized, so revoke access at myaccount.google.com/permissions "
              "and try again if you've run this before.")
        return

    print("\nSuccess! Your refresh token:\n")
    print(refresh_token)
    print("\nPaste this into .env as e.g. INDUS_YT_REFRESH_TOKEN=...")


if __name__ == "__main__":
    main()
