"""YouTube Data API v3 client — polls the channel's recent comment threads and
posts top-level replies. Uses a refresh token obtained via scripts/youtube_login.py."""
from __future__ import annotations

import requests

from .base import Comment

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/youtube/v3"


class YouTubeClient:
    platform_name = "youtube"

    def __init__(self, channel_id: str, client_id: str, client_secret: str, refresh_token: str):
        self.channel_id = channel_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._access_token: str | None = None

    def _get_access_token(self) -> str:
        if self._access_token:
            return self._access_token
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
        resp.raise_for_status()
        self._access_token = resp.json()["access_token"]
        return self._access_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_access_token()}"}

    def fetch_new_comments(self) -> list[Comment]:
        """Pulls the most recent comment threads for the whole channel."""
        resp = requests.get(
            f"{API_BASE}/commentThreads",
            headers=self._headers(),
            params={
                "part": "snippet",
                "allThreadsRelatedToChannelId": self.channel_id,
                "order": "time",
                "maxResults": 50,
                "textFormat": "plainText",
            },
            timeout=20,
        )
        resp.raise_for_status()
        out: list[Comment] = []
        for item in resp.json().get("items", []):
            top = item["snippet"]["topLevelComment"]["snippet"]
            out.append(
                Comment(
                    comment_id=item["snippet"]["topLevelComment"]["id"],
                    post_id=item["snippet"]["videoId"],
                    author=top.get("authorDisplayName", ""),
                    author_id=top.get("authorChannelId", {}).get("value", ""),
                    text=top.get("textOriginal", ""),
                    created_at=top["publishedAt"],
                )
            )
        return out

    def post_reply(self, comment: Comment, reply_text: str) -> None:
        resp = requests.post(
            f"{API_BASE}/comments",
            headers=self._headers(),
            params={"part": "snippet"},
            json={
                "snippet": {
                    "parentId": comment.comment_id,
                    "textOriginal": reply_text,
                }
            },
            timeout=20,
        )
        resp.raise_for_status()

    def is_own_comment(self, comment: Comment) -> bool:
        return comment.author_id == self.channel_id
