# Paper Trader web API

Frozen JSON API served by `app_web.py`. `web/app-client.js` is the only client.
Every response is a JSON object with `ok` (unless noted). Errors are
`{"ok": false, "error": "..."}` with a 4xx/5xx status.

Auth: the app is multi-user. Each account has an e-mail and a password, and the
session cookie (`pt_session`, HttpOnly, SameSite=Lax, 30 days) carries the user
id *inside its HMAC*, so a cookie cannot be replayed as somebody else. All
`/api/*` routes need that cookie except `/api/session`, `/api/login`,
`/api/register`, `/api/logout` and `/api/health`. An expired, missing or forged
cookie answers `401`, and the client returns to the login screen.

Every route that names or implies a portfolio checks that the portfolio belongs
to the session user. Somebody else's portfolio is reported as **404 No such
profile** rather than 403, so the API never confirms that another account's
portfolio exists.

## Accounts

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/session` | – | `{ok, authed, user\|null, registration_open}` |
| POST | `/api/login` | `{email, password}` | `{ok, user}` + `Set-Cookie` |
| POST | `/api/register` | `{email, password, currency?, code?}` | `{ok, user, profile}` + `Set-Cookie` |
| POST | `/api/logout` | – | `{ok}` + cookie cleared |
| GET | `/api/health` | – | `{ok, engine_running, server_time, talib, pandas, tv_available, users, registration_open}` |

* `user` is `{id, email, is_owner, created_at}`.
* **Login without an e-mail** is allowed and signs in the owner account, which
  is what makes the password printed on first start usable on its own.
* **Registration** is open unless the server sets
  `PAPER_TRADER_REGISTRATION_CODE`: then `code` is required (`403` when it is
  wrong or missing) and `registration_open` is `false`, which is how the client
  knows to show the invite-code field. A taken e-mail answers `409`, a bad
  e-mail or a password under 8 characters answers `400`.
* Registering signs the new account in and creates one empty portfolio for it,
  named after the local part of the e-mail (`bob`), with a numeric suffix if the
  name is already in use.
* Login failures are throttled: 8 attempts per client per 5 minutes → `429`.

## State

| Method | Path | Returns |
|---|---|---|
| GET | `/api/state[?force=1]` | the whole dashboard (see below) |
| GET | `/api/log?since=<n>` | `{ok, lines:[{n,t,text}], next}` |
| GET | `/api/history?profile_id=<id>` | `{ok, trades:[...]}` |

`/api/state` is the single polling endpoint. The server caches it for 2 s so
bursty polling from several devices collapses into one price fetch.

```jsonc
{
  "ok": true,
  "server_time": "2026-10-02 23:40:11",
  "market_open": true,
  "engine": {                     // engine.status() + scheduler
    "running": false, "profile_id": 1, "label": "1 day", "cycle": 12,
    "started_at": "...", "last_cycle_at": "...", "last_cycle_s": 13.4,
    "last_status": "🕯️ Trading Mode ... Staying flat.",
    "last_error": null, "scanned": 498, "fetch_s": 12.1, "decisions": 0,
    "data_available": true, "data_error": "", "log_seq": 88,
    "next_cycle_in_s": 421,
    "armed_profiles": [1],        // every portfolio trading right now
    "your_profile_armed": true    // ...and whether one of them is YOURS
  },
  "user": {"id": 1, "email": "bob@example.com", "is_owner": 0,
           "created_at": "2026-10-01 09:12:44"},
  "profiles": [{"id":1,"name":"Main","currency":"USD","balance":25000.0,
                "auto_mode":0,"total_deposited":25000.0}],
  "current_profile_id": 1,
  "account": {
    "currency":"USD","symbol":"$","cash":25000.0,
    "total_portfolio_value":8123.4,"net_worth":33123.4,
    "realized_pnl":0.0,"unrealized_pnl":123.4,"overall_pnl":123.4,
    "avg_trade_pct":null,"avg_position_pct":1.52,
    "total_deposited":25000.0,"cost_basis":8000.0,"stale_count":0,
    "prices_as_of":"23:40:09"
  },
  "positions": [{
    "symbol":"AAPL","quantity":10.0,"avg_price":180.0,"price":192.3,
    "value":1923.0,"pnl_pct":6.83,"pnl_money":123.0,
    "stop_loss":175.2,"take_profit":196.0,"initial_stop":175.2,
    "high_water":192.3,"risk_per_share":4.8,"breakeven_done":0,
    "opened_at":"2026-10-01 15:31:02"
  }],
  "risk": {                        // null when no profile is selected
    "day":"2026-10-02","day_start_equity":33000.0,"peak_equity":33200.0,
    "halted":false,"halt_reason":"","day_pnl_pct":0.37,"drawdown_pct":-0.23,
    "rules":{"risk_per_trade_pct":0.5,"max_open_positions":8, ...}
  },
  "recommendations": [{"action":"BUY (new)","symbol":"MSFT","quantity":3,
                       "amount":1245.0,"reason":"🕯️ CDLENGULFING | ..."}],
  "last_scan": {"scanned":498,"fetch_s":12.1,"bullish_patterns":4,
                "flat_reason":"...","top_blockers":[["no candlestick pattern on the last completed candle",497]]},
  "trading_timeframes": [{"label":"1 day","interval":"1D","rescan_min":60,"cooldown_h":24.0}],
  "analysis": {"running":false,"last_at":"23:12:00","last_error":null},
  "auto_mode": 0
}
```

`profiles`, `current_profile_id`, `account`, `positions`, `risk`,
`recommendations` and `auto_mode` all describe the **session user's** selected
portfolio only. `profiles` lists just that user's portfolios.

## Profiles

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/profiles` | `{name, currency, initial_balance?}` | `initial_balance` defaults to 0; the UI always sends 0. The new portfolio belongs to the caller (names are unique server-wide, a clash answers 409) |
| POST | `/api/profiles/select` | `{profile_id}` | selects one of the caller's own portfolios |
| DELETE | `/api/profiles/<pid>` | – | cascades positions, history and risk state; stops the engine if that portfolio was the caller's armed one |
| POST | `/api/profiles/<pid>/funds` | `{amount, note?}` | positive = deposit, negative = withdrawal; writes a DEPOSIT/WITHDRAW row |
| POST | `/api/profiles/<pid>/auto_mode` | `{enabled}` | persists auto-trading for that profile |

