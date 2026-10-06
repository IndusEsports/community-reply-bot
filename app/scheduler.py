"""Two asyncio loops run inside the FastAPI process:
  - poll_loop:   fetches new comments per enabled account+platform, runs policy.py
                 then (if it passes) llm.py, and writes the result to the DB.
  - sender_loop: in auto mode, posts any queued reply whose scheduled_send_at is due.
"""
from __future__ import annotations

import asyncio
import json
import logging

from . import db, llm, policy
from .config import Settings, load_accounts, load_voice
from .platforms.base import PlatformClient
from .platforms.instagram import InstagramClient
from .platforms.x_twitter import XClient
from .platforms.youtube import YouTubeClient

log = logging.getLogger("reply_bot.scheduler")


class BotRuntime:
    """Owns the two background loops and exposes live status + a restart
    control so the dashboard can recover from a bad config edit (new
    accounts.yaml / voice.md) without a full redeploy."""

    def __init__(self) -> None:
        self._stop_event: asyncio.Event | None = None
        self._tasks: list[asyncio.Task] = []
        self.started_at: str | None = None
        self.poll_cycles: int = 0
        self.last_poll_started_at: str | None = None
        self.last_poll_finished_at: str | None = None
        self.last_poll_error: str | None = None

    def start(self, settings: Settings) -> None:
        self._stop_event = asyncio.Event()
        self._tasks = [
            asyncio.create_task(poll_loop(settings, self._stop_event, self)),
            asyncio.create_task(sender_loop(settings, self._stop_event)),
        ]
        self.started_at = db.now_iso()
        # Reset per-run stats so the status strip reflects *this* run, not
        # whatever happened before the last Restart click.
        self.poll_cycles = 0
        self.last_poll_started_at = None
        self.last_poll_finished_at = None
        self.last_poll_error = None

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []
        self.started_at = None

    async def restart(self, settings: Settings) -> None:
        log.info("Restarting bot loops (config will be re-read from disk)")
        await self.stop()
        self.start(settings)

    def status(self) -> dict:
        return {
            "running": bool(self._tasks) and not all(t.done() for t in self._tasks),
            "started_at": self.started_at,
            "poll_cycles": self.poll_cycles,
            "last_poll_started_at": self.last_poll_started_at,
            "last_poll_finished_at": self.last_poll_finished_at,
            "last_poll_error": self.last_poll_error,
        }


runtime = BotRuntime()


def build_clients(settings: Settings) -> dict[str, dict[str, PlatformClient]]:
    """Returns {account_slug: {platform_name: client}} for every connected combo.
    Per-account tokens come from Supabase (db.platform_connections, populated by
    the dashboard's /connections OAuth flow); the app-level OAuth client id/secret
    for each platform comes from Settings — one Google/Meta/X app serves every
    connected account."""
    clients: dict[str, dict[str, PlatformClient]] = {}
    for conn in db.list_connections():
        slug, platform = conn["account_slug"], conn["platform"]
        per_account = clients.setdefault(slug, {})

        if platform == "youtube":
            per_account["youtube"] = YouTubeClient(
                channel_id=conn["external_id"],
                client_id=settings.google_oauth_client_id,
                client_secret=settings.google_oauth_client_secret,
                refresh_token=conn["refresh_token"],
            )
        elif platform == "instagram":
            per_account["instagram"] = InstagramClient(
                user_id=conn["external_id"],
                access_token=conn["access_token"],
            )
        elif platform == "x":
            per_account["x"] = XClient(
                handle=conn["handle"] or "",
                api_key=settings.x_api_key,
                api_secret=settings.x_api_secret,
                access_token=conn["access_token"],
                access_secret=conn["refresh_token"],  # OAuth1 access-token secret, stored in this column
            )

    return clients


def _classify(account_slug: str, account_cfg: dict, comment_text: str, settings: Settings, voice: str) -> llm.LLMResult:
    """Shared LLM call site: always feeds in the account's auto-learned examples
    (Part 4) alongside the static voice guide."""
    learned = [dict(r) for r in db.list_learned_examples(account_slug)]
    return llm.classify_and_draft(
        comment_text=comment_text,
        account_cfg=account_cfg,
        voice=voice,
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        learned_examples=learned,
    )


