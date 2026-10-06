"""X (Twitter) API v2 client via tweepy — polls mentions/replies to the handle
and replies in-thread."""
from __future__ import annotations

import tweepy

from .base import Comment


class XClient:
    platform_name = "x"

    def __init__(self, handle: str, api_key: str, api_secret: str, access_token: str, access_secret: str):
        self.handle = handle
        self._client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_secret,
        )
        self._own_user_id: str | None = None

    def _own_id(self) -> str:
        if self._own_user_id is None:
            me = self._client.get_me()
            self._own_user_id = str(me.data.id)
        return self._own_user_id

    def fetch_new_comments(self) -> list[Comment]:
        own_id = self._own_id()
        resp = self._client.get_users_mentions(
            id=own_id,
            max_results=50,
            tweet_fields=["created_at", "author_id", "conversation_id", "text"],
        )
        out: list[Comment] = []
        for tweet in resp.data or []:
            out.append(
                Comment(
                    comment_id=str(tweet.id),
                    post_id=str(tweet.conversation_id),
                    author=str(tweet.author_id),
                    author_id=str(tweet.author_id),
                    text=tweet.text,
                    created_at=tweet.created_at.isoformat(),
                )
            )
        return out

    def post_reply(self, comment: Comment, reply_text: str) -> None:
        self._client.create_tweet(text=reply_text, in_reply_to_tweet_id=comment.comment_id)

    def is_own_comment(self, comment: Comment) -> bool:
        return comment.author_id == self._own_id()
