from __future__ import annotations

import contextvars
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth, db, llm, scheduler
from ..config import load_accounts, load_voice, settings
from ..oauth import facebook as oauth_facebook
from ..oauth import instagram as oauth_instagram
from ..oauth import x_twitter as oauth_x
from ..oauth import youtube as oauth_youtube
from ..scheduler import build_clients

router = APIRouter()
templates = Jinja2Templates(directory="app/dashboard/templates")

# Set by require_login on every authenticated request, read by the current_user()
# Jinja global so base.html can show who's logged in without every route having
# to thread it through context manually.
_current_user: contextvars.ContextVar[dict | None] = contextvars.ContextVar("current_user", default=None)

templates.env.globals["bot_status"] = lambda: {**scheduler.runtime.status(), "mode": settings.bot_mode}
templates.env.globals["current_user"] = lambda: _current_user.get()


def require_login(request: Request) -> dict:
    # Fully-open mode: no users provisioned and no bootstrap password set.
    # Anyone with the URL gets straight in — only true if you've deliberately
    # left DASHBOARD_PASSWORD blank with no team accounts created.
    if db.count_users() == 0 and not settings.dashboard_password:
        user = {"username": "anonymous", "role": "admin"}
        _current_user.set(user)
        return user

    session = auth.read_session_cookie(request.cookies.get(auth.COOKIE_NAME))
    if not session:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    _current_user.set(session)
    return session


