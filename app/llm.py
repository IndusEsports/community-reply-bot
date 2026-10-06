"""Gemini wrapper: one call per comment, asks for strict JSON back.

Kept as a single function (`classify_and_draft`) with the actual network call
split into `_call_gemini` so tests can monkeypatch just the network part.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

SYSTEM_TEMPLATE = """You are drafting a reply, AS THE GAME STUDIO, to a comment on a social
media post for "{display_name}". Follow the voice guide and facts below exactly.

--- VOICE GUIDE ---
{voice}

--- GAME FACTS (only state facts listed here, never invent new ones) ---
About: {about}
Support guidance: {support_hint}
FAQ:
{faq}

--- YOUR JOB ---
Read the player's comment and decide:
- "reply" if it's positive, fun, a friendly question, or a bug report (bug reports
  get an empathetic, helpful reply pointing at the support guidance above).
- "skip" if it's angry, spammy, trolling, or clearly not worth a reply.

Never promise a refund, a fix date, a reward, or compensation. Never include a
link or invite. Keep replies short (1-2 sentences), in the voice above.

Respond with ONLY minified JSON, no markdown fences, matching exactly:
{{"action": "reply" or "skip", "category": "short label", "reply_text": "..." or null}}
"""


@dataclass
class LLMResult:
    action: str          # "reply" | "skip"
    category: str
    reply_text: str | None


def _build_prompt(comment_text: str, account_cfg: dict, voice: str) -> tuple[str, str]:
    faq_lines = "\n".join(f"- Q: {item['q']}\n  A: {item['a']}" for item in account_cfg.get("faq", []))
    system = SYSTEM_TEMPLATE.format(
        display_name=account_cfg.get("display_name", account_cfg.get("slug", "the game")),
        voice=voice.strip() or "(no voice guide provided yet)",
        about=account_cfg.get("about", "").strip(),
        support_hint=account_cfg.get("support_hint", "").strip(),
        faq=faq_lines or "(none yet)",
    )
    user = f"Player comment:\n{comment_text.strip()}"
    return system, user


def _call_gemini(system: str, user: str, api_key: str, model: str) -> str:
    """Real network call. Imports the SDK lazily so offline tests never need it installed.

    Gemini's free tier returns transient 503s under load reasonably often, so this
    retries a couple of times with a short backoff before giving up — the caller
    (scheduler.process_one_comment) treats a final failure as "hold for a human"
    rather than crashing, but retrying here avoids unnecessarily holding comments
    that would have succeeded a second later.
    """
    import time

    from google import genai
    from google.genai import errors as genai_errors

    client = genai.Client(api_key=api_key)
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=model,
                contents=user,
                config={"system_instruction": system, "temperature": 0.7},
            )
            return response.text or ""
        except genai_errors.ServerError as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise last_exc


def _parse_json_response(raw: str) -> LLMResult:
    cleaned = raw.strip()
    # Gemini sometimes wraps JSON in ```json fences despite instructions — strip them.
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned.strip(), flags=re.IGNORECASE)
    try:
        data = json.loads(cleaned)
        action = data.get("action", "skip")
        if action not in ("reply", "skip"):
            action = "skip"
        reply_text = data.get("reply_text") if action == "reply" else None
        return LLMResult(action=action, category=data.get("category", "unknown"), reply_text=reply_text)
    except (json.JSONDecodeError, AttributeError):
        # Fail safe: if the model didn't return valid JSON, skip rather than guess.
        return LLMResult(action="skip", category="llm_parse_error", reply_text=None)


def classify_and_draft(comment_text: str, account_cfg: dict, voice: str, api_key: str, model: str) -> LLMResult:
    system, user = _build_prompt(comment_text, account_cfg, voice)
    raw = _call_gemini(system, user, api_key, model)
    return _parse_json_response(raw)