def process_one_comment(account_slug: str, account_cfg: dict, platform: str, client: PlatformClient, comment, settings: Settings, voice: str, channel: str = "comment") -> None:
    if db.has_seen(platform, account_slug, comment.comment_id):
        return

    startup_time = db.get_startup_time(account_slug, platform)
    today = db.today_count(account_slug, platform)

    result = policy.evaluate(
        comment_text=comment.text,
        comment_created_at=comment.created_at,
        startup_time=startup_time,
        already_seen=False,
        is_own_comment=client.is_own_comment(comment),
        today_count=today,
        daily_cap=settings.daily_reply_cap,
    )

    base_fields = dict(
        platform=platform,
        account_slug=account_slug,
        channel=channel,
        comment_id=comment.comment_id,
        post_id=comment.post_id,
        author=comment.author,
        text=comment.text,
        comment_created_at=comment.created_at,
    )

    if result.decision == "ignore":
        db.insert_comment(**base_fields, status="ignored", reason=result.reason)
        return

    if result.decision == "hold":
        automations = db.get_automations(account_slug)
        if automations["auto_hide_spam"] and "hold" in result.reason and hasattr(client, "hide_comment"):
            try:
                client.hide_comment(comment)
                db.insert_comment(**base_fields, status="ignored", reason=f"auto-hidden ({result.reason})")
                return
            except Exception:  # noqa: BLE001 - fall through to a normal hold if hiding fails
                log.exception("auto_hide_spam failed for comment %s", comment.comment_id)
        db.insert_comment(**base_fields, status="held", reason=result.reason)
        return

    if db.get_automations(account_slug)["instant_faq_reply"]:
        faq_answer = policy.match_faq(comment.text, account_cfg.get("faq", []))
        if faq_answer:
            send_at = policy.scheduled_send_at(comment.created_at)
            db.insert_comment(
                **base_fields, status="queued", category="faq_instant", sentiment="question",
                draft_reply=faq_answer, draft_variants=json.dumps([faq_answer]), scheduled_send_at=send_at,
            )
            return

    try:
        llm_result = _classify(account_slug, account_cfg, comment.text, settings, voice)
    except Exception as exc:  # noqa: BLE001 - network/LLM failure must not crash the poller
        # Distinct from "held": this is a technical failure, not a policy decision,
        # so it belongs on the Failed page with a Retry button, not the Held page.
        log.exception("LLM call failed for %s/%s comment %s", platform, account_slug, comment.comment_id)
        db.insert_comment(**base_fields, status="failed", reason=f"LLM error: {exc}")
        return

    if llm_result.action == "skip":
        db.insert_comment(
            **base_fields, status="ignored", reason=f"skipped ({llm_result.category})",
            category=llm_result.category, sentiment=llm_result.sentiment,
        )
        return

    send_at = policy.scheduled_send_at(comment.created_at)
    db.insert_comment(
        **base_fields,
        status="queued",
        category=llm_result.category,
        sentiment=llm_result.sentiment,
        draft_reply=llm_result.reply_text,
        draft_variants=json.dumps(llm_result.reply_variants),
        scheduled_send_at=send_at,
    )


def retry_failed_comment(row, account_cfg: dict, settings: Settings, voice: str) -> None:
    """Re-runs the LLM step for a row stuck in 'failed' (e.g. after a Gemini
    outage clears). Leaves it 'failed' again (with the new error) if it still
    doesn't work."""
    try:
        llm_result = _classify(row["account_slug"], account_cfg, row["text"], settings, voice)
    except Exception as exc:  # noqa: BLE001
        log.exception("Retry failed again for comment row %s", row["id"])
        db.update_comment(row["id"], reason=f"LLM error (retry): {exc}")
        return

    if llm_result.action == "skip":
        db.update_comment(
            row["id"], status="ignored", reason=f"skipped on retry ({llm_result.category})",
            category=llm_result.category, sentiment=llm_result.sentiment,
        )
        return

    send_at = policy.scheduled_send_at(row["comment_created_at"])
    db.update_comment(
        row["id"], status="queued", category=llm_result.category, sentiment=llm_result.sentiment,
        draft_reply=llm_result.reply_text, draft_variants=json.dumps(llm_result.reply_variants),
        scheduled_send_at=send_at, reason=None,
    )


def draft_anyway_for_held(row, account_cfg: dict, settings: Settings, voice: str) -> None:
    """A human looked at a policy-held comment (link / refund-ish language /
    daily cap) and wants a draft produced despite the hold. Still lands in the
    normal review queue — this does not bypass Approve."""
    try:
        llm_result = _classify(row["account_slug"], account_cfg, row["text"], settings, voice)
    except Exception as exc:  # noqa: BLE001
        log.exception("Manual draft-anyway failed for comment row %s", row["id"])
        db.update_comment(row["id"], status="failed", reason=f"LLM error: {exc}")
        return

    if llm_result.action == "skip":
        db.update_comment(
            row["id"], status="ignored",
            reason=f"skipped after manual override ({llm_result.category})",
            category=llm_result.category, sentiment=llm_result.sentiment,
        )
        return

    send_at = policy.scheduled_send_at(row["comment_created_at"])
    db.update_comment(
        row["id"], status="queued", category=llm_result.category, sentiment=llm_result.sentiment,
        draft_reply=llm_result.reply_text, draft_variants=json.dumps(llm_result.reply_variants),
        scheduled_send_at=send_at, reason=None,
    )


def dismiss_held(row_id: int) -> None:
    db.update_comment(row_id, status="ignored", reason="dismissed by operator")


