# Community Reply Bot - Setup Guide (for beginners)

## What this does
It watches the comments on **your own** posts (YouTube, Instagram, X). For each new comment, Gemini
decides: reply or skip. Positive / fun / bug-report / friendly question comments get a short, human
reply (bug reports get an empathetic one). Angry, spammy or risky comments are skipped.
Replies go out about 1.5 to 6 minutes after the comment, never later than 9 minutes.

**TikTok:** TikTok has no safe official way for a bot to reply, so you get a **TikTok helper page**
instead: paste the comment, get a reply in your voice, copy and paste it into TikTok.

## How it stays safe
- **Review mode first.** Every reply waits on a page for you to press Approve. When you trust it, switch to auto.
- It **ignores every comment that existed before it started**, and anything older than 60 minutes.
- It **never replies to the same comment twice** and never replies to your own comments.
- Anything that promises a refund / fix date / reward, or contains a link, is held for you — this
  check happens in plain code, not left up to the model, so it can't be talked around.
- Daily cap per account (default 150).

---

## STEP 1 - Get a Gemini API key (5 min, free)
1. Go to https://aistudio.google.com/apikey and sign in with a Google account.
2. Click **Create API key**. No card required for the free tier. Copy it. This is `GEMINI_API_KEY`.
3. Free tier has its own rate limits — check current numbers at https://ai.google.dev/pricing. If you
   outgrow it later, the same key starts billing automatically once you add a card.
4. The bot defaults to `GEMINI_MODEL=gemini-flash-lite-latest` on purpose: it's the "lite" tier, which
   gets a much bigger free daily quota than the full flash model (we measured the full model capping
   out at just 20 requests/day on a fresh key — easy to blow through in one afternoon of testing). Lite
   is still plenty good for short classify-and-draft replies. If you want the strongest possible
   drafts and don't mind a far lower daily cap (or you're paying), set `GEMINI_MODEL=gemini-flash-latest` instead.

## STEP 2 - Put your real voice in (10 min, the most important step)
Open `tone/voice.md`. At the bottom, under **MY REAL REPLIES**, paste 10-20 of your best real replies
(include the player comment above each). Also fix the "Never do these" list to match your taste.
Open `accounts.yaml` and write the `about`, `support_hint` and `faq` for each game. The bot will only
state facts that are in the FAQ.

## STEP 3 - Connect YouTube (free, ~20 min)
1. https://console.cloud.google.com -> create a project.
2. "APIs & Services" -> Library -> enable **YouTube Data API v3**.
3. "OAuth consent screen" -> External -> fill the basics -> add your Google account under **Test users**.
   Then press **Publish app** (otherwise the key expires every 7 days).
4. "Credentials" -> Create credentials -> **OAuth client ID** -> type **Desktop app**. Copy Client ID and Client Secret.
5. On your computer, install Python from python.org (tick "Add to PATH"), open a terminal in this folder and run:
   `pip install requests` then `python scripts/youtube_login.py`.
   Sign in with the Google account that owns the channel. It prints a **refresh token**.
6. Your channel ID: YouTube -> Settings -> Advanced settings -> Channel ID (starts with UC).
7. You now have `INDUS_YT_CHANNEL_ID`, `INDUS_YT_CLIENT_ID`, `INDUS_YT_CLIENT_SECRET`, `INDUS_YT_REFRESH_TOKEN`.

Limit: YouTube's free daily quota allows roughly 150-180 replies per day per Google project.

## STEP 4 - Connect Instagram (free, ~30 min)
Needs an Instagram **Business or Creator** account (free to switch in the Instagram app settings).
1. https://developers.facebook.com -> My Apps -> Create App -> type Business.
2. Add the product **Instagram** -> "API setup with Instagram login".
3. Add your Instagram account under Roles / "Add or remove Instagram testers" and accept the invite in the Instagram app.
4. Generate a token for the account with the permissions `instagram_business_basic` and
   `instagram_business_manage_comments`. Copy the access token (`INDUS_IG_ACCESS_TOKEN`) and the account ID (`INDUS_IG_USER_ID`).
