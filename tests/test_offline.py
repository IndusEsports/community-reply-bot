"""Self-test: runs the core pipeline end-to-end with zero API keys and no network.

    python -m tests.test_offline

Exits non-zero (and prints which case failed) if anything's broken.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, llm, policy, scheduler  # noqa: E402
from app.platforms.base import Comment  # noqa: E402


def iso(dt: datetime) -> str:
    return dt.isoformat()


class FakeClient:
    """Minimal stand-in for a PlatformClient, no network calls."""

    platform_name = "fake"

    def __init__(self, own_id: str = "bot_account"):
        self.own_id = own_id
        self.sent: list[tuple[Comment, str]] = []

    def fetch_new_comments(self):
        return []

    def post_reply(self, comment: Comment, reply_text: str) -> None:
        self.sent.append((comment, reply_text))

    def is_own_comment(self, comment: Comment) -> bool:
        return comment.author_id == self.own_id


ACCOUNT_CFG = {
    "slug": "testgame",
    "display_name": "Test Game",
    "about": "A test game.",
    "support_hint": "Ask for details, don't promise anything.",
    "faq": [{"q": "when update", "a": "soon"}],
}


class PolicyTests(unittest.TestCase):
    def test_old_comment_ignored(self):
        old = iso(datetime.now(timezone.utc) - timedelta(minutes=90))
        result = policy.evaluate(
            comment_text="hi",
            comment_created_at=old,
            startup_time=None,
            already_seen=False,
            is_own_comment=False,
            today_count=0,
            daily_cap=150,
        )
        self.assertEqual(result.decision, "ignore")

    def test_predates_startup_ignored(self):
        now = datetime.now(timezone.utc)
        comment_time = iso(now - timedelta(minutes=5))
        startup_time = iso(now)
        result = policy.evaluate(
            comment_text="hi",
            comment_created_at=comment_time,
            startup_time=startup_time,
            already_seen=False,
            is_own_comment=False,
            today_count=0,
            daily_cap=150,
        )
        self.assertEqual(result.decision, "ignore")

    def test_duplicate_ignored(self):
        result = policy.evaluate(
            comment_text="hi",
            comment_created_at=iso(datetime.now(timezone.utc)),
            startup_time=None,
            already_seen=True,
            is_own_comment=False,
            today_count=0,
            daily_cap=150,
        )
        self.assertEqual(result.decision, "ignore")

    def test_link_held(self):
        result = policy.evaluate(
            comment_text="check this out https://example.com",
            comment_created_at=iso(datetime.now(timezone.utc)),
            startup_time=None,
            already_seen=False,
            is_own_comment=False,
            today_count=0,
            daily_cap=150,
        )
        self.assertEqual(result.decision, "hold")

    def test_refund_promise_held(self):
        result = policy.evaluate(
            comment_text="can I get a refund for this",
            comment_created_at=iso(datetime.now(timezone.utc)),
            startup_time=None,
            already_seen=False,
            is_own_comment=False,
            today_count=0,
            daily_cap=150,
        )
        self.assertEqual(result.decision, "hold")

    def test_daily_cap_held(self):
        result = policy.evaluate(
            comment_text="hi",
            comment_created_at=iso(datetime.now(timezone.utc)),
            startup_time=None,
            already_seen=False,
            is_own_comment=False,
            today_count=150,
            daily_cap=150,
        )
        self.assertEqual(result.decision, "hold")

    def test_normal_comment_proceeds(self):
        result = policy.evaluate(
            comment_text="this game is great!",
            comment_created_at=iso(datetime.now(timezone.utc)),
            startup_time=None,
            already_seen=False,
            is_own_comment=False,
            today_count=0,
            daily_cap=150,
        )
        self.assertEqual(result.decision, "proceed")

    def test_delay_within_bounds(self):
        now = iso(datetime.now(timezone.utc))
        send_at = datetime.fromisoformat(policy.scheduled_send_at(now))
        created = datetime.fromisoformat(now)
        delay_minutes = (send_at - created).total_seconds() / 60
        self.assertGreaterEqual(delay_minutes, policy.MIN_REPLY_DELAY_MINUTES - 0.01)
        self.assertLessEqual(delay_minutes, policy.HARD_MAX_REPLY_DELAY_MINUTES + 0.01)


class LLMParsingTests(unittest.TestCase):
    def test_parses_clean_json(self):
        raw = '{"action": "reply", "category": "positive", "reply_text": "thanks!"}'
        result = llm._parse_json_response(raw)
        self.assertEqual(result.action, "reply")
        self.assertEqual(result.reply_text, "thanks!")

    def test_strips_markdown_fences(self):
        raw = '```json\n{"action": "skip", "category": "angry", "reply_text": null}\n```'
        result = llm._parse_json_response(raw)
        self.assertEqual(result.action, "skip")

    def test_malformed_json_fails_safe_to_skip(self):
        result = llm._parse_json_response("not json at all")
        self.assertEqual(result.action, "skip")
        self.assertEqual(result.category, "llm_parse_error")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.init_db(self._tmp.name)

    def tearDown(self):
        db.close_db()
        os.unlink(self._tmp.name)

    def _settings(self):
        from app.config import Settings

        return Settings(gemini_api_key="unused", bot_mode="review", daily_reply_cap=150)

    def test_angry_comment_is_skipped_end_to_end(self):
        client = FakeClient()
        comment = Comment(
            comment_id="c1", post_id="p1", author="troll", author_id="troll_id",
            text="this game is trash uninstalling", created_at=iso(datetime.now(timezone.utc)),
        )
        with mock.patch("app.llm._call_gemini", return_value='{"action":"skip","category":"angry","reply_text":null}'):
            scheduler.process_one_comment("testgame", ACCOUNT_CFG, "fake", client, comment, self._settings(), "be nice")

        rows = db.list_history()
        self.assertEqual(len(rows), 0)  # "ignored" isn't in history (sent/rejected only)
        self.assertFalse(db.list_queue())

    def test_positive_comment_gets_queued(self):
        client = FakeClient()
        comment = Comment(
            comment_id="c2", post_id="p1", author="fan", author_id="fan_id",
            text="loved this update!", created_at=iso(datetime.now(timezone.utc)),
        )
        with mock.patch("app.llm._call_gemini", return_value='{"action":"reply","category":"positive","reply_text":"glad you liked it!"}'):
            scheduler.process_one_comment("testgame", ACCOUNT_CFG, "fake", client, comment, self._settings(), "be nice")

        queue = db.list_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["draft_reply"], "glad you liked it!")

    def test_link_comment_held_without_calling_llm(self):
        client = FakeClient()
        comment = Comment(
            comment_id="c3", post_id="p1", author="spammer", author_id="spammer_id",
            text="follow me at http://spam.example", created_at=iso(datetime.now(timezone.utc)),
        )
        with mock.patch("app.llm._call_gemini") as fake_call:
            scheduler.process_one_comment("testgame", ACCOUNT_CFG, "fake", client, comment, self._settings(), "be nice")
            fake_call.assert_not_called()
        self.assertFalse(db.list_queue())

    def test_duplicate_comment_processed_only_once(self):
        client = FakeClient()
        comment = Comment(
            comment_id="c4", post_id="p1", author="fan", author_id="fan_id",
            text="nice game", created_at=iso(datetime.now(timezone.utc)),
        )
        with mock.patch("app.llm._call_gemini", return_value='{"action":"reply","category":"positive","reply_text":"thanks!"}'):
            scheduler.process_one_comment("testgame", ACCOUNT_CFG, "fake", client, comment, self._settings(), "be nice")
            scheduler.process_one_comment("testgame", ACCOUNT_CFG, "fake", client, comment, self._settings(), "be nice")
        self.assertEqual(len(db.list_queue()), 1)

    def test_own_comment_ignored(self):
        client = FakeClient(own_id="fan_id")
        comment = Comment(
            comment_id="c5", post_id="p1", author="fan", author_id="fan_id",
            text="replying to myself", created_at=iso(datetime.now(timezone.utc)),
        )
        with mock.patch("app.llm._call_gemini") as fake_call:
            scheduler.process_one_comment("testgame", ACCOUNT_CFG, "fake", client, comment, self._settings(), "be nice")
            fake_call.assert_not_called()
        self.assertFalse(db.list_queue())

    def test_old_comment_never_reaches_llm(self):
        client = FakeClient()
        old_time = iso(datetime.now(timezone.utc) - timedelta(minutes=120))
        comment = Comment(
            comment_id="c6", post_id="p1", author="fan", author_id="fan_id",
            text="nice game", created_at=old_time,
        )
        with mock.patch("app.llm._call_gemini") as fake_call:
            scheduler.process_one_comment("testgame", ACCOUNT_CFG, "fake", client, comment, self._settings(), "be nice")
            fake_call.assert_not_called()
        self.assertFalse(db.list_queue())

    def test_llm_error_lands_in_failed_not_held(self):
        client = FakeClient()
        comment = Comment(
            comment_id="c7", post_id="p1", author="fan", author_id="fan_id",
            text="nice game", created_at=iso(datetime.now(timezone.utc)),
        )
        with mock.patch("app.llm._call_gemini", side_effect=RuntimeError("boom")):
            scheduler.process_one_comment("testgame", ACCOUNT_CFG, "fake", client, comment, self._settings(), "be nice")
        failed = db.list_failed()
        self.assertEqual(len(failed), 1)
        self.assertFalse(db.list_held())


class RecoveryActionTests(unittest.TestCase):
    """Held/Failed page actions: draft-anyway, dismiss, retry."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.init_db(self._tmp.name)

    def tearDown(self):
        db.close_db()
        os.unlink(self._tmp.name)

    def _settings(self):
        from app.config import Settings

        return Settings(gemini_api_key="unused", bot_mode="review", daily_reply_cap=150)

    def _insert_held(self, comment_id="h1", text="check this out https://example.com"):
        row_id = db.insert_comment(
            platform="fake", account_slug="testgame", comment_id=comment_id, post_id="p1",
            author="fan", text=text, comment_created_at=iso(datetime.now(timezone.utc)),
            status="held", reason="comment contains a link",
        )
        return db.get_comment(row_id)

    def _insert_failed(self, comment_id="f1", draft_reply=None):
        row_id = db.insert_comment(
            platform="fake", account_slug="testgame", comment_id=comment_id, post_id="p1",
            author="fan", text="nice game", comment_created_at=iso(datetime.now(timezone.utc)),
            status="failed", reason="LLM error: boom", draft_reply=draft_reply,
        )
        return db.get_comment(row_id)

    def test_draft_anyway_queues_a_reply(self):
        row = self._insert_held()
        with mock.patch("app.llm._call_gemini", return_value='{"action":"reply","category":"positive","reply_text":"thanks!"}'):
            scheduler.draft_anyway_for_held(row, ACCOUNT_CFG, self._settings(), "be nice")
        queue = db.list_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["draft_reply"], "thanks!")
        self.assertFalse(db.list_held())

    def test_draft_anyway_can_still_skip(self):
        row = self._insert_held()
        with mock.patch("app.llm._call_gemini", return_value='{"action":"skip","category":"spam","reply_text":null}'):
            scheduler.draft_anyway_for_held(row, ACCOUNT_CFG, self._settings(), "be nice")
        self.assertFalse(db.list_queue())
        self.assertEqual(len(db.list_ignored()), 1)

    def test_dismiss_held_moves_to_ignored(self):
        row = self._insert_held()
        scheduler.dismiss_held(row["id"])
        self.assertFalse(db.list_held())
        self.assertEqual(len(db.list_ignored()), 1)

    def test_retry_failed_without_draft_reruns_llm(self):
        row = self._insert_failed()
        with mock.patch("app.llm._call_gemini", return_value='{"action":"reply","category":"positive","reply_text":"hi!"}'):
            scheduler.retry_failed_comment(row, ACCOUNT_CFG, self._settings(), "be nice")
        queue = db.list_queue()
        self.assertEqual(len(queue), 1)
        self.assertFalse(db.list_failed())

    def test_retry_failed_still_failing_stays_failed(self):
        row = self._insert_failed()
        with mock.patch("app.llm._call_gemini", side_effect=RuntimeError("still down")):
            scheduler.retry_failed_comment(row, ACCOUNT_CFG, self._settings(), "be nice")
        self.assertEqual(len(db.list_failed()), 1)

    def test_resend_failed_with_existing_draft_does_not_call_llm(self):
        row = self._insert_failed(draft_reply="already drafted reply")
        client = FakeClient()
        with mock.patch("app.llm._call_gemini") as fake_call:
            scheduler.resend_failed_comment(row, {"testgame": {"fake": client}})
            fake_call.assert_not_called()
        self.assertEqual(len(client.sent), 1)
        self.assertFalse(db.list_failed())
        self.assertEqual(len(db.list_history()), 1)


class BotRuntimeTests(unittest.IsolatedAsyncioTestCase):
    """Smoke test for the start/stop/restart lifecycle used by the dashboard's
    Restart button. Uses the real accounts.yaml (ships with everything disabled),
    so no real network calls happen."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.init_db(self._tmp.name)

    def tearDown(self):
        db.close_db()
        os.unlink(self._tmp.name)

    async def test_start_reports_running_then_stop_reports_stopped(self):
        from app.config import Settings

        settings = Settings(gemini_api_key="unused", poll_interval_seconds=3600)
        runtime = scheduler.BotRuntime()
        runtime.start(settings)
        try:
            await asyncio.sleep(0.05)
            self.assertTrue(runtime.status()["running"])
        finally:
            await runtime.stop()
        self.assertFalse(runtime.status()["running"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
