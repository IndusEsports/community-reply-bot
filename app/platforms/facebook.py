"""Facebook Pages Graph API client — same family as Instagram, polls comments on
the page's recent posts and replies. Needs pages_read_engagement + pages_manage_posts
+ pages_manage_engagement, and a Page Access Token (not a user token — see
app/oauth/facebook.py for the /me/accounts exchange that gets one)."""
from __future__ import annotations

import requests

from .base import Comment

API_BASE = "https://graph.facebook.com/v21.0"


class FacebookClient:
    platform_name = "facebook"

    def __init__(self, page_id: str, page_access_token: str):
        self.page_id = page_id
        self.access_token = page_access_token

    def fetch_new_comments(self) -> list[Comment]:
        posts_resp = requests.get(
            f"{API_BASE}/{self.page_id}/feed",
            params={"fields": "id", "limit": 5, "access_token": self.access_token},
            timeout=20,
        )
        posts_resp.raise_for_status()
        out: list[Comment] = []
        for post in posts_resp.json().get("data", []):
            c_resp = requests.get(
                f"{API_BASE}/{post['id']}/comments",
                params={
                    "fields": "id,message,from,created_time",
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
                        post_id=post["id"],
                        author=c.get("from", {}).get("name", ""),
                        author_id=c.get("from", {}).get("id", ""),
                        text=c.get("message", ""),
                        created_at=c["created_time"],
                    )
                )
        return out

    def post_reply(self, comment: Comment, reply_text: str) -> None:
        resp = requests.post(
            f"{API_BASE}/{comment.comment_id}/comments",
            data={"message": reply_text, "access_token": self.access_token},
            timeout=20,
        )
        resp.raise_for_status()

    def is_own_comment(self, comment: Comment) -> bool:
        return comment.author_id == self.page_id

    def hide_comment(self, comment: Comment) -> None:
        resp = requests.post(
            f"{API_BASE}/{comment.comment_id}",
            data={"is_hidden": "true", "access_token": self.access_token},
            timeout=20,
        )
        resp.raise_for_status()

    def delete_comment(self, comment: Comment) -> None:
        resp = requests.delete(
            f"{API_BASE}/{comment.comment_id}",
            params={"access_token": self.access_token},
            timeout=20,
        )
        resp.raise_for_status()