def resend_failed_comment(row, clients: dict[str, dict[str, PlatformClient]]) -> None:
    """Retry for a row that already had a draft and failed at the *send* step
    (platform API hiccup) rather than the LLM step — re-post the existing
    draft instead of drafting again."""
    from .platforms.base import Comment as _Comment

    client = clients.get(row["account_slug"], {}).get(row["platform"])
    if not client:
        db.update_comment(row["id"], reason="retry failed: platform client not configured")
        return

    comment = _Comment(
        comment_id=row["comment_id"], post_id=row["post_id"], author=row["author"],
        author_id="", text=row["text"], created_at=row["comment_created_at"],
    )
    try:
        if row["channel"] == "dm":
            client.reply_dm(comment, row["draft_reply"])
        else:
            client.post_reply(comment, row["draft_reply"])
        db.update_comment(row["id"], status="sent", sent_at=db.now_iso(), reason=None)
        db.increment_today_count(row["account_slug"], row["platform"])
    except Exception as exc:  # noqa: BLE001
        log.exception("Resend retry failed again for comment row %s", row["id"])
        db.update_comment(row["id"], reason=f"send error (retry): {exc}")


async def poll_loop(settings: Settings, stop_event: asyncio.Event, runtime: "BotRuntime | None" = None) -> None:
    accounts = load_accounts()
    voice = load_voice()
    clients = build_clients(settings)

    for slug, per_account in clients.items():
        for platform in per_account:
            db.mark_startup(slug, platform)

    while not stop_event.is_set():
        if runtime is not None:
            runtime.last_poll_started_at = db.now_iso()
            runtime.last_poll_error = None

        try:
            for slug, per_account in clients.items():
                account_cfg = accounts[slug]
                for platform, client in per_account.items():
                    try:
                        comments = client.fetch_new_comments()
                    except Exception as exc:  # noqa: BLE001 - one platform failing shouldn't stop the others
                        log.exception("fetch_new_comments failed for %s/%s", slug, platform)
                        if runtime is not None:
                            runtime.last_poll_error = f"{slug}/{platform} fetch error: {exc}"
                        continue
                    for comment in comments:
                        try:
                            process_one_comment(slug, account_cfg, platform, client, comment, settings, voice)
                        except Exception:  # noqa: BLE001
                            log.exception("Failed processing comment %s on %s/%s", comment.comment_id, slug, platform)

                    # Part 5: DM support (Instagram only for now — see fetch_new_dms docstring).
                    if hasattr(client, "fetch_new_dms"):
                        try:
                            dms = client.fetch_new_dms()
                        except Exception as exc:  # noqa: BLE001
                            log.exception("fetch_new_dms failed for %s/%s", slug, platform)
                            if runtime is not None:
                                runtime.last_poll_error = f"{slug}/{platform} DM fetch error: {exc}"
                            dms = []
                        for dm in dms:
                            try:
                                process_one_comment(slug, account_cfg, platform, client, dm, settings, voice, channel="dm")
                            except Exception:  # noqa: BLE001
                                log.exception("Failed processing DM %s on %s/%s", dm.comment_id, slug, platform)
        finally:
            if runtime is not None:
                runtime.poll_cycles += 1
                runtime.last_poll_finished_at = db.now_iso()

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.poll_interval_seconds)
        except asyncio.TimeoutError:
            pass


async def sender_loop(settings: Settings, stop_event: asyncio.Event) -> None:
    """Only auto-posts when BOT_MODE=auto. In review mode, the dashboard's
    /api/approve route sends instead."""
    clients = build_clients(settings)

    while not stop_event.is_set():
        if settings.auto_mode:
            from datetime import datetime, timezone as _tz

            is_weekend = datetime.now(_tz.utc).weekday() >= 5  # Sat=5, Sun=6

            due = db.list_due_queue(db.now_iso())
            for row in due:
                automations = db.get_automations(row["account_slug"])
                if automations["weekend_review_mode"] and is_weekend:
                    continue  # leave it queued for manual review this weekend
                if automations["notify_only_bug_reports"] and row["category"] == "bug_report":
                    continue  # always wants a human on bug reports, even in auto mode

                per_account = clients.get(row["account_slug"], {})
                client = per_account.get(row["platform"])
                if not client:
                    continue
                try:
                    from .platforms.base import Comment as _Comment

                    comment = _Comment(
                        comment_id=row["comment_id"],
                        post_id=row["post_id"],
                        author=row["author"],
                        author_id="",
                        text=row["text"],
                        created_at=row["comment_created_at"],
                    )
                    if row["channel"] == "dm":
                        client.reply_dm(comment, row["draft_reply"])
                    else:
                        client.post_reply(comment, row["draft_reply"])
                    db.update_comment(row["id"], status="sent", sent_at=db.now_iso())
                    db.increment_today_count(row["account_slug"], row["platform"])
                    db.add_learned_example(row["account_slug"], row["text"], row["draft_reply"])
                except Exception as exc:  # noqa: BLE001
                    log.exception("Failed to send reply for comment row %s", row["id"])
                    db.update_comment(row["id"], status="failed", reason=f"send error: {exc}")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=15)
        except asyncio.TimeoutError:
            pass