def require_admin(user: dict = Depends(require_login)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Admin access required")
    return user


def _daily_cap_usage() -> list[dict]:
    """How much of today's daily_reply_cap each connected account+platform has
    used, for the dashboard stats strip."""
    return [
        {
            "slug": conn["account_slug"],
            "platform": conn["platform"],
            "used": db.today_count(conn["account_slug"], conn["platform"]),
            "cap": settings.daily_reply_cap,
        }
        for conn in db.list_connections()
    ]


@router.get("/", response_class=HTMLResponse)
def inbox(request: Request, status: str = "all", user: dict = Depends(require_login)):
    if status == "queued":
        rows = db.list_queue()
    elif status == "held":
        rows = db.list_held()
    elif status == "failed":
        rows = db.list_failed()
    else:
        status = "all"
        rows = db.list_inbox()

    parsed_rows = []
    for row in rows:
        row = dict(row)
        try:
            row["variants"] = json.loads(row.get("draft_variants") or "[]")
        except (json.JSONDecodeError, TypeError):
            row["variants"] = []
        parsed_rows.append(row)

    counts = db.counts_snapshot()
    return templates.TemplateResponse(
        request,
        "inbox.html",
        {"rows": parsed_rows, "active_status": status, "counts": counts, "cap_usage": _daily_cap_usage()},
    )


@router.get("/history", response_class=HTMLResponse)
def history(request: Request, user: dict = Depends(require_login)):
    rows = db.list_history()
    return templates.TemplateResponse(request, "history.html", {"rows": rows})


@router.get("/ignored", response_class=HTMLResponse)
def ignored(request: Request, user: dict = Depends(require_login)):
    rows = db.list_ignored()
    return templates.TemplateResponse(request, "ignored.html", {"rows": rows})


@router.post("/api/approve/{row_id}")
def approve(row_id: int, variant_index: int = Form(0), user: dict = Depends(require_login)):
    row = db.get_comment(row_id)
    if not row or row["status"] != "queued":
        raise HTTPException(404, "Not found or already handled")

    clients = build_clients(settings)
    client = clients.get(row["account_slug"], {}).get(row["platform"])
    if not client:
        raise HTTPException(400, "Platform client not configured")

    try:
        variants = json.loads(row["draft_variants"] or "[]")
    except (json.JSONDecodeError, TypeError):
        variants = []
    reply_text = variants[variant_index] if variants and 0 <= variant_index < len(variants) else row["draft_reply"]

    from ..platforms.base import Comment as _Comment

    comment = _Comment(
        comment_id=row["comment_id"],
        post_id=row["post_id"],
        author=row["author"],
        author_id="",
        text=row["text"],
        created_at=row["comment_created_at"],
    )
    if row["channel"] == "dm":
        client.reply_dm(comment, reply_text)
    else:
        client.post_reply(comment, reply_text)
    db.update_comment(row_id, status="sent", sent_at=db.now_iso(), draft_reply=reply_text)
    db.increment_today_count(row["account_slug"], row["platform"])
    # Part 4: the voice guide grows automatically from what actually gets approved.
    db.add_learned_example(row["account_slug"], row["text"], reply_text)
    return RedirectResponse(url="/", status_code=303)


@router.post("/api/reject/{row_id}")
def reject(row_id: int, user: dict = Depends(require_login)):
    row = db.get_comment(row_id)
    if not row or row["status"] != "queued":
        raise HTTPException(404, "Not found or already handled")
    db.update_comment(row_id, status="rejected")
    return RedirectResponse(url="/", status_code=303)


@router.post("/api/held/{row_id}/draft-anyway")
def held_draft_anyway(row_id: int, user: dict = Depends(require_login)):
    row = db.get_comment(row_id)
    if not row or row["status"] != "held":
        raise HTTPException(404, "Not found or already handled")
    accounts = load_accounts()
    account_cfg = accounts.get(row["account_slug"])
    if not account_cfg:
        raise HTTPException(400, "Unknown account")
    scheduler.draft_anyway_for_held(row, account_cfg, settings, load_voice())
    return RedirectResponse(url="/held", status_code=303)


@router.post("/api/held/{row_id}/dismiss")
def held_dismiss(row_id: int, user: dict = Depends(require_login)):
    row = db.get_comment(row_id)
    if not row or row["status"] != "held":
        raise HTTPException(404, "Not found or already handled")
    scheduler.dismiss_held(row_id)
    return RedirectResponse(url="/held", status_code=303)


@router.post("/api/failed/{row_id}/retry")
def failed_retry(row_id: int, user: dict = Depends(require_login)):
    row = db.get_comment(row_id)
    if not row or row["status"] != "failed":
        raise HTTPException(404, "Not found or already handled")

    if row["draft_reply"]:
        # It already had a draft and failed at the send step — resend it rather
        # than drafting again.
        scheduler.resend_failed_comment(row, build_clients(settings))
    else:
        # Never got a draft (the LLM call itself failed) — try that again.
        accounts = load_accounts()
        account_cfg = accounts.get(row["account_slug"])
        if not account_cfg:
            raise HTTPException(400, "Unknown account")
        scheduler.retry_failed_comment(row, account_cfg, settings, load_voice())

    return RedirectResponse(url="/failed", status_code=303)


@router.post("/api/restart")
async def restart_bot(user: dict = Depends(require_login)):
    """Reloads accounts.yaml / tone/voice.md / .env-derived settings and
    restarts the poll + sender loops, without needing a full redeploy."""
    await scheduler.runtime.restart(settings)
    return RedirectResponse(url="/", status_code=303)


@router.get("/tiktok-helper", response_class=HTMLResponse)
def tiktok_helper_page(request: Request, user: dict = Depends(require_login)):
    accounts = load_accounts()
    return templates.TemplateResponse(
        request,
        "tiktok_helper.html",
        {"accounts": accounts, "draft": None, "note": None, "comment_text": "", "slug": next(iter(accounts), "")},
    )


@router.post("/tiktok-helper", response_class=HTMLResponse)
def tiktok_helper_submit(
    request: Request,
    comment_text: str = Form(...),
    slug: str = Form(...),
    user: dict = Depends(require_login),
):
    accounts = load_accounts()
    account_cfg = accounts.get(slug)
    if not account_cfg:
        raise HTTPException(400, "Unknown account")

    voice = load_voice()
    result = llm.classify_and_draft(
        comment_text=comment_text,
        account_cfg=account_cfg,
        voice=voice,
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
    )
    draft = result.reply_text if result.action == "reply" else None
    note = None if result.action == "reply" else f"Gemini suggests skipping this one (category: {result.category}). You can still reply manually if you disagree."

    return templates.TemplateResponse(
        request,
        "tiktok_helper.html",
        {
            "accounts": accounts,
            "draft": draft,
            "note": note,
            "comment_text": comment_text,
            "slug": slug,
        },
    )


# --- Connections: in-app OAuth, replacing manual .env token pasting ---

_OAUTH_MODULES = {"youtube": oauth_youtube, "instagram": oauth_instagram, "x": oauth_x, "facebook": oauth_facebook}


@router.get("/connections", response_class=HTMLResponse)
def connections_page(request: Request, user: dict = Depends(require_admin)):
    accounts = load_accounts()
    existing = {(c["account_slug"], c["platform"]): c for c in db.list_connections()}
    rows = []
    for slug, cfg in accounts.items():
        for platform in ("youtube", "instagram", "facebook", "x"):
            rows.append({
                "slug": slug,
                "display_name": cfg.get("display_name", slug),
                "platform": platform,
                "connection": existing.get((slug, platform)),
            })
    return templates.TemplateResponse(request, "connections.html", {"rows": rows})


@router.get("/connections/{platform}/start")
def connections_start(platform: str, account: str, user: dict = Depends(require_admin)):
    module = _OAUTH_MODULES.get(platform)
    if not module:
        raise HTTPException(404, "Unknown platform")
    return RedirectResponse(url=module.build_auth_url(account))


@router.get("/connections/youtube/callback", response_class=HTMLResponse)
def connections_youtube_callback(request: Request, code: str, state: str, user: dict = Depends(require_login)):
    try:
        fields = oauth_youtube.handle_callback(code, state)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"YouTube connect failed: {exc}")
    account_slug = fields.pop("account_slug")
    if not fields.get("refresh_token"):
        existing = db.get_connection(account_slug, "youtube")
        if existing:
            fields["refresh_token"] = existing["refresh_token"]
        else:
            raise HTTPException(
                400,
                "Google didn't return a refresh token (it only does on first consent). "
                "Revoke access at myaccount.google.com/permissions for this app and try Connect again.",
            )
    db.upsert_connection(account_slug, "youtube", **fields)
    return RedirectResponse(url="/connections", status_code=303)


