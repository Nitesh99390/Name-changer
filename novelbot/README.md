# 📖 Novel Name Converter Bot — v2.0 (production)

Telegram bot + **Mini App** that rewrites the Chinese character names in a novel
into Indian names — **consistently, from the first page to the last** — plus a
Supabase metadata layer and a Render free-tier deployment.

> ### ⚠️ Pehle ye padho — aapka purana `api_id` / `api_hash` / token leak ho chuka hai
> Purane code mein `api_id=30417468` aur `api_hash="3905c3cb..."` **seedha code
> mein hardcoded** the. Agar woh file kisi public repo / gist / screenshot mein
> gayi, to woh **compromised** maane jayenge.
>
> **Karna kya hai:**
> 1. https://my.telegram.org/apps → apne app ka **API hash revoke/regenerate** karo.
> 2. @BotFather → `/mybots` → apna bot → **Revoke token**.
> 3. Naye values sirf **environment variables** mein daalo (`.env` locally,
>    Render dashboard mein production). Naya code mein **koi bhi** secret
>    hardcoded nahi hai — sab `app/config.py` se env se aata hai.

---

## Table of contents
1. [What was wrong in v1](#what-was-wrong-in-v1)
2. [Architecture](#architecture)
3. [Project layout](#project-layout)
4. [Local setup](#local-setup)
5. [Supabase setup](#supabase-setup)
6. [Render free-tier deploy](#render-free-tier-deploy)
7. [Mini App](#mini-app)
8. [API reference](#api-reference)
9. [Environment variables](#environment-variables)
10. [Free-tier limits & keep-alive](#free-tier-limits--keep-alive)
11. [Troubleshooting](#troubleshooting)

---

## What was wrong in v1

| # | Bug | Effect | v2 fix |
|---|-----|--------|--------|
| 1 | `api_id` / `api_hash` / token hardcoded | account takeover | all secrets from env, validated at boot, redacted in logs |
| 2 | `content.replace(old, new)` in a dict loop | **cascading corruption** — a replaced name gets replaced again | one compiled regex, **single pass**, keys sorted longest-first |
| 3 | plain substring replace | `Li` broke `Liang`, `Elizabeth`, `Delhi` | latin keys wrapped in `(?<![A-Za-z0-9])…(?![A-Za-z0-9])` |
| 4 | two hardcoded GitHub raw URLs | both return **HTTP 404** → bot dead on arrival | curated **bundled** resources (offline-safe); remote URLs optional |
| 5 | no `try/except` in handlers | one exception killed the worker silently | `@guard` on every handler + global error middleware |
| 6 | `filters.document` on any file | crash on PDF/ZIP | extension + MIME guard **before** download |
| 7 | full file read into RAM twice | OOM on 512 MB | line-aligned chunked streaming |
| 8 | no size / rate limit | free tier abuse, ban risk | `MAX_FILE_MB`, per-user sliding-window quota, concurrency semaphore |
| 9 | hardcoded `downloads/` dir | ephemeral disk confusion, no cleanup on crash | per-chat `/tmp` dir, `finally:` cleanup always runs |
| 10 | no web service | **Render free plan cannot run workers at all** | bot + aiohttp web server in one process/loop, binds `0.0.0.0:$PORT` |
| 11 | no persistence | no stats, no settings | Supabase REST layer, **metadata only** |
| 12 | `print()` debugging | unreadable logs | structured JSON logs with secret redaction |

---

## Architecture

```
                    ┌──────────────────── one Render Web Service (free) ────────────────────┐
                    │                        single asyncio event loop                    │
  Telegram ──►      │  ┌──────────────┐   ┌──────────────┐   ┌──────────────────────────┐   │
  long polling      │  │   Pyrogram   │   │   aiohttp    │   │  keep-alive self-ping    │   │
                    │  │  bot client  │   │  web server  │   │  (in-process, optional)  │   │
                    │  └──────┬───────┘   └──────┬───────┘   └──────────────────────────┘   │
                    │         │                  │                                        │
                    │  ┌──────▼──────────────────▼───────┐   ┌──────────────────────────┐   │
  Telegram Mini App │  │  services layer                 │   │  /tmp  (EPHEMERAL)       │   │
  (initData signed) │  │  job manager · rate limiter     │──►│  source + converted file │   │
                    │  │  file guards · chunked reader   │   │  deleted in `finally`    │   │
                    │  └──────┬──────────────────────────┘   └──────────────────────────┘   │
                    │         │                                                             │
                    │  ┌──────▼──────────────┐                                              │
                    │  │  core: name registry│  deterministic mapping (seed) + regex replacer│
                    │  └──────┬──────────────┘                                              │
                    └─────────┼─────────────────────────────────────────────────────────────┘
                              │  httpx (PostgREST) — metadata only, never novel text
                       ┌──────▼──────┐
                       │  Supabase   │  bot_users · conversion_jobs · bot_settings · name_mappings
                       └─────────────┘
```

**Key design decisions**

* **One process, one loop.** The bot and the web server share the same
  `asyncio` loop — no IPC, no Redis, no extra service. Render's free plan has no
  worker service type, so this is not a preference, it is a constraint.
* **Health endpoint is dependency-free.** `/health` returns `ok` in ~1 ms and
  never touches Supabase or Telegram, so a cold DB can't fail Render's probe.
* **Graceful degradation.** No Supabase ⇒ the bot still converts files, it just
  loses stats. A dead remote name URL ⇒ skipped, not fatal.
* **Determinism.** The mapping is built from a `seed`, so the same novel always
  produces the same names, and the checksum is recorded per job.

---

## Project layout

```
novelbot/
├── main.py                       # entry point: bot + web in one loop, graceful shutdown
├── requirements.txt              # pinned deps (TgCrypto wheels, no build chain)
├── render.yaml                   # Render Blueprint (free plan)
├── Dockerfile                    # optional container path
├── .env.example                  # every variable documented
├── app/
│   ├── config.py                 # typed, validated settings (no hardcoded secrets)
│   ├── errors.py                 # BotError hierarchy -> safe user messages
│   ├── logging_setup.py          # JSON logs + secret redaction filter
│   ├── runtime.py                # shared registry + compiled converters
│   ├── keepalive.py              # in-process self-ping (see limits section)
│   ├── bot/
│   │   ├── client.py             # Pyrogram factory (session in /tmp)
│   │   ├── keyboards.py          # Mini App + settings inline keyboards
│   │   ├── middleware.py         # @guard / @track_user / @rate_limit
│   │   └── handlers.py           # all commands + the document pipeline
│   ├── core/
│   │   ├── names.py              # pools, deterministic mapping builder
│   │   └── converter.py          # single-pass regex replacer (the bug fix)
│   ├── services/
│   │   ├── files.py              # guards, encoding ladder, chunked reader
│   │   ├── jobs.py               # concurrency + timeout + guaranteed cleanup
│   │   └── ratelimit.py          # sliding-window per-user quota
│   ├── db/
│   │   ├── supabase.py           # resilient PostgREST client (retry, non-fatal)
│   │   ├── repository.py         # metadata-only helpers
│   │   └── client.py             # shared singleton
│   └── web/
│       ├── app.py                # aiohttp routes + error middleware
│       ├── security.py           # Telegram initData HMAC verification
│       └── static/index.html     # the Mini App
├── resources/                    # bundled name pools (offline-safe)
│   ├── zh_surnames.tsv           # hanzi <TAB> pinyin
│   ├── zh_given.tsv
│   ├── in_first.txt
│   └── in_last.txt
├── sql/schema.sql                # Supabase tables + RLS + RPC
└── scripts/selftest.py           # 25+ offline checks, run before deploy
```

---

## Local setup

```bash
git clone <your-repo> && cd novelbot
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env        # then fill in the 3 Telegram values
python scripts/selftest.py  # 25+ offline checks - no network needed
python main.py              # http://localhost:8000/health  ->  ok
```

`.env` minimum:

```env
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=your_new_hash_from_my.telegram.org
BOT_TOKEN=123456:your_new_token_from_BotFather
SUPABASE_ENABLED=false      # optional locally
```

---

## Supabase setup

1. Create a free project at https://supabase.com
2. **SQL Editor → New query** → paste the whole of `sql/schema.sql` → **Run**.
3. **Project Settings → API** → copy:
   * `Project URL` → `SUPABASE_URL`
   * `service_role` secret → `SUPABASE_SERVICE_KEY`

   > The **service_role** key bypasses RLS. It is used server-side only and must
   > never reach the Mini App or a browser. Never put it in frontend code.
4. The bot registers its mapping version at boot and writes one row per job.

### What is stored — and what is never stored

| Stored (metadata) | Never stored |
|---|---|
| `user_id`, username, first/last name, language | ❌ the novel text |
| per-job counters: file **name**, size, characters, replacements, duration | ❌ the converted text |
| your preferences (`latin_names`, `keep_original`, …) | ❌ the uploaded file |
| mapping version + checksum used for each job | ❌ the generated file |
| | ❌ any path to a temp file |

Files exist only in `/tmp/<chat_id>/` for the duration of one job and are
deleted in a `finally:` block — even on crash, timeout or cancel.

---

## Render free-tier deploy

### Option A — Blueprint (recommended)

1. Push this repo to GitHub (`.gitignore` already excludes `.env`, `*.session`).
2. Render Dashboard → **New → Blueprint** → pick the repo.
3. Render reads `render.yaml`. Fill the `sync: false` values in the dashboard:
   `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `BOT_TOKEN`, `SUPABASE_URL`,
   `SUPABASE_SERVICE_KEY`, `MINI_APP_URL`, `RETURN_TO_BOT`.
4. Deploy. Build: `pip install -r requirements.txt`. Start: `python main.py`.
   Health check: `/health`.

### Option B — Manual Web Service

| Field | Value |
|---|---|
| Environment | Python 3 |
| Build command | `pip install --upgrade pip && pip install -r requirements.txt` |
| Start command | `python main.py` |
| Health check path | `/health` |
| Instance type | Free |
| Env vars | from `.env.example` |

`PORT` is injected by Render — do **not** set it manually in production.
`RENDER_EXTERNAL_URL` is also injected automatically and is used as the Mini App
base URL if you don't set `MINI_APP_URL`.

### Docker (optional)

```bash
docker build -t novelbot .
docker run --rm -p 8000:8000 --env-file .env novelbot
```

---

## Mini App

Served by the same web service at `/`. It is a **Telegram Mini App**, so it
must be opened from inside Telegram over HTTPS.

* Set `MINI_APP_URL=https://<your-service>.onrender.com` (or rely on
  `RENDER_EXTERNAL_URL`) and the bot attaches it as the **chat menu button**.
* `/start` also shows an **🚀 Open Mini App** inline button.

What it does:

| Section | Function |
|---|---|
| Status | live name-pair count, conversions, characters, DB health |
| Try it | paste ≤ 4000 chars → `/api/preview` shows the converted text instantly |
| Options | toggle pinyin / live progress / keep-original (saved to Supabase) |
| How to use | 3-step guide + close button |
| Privacy | plain-language storage policy |

**Security:** every data-bearing call must send Telegram's signed `initData`
(header `X-Init-Data`). It is verified with
`HMAC_SHA256(key=HMAC_SHA256("WebAppData", bot_token), msg=data_check_string)`
using constant-time comparison, and payloads older than 24 h are rejected. A
user id is **never** read from a plain query parameter.

---

## API reference

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/health`, `/ping` | – | Render health probe (instant, no deps) |
| `GET` | `/` | – | Mini App |
| `GET` | `/api/health` | – | JSON health + runtime stats |
| `GET` | `/api/mapping` | – | mapping version/checksum + 40 sample pairs |
| `POST` | `/api/preview` | – | convert a ≤4000-char snippet |
| `GET` | `/api/stats` | – | global aggregate counters |
| `GET` | `/api/me` | ✅ initData | caller's settings, quota, history |
| `POST` | `/api/settings` | ✅ initData | update caller's settings |

```bash
curl -s https://your-service.onrender.com/api/health | jq
curl -s -X POST https://your-service.onrender.com/api/preview \
     -H 'Content-Type: application/json' \
     -d '{"text":"王云看着李雪说：你终于来了。"}' | jq -r .output
```

---

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_API_ID` | – | **required** |
| `TELEGRAM_API_HASH` | – | **required**, secret |
| `BOT_TOKEN` | – | **required**, secret |
| `PORT` | `8000` | Render injects it |
| `PUBLIC_URL` | – | optional explicit base URL |
| `MINI_APP_URL` | – | HTTPS Mini App URL (enables the menu button) |
| `RETURN_TO_BOT` | `https://t.me` | "back to bot" link |
| `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` | – | metadata storage |
| `SUPABASE_ENABLED` | `true` | auto-disabled if keys are absent |
| `SUPABASE_TIMEOUT` / `SUPABASE_MAX_RETRIES` | `8` / `3` | REST resilience |
| `MAX_FILE_MB` | `20` | free-tier guard |
| `MAX_FILE_CHARS` | `4000000` | hard character ceiling |
| `RATE_LIMIT_PER_HOUR` | `20` | per user, sliding window |
| `MAX_CONCURRENT_JOBS` | `2` | global semaphore (512 MB RAM) |
| `JOB_TIMEOUT_SECONDS` | `240` | wall-clock budget |
| `MAX_KEY_LENGTH` | `24` | longest latin key accepted |
| `ALLOW_LATIN_SINGLE_TOKEN` | `true` | allow bare given-name pinyin |
| `NAMES_VERSION` / `NAMES_SEED` | `v2` / `novelbot-v2` | mapping identity |
| `NAMES_REMOTE_URLS` | – | optional extra sources (comma separated) |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `true` | structured logging |
| `KEEPALIVE_ENABLED` / `KEEPALIVE_INTERVAL` | `true` / `600` | in-process self-ping |
| `ADMIN_IDS` | – | comma-separated ids for `/admin` |
| `DATA_DIR` | `/tmp/novelbot` | **ephemeral** — keep it on `/tmp` |

---

## Free-tier limits & keep-alive

Render free plan, honestly:

* **512 MB RAM / 0.1 CPU** → `MAX_CONCURRENT_JOBS=2`, chunked streaming,
  `MAX_FILE_MB=20`.
* **No background workers** → the bot *must* live inside the web service
  process. That is what `main.py` does.
* **Ephemeral disk** → every artefact goes to `/tmp` and is deleted per job.
  The Pyrogram session also lives there, so a redeploy re-logs in once
  (harmless).
* **Spins down after ~15 min idle.** The in-process `keepalive.py` cannot wake a
  sleeping container — nothing inside a stopped process can. To stay up 24/7,
  add an **external** monitor:

  | Service | URL to ping | Interval |
  |---|---|---|
  | cron-job.org / UptimeRobot / GitHub Actions | `https://<service>.onrender.com/health` | every 10 min |

  External uptime monitors are allowed by Render's free plan; the `/health`
  endpoint is intentionally trivial so pinging it costs nothing.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ConfigError: TELEGRAM_API_ID is missing…` at boot | env not set | fill the dashboard env vars / `.env` |
| Render says "no open ports detected" | process didn't bind `$PORT` | make sure `python main.py` is the start command |
| Bot silent, logs show `FloodWait` | too many re-logins | keep `DATA_DIR=/tmp/novelbot` so the session persists between deploys |
| `supabase disabled - running in stateless mode` | keys missing | expected; bot still works, stats off |
| `unsupported_file` | sent PDF/DOCX/ZIP | re-save as `.txt` |
| `file_too_large` | over `MAX_FILE_MB` | split the novel |
| `rate_limited` | over `RATE_LIMIT_PER_HOUR` | wait, or raise the limit (mind abuse) |
| `job_timeout` | huge file on 0.1 CPU | raise `JOB_TIMEOUT_SECONDS` or split |
| `encoding_error` | exotic encoding | re-save as UTF-8 |
| Names look wrong / same name twice | mapping seed changed | `/admin reload` changes it — the checksum in the reply tells you which mapping was used |

### Admin commands

```text
/admin          # mapping info, job stats, DB state, uptime
/admin reload   # regenerate the mapping with a fresh seed
```

Requires your Telegram user id in `ADMIN_IDS`.

---

## License / credits

Name pools are small, hand-curated lists bundled in `resources/`. Add your own
by editing those files or setting `NAMES_REMOTE_URLS`. Note the two URLs in the
original script (`sainn/chinese-names`, `aruljohn/popular-indian-names`) both
return **HTTP 404** today and are therefore not used as a dependency.
