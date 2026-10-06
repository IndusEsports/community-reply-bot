"""Persistence: seen comments, their lifecycle, and daily send counts.

Two backends behind the same function interface:
  - SQLite (default, zero-setup) — used for local dev and the offline test suite,
    so `python -m tests.test_offline` keeps working with no keys/network at all.
  - Postgres via Supabase — used in production when DATABASE_URL is set (get it
    from Supabase: Project Settings -> Database -> Connection string -> URI,
    the direct connection on port 5432, not the transaction pooler — this bot
    holds one long-lived connection rather than opening short-lived ones).

Every function below is backend-agnostic from the caller's side; the only
per-backend branching lives in this file.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    account_slug TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    post_id TEXT,
    channel TEXT NOT NULL DEFAULT 'comment',  -- 'comment' | 'dm'
    author TEXT,
    text TEXT NOT NULL,
    comment_created_at TEXT NOT NULL,
    status TEXT NOT NULL,              -- ignored | held | failed | queued | sent | rejected
    reason TEXT,                       -- why it was skipped/held
    category TEXT,                     -- llm classification label
    sentiment TEXT,                    -- llm sentiment/intent tag
    draft_reply TEXT,                  -- the variant that will actually be sent
    draft_variants TEXT,               -- JSON list of up to 3 drafted options
    scheduled_send_at TEXT,
    sent_at TEXT,
    discovered_at TEXT NOT NULL,
    UNIQUE(platform, account_slug, comment_id)
);

CREATE TABLE IF NOT EXISTS daily_counts (
    account_slug TEXT NOT NULL,
    platform TEXT NOT NULL,
    day TEXT NOT NULL,                 -- YYYY-MM-DD, UTC
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_slug, platform, day)
);

CREATE TABLE IF NOT EXISTS startup_markers (
    account_slug TEXT NOT NULL,
    platform TEXT NOT NULL,
    started_at TEXT NOT NULL,
    PRIMARY KEY (account_slug, platform)
);

CREATE TABLE IF NOT EXISTS platform_connections (
    account_slug TEXT NOT NULL,
    platform TEXT NOT NULL,
    access_token TEXT,
    refresh_token TEXT,
    external_id TEXT,                  -- channel id / IG user id / X user id
    handle TEXT,                       -- display handle, for the Connections page
    extra TEXT,                        -- JSON blob for anything platform-specific
    connected_at TEXT NOT NULL,
    expires_at TEXT,
    PRIMARY KEY (account_slug, platform)
);

CREATE TABLE IF NOT EXISTS oauth_pending (
    state TEXT PRIMARY KEY,            -- random token embedded in the OAuth redirect
    platform TEXT NOT NULL,
    account_slug TEXT NOT NULL,
    extra TEXT,                        -- e.g. X's oauth_token_secret between the two hops
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learned_examples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_slug TEXT NOT NULL,
    comment_text TEXT NOT NULL,
    reply_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS automations (
    account_slug TEXT NOT NULL,
    key TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_slug, key)
);
"""

_SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS comments (
    id SERIAL PRIMARY KEY,
    platform TEXT NOT NULL,
    account_slug TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    post_id TEXT,
    channel TEXT NOT NULL DEFAULT 'comment',
    author TEXT,
    text TEXT NOT NULL,
    comment_created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    category TEXT,
    sentiment TEXT,
    draft_reply TEXT,
    draft_variants TEXT,
    scheduled_send_at TEXT,
    sent_at TEXT,
    discovered_at TEXT NOT NULL,
    UNIQUE(platform, account_slug, comment_id)
);

CREATE TABLE IF NOT EXISTS daily_counts (
    account_slug TEXT NOT NULL,
    platform TEXT NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_slug, platform, day)
);

CREATE TABLE IF NOT EXISTS startup_markers (
    account_slug TEXT NOT NULL,
    platform TEXT NOT NULL,
    started_at TEXT NOT NULL,
    PRIMARY KEY (account_slug, platform)
);

CREATE TABLE IF NOT EXISTS platform_connections (
    account_slug TEXT NOT NULL,
    platform TEXT NOT NULL,
    access_token TEXT,
    refresh_token TEXT,
    external_id TEXT,
    handle TEXT,
    extra TEXT,
    connected_at TEXT NOT NULL,
    expires_at TEXT,
    PRIMARY KEY (account_slug, platform)
);

