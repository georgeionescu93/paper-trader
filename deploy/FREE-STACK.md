# The free split stack: trading engine + dashboard + database, no server of your own

This is the honest version of "make it run on free hosting". It is three free
services doing one job each, because no single free host will run a
continuously trading Python process:

| piece | service | what it does | what breaks without it |
|---|---|---|---|
| **database** | [Neon](https://neon.com) (free Postgres) | the ledger: portfolios, positions, all trades, who is armed | nothing to trade with |
| **engine** | **GitHub Actions** (this repo's `cycle.yml`) | runs one real cycle every 15 minutes and pings the dashboard | no trading |
| **dashboard** | [Render](https://render.com) (free Docker web service) | serves the page and the API on an HTTPS URL | you cannot see or arm anything |

The dashboard host sleeps when nobody visits it and loses its disk on every
restart, which is exactly why the trading loop does **not** live there. It is a
window into the database; the work happens in the Actions job.

```
    you (any device)                         GitHub Actions (every 15 min)
          |                                          |
          | arm / stop / watch                       | one cycle when a portfolio is due
          v                                          v
   Render: dashboard  ------------------------>  PostgreSQL  <-- the only durable piece
          (no loop, may sleep)                  (Neon free)
```

## What this really is (read before trusting it)

* **It works, and it is free**, but it is three free tiers stacked up. Free tiers
  change their rules. Render's own documentation says plainly: *"Do not use them
  for production applications."*
* **Cycles can be a few minutes late.** GitHub's scheduler is best-effort and in
  UTC. For a daily or auto timeframe this is irrelevant; for a 1-minute timeframe
  it is not (see *Cadence* below).
* **GitHub disables scheduled workflows after ~60 days without repository
  activity.** Push anything, or run the workflow by hand, to keep it alive.
* **The database is the only thing that must not be lost.** Everything else can
  be rebuilt from the repository in minutes.
* **If Neon suspends or the free quota runs out, cycles start failing loudly**
  (a red run in Actions) instead of silently. That is the intended failure mode:
  no trades are ever invented.
* This has nothing to do with InfinityFree. That host forbids application-server
  use in its ToS, so it cannot run any part of this.

### What was actually verified, not just written

* Against a **real PostgreSQL 16.6 server**: the schema the app builds, every
  SQL statement the app relies on (translated and executed), the session-level
  advisory lock, and the migration - 2 portfolios, 2 accounts, 5 positions and
  31 trades copied with every row count and every total identical, and the id
  sequences reset so the next trade does not collide.
* A **real cycle from the external job against PostgreSQL**: 503 S&P 500 symbols
  scanned in 63 s, and the very next tick doing nothing in 2 s because the
  portfolio was not due yet.
* The **dashboard against PostgreSQL**, engine off: the migrated owner signs in,
  their portfolio and positions load, arming writes the flag the job reads, and
  the activity feed shows the cycle the *other* process ran - which is the whole
  point of the split design.
* The local SQLite suites still pass unchanged (the browser UI suite, the
  multi-user isolation suite, a scheduling suite, and a suite that runs the app
  exactly as Render will, with the engine off and the config in the environment).
* During all of that, the local `paper_trading_app.db` and `web_config.json`
  were checked byte-for-byte before and after: untouched.

### One difference from the desktop build

The **activity feed is shared** (it lives in the database), but the
"AI Recommendations & Execution" tree is filled by whichever process ran the
cycle, in its own memory. So it fills when you run a cycle from the dashboard;
the cycles the GitHub job runs appear in the activity feed, with the same
decisions spelled out in the text, but not as rows in that tree. Say the word
and the job's decision rows can be stored the same way the log is.

The cycles also use the **built-in pattern engine**, not TA-Lib: the Actions
runner installs only `requirements.txt`, and TA-Lib needs its C library compiled
there (which the desktop build has locally, so it reports "TA-Lib (61 patterns)").
The built-in engine is the app's own documented fallback and trades the same way,
just with fewer candlestick patterns recognised - so expect fewer entries, not
different rules. Compiling TA-Lib inside the workflow is possible (it becomes a
~3 minute step, cacheable) if you want the two to match exactly.

---

## 1. Database (Neon, ~3 minutes)

1. Create a free project at <https://neon.com>.
2. Copy the **direct** connection string (the one *without* `-pooler` in the
   hostname), and append `?sslmode=require` if it is not already there.

   > Use the direct string, not the pooled one. The cycle lock is a
   > session-level PostgreSQL advisory lock; a transaction-pooling proxy can move
   > the session between backends and the lock would no longer mean anything.

3. That string is `DATABASE_URL` / `PAPER_TRADER_DB_URL` everywhere below.

## 2. Move your existing ledger into it (optional but recommended)

Your local `paper_trading_app.db` already has portfolios, positions and trades.
This copies all of it, keeping the row ids, and verifies the result:

```powershell
cd "C:\Users\iones\Desktop\aplicatie de facut bani"
$env:PAPER_TRADER_DB_URL = "postgresql://...neon.tech/neondb?sslmode=require"
py -m pip install psycopg2-binary          # the only extra dependency
py migrate_to_postgres.py --check          # shows row counts, writes nothing
py migrate_to_postgres.py --apply          # creates the schema and copies
```

The script refuses to run into a database that already has portfolios (so two
ledgers can never be mixed), never opens the SQLite file for writing, and exits
non-zero if any row count or total disagrees afterwards.

Skip this step entirely if you want to start the cloud account with a clean
ledger - the app builds its own schema on first use. Starting clean works like
this: `py verify_free_stack.py` creates the empty tables, and the first boot of
the dashboard creates the owner account from `PAPER_TRADER_PASSWORD_HASH`, so
the password you already use signs you in to an empty portfolio list. Add a
portfolio from the page (it starts at zero - Deposit Cash is how money gets in,
exactly like the desktop build).

One trap worth knowing: if `PAPER_TRADER_PASSWORD_HASH` is missing or misspelt
on the host, the app generates a throwaway password for that boot instead - and
because an existing owner account keeps the password stored in the database,
that generated password will **not** log you in. The server now prints a loud
warning when this happens. Copy the value from `py app_web.py --print-env`
exactly, without the leading spaces that command's output puts in front of it.

## 3. Dashboard (Render, ~5 minutes)

```powershell
py app_web.py --print-env
```

That prints the exact environment the hosted side needs. The password hash it
prints is the one your account **already** has, so the password you know keeps
working.

1. Push this repository to GitHub.
2. Render → **New** → **Blueprint** → pick the repository. It reads `render.yaml`.
3. Fill in the variables marked `sync: false` (Render asks for them; they are
   never stored in git):

   | variable | value |
   |---|---|
   | `DATABASE_URL` | the Neon string from step 1 |
   | `PAPER_TRADER_PASSWORD_HASH` | from `--print-env` |
   | `PAPER_TRADER_SESSION_SECRET` | from `--print-env` |
   | `PAPER_TRADER_OWNER_EMAIL` | your e-mail |
   | `PAPER_TRADER_REGISTRATION_CODE` | a word, if you do not want open sign-up |
   | `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | optional, for AI analysis |

   `PAPER_TRADER_ENGINE=off` is already set by the blueprint: this host serves,
   it does not trade.
4. Deploy and open the URL. Sign in with your existing password.

## 4. The engine (GitHub Actions, ~2 minutes)

In the repository: **Settings → Secrets and variables → Actions → New repository
secret**

| secret | value |
|---|---|
| `DATABASE_URL` | the same Neon string |
| `WEB_URL` | your Render URL, e.g. `https://paper-trader.onrender.com` |

`WEB_URL` is only used to ping the dashboard awake. Without it the workflow still
trades; the dashboard just takes a minute to wake up when you visit it.

Then **Actions → paper-trader cycle → Run workflow** once, by hand, to confirm it
works before waiting for the schedule.

## 5. Arm a portfolio

Open the dashboard, press **Trading Mode**, pick a timeframe. That writes one
flag into PostgreSQL. The next Actions tick picks it up and starts trading that
portfolio - with its own cash, its own positions and its own rules. Several
accounts can each be armed at the same time; they share one market scan per
cycle and each trade their own ledger.

---

## Cadence: what the 15-minute cron actually does

`run_cycle_job.py` is not a "run everything now" button. Each portfolio carries a
due time (in the `cycle_state` table), exactly like the desktop build's
scheduler:

| timeframe | rescans every | with a 15-minute cron |
|---|---|---|
| Auto (all timeframes) | 15 min | every tick |
| 5 minutes | 5 min | every tick |
| 1 hour | 15 min | every tick |
| 1 day | 60 min | every 4th tick |
| 1 month | 360 min | every 24th tick |

A tick where nothing is due exits in about 2 seconds without touching the market
data, which is what keeps the free GitHub minutes and Neon compute hours cheap.
A real cycle scans all 503 S&P 500 symbols and takes roughly 50-60 seconds.

If you want the 1- and 5-minute timeframes, change the cron in
`.github/workflows/cycle.yml` to `*/5 * * * *` (GitHub's minimum) - and expect
~600 runs a day.

## Everyday operations

| I want to... | do this |
|---|---|
| stop trading | press **Stop** in the dashboard (writes the flag in the DB; the next tick does nothing) |
| change the timeframe | arm again with the new timeframe |
| see what the engine did | the dashboard's activity feed - the job writes its lines into the shared log |
| see whether cycles are happening | GitHub → Actions → paper-trader cycle (each run ends with a `CYCLE_SUMMARY` line and a run summary) |
| change the owner password | locally: `py app_web.py --print-env` again with the new hash... or simply `py app_web.py --reset-password` on a machine that can reach the DB, then update `PAPER_TRADER_PASSWORD_HASH` in Render |
| stop everything | Actions → the workflow → **Disable workflow**. The ledger stays in PostgreSQL, untouched. |
| move to a real server later | point the VPS at the same `PAPER_TRADER_DB_URL` and let it run with the engine on (see `deploy/ORACLE-STEP-BY-STEP.md`); the code, the schema and the ledger are identical |

**Stopping the Render service does not stop trading** - that is deliberate. To
stop trades, disarm the portfolio or disable the workflow.

## Troubleshooting

| symptom | cause | fix |
|---|---|---|
| dashboard is slow on first visit | the free service was asleep | wait ~30-60 s; the Actions ping keeps it mostly warm |
| signed out after a restart | `PAPER_TRADER_SESSION_SECRET` changed | set it once and leave it alone |
| owner password stopped working | `PAPER_TRADER_PASSWORD_HASH` changed or missing | re-run `--print-env`, update the variable |
| Actions run fails with a connection error | Neon project suspended, quota used, or a wrong URL | open the Neon console; check the quota |
| Actions run says `no portfolio is due yet` | normal | nothing to do |
| Actions run says `nothing is armed` | no portfolio in Trading Mode | arm one in the dashboard |
| dashboard shows old data | the page caches `/api/state` | reload, or press the timeframe again |
| workflow never runs | scheduled workflows are off (a fork, or 60 days idle) | run it once by hand; push a commit |

## Files this added

| file | purpose |
|---|---|
| `pg_compat.py` | makes the app's existing SQL run on PostgreSQL, unchanged (`sqlite3` is rebound to it when a URL is configured) |
| `shared_log.py` | puts the engine's activity log in the database, so the dashboard shows what the remote job did |
| `cycle_state.py` | stores when each portfolio is next due, so the job does not re-scan every tick |
| `run_cycle_job.py` | one cycle, for an external scheduler; takes the advisory lock, respects due times, prints a JSON summary |
| `migrate_to_postgres.py` | copies SQLite into PostgreSQL and verifies it |
| `verify_free_stack.py` | checks a database before you trust it with the ledger |
| `deploy/push-to-github.py` | publishes this folder to a GitHub repository through the API - **no git needed**, which matters on a machine that does not have git |
| `.github/workflows/cycle.yml` | the 15-minute trading loop and the keep-alive ping |
| `render.yaml` | the dashboard service |
| `requirements-postgres.txt` | `psycopg2-binary`, only needed for a hosted database |

## Publishing without git

This machine has no git and no GitHub CLI, so the usual `git init && git push`
is not available. `deploy/push-to-github.py` does it through GitHub's REST API
with nothing but a token (classic token with the `repo` and `workflow` scopes):

```powershell
$env:GITHUB_TOKEN = "ghp_..."
py deploy/push-to-github.py                 # dry run: lists what would go up
py deploy/push-to-github.py --go --repo paper-trader --public
```

The dry run prints every file that would be uploaded and explicitly asserts that
the ledger, `web_config.json`, `deploy/cloud-env.txt` and
`deploy/my-deployment.ps1` are **not** among them. Add `--secrets` with
`--database-url` and `--web-url` to set the two Actions secrets in the same
breath (`py -m pip install pynacl` for that), and `--dispatch` to start one run.
It is the one script here that has not been exercised against the live API,
because that needs a token - the dry run is the part that was tested.

None of this changes how the app behaves on your own machine: with no
`PAPER_TRADER_DB_URL` set, `pg_compat` is a no-op and the app uses SQLite exactly
as before.