## Trading

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/trade` | `{action:"BUY"\|"SELL", symbol, quantity}` | prices live from TradingView, applies adverse slippage, then executes through the same `process_trade` the engine uses |
| GET | `/api/history?profile_id=` | – | newest first, up to 500 rows |
| POST | `/api/history/clear` | `{profile_id}` | deletes the ledger only (cash and positions untouched) |
| POST | `/api/history/<tid>/price` | `{price}` | corrects an execution price and re-derives cash / average cost / realized P/L |

## Engine (continuous Trading Mode)

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/engine/start` | `{profile_id, timeframe}` | arms **the caller's** portfolio for the server-side loop; it keeps running with no browser open, and other accounts keep their own state |
| POST | `/api/engine/stop` | – | stops after the cycle in flight (only the caller's arm) |
| POST | `/api/engine/cycle` | `{profile_id?, timeframe?}` | one scan/decide/execute cycle on the caller's portfolio now, without touching the loop |

Timeframe labels are the keys of `TRADING_TIMEFRAMES` in
`paper_trading_app_TV.py` ("Auto (all timeframes)", "1 minute", … "1 month")
and arrive in `trading_timeframes`.

Trading Mode is per account and each armed portfolio is remembered in the
`users` table, not in a file: restarting the server resumes exactly the
accounts that were armed and nobody else. One account arms one portfolio at a
time, and the expensive part of a cycle - the S&P 500 scan - is shared between
however many portfolios are armed at the same interval, so adding users does not
multiply the data traffic. Decisions and executions stay per portfolio.

## AI

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/analyse` | `{profile_id, lens, web?}` | `lens` ∈ `general \| technical \| fundamental \| combined`; runs on a background thread; `409` if one is already running |
| GET | `/api/models` | – | `{ok, models:[...], selected, engine}` where `engine` is `ollama` or `openai-compatible` |
| POST | `/api/models` | `{model}` | remembers the model in the `settings` table |

The AI suggestions of one account are never executed into another account's
portfolio: `auto_mode` is a per-profile column and the auto-execute branch only
runs for the portfolio the caller has selected.

## Client polling contract

* `/api/state` every 5 s while the "Live Prices" box is ticked (server cache 2 s).
* `/api/log?since=<last n>` on every poll; `next` is echoed back as the new cursor.
* `/api/history` only while the Trade History tab is open, and after any action.
* Any `401` → the client shows the login screen again.

