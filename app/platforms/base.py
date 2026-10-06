"""Common shape every platform client implements."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Comment:
    comment_id: str
    post_id: str
    author: str
    author_id: str
    text: str
    created_at: str   # ISO 8601, UTC


class PlatformClient(Protocol):
    platform_name: str

    def fetch_new_comments(self) -> list[Comment]:
        """Return comments not yet seen. Implementations should be safe to call
        repeatedly (the poller dedupes via the database, not the client)."""
        ...

    def post_reply(self, comment: Comment, reply_text: str) -> None:
        ...

    def is_own_comment(self, comment: Comment) -> bool:
        ...