@router.get("/connections/instagram/callback", response_class=HTMLResponse)
def connections_instagram_callback(request: Request, code: str, state: str, user: dict = Depends(require_login)):
    try:
        fields = oauth_instagram.handle_callback(code, state)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Instagram connect failed: {exc}")
    account_slug = fields.pop("account_slug")
    db.upsert_connection(account_slug, "instagram", **fields)
    return RedirectResponse(url="/connections", status_code=303)


@router.get("/connections/facebook/callback", response_class=HTMLResponse)
def connections_facebook_callback(request: Request, code: str, state: str, user: dict = Depends(require_login)):
    try:
        fields = oauth_facebook.handle_callback(code, state)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Facebook connect failed: {exc}")
    account_slug = fields.pop("account_slug")
    db.upsert_connection(account_slug, "facebook", **fields)
    return RedirectResponse(url="/connections", status_code=303)


@router.get("/connections/x/callback", response_class=HTMLResponse)
def connections_x_callback(request: Request, oauth_token: str, oauth_verifier: str, user: dict = Depends(require_login)):
    try:
        fields = oauth_x.handle_callback(oauth_token, oauth_verifier)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"X connect failed: {exc}")
    account_slug = fields.pop("account_slug")
    db.upsert_connection(account_slug, "x", **fields)
    return RedirectResponse(url="/connections", status_code=303)


@router.post("/connections/{platform}/disconnect")
def connections_disconnect(platform: str, account: str = Form(...), user: dict = Depends(require_admin)):
    db.delete_connection(account, platform)
    return RedirectResponse(url="/connections", status_code=303)


# --- Automations: a small, honest set of toggleable rules (not "100+") ---

_AUTOMATION_LABELS = {
    "auto_hide_spam": (
        "Auto-hide spam/toxic comments",
        "When a comment is held for a link/refund/reward-language reason, hide it on "
        "the platform immediately instead of just holding it for review (YouTube/Instagram only).",
    ),
    "instant_faq_reply": (
        "Instant FAQ auto-reply",
        "Skip the LLM and send a canned FAQ answer immediately when a comment closely "
        "matches one of this account's FAQ questions in accounts.yaml.",
    ),
    "notify_only_bug_reports": (
        "Always hold bug reports for a human",
        "Even in auto mode, bug-report-tagged comments go to the review queue instead "
        "of auto-sending — useful if you want a person checking every bug report.",
    ),
    "weekend_review_mode": (
        "Weekend review mode",
        "Force review mode on Saturdays/Sundays regardless of BOT_MODE, so nothing "
        "auto-sends while the team's offline.",
    ),
    "vip_priority_review": (
        "VIP priority review",
        "Authors listed in accounts.yaml's vip_authors: always get held for manual "
        "review, never auto-sent — for mods, press, partners, etc.",
    ),
    "max_reply_length_280": (
        "Cap reply length at 280 characters",
        "Truncates drafted replies at a sentence boundary if they exceed 280 characters "
        "— mainly matters for X, where longer replies would fail to post.",
    ),
    "auto_dismiss_stale_held": (
        "Auto-dismiss stale held comments",
        "Held comments older than 7 days automatically move to Ignored, so Held "
        "doesn't silently pile up forever.",
    ),
    "skip_low_signal_comments": (
        "Skip low-signal comments",
        "Comments under 3 words or emoji-only never reach the LLM at all — saves quota "
        "on comments with nothing to meaningfully reply to.",
    ),
    "auto_approve_high_confidence_positive": (
        "Auto-approve high-confidence positive replies",
        "Even in review mode, a clearly positive, safe-category reply sends immediately "
        "instead of waiting for a manual Approve click.",
    ),
    "flag_repeat_commenter": (
        "Flag repeat commenters",
        "If the same author comments 3+ times in 24 hours, hold for review instead of "
        "auto-handling — a lightweight signal against brigading/spam floods.",
    ),
    "reply_in_english_only": (
        "Always reply in English",
        "Opts out of automatic language-matching (Part 4) — replies are always drafted "
        "in English regardless of what language the comment is in.",
    ),
}


