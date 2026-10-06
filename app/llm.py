"""Gemini wrapper: one call per comment, asks for strict JSON back — including a
sentiment tag and up to 3 reply variants to pick from (Part 2 of the Replient-parity
build), grounded in both the static tone/voice.md guide and auto-learned examples
from previously approved replies (Part 4).

Kept as a single function (`classify_and_draft`) with the actual network call
split into `_call_gemini` so tests can monkeypatch just the network part.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

SYSTEM_TEMPLATE = """You are drafting a reply, AS THE GAME STUDIO, to a comment on a social
media post for "{display_name}". Follow the voice guide and facts below exactly.

--- VOICE GUIDE ---
{voice}

--- REAL EXAMPLES OF REPLIES THIS STUDIO HAS ACTUALLY APPROVED (learn the pattern) ---
{learned_examples}

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

Also tag the comment's sentiment/intent with one short word or two, e.g. "positive",
"angry", "spam", "bug_report", "question", "neutral". And detect the comment's
language (e.g. "english", "spanish", "portuguese", "tagalog") — {language_instruction}

Never promise a refund, a fix date, a reward, or compensation. Never include a
link or invite. Keep replies short (1-2 sentences), in the voice above.

If action is "reply", give up to 3 DIFFERENT short reply options (phrased
differently from each other, same facts/voice) so a human can pick the one they
like best. If action is "skip", reply_variants must be an empty list.

Respond with ONLY minified JSON, no markdown fences, matching exactly:
{{"action": "reply" or "skip", "category": "short label", "sentiment": "short label", "language": "detected language", "reply_variants": ["...", "...", "..."]}}
"""

_LANG_MATCH_INSTRUCTION = "write the reply variants in that SAME language, not English."
_LANG_ENGLISH_ONLY_INSTRUCTION = "but always write the reply variants in English regardless of the comment's language."


@dataclass
class LLMResult:
    action: str          # "reply" | "skip"
    category: str
    sentiment: str = "neutral"
    language: str = "english"
    reply_variants: list[str] = field(default_factory=list)

    @property
    def reply_text(self) -> str | None:
        """The first variant, for callers (e.g. the TikTok helper) that just want one answer."""
        return self.reply_variants[0] if self.reply_variants else None


def _format_learned_examples(learned_examples: list[dict] | None) -> str:
    if not learned_examples:
        return "(none yet — examples accumulate automatically as you approve replies)"
    # Caller passes newest-first (matches db.list_learned_examples); show oldest-first
    # so the prompt reads as a natural history.
    ordered = list(reversed(learned_examples))
    return "\n".join(
        f'- Comment: "{ex["comment_text"]}"\n  Reply: "{ex["reply_text"]}"' for ex in ordered
    )


def _build_prompt(
    comment_text: str, account_cfg: dict, voice: str, learned_examples: list[dict] | None, english_only: bool = False
) -> tuple[str, str]:
    faq_lines = "\n".join(f"- Q: {item['q']}\n  A: {item['a']}" for item in account_cfg.get("faq", []))
    system = SYSTEM_TEMPLATE.format(
        display_name=account_cfg.get("display_name", account_cfg.get("slug", "the game")),
        voice=voice.strip() or "(no voice guide provided yet)",
        learned_examples=_format_learned_examples(learned_examples),
        about=account_cfg.get("about", "").strip(),
        support_hint=account_cfg.get("support_hint", "").strip(),
        faq=faq_lines or "(none yet)",
        language_instruction=_LANG_ENGLISH_ONLY_INSTRUCTION if english_only else _LANG_MATCH_INSTRUCTION,
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
        variants = data.get("reply_variants") or []
        if not isinstance(variants, list):
            variants = [str(variants)]
        variants = [str(v) for v in variants if v][:3]
        if action == "skip":
            variants = []
        return LLMResult(
            action=action,
            category=data.get("category", "unknown"),
            sentiment=data.get("sentiment", "neutral"),
            language=data.get("language", "english"),
            reply_variants=variants,
        )
    except (json.JSONDecodeError, AttributeError):
        # Fail safe: if the model didn't return valid JSON, skip rather than guess.
        return LLMResult(action="skip", category="llm_parse_error", sentiment="unknown", reply_variants=[])


def classify_and_draft(
    comment_text: str,
    account_cfg: dict,
    voice: str,
    api_key: str,
    model: str,
    learned_examples: list[dict] | None = None,
    english_only: bool = False,
) -> LLMResult:
    system, user = _build_prompt(comment_text, account_cfg, voice, learned_examples, english_only)
    raw = _call_gemini(system, user, api_key, model)
    return _parse_json_response(raw)
