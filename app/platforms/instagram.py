"""Instagram Graph API client — polls comments on the account's latest 5 posts
and replies. Needs instagram_business_basic + instagram_business_manage_comments."""
from __future__ import annotations

import requests

from .base import Comment

API_BASE = "https://graph.instagram.com/v21.0"


class InstagramClient:
    platform_name = "instagram"

    def __init__(self, user_id: str, access_token: str):
        self.user_id = user_id
        self.access_token = access_token

    def fetch_new_comments(self) -> list[Comment]:
        media_resp = requests.get(
            f"{API_BASE}/{self.user_id}/media",
            params={"fields": "id", "limit": 5, "access_token": self.access_token},
            timeout=20,
        )
        media_resp.raise_for_status()
        out: list[Comment] = []
        for media in media_resp.json().get("data", []):
            c_resp = requests.get(
                f"{API_BASE}/{media['id']}/comments",
                params={
                    "fields": "id,text,username,timestamp,from",
                    "limit": 50,
                    "access_token": self.access_token,
                },
                timeout=20,
            )
            c_resp.raise_for_status()
            for c in c_resp.json().get("data", []):
                out.append(
                    Comment(
                        comment_id=c["id"],
                        post_id=media["id"],
                        author=c.get("username", ""),
                        author_id=c.get("from", {}).get("id", c.get("username", "")),
                        text=c.get("text", ""),
                        created_at=c["timestamp"],
                    )
                )
        return out

    def post_reply(self, comment: Comment, reply_text: str) -> None:
        resp = requests.post(
            f"{API_BASE}/{comment.comment_id}/replies",
            data={"message": reply_text, "access_token": self.access_token},
            timeout=20,
        )
        resp.raise_for_status()

    def is_own_comment(self, comment: Comment) -> bool:
        return comment.author_id == self.user_id
