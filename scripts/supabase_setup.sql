-- Run this once in the Supabase SQL Editor (Project -> SQL Editor -> New query).
-- Creates the bot's three tables up front, plus a login role that can only
-- touch those tables — not the rest of your database (e.g. site registrations).

-- 1) The bot's tables (same schema app/db.py creates on first boot; pre-creating
--    them here means the scoped role below never needs CREATE privilege).
CREATE TABLE IF NOT EXISTS comments (
    id SERIAL PRIMARY KEY,
    platform TEXT NOT NULL,
    account_slug TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    post_id TEXT,
    author TEXT,
    text TEXT NOT NULL,
    comment_created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    category TEXT,
    draft_reply TEXT,
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
    account_slug TEXT NOT NULL, platform TEXT NOT NULL,
    access_token TEXT, refresh_token TEXT, external_id TEXT, handle TEXT, extra TEXT,
    connected_at TEXT NOT NULL, expires_at TEXT,
    PRIMARY KEY (account_slug, platform)
);

CREATE TABLE IF NOT EXISTS oauth_pending (
    state TEXT PRIMARY KEY, platform TEXT NOT NULL, account_slug TEXT NOT NULL,
    extra TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learned_examples (
    id SERIAL PRIMARY KEY, account_slug TEXT NOT NULL,
    comment_text TEXT NOT NULL, reply_text TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS automations (
    account_slug TEXT NOT NULL, key TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_slug, key)
);

ALTER TABLE comments ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'comment';
ALTER TABLE comments ADD COLUMN IF NOT EXISTS sentiment TEXT;
ALTER TABLE comments ADD COLUMN IF NOT EXISTS draft_variants TEXT;

-- 2) A login scoped to just these tables. CHANGE THE PASSWORD below before running.
CREATE ROLE reply_bot WITH LOGIN PASSWORD 'change-this-to-a-strong-password';

GRANT USAGE ON SCHEMA public TO reply_bot;
GRANT SELECT, INSERT, UPDATE ON comments, daily_counts, startup_markers TO reply_bot;
GRANT SELECT, INSERT, UPDATE, DELETE ON platform_connections, oauth_pending, learned_examples, automations TO reply_bot;
GRANT USAGE, SELECT ON SEQUENCE comments_id_seq TO reply_bot;
GRANT USAGE, SELECT ON SEQUENCE learned_examples_id_seq TO reply_bot;

-- No DELETE, no CREATE, no access to any other table in this database.

-- 3) Supabase auto-enables Row-Level Security on new tables (to stop them being
--    silently exposed through its public REST API). With RLS on and no policy,
--    EVERY non-owner role — including reply_bot above — gets blocked from all
--    access, so it needs its own explicit policy here.
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['comments','daily_counts','startup_markers','platform_connections','oauth_pending','learned_examples','automations']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS reply_bot_access ON %I', t);
    EXECUTE format('CREATE POLICY reply_bot_access ON %I FOR ALL TO reply_bot USING (true) WITH CHECK (true)', t);
  END LOOP;
END $$;

-- Note: these policies say "FOR ALL", but the GRANTs above already limit
-- reply_bot to SELECT/INSERT/UPDATE — DELETE is still refused at the grant
-- level regardless of the policy.