@router.get("/automations", response_class=HTMLResponse)
def automations_page(request: Request, user: dict = Depends(require_admin)):
    accounts = load_accounts()
    rows = []
    for slug, cfg in accounts.items():
        enabled = db.get_automations(slug)
        for key, (label, desc) in _AUTOMATION_LABELS.items():
            rows.append({
                "slug": slug,
                "display_name": cfg.get("display_name", slug),
                "key": key,
                "label": label,
                "desc": desc,
                "enabled": enabled[key],
            })
    return templates.TemplateResponse(request, "automations.html", {"rows": rows})


@router.post("/api/automations/{slug}/{key}/toggle")
def automations_toggle(slug: str, key: str, user: dict = Depends(require_admin)):
    if key not in _AUTOMATION_LABELS:
        raise HTTPException(404, "Unknown automation")
    current = db.get_automations(slug)[key]
    db.set_automation(slug, key, not current)
    return RedirectResponse(url="/automations", status_code=303)


# --- Login / logout / team accounts ---

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@router.post("/login")
def login_submit(username: str = Form(...), password: str = Form(...)):
    row = db.get_user_by_username(username)
    if not row or not auth.verify_password(password, row["password_hash"], row["salt"]):
        return RedirectResponse(url="/login?error=Wrong+username+or+password", status_code=303)
    token = auth.create_session_cookie(row["username"], row["role"])
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(auth.COOKIE_NAME, token, max_age=auth.SESSION_MAX_AGE_SECONDS, httponly=True, samesite="lax")
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, user: dict = Depends(require_admin)):
    rows = db.list_users()
    return templates.TemplateResponse(request, "users.html", {"rows": rows})


@router.post("/users/create")
def users_create(username: str = Form(...), password: str = Form(...), role: str = Form("reviewer"), user: dict = Depends(require_admin)):
    if db.get_user_by_username(username):
        raise HTTPException(400, "That username already exists")
    password_hash, salt = auth.hash_password(password)
    db.create_user(username, password_hash, salt, role)
    return RedirectResponse(url="/users", status_code=303)


@router.post("/users/{user_id}/delete")
def users_delete(user_id: int, user: dict = Depends(require_admin)):
    db.delete_user(user_id)
    return RedirectResponse(url="/users", status_code=303)


# --- Analytics (Part 3): real charts over the comment history, no external JS ---

@router.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request, user: dict = Depends(require_login)):
    from .. import charts

    daily_rows = db.stats_by_day(days=14)
    by_day: dict[str, dict] = {}
    for row in daily_rows:
        by_day.setdefault(row["day"], {})[row["status"]] = row["n"]
    volume_rows = [{"day": day, **counts} for day, counts in sorted(by_day.items())]

    sentiment_rows = [(row["sentiment"] or "unknown", row["n"]) for row in db.stats_by_sentiment()]
    category_rows = [(row["category"] or "unknown", row["n"]) for row in db.stats_by_category()]

    return templates.TemplateResponse(
        request,
        "analytics.html",
        {
            "volume_svg": charts.daily_volume_chart(volume_rows),
            "sentiment_svg": charts.categorical_bar_list(sentiment_rows),
            "category_svg": charts.magnitude_bar_list(category_rows),
            "has_data": bool(volume_rows or sentiment_rows or category_rows),
        },
    )