CREATE TABLE IF NOT EXISTS oauth_pending (
    state TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    account_slug TEXT NOT NULL,
    extra TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learned_examples (
    id SERIAL PRIMARY KEY,
    account_slug TEXT NOT NULL,
    comment_text TEXT NOT NULL,
    reply_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS automations (
    account_slug TEXT NOT NULL,
    key TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_slug, key)
);
"""

_lock = threading.Lock()
_conn = None
_backend: str = "sqlite"


def init_db(db_path: str, database_url: str = "") -> None:
    """If database_url is set, connects to Postgres (Supabase). Otherwise falls
    back to a local SQLite file at db_path — this is the default so tests and
    local dev never need a database to exist."""
    global _conn, _backend
    if _conn is not None:
        close_db()

    if database_url:
        _backend = "postgres"
        _conn = psycopg2.connect(database_url)
        with _lock:
            try:
                with _conn.cursor() as cur:
                    cur.execute(_SCHEMA_POSTGRES)
                _conn.commit()
            except psycopg2.errors.InsufficientPrivilege:
                # Expected for a least-privilege role (e.g. scripts/supabase_setup.sql's
                # reply_bot) that deliberately has no CREATE on the schema — the tables
                # are pre-created by that script, not by the app. Postgres checks CREATE
                # privilege before checking "does it already exist", so this always
                # raises for that role even with IF NOT EXISTS. Confirm the tables are
                # actually there before treating it as fine.
                _conn.rollback()
                with _conn.cursor() as cur:
                    cur.execute(
                        "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                        "AND tablename IN ('comments','daily_counts','startup_markers',"
                        "'platform_connections','oauth_pending','learned_examples','automations')"
                    )
                    found = {row[0] for row in cur.fetchall()}
                required = {
                    "comments", "daily_counts", "startup_markers",
                    "platform_connections", "oauth_pending", "learned_examples", "automations",
                }
                missing = required - found
                if missing:
                    raise RuntimeError(
                        f"DATABASE_URL's role can't CREATE TABLE and these tables don't exist yet: "
                        f"{missing}. Run scripts/supabase_setup.sql as an admin first."
                    )
    else:
        _backend = "sqlite"
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        with _lock:
            _conn.executescript(_SCHEMA_SQLITE)
            _conn.commit()


def close_db() -> None:
    """Closes the connection (also lets Windows delete the SQLite file, which
    it otherwise keeps locked). Tests call this in tearDown."""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def _sql(query: str) -> str:
    """Translates the `?` placeholders used throughout this file into psycopg2's
    `%s` style when running against Postgres. None of the SQL below contains a
    literal '?' outside of placeholders, so this is a safe blanket swap."""
    return query.replace("?", "%s") if _backend == "postgres" else query


@contextmanager
def _cursor():
    assert _conn is not None, "init_db() must be called before using the database"
    with _lock:
        if _backend == "postgres":
            cur = _conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = _conn.cursor()
        try:
            yield cur
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise
        finally:
            cur.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mark_startup(account_slug: str, platform: str) -> None:
    """Record 'when the bot first saw this account+platform' so pre-existing
    comments are never replied to."""
    with _cursor() as cur:
        if _backend == "postgres":
            cur.execute(
                "INSERT INTO startup_markers (account_slug, platform, started_at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (account_slug, platform, now_iso()),
            )
        else:
            cur.execute(
                "INSERT OR IGNORE INTO startup_markers (account_slug, platform, started_at) VALUES (?, ?, ?)",
                (account_slug, platform, now_iso()),
            )


def get_startup_time(account_slug: str, platform: str) -> str | None:
    with _cursor() as cur:
        cur.execute(
            _sql("SELECT started_at FROM startup_markers WHERE account_slug=? AND platform=?"),
            (account_slug, platform),
        )
        row = cur.fetchone()
        return row["started_at"] if row else None


def has_seen(platform: str, account_slug: str, comment_id: str) -> bool:
    with _cursor() as cur:
        cur.execute(
            _sql("SELECT 1 FROM comments WHERE platform=? AND account_slug=? AND comment_id=?"),
            (platform, account_slug, comment_id),
        )
        return cur.fetchone() is not None


def insert_comment(**fields) -> int | None:
    fields.setdefault("discovered_at", now_iso())
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    with _cursor() as cur:
        if _backend == "postgres":
            cur.execute(
                _sql(f"INSERT INTO comments ({cols}) VALUES ({placeholders}) ON CONFLICT DO NOTHING RETURNING id"),
                tuple(fields.values()),
            )
            row = cur.fetchone()
            return row["id"] if row else None
        else:
            cur.execute(
                f"INSERT OR IGNORE INTO comments ({cols}) VALUES ({placeholders})",
                tuple(fields.values()),
            )
            return cur.lastrowid


def update_comment(comment_row_id: int, **fields) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    with _cursor() as cur:
        cur.execute(
            _sql(f"UPDATE comments SET {sets} WHERE id=?"),
            (*fields.values(), comment_row_id),
        )


def get_comment(comment_row_id: int):
    with _cursor() as cur:
        cur.execute(_sql("SELECT * FROM comments WHERE id=?"), (comment_row_id,))
        return cur.fetchone()


def list_queue() -> list:
    with _cursor() as cur:
        cur.execute("SELECT * FROM comments WHERE status='queued' ORDER BY scheduled_send_at ASC")
        return cur.fetchall()


def list_due_queue(now: str) -> list:
    with _cursor() as cur:
        cur.execute(
            _sql("SELECT * FROM comments WHERE status='queued' AND scheduled_send_at<=? ORDER BY scheduled_send_at ASC"),
            (now,),
        )
        return cur.fetchall()


def list_history(limit: int = 200) -> list:
    with _cursor() as cur:
        cur.execute(
            _sql("SELECT * FROM comments WHERE status IN ('sent','rejected') ORDER BY id DESC LIMIT ?"),
            (limit,),
        )
        return cur.fetchall()


def list_held(limit: int = 200) -> list:
    """Policy-held comments (link / refund-fix-reward language / daily cap) —
    these never reached the LLM, so they need a human call: draft anyway, or dismiss."""
    with _cursor() as cur:
        cur.execute(_sql("SELECT * FROM comments WHERE status='held' ORDER BY id DESC LIMIT ?"), (limit,))
        return cur.fetchall()


def list_failed(limit: int = 200) -> list:
    """Comments where the LLM call or the send itself errored out (not a policy
    decision) — worth retrying once the transient issue clears."""
    with _cursor() as cur:
        cur.execute(_sql("SELECT * FROM comments WHERE status='failed' ORDER BY id DESC LIMIT ?"), (limit,))
        return cur.fetchall()


def list_ignored(limit: int = 200) -> list:
    """Comments the bot deliberately left untouched: duplicates, pre-startup,
    too old, the account's own comments, or an angry/spam LLM skip."""
    with _cursor() as cur:
        cur.execute(_sql("SELECT * FROM comments WHERE status='ignored' ORDER BY id DESC LIMIT ?"), (limit,))
        return cur.fetchall()


def counts_snapshot() -> dict[str, int]:
    """All-time count per status, for the dashboard's stats strip."""
    with _cursor() as cur:
        cur.execute("SELECT status, COUNT(*) AS n FROM comments GROUP BY status")
        rows = cur.fetchall()
        return {row["status"]: row["n"] for row in rows}


def today_count(account_slug: str, platform: str) -> int:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _cursor() as cur:
        cur.execute(
            _sql("SELECT count FROM daily_counts WHERE account_slug=? AND platform=? AND day=?"),
            (account_slug, platform, day),
        )
        row = cur.fetchone()
        return row["count"] if row else 0


def increment_today_count(account_slug: str, platform: str) -> None:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _cursor() as cur:
        cur.execute(
            _sql(
                """INSERT INTO daily_counts (account_slug, platform, day, count) VALUES (?, ?, ?, 1)
                   ON CONFLICT(account_slug, platform, day) DO UPDATE SET count = count + 1"""
            ),
            (account_slug, platform, day),
        )


def list_inbox(limit: int = 300) -> list:
    """Unified feed: everything that needs (or recently needed) a human look —
    queued, held, and failed — newest first. Backs the single /inbox page."""
    with _cursor() as cur:
        cur.execute(
            _sql("SELECT * FROM comments WHERE status IN ('queued','held','failed') ORDER BY id DESC LIMIT ?"),
            (limit,),
        )
        return cur.fetchall()


# --- platform_connections: per-account OAuth tokens, managed by the Connections page ---

def upsert_connection(account_slug: str, platform: str, **fields) -> None:
    fields.setdefault("connected_at", now_iso())
    cols = ["account_slug", "platform"] + list(fields.keys())
    values = [account_slug, platform] + list(fields.values())
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{k}=EXCLUDED.{k}" if _backend == "postgres" else f"{k}=excluded.{k}" for k in fields)
    with _cursor() as cur:
        cur.execute(
            _sql(
                f"INSERT INTO platform_connections ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(account_slug, platform) DO UPDATE SET {updates}"
            ),
            tuple(values),
        )


def get_connection(account_slug: str, platform: str):
    with _cursor() as cur:
        cur.execute(
            _sql("SELECT * FROM platform_connections WHERE account_slug=? AND platform=?"),
            (account_slug, platform),
        )
        return cur.fetchone()


def list_connections() -> list:
    with _cursor() as cur:
        cur.execute("SELECT * FROM platform_connections")
        return cur.fetchall()


def delete_connection(account_slug: str, platform: str) -> None:
    with _cursor() as cur:
        cur.execute(
            _sql("DELETE FROM platform_connections WHERE account_slug=? AND platform=?"),
            (account_slug, platform),
        )


# --- oauth_pending: short-lived state between an OAuth redirect and its callback ---

def store_pending_oauth(state: str, platform: str, account_slug: str, extra: str | None = None) -> None:
    with _cursor() as cur:
        cur.execute(
            _sql("INSERT INTO oauth_pending (state, platform, account_slug, extra, created_at) VALUES (?, ?, ?, ?, ?)"),
            (state, platform, account_slug, extra, now_iso()),
        )


def pop_pending_oauth(state: str):
    with _cursor() as cur:
        cur.execute(_sql("SELECT * FROM oauth_pending WHERE state=?"), (state,))
        row = cur.fetchone()
        if row:
            cur.execute(_sql("DELETE FROM oauth_pending WHERE state=?"), (state,))
        return row


# --- learned_examples: auto-growing voice examples from approved replies ---

def add_learned_example(account_slug: str, comment_text: str, reply_text: str) -> None:
    with _cursor() as cur:
        cur.execute(
            _sql(
                "INSERT INTO learned_examples (account_slug, comment_text, reply_text, created_at) "
                "VALUES (?, ?, ?, ?)"
            ),
            (account_slug, comment_text, reply_text, now_iso()),
        )


def list_learned_examples(account_slug: str, limit: int = 8) -> list:
    """Most recent examples first; llm.py reverses this so the prompt reads oldest-first."""
    with _cursor() as cur:
        cur.execute(
            _sql("SELECT * FROM learned_examples WHERE account_slug=? ORDER BY id DESC LIMIT ?"),
            (account_slug, limit),
        )
        return cur.fetchall()


# --- automations: per-account toggle switches ---

_AUTOMATION_DEFAULTS = {
    "auto_hide_spam": False,
    "instant_faq_reply": False,
    "notify_only_bug_reports": False,
    "weekend_review_mode": False,
}


def get_automations(account_slug: str) -> dict[str, bool]:
    with _cursor() as cur:
        cur.execute(_sql("SELECT key, enabled FROM automations WHERE account_slug=?"), (account_slug,))
        overrides = {row["key"]: bool(row["enabled"]) for row in cur.fetchall()}
    return {**_AUTOMATION_DEFAULTS, **overrides}


def set_automation(account_slug: str, key: str, enabled: bool) -> None:
    with _cursor() as cur:
        updates = "enabled=EXCLUDED.enabled" if _backend == "postgres" else "enabled=excluded.enabled"
        cur.execute(
            _sql(
                f"INSERT INTO automations (account_slug, key, enabled) VALUES (?, ?, ?) "
                f"ON CONFLICT(account_slug, key) DO UPDATE SET {updates}"
            ),
            (account_slug, key, int(enabled)),
        )