5. These tokens expire roughly every 60 days — regenerate in the same place when Instagram starts
   rejecting it (a red error on the dashboard's History page is the signal).

For your own accounts this works while the app is in development mode (no Meta app review needed).

## STEP 5 - Connect X / Twitter (paid)
X no longer has a useful free tier. Check the current price at https://developer.x.com before you commit.
1. Sign up at developer.x.com, create a Project and App, set permissions to **Read and Write**.
2. Generate API Key, API Secret, Access Token and Access Token Secret (after setting Read and Write).
3. You get `INDUS_X_HANDLE` (without @), `INDUS_X_API_KEY`, `INDUS_X_API_SECRET`, `INDUS_X_ACCESS_TOKEN`, `INDUS_X_ACCESS_SECRET`.
Skip this whole step if you don't want X yet. Just leave it `INDUS_X_ENABLED=false`.

## STEP 6 - Turn platforms on
In `.env`, change `*_ENABLED=false` to `*_ENABLED=true` for each platform you connected.
To add a second game, copy the whole `indus` block in `accounts.yaml`, change the key and `slug` to
e.g. `primerush`, copy the matching block in `.env.example` and prefix its vars with `PRIMERUSH_`.

## OPTIONAL - Use Supabase instead of local storage
By default the bot remembers what it's answered in a local SQLite file (`bot.db`). If you already
run a Supabase project (e.g. for the main site), you can point the bot at that Postgres database
instead — no code changes, no local file to lose if a host gets wiped.

**Recommended: give the bot its own scoped login**, not your project's full admin credential — that
way a leaked `DATABASE_URL` can't touch your site's other tables (like registrations).
1. Supabase dashboard -> your project -> **SQL Editor -> New query**. Paste in `scripts/supabase_setup.sql`,
   change the placeholder password, and run it. This pre-creates the bot's 3 tables and a `reply_bot`
   login that can only read/write those tables.
2. Get the pooler host: click **Connect** at the top of your Supabase project -> **Session pooler**
   tab (not "Direct connection" — that host is IPv6-only now, and most home/office networks don't
   have a working IPv6 route, which will just hang or fail to resolve; not transaction pooler either,
   since this bot holds one long-lived connection rather than opening lots of short ones).
3. Build the connection string: `postgresql://reply_bot.<project-ref>:<your password>@<pooler-host>:5432/postgres`
   — note the username is `reply_bot.<project-ref>` (with the project ref appended), not just
   `reply_bot` — that's how Supabase's pooler routes to the right project.
4. Paste that into `.env` as `DATABASE_URL=...`.
5. Leave `DATABASE_URL` blank and it just uses the local SQLite file as before — nothing else changes.

(Simpler but less safe: skip the script and use the default admin connection string straight from
Project Settings -> Database. Faster, but that credential can touch every table in the database.)

## STEP 7 - Test on your computer first (optional but smart)
1. Copy `.env.example` to `.env`, fill in values. Set `DASHBOARD_PASSWORD`.
2. `pip install -r requirements.txt` then `python -m app.main` (or `uvicorn app.main:app --reload`).
3. Open http://localhost:8000 (username: anything, password: your DASHBOARD_PASSWORD).
4. Comment on your own post from another account. Within ~1-2 poll cycles a draft appears. Approve it.

(`python -m tests.test_offline` runs the built-in self-test with no keys — it should print `OK`.)

## STEP 8 - Put it online 24/7 on Railway
1. Put this folder in a **private** GitHub repository (never public: it must not contain your keys; `.env` is already excluded).
2. railway.app -> New Project -> Deploy from GitHub repo.
3. In the service: **Variables** tab -> add everything from `.env.example` with your real values
   (`BOT_MODE=review`, `DB_PATH=/data/bot.db`).
4. **Volumes** -> add a volume mounted at `/data` (so the bot remembers what it already answered).
5. **Settings -> Networking -> Generate Domain**. Open that link, log in. That is your control page.

## STEP 9 - Go live
Run in **review** mode for a few days. Read the drafts, fix `tone/voice.md` when something sounds off.
When 95%+ of drafts need no edits, set `BOT_MODE=auto` in Railway Variables. Check the History page
daily for the first week.

---

## Good to know
- **"Without API keys"?** Platforms don't allow bots that log in as you (it breaks their rules and risks banning
  your game accounts). The official APIs above are the safe way.
- Replies to comments on **all** your recent posts are covered (YouTube: whole channel, Instagram: latest 5 posts, X: anything sent to the handle).
- Costs: Gemini's free tier covers this kind of volume comfortably; Railway is a few dollars a month; X API is the pricey one.
- If a platform shows a red error on the control page, copy it to me and I'll fix it.
