from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from .. import db, llm, scheduler
from ..config import load_accounts, load_voice, resolve_platform_config, settings
from ..scheduler import build_clients

router = APIRouter()
templates = Jinja2Templates(directory="app/dashboard/templates")
security = HTTPBasic()

# Exposed as a callable in every template (base.html's nav/status strip) so
# individual routes don't each have to thread runtime status through context.
templates.env.globals["bot_status"] = lambda: {**scheduler.runtime.status(), "mode": settings.bot_mode}


def require_login(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    # Username can be anything; only the password is checked (matches README step 7).
    correct = secrets.compare_digest(credentials.password, settings.dashboard_password)
    if not correct:
        raise HTTPException(status_code=401, detail="Wrong password", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


def _daily_cap_usage() -> list[dict]:
    """How much of today's daily_reply_cap each enabled account+platform has used,
    for the dashboard stats strip."""
    accounts = load_accounts()
    usage = []
    for slug, account_cfg in accounts.items():
        for platform in ("youtube", "instagram", "x"):
            if resolve_platform_config(account_cfg, platform):
                usage.append({
                    "slug": slug,
                    "platform": platform,
                    "used": db.today_count(slug, platform),
                    "cap": settings.daily_reply_cap,
                })
    return usage


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: str = Depends(require_login)):
    queue = db.list_queue()
    counts = db.counts_snapshot()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"queue": queue, "counts": counts, "cap_usage": _daily_cap_usage()},
    )


@router.get("/history", response_class=HTMLResponse)
def history(request: Request, user: str = Depends(require_login)):
    rows = db.list_history()
    return templates.TemplateResponse(request, "history.html", {"rows": rows})


@router.get("/held", response_class=HTMLResponse)
def held(request: Request, user: str = Depends(require_login)):
    rows = db.list_held()
    return templates.TemplateResponse(request, "held.html", {"rows": rows})


@router.get("/failed", response_class=HTMLResponse)
def failed(request: Request, user: str = Depends(require_login)):
    rows = db.list_failed()
    return templates.TemplateResponse(request, "failed.html", {"rows": rows})


@router.get("/ignored", response_class=HTMLResponse)
def ignored(request: Request, user: str = Depends(require_login)):
    rows = db.list_ignored()
    return templates.TemplateResponse(request, "ignored.html", {"rows": rows})


@router.post("/api/approve/{row_id}")
def approve(row_id: int, user: str = Depends(require_login)):
    row = db.get_comment(row_id)
    if not row or row["status"] != "queued":
        raise HTTPException(404, "Not found or already handled")

    clients = build_clients(settings)
    client = clients.get(row["account_slug"], {}).get(row["platform"])
    if not client:
        raise HTTPException(400, "Platform client not configured")

    from ..platforms.base import Comment as _Comment

    comment = _Comment(
        comment_id=row["comment_id"],
        post_id=row["post_id"],
        author=row["author"],
        author_id="",
        text=row["text"],
        created_at=row["comment_created_at"],
    )
    client.post_reply(comment, row["draft_reply"])
    db.update_comment(row_id, status="sent", sent_at=db.now_iso())
    db.increment_today_count(row["account_slug"], row["platform"])
    return RedirectResponse(url="/", status_code=303)


@router.post("/api/reject/{row_id}")
def reject(row_id: int, user: str = Depends(require_login)):
    row = db.get_comment(row_id)
    if not row or row["status"] != "queued":
        raise HTTPException(404, "Not found or already handled")
    db.update_comment(row_id, status="rejected")
    return RedirectResponse(url="/", status_code=303)


@router.post("/api/held/{row_id}/draft-anyway")
def held_draft_anyway(row_id: int, user: str = Depends(require_login)):
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
def held_dismiss(row_id: int, user: str = Depends(require_login)):
    row = db.get_comment(row_id)
    if not row or row["status"] != "held":
        raise HTTPException(404, "Not found or already handled")
    scheduler.dismiss_held(row_id)
    return RedirectResponse(url="/held", status_code=303)


@router.post("/api/failed/{row_id}/retry")
def failed_retry(row_id: int, user: str = Depends(require_login)):
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
async def restart_bot(user: str = Depends(require_login)):
    """Reloads accounts.yaml / tone/voice.md / .env-derived settings and
    restarts the poll + sender loops, without needing a full redeploy."""
    await scheduler.runtime.restart(settings)
    return RedirectResponse(url="/", status_code=303)


@router.get("/tiktok-helper", response_class=HTMLResponse)
def tiktok_helper_page(request: Request, user: str = Depends(require_login)):
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
    user: str = Depends(require_login),
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
