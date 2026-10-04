"""
AI Stock Portfolio & Paper Trader - Ollama-powered paper trading with live
market data and optional web-search context.

Key behaviors
=============
- Profiles are persisted in SQLite and start completely EMPTY (0.00 cash).
  Use "Deposit Cash" to fund an account before trading.
- The analysis model defaults to DeepSeek v4 Pro served via Ollama cloud
  ("deepseek-v4-pro:cloud", thinking set to MAXIMUM). Any local Ollama model
  can be picked instead (auto-discovered from /api/tags); cloud models are
  always included in the dropdown. The choice is remembered across restarts.
- Analysis lens checkboxes: "Fundamental" and/or "Technical" set the AI's
  point of view (both = combined, neither = general conservative). The
  Technical lens also injects real computed indicators (SMA20/50/200, RSI-14,
  52-week range, 1/3/6-month momentum) into the prompt.
- Optional web search: before each analysis, live DuckDuckGo results (market
  news + per-symbol news, phrased per lens) are injected into the AI prompt.
- MARKET DATA IS TRADINGVIEW (see tv_data.py). yfinance is gone. Two routes:
  the TradingView scanner API returns live prices AND indicators for ten
  timeframes for the whole universe in ONE request (<1s), and the TradingView
  chart websocket streams real OHLCV candles (~13 batched connections cover
  all 503 S&P 500 names in ~11-17s). All fetches run in background threads;
  holdings and account totals auto-refresh every 30 seconds and never freeze
  the UI.
- Auto-trading is disciplined: the AI is explicitly told to trade ONLY when
  the portfolio genuinely needs it, and independent guard rails enforce it
  (25% cash reserve, 25% per-position cap, 12h per-symbol cooldown, one
  order per symbol per batch). Every executed trade is written to a Trade
  History log.
- "Trading Mode" button: starts a CONTINUOUS no-AI trading engine that scans
  the WHOLE S&P 500 universe (503 names, refreshed weekly from Wikipedia) on
  ANY timeframe - 1m, 5m, 15m, 30m, 1h, 2h, 4h, daily, weekly, monthly, or
  "Auto" (all of them, requiring multi-timeframe confluence) - with the
  classic candlestick strategy: all 61 patterns via TA-Lib when its C library
  is installed, or a curated pure-Python engine otherwise. A signal is only
  "clear" when the pattern is corroborated (pattern tier + EMA-50 trend +
  RSI-14 momentum + above-average volume + higher-timeframe agreement), so
  the engine stays flat far more often than it trades.
  A BUY is a new long entry; a SELL only ever closes a position YOU hold
  (this is a long-only paper account - a bearish pattern on a stock you do
  not own is information, not an order). Exits fire on bearish signals and
  whenever a stored stop-loss, breakeven stop, trailing stop or take-profit
  level is touched by the LIVE quote. Every cycle the recommendations table
  labels each decision "SELL (exit)" or "BUY (new)" so the direction is
  unmistakable, and when it stays flat the status line explains which entry
  condition was missing. Signals come only from COMPLETED candles - an
  in-progress bar is never traded on, because its partial volume would veto
  (or fake) a signal, and acting on it would be look-ahead bias.
  Click again to stop; switch timeframes live.
- CAPITAL PRESERVATION IS ENFORCED IN CODE, not left to the strategy. Every
  entry is sized from its own stop distance so a full stop-out costs exactly
  RISK_PER_TRADE_PCT of equity (0.5% by default) - a wider stop simply buys
  fewer shares. Positions carry a structural/ATR stop, a 1:2 take-profit, a
  stop moved to BREAKEVEN once the trade is +1R (after which it can no longer
  lose money), and then an ATR trailing stop that locks gains in. Portfolio
  circuit breakers halt new entries on a daily loss limit, on a peak-to-trough
  drawdown limit, when the position cap or the cash floor is reached, and
  while a symbol is in its re-entry cooldown. Fills are charged realistic
  adverse slippage, and only liquid names above a price/volume floor are
  traded. Exits are NEVER blocked by any breaker - risk reduction always
  gets through.
  No trading system can promise profit, and this one does not: what it
  guarantees is that the *sizing and the exits* bound the damage of being
  wrong, which is the only honest form of "do not lose money".
- Account Overview shows Realized P/L, Unrealized P/L and Overall P/L in
  money AND percentage terms (average % per closed trade, % of cost basis,
  % of deposits, and average position %), color-coded and updated live.
- Trade History prices are editable (double-click a trade, or select it and
  press "Edit Price"): cash, average cost basis and realized P/L are all
  recalculated automatically from the corrected ledger.
- Modern dark interface built on pure ttk styling - native speed, no extra
  dependencies. Every prompt/notification is a themed dark dialog (no old
  native light pop-ups), the window auto-fits the screen so the
  recommendations and BUY/SELL buttons are always visible, and all tables
  have scrollbars.

Requirements
============
    pip install pandas websocket-client
    (optional) pip install TA-Lib   -> enables all 61 candlestick patterns
    Ollama running locally (http://localhost:11434):   ollama serve

Market data comes from TradingView through tv_data.py, which must sit next to
this file. No API key is needed. yfinance is no longer used at all.

Run
===
    python paper_trading_app_TV.py
"""

import html as html_lib
import json
import queue
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# tkinter is OPTIONAL on purpose. Everything above the GUI - the TradingView
# data layer, the candlestick engine, the risk engine and every database
# operation - must be importable on a headless server that has no Tk and no
# display, because the web build (engine.py / app_web.py) reuses this module
# without ever opening a window. Only constructing PaperTradingApp() needs Tk,
# and that raises a clear error if it is missing.
try:
    import tkinter as tk
    from tkinter import ttk
    TK_AVAILABLE = True
    TK_ERROR = ""
except ImportError as exc:          # pragma: no cover - depends on the host
    tk = None
    ttk = None
    TK_AVAILABLE = False
    TK_ERROR = (f"tkinter is not available on this Python ({exc}). The desktop "
                "GUI cannot start, but the headless engine and the web app "
                "still work.")

# ---------------------------------------------------------------------------
# MARKET DATA: TradingView (tv_data.py). yfinance has been removed entirely.
# tv_data.py uses the TradingView scanner API for batched live quotes and
# multi-timeframe indicators, and the TradingView chart websocket for real
# OHLCV candles (which the candlestick engine needs).
# ---------------------------------------------------------------------------
try:
    import tv_data as tv
    TV_MODULE = True
    TV_DATA_AVAILABLE = tv.TV_AVAILABLE
    TV_DATA_ERROR = tv.unavailable_reason()
except ImportError as exc:
    tv = None
    TV_MODULE = False
    TV_DATA_AVAILABLE = False
    TV_DATA_ERROR = (f"tv_data.py could not be imported ({exc}). It must sit "
                     "next to this file.")

# Candlestick Trading Mode dependencies. TA-Lib needs its C library
# installed separately (hard on Windows); when it is missing, the app falls
# back to a curated pure-Python pattern engine so the feature still works.
try:
    import pandas as pd
    import numpy as np
    PANDAS_AVAILABLE = True
except ImportError:
    pd = None
    np = None
    PANDAS_AVAILABLE = False

try:
    import talib
    TALIB_AVAILABLE = True
except ImportError:
    talib = None
    TALIB_AVAILABLE = False


# ==========================================
# 1. DATABASE & PERSISTENCE LAYER (SQLite)
# ==========================================
DB_FILE = "paper_trading_app.db"

# Auto-refresh interval for live prices (milliseconds).
AUTO_REFRESH_MS = 30_000

# How often the main thread drains the thread-safe UI dispatch queue.
UI_POLL_MS = 50


def get_db_connection():
    """One connection per operation. foreign_keys=ON is required for the
    ON DELETE CASCADE rules to actually work (a real SQLite gotcha)."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Initializes SQLite database tables to ensure persistent storage across
    restarts. Safe to run on databases created by older versions of this app:
    new tables are simply added."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Profiles table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            currency TEXT NOT NULL,
            balance REAL NOT NULL,
            auto_mode INTEGER DEFAULT 0,
            total_deposited REAL DEFAULT 0
        )
    """)

    # Portfolio holdings table. stop_loss/take_profit are managed by the
    # continuous Trading Mode; initial_stop / high_water / risk_per_share /
    # breakeven_done are the risk engine's memory, so each cycle can ratchet
    # the stop to breakeven and then trail it without re-deriving the trade.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            quantity REAL NOT NULL,
            avg_price REAL NOT NULL,
            stop_loss REAL,
            take_profit REAL,
            FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
            UNIQUE(profile_id, symbol)
        )
    """)

    # Risk state per profile: today's starting equity, the all-time peak
    # equity, and whether the circuit breakers are currently halting entries.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS risk_state (
            profile_id INTEGER PRIMARY KEY,
            day TEXT,
            day_start_equity REAL,
            peak_equity REAL,
            halted INTEGER DEFAULT 0,
            halt_reason TEXT DEFAULT '',
            FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        )
    """)

    # Trade history log (realized_pnl records the locked-in profit/loss of
    # SELL trades, so Overall P/L can be shown in the Account Overview).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            trade_time TEXT NOT NULL,
            action TEXT NOT NULL,
            symbol TEXT NOT NULL,
            quantity REAL NOT NULL,
            price REAL NOT NULL,
            total REAL NOT NULL,
            reason TEXT DEFAULT '',
            realized_pnl REAL,
            FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        )
    """)

    # Migrate databases created by older versions of this app.
    trade_cols = {row[1] for row in cursor.execute("PRAGMA table_info(trades)").fetchall()}
    if "realized_pnl" not in trade_cols:
        cursor.execute("ALTER TABLE trades ADD COLUMN realized_pnl REAL")
    profile_cols = {row[1] for row in cursor.execute("PRAGMA table_info(profiles)").fetchall()}
    if "total_deposited" not in profile_cols:
        cursor.execute("ALTER TABLE profiles ADD COLUMN total_deposited REAL DEFAULT 0")
    pos_cols = {row[1] for row in cursor.execute("PRAGMA table_info(positions)").fetchall()}
    if "stop_loss" not in pos_cols:
        cursor.execute("ALTER TABLE positions ADD COLUMN stop_loss REAL")
    if "take_profit" not in pos_cols:
        cursor.execute("ALTER TABLE positions ADD COLUMN take_profit REAL")
    # Risk-engine columns (added when the TradingView release landed).
    if "initial_stop" not in pos_cols:
        cursor.execute("ALTER TABLE positions ADD COLUMN initial_stop REAL")
    if "high_water" not in pos_cols:
        cursor.execute("ALTER TABLE positions ADD COLUMN high_water REAL")
    if "risk_per_share" not in pos_cols:
        cursor.execute("ALTER TABLE positions ADD COLUMN risk_per_share REAL")
    if "breakeven_done" not in pos_cols:
        cursor.execute("ALTER TABLE positions ADD COLUMN breakeven_done INTEGER DEFAULT 0")
    if "opened_at" not in pos_cols:
        cursor.execute("ALTER TABLE positions ADD COLUMN opened_at TEXT")

    # Key/value settings (e.g. selected Ollama model) (new)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_profile ON positions(profile_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_profile ON trades(profile_id)")

    conn.commit()
    conn.close()


def get_setting(key, default=None):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else default
    except sqlite3.Error:
        return default


def set_setting(key, value):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


# ==========================================
# 2. OLLAMA ENGINE
# ==========================================
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_CHAT_URL = OLLAMA_BASE_URL + "/api/chat"
OLLAMA_TAGS_URL = OLLAMA_BASE_URL + "/api/tags"
# Default analysis model: DeepSeek v4 Pro, served via Ollama cloud.
DEFAULT_MODEL = "deepseek-v4-pro:cloud"
# Cloud models never appear in Ollama's /api/tags listing, so they are added
# to the model dropdown manually. Extend this list with other cloud tags as
# Ollama makes them available.
CLOUD_MODELS = ["deepseek-v4-pro:cloud"]

# Local inference can be slow (large models on modest hardware easily take
# several minutes per analysis), so the chat timeout is generous. The UI stays
# responsive the whole time because analysis runs in a background thread.
OLLAMA_TIMEOUT_S = 600
# Keep the selected model loaded in Ollama between analyses (faster repeats).
OLLAMA_KEEP_ALIVE = "10m"

# Set True to print the raw Ollama reply to the console (debugging parsing).
DEBUG_MODEL_OUTPUT = False

# Watchlist of moderate/lower-risk, stable blue-chip candidate stocks
CANDIDATE_STOCKS = ["AAPL", "MSFT", "GOOGL", "JNJ", "PG", "BRK-B", "JPM", "V", "XOM", "KO"]

# Auto-trading guard rails. The AI is told to trade ONLY when the portfolio
# genuinely needs it - and the executor independently enforces these limits
# whatever the AI says, so repeated analyses can never churn the account:
AUTO_CASH_RESERVE_PCT = 0.25       # auto-buys never spend below 25% of net worth
AUTO_MAX_POSITION_PCT = 0.25       # no single position above 25% of net worth
AUTO_TRADE_COOLDOWN_HOURS = 12.0   # no auto trade on a symbol traded < 12h ago

# Candlestick Trading Mode (continuous whole-S&P-500 scan, no AI):
TRADING_MODE_MAX_BUYS = 2        # new entries per scan cycle (max 8 positions)

# ---------------------------------------------------------------------------
# CAPITAL PRESERVATION - the risk engine. These numbers decide how much can be
# lost, and they are enforced in code on every cycle regardless of what the
# signals say. Defaults are deliberately conservative: a full stop-out costs
# 0.5% of equity, so even a bad streak of 8 losing trades costs ~4%.
# ---------------------------------------------------------------------------
RISK_PER_TRADE_PCT = 0.005      # equity risked if one stop-loss is hit (0.5%)
MAX_OPEN_POSITIONS = 8          # never hold more than this many positions
MAX_POSITION_PCT = 0.12         # no single position above 12% of equity
CASH_FLOOR_PCT = 0.20           # never invest below 20% cash
DAILY_LOSS_LIMIT_PCT = 0.02     # halt NEW entries after -2% on the day
MAX_DRAWDOWN_HALT_PCT = 0.10    # halt NEW entries after -10% from peak equity
MAX_STOP_DISTANCE_PCT = 0.08    # skip a trade whose stop is further than 8%
MIN_STOP_DISTANCE_PCT = 0.002   # ...and one whose stop is absurdly tight
BREAKEVEN_AT_R = 1.0            # move the stop to breakeven at +1R
TRAIL_ATR_MULT = 1.5            # then trail it 1.5 x ATR behind the high
TAKE_PROFIT_R = 2.0             # 1:2 risk/reward target
ATR_STOP_MULT = 1.5             # stop sits 1.5 x ATR below entry, at most
SLIPPAGE_BPS = 5.0              # adverse slippage charged on every fill
MIN_PRICE = 5.0                 # ignore sub-$5 names
MIN_AVG_DOLLAR_VOLUME = 5e6     # ignore names trading under $5M/day

# "Clear signal" strictness. A pattern alone is never enough: the signal must
# reach this score, which combines the pattern tier with trend, momentum,
# volume and higher-timeframe agreement. Higher = fewer, cleaner trades.
MIN_ENTRY_SCORE = 4
# Exits are deliberately EASIER to trigger than entries: getting out of a
# position on a confirmed bearish reversal must never be the hard part.
MIN_EXIT_SCORE = 3
STRONG_PATTERN_SCORE = 2        # weight of a high-significance reversal pattern
WEAK_PATTERN_SCORE = 1          # weight of a minor one-off pattern

# Batched candle sweep shape: symbols per websocket connection, and how many
# connections run in parallel. 503 symbols / 40 = 13 connections, which keeps
# the load on TradingView low while a full sweep finishes in ~11-25s.
SWEEP_CHUNK = 40
SWEEP_WORKERS = 6

# Candles pulled per symbol per scan: enough for EMA-200 and 61-pattern TA-Lib
# lookbacks while keeping the whole-universe sweep at 13 connections.
BARS_PER_SCAN = 260

# Selectable timeframes. Every TradingView timeframe is available; "Auto"
# scans several at once and demands multi-timeframe agreement, which is the
# strictest and usually the best-quality mode.
#   cooldown_h : per-symbol re-entry cooldown (hours). BUYs only - exits and
#                stop-losses are never delayed.
#   rescan_min : pause between continuous scan cycles (minutes).
TRADING_TIMEFRAMES = {
    "Auto (all timeframes)": {"interval": None, "cooldown_h": 12.0, "rescan_min": 15},
    "1 minute":   {"interval": "1m",  "cooldown_h": 0.25,  "rescan_min": 2},
    "5 minutes":  {"interval": "5m",  "cooldown_h": 0.5,   "rescan_min": 5},
    "15 minutes": {"interval": "15m", "cooldown_h": 1.0,   "rescan_min": 5},
    "30 minutes": {"interval": "30m", "cooldown_h": 2.0,   "rescan_min": 10},
    "1 hour":     {"interval": "1h",  "cooldown_h": 4.0,   "rescan_min": 15},
    "2 hours":    {"interval": "2h",  "cooldown_h": 8.0,   "rescan_min": 30},
    "4 hours":    {"interval": "4h",  "cooldown_h": 12.0,  "rescan_min": 30},
    "1 day":      {"interval": "1D",  "cooldown_h": 24.0,  "rescan_min": 60},
    "1 week":     {"interval": "1W",  "cooldown_h": 72.0,  "rescan_min": 180},
    "1 month":    {"interval": "1M",  "cooldown_h": 168.0, "rescan_min": 360},
}
# Timeframes used by "Auto", and how many of them must agree on a fresh
# signal before it counts as "clear". 3 of 5 is the default balance; raising
# AUTO_MIN_AGREEMENT to 4 makes the engine extremely selective.
AUTO_TIMEFRAMES = ("15m", "1h", "4h", "1D", "1W")
AUTO_MIN_AGREEMENT = 3
# The higher timeframe used as the regime (trend) filter for every mode.
REGIME_INTERVAL = "1D"

# The "whole market" universe is the full S&P 500, refreshed weekly from
# Wikipedia by tv_data.get_sp500_symbols() and falling back to a bundled list
# when offline. AI analysis still focuses on stable blue chips.
MARKET_SCAN_SYMBOLS = []        # filled in lazily by get_market_universe()


def get_market_universe(force_refresh=False):
    """The S&P 500 universe used by the continuous scanner. Falls back to the
    blue-chip watchlist when TradingView's universe source is unavailable."""
    global MARKET_SCAN_SYMBOLS
    if tv is not None:
        symbols = tv.get_sp500_symbols(force_refresh=force_refresh)
        if symbols:
            MARKET_SCAN_SYMBOLS = sorted(set(symbols))
            return list(MARKET_SCAN_SYMBOLS)
    if not MARKET_SCAN_SYMBOLS:
        MARKET_SCAN_SYMBOLS = sorted(set(CANDIDATE_STOCKS))
    return list(MARKET_SCAN_SYMBOLS)



def list_ollama_models():
    """Returns the names of models installed in the local Ollama server,
    or an empty list if Ollama is unreachable."""
    try:
        req = urllib.request.Request(OLLAMA_TAGS_URL, headers={"User-Agent": "paper-trader/2.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
        models = []
        for m in data.get("models", []) or []:
            name = m.get("name") or m.get("model")
            if name:
                models.append(name)
        return sorted(models)
    except Exception:
        return []


# ==========================================
# 3. LIVE MARKET DATA (TradingView via tv_data.py)
# ==========================================
# Every price and candle in this app now comes from TradingView. Quoting is
# batched: one scanner request prices hundreds of symbols, so the old
# per-symbol thread pool is gone - fetching is both faster and lighter.
_price_cache = {}
_price_cache_lock = threading.Lock()
PRICE_CACHE_TTL = 20.0  # seconds


def get_stock_price(symbol):
    """Last traded price for one symbol (TradingView scanner, cached briefly)."""
    return get_stock_prices([symbol]).get(str(symbol).upper(), 0.0)


def get_stock_prices(symbols):
    """Bulk live prices from TradingView in a single batched request.

    Returns {SYMBOL: price}, with 0.0 for anything that could not be priced
    (unknown ticker, network failure) so callers keep their existing shape.
    """
    ordered = sorted({str(s).upper() for s in symbols if s})
    if not ordered:
        return {}
    prices = {s: 0.0 for s in ordered}
    if tv is None:
        return prices
    try:
        return {**prices, **tv.quotes(ordered, ttl=PRICE_CACHE_TTL)}
    except Exception:
        return prices


def _rnd(value, nd=2):
    return round(float(value), nd) if value is not None else None


def get_technical_indicators(symbols):
    """Real technical indicators for the AI's Technical lens.

    Computed from one year of daily CLOSES downloaded from TradingView:
    SMA20/50/200, RSI-14, the 52-week range and 1/3/6-month momentum.
    Returns {symbol: {...}}; symbols without enough history are omitted so
    the analysis still proceeds with the rest.
    """
    result = {}
    ordered = sorted({str(s).upper() for s in symbols if s})
    if not ordered or tv is None or not PANDAS_AVAILABLE:
        return result

    try:
        frames, _errors = tv.history_batch(ordered, "1D", 260, chunk=20, workers=5)
    except Exception:
        return result

    for sym, hist in frames.items():
        try:
            hist, _dropped = tv.drop_forming_bar(hist, "1D")
            close = hist["Close"].dropna()
            if len(close) < 30:
                continue
            last = float(close.iloc[-1])

            def sma(n):
                return float(close.tail(n).mean()) if len(close) >= n else None

            rsi = None
            delta = close.diff().dropna()
            if len(delta) >= 14:
                gains = float(delta.clip(lower=0).tail(14).mean())
                losses = float((-delta.clip(upper=0)).tail(14).mean())
                rsi = 100.0 if losses <= 0 else 100.0 - (100.0 / (1.0 + gains / losses))

            hi52 = float(close.max())
            lo52 = float(close.min())

            def momentum(days):
                if len(close) > days and close.iloc[-1 - days] > 0:
                    return (last / float(close.iloc[-1 - days]) - 1.0) * 100.0
                return None

            result[sym] = {
                "last": _rnd(last),
                "sma20": _rnd(sma(20)),
                "sma50": _rnd(sma(50)),
                "sma200": _rnd(sma(200)),
                "rsi14": _rnd(rsi, 1),
                "52w_high": _rnd(hi52),
                "52w_low": _rnd(lo52),
                "pct_from_52w_high": _rnd((last / hi52 - 1.0) * 100.0, 1) if hi52 > 0 else None,
                "momentum_1m_pct": _rnd(momentum(21), 1),
                "momentum_3m_pct": _rnd(momentum(63), 1),
                "momentum_6m_pct": _rnd(momentum(126), 1),
            }
        except Exception:
            continue
    return result


def get_market_snapshot(symbols, timeframes=("1D", "1W")):
    """TradingView's own multi-timeframe indicators for many symbols in one
    request: RSI, EMA20/50/200, ATR, ADX, MACD, Stochastic and the aggregate
    "Recommend.All" rating per timeframe, plus price, volume and sector.
    Returns {symbol: {...}} (empty dict if TradingView is unreachable)."""
    if tv is None:
        return {}
    try:
        return tv.snapshot(symbols, timeframes)
    except Exception:
        return {}



# ==========================================
# 4. WEB SEARCH (DuckDuckGo, no API key needed)
# ==========================================
_search_cache = {}
SEARCH_CACHE_TTL = 600.0  # seconds
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _resolve_ddg_href(href):
    """DuckDuckGo wraps result URLs in a redirect; extract the real target."""
    if not href:
        return ""
    href = html_lib.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    if "duckduckgo.com/l/" in href:
        try:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            if params.get("uddg"):
                return urllib.parse.unquote(params["uddg"][0])
        except Exception:
            pass
    return href


def _extract_by_class(page, token, tag_pattern):
    """Collects (text, href) for all tags whose class attribute contains the
    given token. Works with single or double quoted attributes."""
    items = []
    for m in re.finditer(tag_pattern, page, re.S | re.I):
        attrs, inner = m.group(1), m.group(2)
        cls_match = re.search(r"""class=(["'])(.*?)\1""", attrs)
        cls = cls_match.group(2) if cls_match else ""
        if token in cls.split():
            href_match = re.search(r"""href=(["'])(.*?)\1""", attrs)
            items.append((_strip_html(inner), href_match.group(2) if href_match else ""))
    return items


def duckduckgo_search(query, max_results=4):
    """Searches the web via DuckDuckGo (HTML endpoint, with the lite endpoint
    as fallback). Returns a list of {title, url, snippet} dicts; empty list on
    failure. Results are cached for 10 minutes."""
    now = time.time()
    cached = _search_cache.get(query)
    if cached and (now - cached[0]) < SEARCH_CACHE_TTL:
        return cached[1]

    attempts = (
        ("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query),
         "result__a", "result__snippet"),
        ("https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote_plus(query),
         "result-link", "result-snippet"),
    )

    results = []
    for url, link_token, snippet_token in attempts:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                page = response.read().decode("utf-8", "ignore")
        except Exception:
            continue

        # Snippets are <a> tags on the html endpoint and <td> tags on lite.
        anchor_items = _extract_by_class(page, link_token, r"""<a\b([^>]*)>(.*?)</a>""")
        snippet_items = _extract_by_class(
            page, snippet_token, r"""<(?:a|td)\b([^>]*)>(.*?)</(?:a|td)>"""
        )
        for i, (title, href) in enumerate(anchor_items[:max_results]):
            if not title:
                continue
            snippet = snippet_items[i][0] if i < len(snippet_items) else ""
            results.append({
                "title": title[:150],
                "url": _resolve_ddg_href(href),
                "snippet": snippet[:350],
            })
        if results:
            break

    _search_cache[query] = (now, results)
    return results


# Web search phrasing per analysis lens.
_LENS_SEARCH_SUFFIX = {
    "fundamental": "earnings valuation fundamentals",
    "technical": "stock technical analysis",
    "combined": "stock analysis outlook",
    "general": "stock news outlook",
}


def gather_web_context(symbols, lens="general", market_news=True,
                       max_symbols=5, results_per_query=2):
    """Runs a market-level query plus one lens-tailored news query per symbol
    (holdings first, capped at max_symbols), in parallel, and returns
    {query: [results]} for prompt injection. Deliberately bounded so the
    prompt stays small enough for slow local models."""
    ordered = []
    for s in symbols:
        s = str(s).upper()
        if s and s not in ordered:
            ordered.append(s)

    suffix = _LENS_SEARCH_SUFFIX.get(lens, _LENS_SEARCH_SUFFIX["general"])
    queries = []
    if market_news:
        queries.append("stock market news today outlook")
    queries.extend(f"{sym} {suffix}" for sym in ordered[:max_symbols])

    context = {}

    def run(query):
        return query, duckduckgo_search(query, max_results=results_per_query)

    with ThreadPoolExecutor(max_workers=4) as pool:
        for query, res in pool.map(run, queries):
            if res:
                context[query] = res
    return context


def _format_web_context(context):
    lines = []
    for query, results in context.items():
        lines.append(f'Search: "{query}"')
        for r in results:
            lines.append(f"  - {r['title']}: {r['snippet'][:200]} [{r['url']}]")
    return "\n".join(lines)


# ==========================================
# 5. CANDLESTICK TRADING MODE (whole-market scan, no AI)
# ==========================================
# Strategy adapted from the user's reference implementation: ALL Japanese
# candlestick patterns (the full 61 via TA-Lib when its C library is
# installed, otherwise a curated pure-Python subset of the most significant
# ones), confirmed by EMA-50 trend, RSI-14 momentum and a 20-day volume
# average, with 1:2 risk/reward stop levels.

def _pure_python_patterns(open_, high, low, close, ema):
    """Pure-Python replacements for the most significant TA-Lib candlestick
    patterns. Returns {pattern_name: [.., +100, -100, 0, ..]} exactly like
    TA-Lib. Look-alike shapes (hammer vs hanging man, inverted hammer vs
    shooting star, doji family) are separated by trend context versus the
    EMA-50, the same way TA-Lib does."""
    n = len(close)
    names = [
        "CDLHAMMER", "CDLINVERTEDHAMMER", "CDLSHOOTINGSTAR", "CDLHANGINGMAN",
        "CDLENGULFING", "CDLPIERCING", "CDLDARKCLOUDCOVER", "CDLMORNINGSTAR",
        "CDLEVENINGSTAR", "CDL3WHITESOLDIERS", "CDL3BLACKCROWS", "CDLHARAMI",
        "CDLKICKING", "CDLDRAGONFLYDOJI", "CDLGRAVESTONEDOJI", "CDLBELTHOLD",
    ]
    out = {name: [0] * n for name in names}

    def body(i):
        return abs(close[i] - open_[i])

    def upper(i):
        return high[i] - max(open_[i], close[i])

    def lower(i):
        return min(open_[i], close[i]) - low[i]

    def rng(i):
        return max(high[i] - low[i], 1e-12)

    def mid(i):
        return (open_[i] + close[i]) / 2.0

    def bull(i):
        return close[i] > open_[i]

    def bear(i):
        return close[i] < open_[i]

    for i in range(2, n):
        up = close[i] > ema[i]
        down = close[i] < ema[i]

        # Hammer / Hanging man (same shape; trend context separates them).
        if body(i) > 0 and lower(i) >= 2 * body(i) and upper(i) <= 0.25 * rng(i):
            if down:
                out["CDLHAMMER"][i] = 100
            elif up:
                out["CDLHANGINGMAN"][i] = -100

        # Inverted hammer / Shooting star.
        if body(i) > 0 and upper(i) >= 2 * body(i) and lower(i) <= 0.25 * rng(i):
            if down:
                out["CDLINVERTEDHAMMER"][i] = 100
            elif up:
                out["CDLSHOOTINGSTAR"][i] = -100

        # Engulfing.
        if (bear(i - 1) and bull(i) and open_[i] <= close[i - 1]
                and close[i] > open_[i - 1] and body(i) > body(i - 1)):
            out["CDLENGULFING"][i] = 100
        elif (bull(i - 1) and bear(i) and open_[i] >= close[i - 1]
                and close[i] < open_[i - 1] and body(i) > body(i - 1)):
            out["CDLENGULFING"][i] = -100

        # Piercing line.
        if (bear(i - 1) and bull(i) and open_[i] < low[i - 1]
                and close[i] > mid(i - 1) and close[i] < open_[i - 1]):
            out["CDLPIERCING"][i] = 100

        # Dark cloud cover.
        if (bull(i - 1) and bear(i) and open_[i] > high[i - 1]
                and close[i] < mid(i - 1) and close[i] > open_[i - 1]):
            out["CDLDARKCLOUDCOVER"][i] = -100

        # Morning star.
        if (bear(i - 2) and body(i - 1) <= 0.3 * body(i - 2) and bull(i)
                and close[i] > mid(i - 2)):
            out["CDLMORNINGSTAR"][i] = 100

        # Evening star.
        if (bull(i - 2) and body(i - 1) <= 0.3 * body(i - 2) and bear(i)
                and close[i] < mid(i - 2)):
            out["CDLEVENINGSTAR"][i] = -100

        # Three white soldiers / three black crows.
        if (bull(i - 2) and bull(i - 1) and bull(i)
                and close[i] > close[i - 1] and close[i - 1] > close[i - 2]
                and open_[i - 1] < close[i - 2] and open_[i] < close[i - 1]):
            out["CDL3WHITESOLDIERS"][i] = 100
        elif (bear(i - 2) and bear(i - 1) and bear(i)
                and close[i] < close[i - 1] and close[i - 1] < close[i - 2]
                and open_[i - 1] > close[i - 2] and open_[i] > close[i - 1]):
            out["CDL3BLACKCROWS"][i] = -100

        # Harami (inside bar).
        if (body(i) < body(i - 1)
                and max(open_[i], close[i]) <= max(open_[i - 1], close[i - 1])
                and min(open_[i], close[i]) >= min(open_[i - 1], close[i - 1])):
            if bear(i - 1) and bull(i):
                out["CDLHARAMI"][i] = 100
            elif bull(i - 1) and bear(i):
                out["CDLHARAMI"][i] = -100

        # Kicker (body gap against the previous candle).
        if bear(i - 1) and bull(i) and open_[i] > open_[i - 1]:
            out["CDLKICKING"][i] = 100
        elif bull(i - 1) and bear(i) and open_[i] < open_[i - 1]:
            out["CDLKICKING"][i] = -100

        # Doji family (context separates the two stars).
        if body(i) <= 0.1 * rng(i):
            if (down and upper(i) <= 0.1 * rng(i) and lower(i) >= 0.6 * rng(i)):
                out["CDLDRAGONFLYDOJI"][i] = 100
            elif (up and lower(i) <= 0.1 * rng(i) and upper(i) >= 0.6 * rng(i)):
                out["CDLGRAVESTONEDOJI"][i] = -100

        # Belt hold (opens at one extreme, closes near the other).
        if bull(i) and down and lower(i) <= 0.1 * rng(i) and body(i) >= 0.7 * rng(i):
            out["CDLBELTHOLD"][i] = 100
        elif bear(i) and up and upper(i) <= 0.1 * rng(i) and body(i) >= 0.7 * rng(i):
            out["CDLBELTHOLD"][i] = -100

    return out


_TALIB_PATTERN_FUNCS = None


def _talib_pattern_functions():
    """The TA-Lib pattern-recognition function list, resolved once.

    talib.get_function_groups() rebuilds its dict on every call, which showed
    up in profiling once the scanner started calling this for 500+ symbols per
    cycle."""
    global _TALIB_PATTERN_FUNCS
    if _TALIB_PATTERN_FUNCS is None:
        names = talib.get_function_groups()["Pattern Recognition"]
        _TALIB_PATTERN_FUNCS = [(name, getattr(talib, name)) for name in names]
    return _TALIB_PATTERN_FUNCS


def _compute_candle_patterns(df, ema):
    """Returns a DataFrame of +100/-100/0 columns, one per candlestick
    pattern (all 61 via TA-Lib when installed, the curated subset above
    otherwise)."""
    if TALIB_AVAILABLE:
        open_, high = df["Open"].to_numpy(), df["High"].to_numpy()
        low, close = df["Low"].to_numpy(), df["Close"].to_numpy()
        data = {}
        for name, func in _talib_pattern_functions():
            data[name] = func(open_, high, low, close)
        return pd.DataFrame(data, index=df.index)

    return pd.DataFrame(
        _pure_python_patterns(df["Open"].values, df["High"].values,
                              df["Low"].values, df["Close"].values,
                              ema.values),
        index=df.index)


# Pattern tiers. A "clear" bullish pattern must be one whose shape is a real
# reversal/continuation signal, not a single-candle curiosity. The names are
# TA-Lib's, and the curated pure-Python engine emits the same names, so the
# tiers behave identically with or without the C library. Direction-agnostic
# names (engulfing, kicking, ...) appear in both sets: the sign of the value
# is what says which way it fired.
STRONG_BULLISH_PATTERNS = {
    "CDLENGULFING", "CDLMORNINGSTAR", "CDLMORNINGDOJISTAR", "CDL3WHITESOLDIERS",
    "CDLHAMMER", "CDLINVERTEDHAMMER", "CDLPIERCING", "CDLABANDONEDBABY",
    "CDL3LINESTRIKE", "CDLKICKING", "CDLBELTHOLD", "CDLDRAGONFLYDOJI",
    "CDLLADDERBOTTOM", "CDLHOMINGPIGEON", "CDLCONCEALBABYBDOLLAR",
    "CDLMATHOLD", "CDLRISEFALL3METHODS", "CDLUNIQUE3RIVER", "CDL3OUTSIDE",
    "CDLSTICKSANDWICH", "CDLTAKURI", "CDL3STARSINSOUTH", "CDLCOUNTERATTACK",
    "CDLBREAKAWAY", "CDLTRISTAR", "CDLXSIDEGAP3METHODS",
}
STRONG_BEARISH_PATTERNS = {
    "CDLENGULFING", "CDLEVENINGSTAR", "CDLEVENINGDOJISTAR", "CDL3BLACKCROWS",
    "CDLSHOOTINGSTAR", "CDLHANGINGMAN", "CDLDARKCLOUDCOVER",
    "CDLGRAVESTONEDOJI", "CDLKICKING", "CDLABANDONEDBABY", "CDL2CROWS",
    "CDLUPSIDEGAP2CROWS", "CDLIDENTICAL3CROWS", "CDLADVANCEBLOCK",
    "CDL3LINESTRIKE", "CDL3OUTSIDE", "CDLDELIBERATION", "CDLTHRUSTING",
    "CDLBREAKAWAY", "CDLCOUNTERATTACK", "CDLSTALLEDPATTERN",
    "CDLXSIDEGAP3METHODS", "CDLHIKKAKE",
}
# ATR lookback and the volume confirmation multiple.
ATR_PERIOD = 14
VOLUME_CONFIRM_MULT = 1.2


def generate_candle_signals(df, interval=None):
    """Adds the full indicator set and the scored BUY/SELL columns.

    df: DataFrame with 'Open', 'High', 'Low', 'Close', 'Volume'.
    interval: TradingView interval code ('1D', '5m', ...); informational.

    Added columns: EMA_20, EMA_50, EMA_200, RSI_14, Vol_SMA_20, ATR_14,
    Bull_Body_Score, Bear_Body_Score (how many patterns fired),
    Bullish_Score / Bearish_Score (the corroborated 0-9 confidence score),
    Bullish_Patterns / Bearish_Patterns, Strong_Bull / Strong_Bear (whether a
    high-significance pattern fired) and Buy_Signal / Sell_Signal.

    The score is deliberately additive across independent evidence:
        2  strong pattern (or 1 for a minor one)
        2  trend      (close > EMA-50, EMA-20 > EMA-50)
        1  regime     (close > EMA-200)
        2  momentum   (RSI in a sane 45-72 band and rising)
        1  volume     (>1.2x the 20-bar average)
        1  volatility (ATR within a tradable band)
    A single pattern can therefore never trigger a trade on its own.
    """
    df = df.copy()
    close = df["Close"]

    df["EMA_20"] = close.ewm(span=20, adjust=False).mean()
    df["EMA_50"] = close.ewm(span=50, adjust=False).mean()
    df["EMA_200"] = close.ewm(span=200, adjust=False).mean()

    # Momentum filter (RSI-14, Wilder smoothing).
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / 14.0, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / 14.0, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    df["RSI_14"] = (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)

    # Volume confirmation (20-bar average).
    df["Vol_SMA_20"] = df["Volume"].rolling(20).mean()

    # Volatility, used for stop placement and for the sizing of every entry.
    prev_close = close.shift(1)
    true_range = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["ATR_14"] = true_range.ewm(alpha=1.0 / ATR_PERIOD, adjust=False).mean()

    # Candlestick patterns -> fired names, tiers and counts. Everything here
    # is numpy rather than per-row pandas lookups: the whole-universe sweep
    # evaluates 500+ symbols at once, and per-element `.iloc` access was
    # measurably the slowest thing in the app (362 ms/symbol -> ~30 ms).
    patterns = _compute_candle_patterns(df, df["EMA_50"])
    pattern_names = list(patterns.columns)
    hits = np.asarray(patterns.to_numpy())
    bull_mask = hits == 100
    bear_mask = hits == -100

    strong_bull_cols = [j for j, name in enumerate(pattern_names)
                        if name in STRONG_BULLISH_PATTERNS]
    strong_bear_cols = [j for j, name in enumerate(pattern_names)
                        if name in STRONG_BEARISH_PATTERNS]
    strong_bull_arr = (bull_mask[:, strong_bull_cols].any(axis=1)
                       if strong_bull_cols else np.zeros(len(hits), dtype=bool))
    strong_bear_arr = (bear_mask[:, strong_bear_cols].any(axis=1)
                       if strong_bear_cols else np.zeros(len(hits), dtype=bool))
    bull_body_arr = bull_mask.sum(axis=1)
    bear_body_arr = bear_mask.sum(axis=1)

    df["Bullish_Patterns"] = [[pattern_names[j] for j in np.flatnonzero(row)]
                              for row in bull_mask]
    df["Bearish_Patterns"] = [[pattern_names[j] for j in np.flatnonzero(row)]
                              for row in bear_mask]
    df["Bull_Body_Score"] = bull_body_arr
    df["Bear_Body_Score"] = bear_body_arr
    df["Strong_Bull"] = strong_bull_arr
    df["Strong_Bear"] = strong_bear_arr
    df["Bullish_Score"] = bull_body_arr
    df["Bearish_Score"] = bear_body_arr

    n = len(df)
    bull_score = [0] * n
    bear_score = [0] * n
    buy = [0] * n
    sell = [0] * n
    trend_gate = [False] * n
    volume_gate = [False] * n

    # Plain numpy views: indexable in nanoseconds instead of per-element
    # pandas lookups, which is what makes a 503-symbol sweep affordable.
    close_a = df["Close"].to_numpy(dtype=float)
    rsi_a = df["RSI_14"].to_numpy(dtype=float)
    ema20_a = df["EMA_20"].to_numpy(dtype=float)
    ema50_a = df["EMA_50"].to_numpy(dtype=float)
    ema200_a = df["EMA_200"].to_numpy(dtype=float)
    vol_a = df["Volume"].to_numpy(dtype=float)
    vol_avg_a = df["Vol_SMA_20"].to_numpy(dtype=float)
    atr_a = df["ATR_14"].to_numpy(dtype=float)
    bull_body_a = np.asarray(bull_body_arr)
    bear_body_a = np.asarray(bear_body_arr)
    strong_bull_a = np.asarray(strong_bull_arr)
    strong_bear_a = np.asarray(strong_bear_arr)

    for i in range(1, n):
        price = close_a[i]
        if not price or price <= 0 or not np.isfinite(price):
            continue
        rsi_now = rsi_a[i]
        rsi_prev = rsi_a[i - 1]
        ema20_now = ema20_a[i]
        ema50_now = ema50_a[i]
        ema200_now = ema200_a[i]
        has_200 = np.isfinite(ema200_now)
        atr_now = atr_a[i] if np.isfinite(atr_a[i]) else 0.0
        atr_pct = (atr_now / price) if price else 0.0
        avg_vol = vol_avg_a[i]
        volume_ok = bool(np.isfinite(avg_vol) and avg_vol > 0
                         and vol_a[i] > VOLUME_CONFIRM_MULT * avg_vol)
        bull_fired = int(bull_body_a[i])
        bear_fired = int(bear_body_a[i])
        strong_bull = bool(strong_bull_a[i])
        strong_bear = bool(strong_bear_a[i])

        # ---- bullish confidence ----
        score_b = 0
        if bull_fired:
            score_b += STRONG_PATTERN_SCORE if strong_bull else WEAK_PATTERN_SCORE
            if strong_bull and bull_fired >= 2:
                score_b += 1
        if price > ema50_now:
            score_b += 1
        if ema20_now > ema50_now:
            score_b += 1
        if has_200 and price > ema200_now:
            score_b += 1
        if 45.0 <= rsi_now <= 72.0:
            score_b += 1
        if rsi_now > rsi_prev:
            score_b += 1
        if volume_ok:
            score_b += 1
        if 0.004 <= atr_pct <= 0.05:
            score_b += 1
        bull_score[i] = score_b

        # ---- bearish confidence ----
        score_s = 0
        if bear_fired:
            score_s += STRONG_PATTERN_SCORE if strong_bear else WEAK_PATTERN_SCORE
            if strong_bear and bear_fired >= 2:
                score_s += 1
        if price < ema50_now:
            score_s += 1
        if ema20_now < ema50_now:
            score_s += 1
        if has_200 and price < ema200_now:
            score_s += 1
        if 28.0 <= rsi_now <= 55.0:
            score_s += 1
        if rsi_now < rsi_prev:
            score_s += 1
        if volume_ok:
            score_s += 1
        if 0.004 <= atr_pct <= 0.05:
            score_s += 1
        bear_score[i] = score_s

        # A trade needs a fired pattern AND the corroborating score. Crucially
        # the trend and volume checks are HARD GATES for a new long, not bonus
        # points: a bullish reversal candle in a downtrend must not be able to
        # reach the threshold on volume/volatility points alone, or the engine
        # would systematically try to catch falling knives.
        trend_ok = price > ema50_now
        trend_gate[i] = trend_ok
        volume_gate[i] = volume_ok
        if (bull_fired and trend_ok and volume_ok
                and score_b >= MIN_ENTRY_SCORE and rsi_now <= 78.0):
            buy[i] = 1
        # Exits get no volume gate - getting OUT on a confirmed reversal must
        # never wait for confirmation - but they still need trend damage
        # (below EMA-50) or a clearly overbought reading.
        elif (bear_fired and score_s >= MIN_EXIT_SCORE and rsi_now >= 22.0
                and (not trend_ok or rsi_now > 60.0)):
            sell[i] = 1

    df["Bullish_Score"] = bull_score
    df["Bearish_Score"] = bear_score
    df["Trend_Gate_Ok"] = trend_gate
    df["Volume_Gate_Ok"] = volume_gate
    df["Buy_Signal"] = buy
    df["Sell_Signal"] = sell
    return df, (interval or "1D")


def _snapshot_value(snapshot_row, key):
    """Reads one scanner value as a float, or None when absent/null."""
    if not snapshot_row:
        return None
    value = snapshot_row.get(key)
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value == value else None   # drop NaN


def multi_timeframe_verdict(snapshot_row, timeframes=AUTO_TIMEFRAMES,
                            min_agreement=AUTO_MIN_AGREEMENT):
    """Counts how many timeframes agree the symbol is bullish, using
    TradingView's per-timeframe aggregate rating and RSI.

    Returns (bullish_votes, total, list_of_agreeing_timeframes). A timeframe
    votes bullish when its `Recommend.All` rating is positive, or when it is
    flat/neutral but RSI sits above 50 - i.e. not fighting the trend.
    """
    votes, agreeing = 0, []
    total = 0
    for interval in timeframes:
        rating = _snapshot_value(snapshot_row, f"rec_all_{interval}")
        rsi = _snapshot_value(snapshot_row, f"rsi_{interval}")
        if rating is None and rsi is None:
            continue
        total += 1
        bullish = False
        if rating is not None:
            bullish = rating > 0.0
        if not bullish and rsi is not None:
            bullish = rsi >= 50.0 and (rating is None or rating >= 0.0)
        if bullish:
            votes += 1
            agreeing.append(interval)
    return votes, total, agreeing


def liquidity_veto(price, avg_dollar_volume):
    """Rejects names that are too cheap or too thin to trade honestly."""
    if price is not None and price < MIN_PRICE:
        return f"price {price:.2f} below the ${MIN_PRICE:.0f} floor"
    if avg_dollar_volume is not None and avg_dollar_volume < MIN_AVG_DOLLAR_VOLUME:
        return (f"only ${avg_dollar_volume/1e6:.1f}M/day traded "
                f"(floor ${MIN_AVG_DOLLAR_VOLUME/1e6:.0f}M)")
    return None


def _structure_stop(df, last, direction=None):
    """Stop level for a long entry, from market structure widened by ATR.

    The stop is placed BELOW both the signal candle's low (the level that
    invalidates the pattern) and 1.5 ATR under the close (the level ordinary
    noise does not reach). Taking the WIDER of the two matters: because every
    entry is sized so that the stop costs a fixed 0.5% of equity, a stop that
    is too tight does not reduce risk - it just converts one small loss into
    many, by being hit on noise. The wider stop is paid for in fewer shares,
    not in more risk. Trades whose stop lands further than
    MAX_STOP_DISTANCE_PCT away are refused later, so this cannot run away.

    Returns (stop, risk_per_share).
    """
    close = float(last["Close"])
    atr = float(last["ATR_14"]) if not pd.isna(last["ATR_14"]) else 0.0
    atr_stop = close - ATR_STOP_MULT * atr if atr > 0 else close * 0.95
    structural = float(last["Low"])
    stop = min(structural, atr_stop)
    stop = min(stop, close * (1 - MIN_STOP_DISTANCE_PCT))
    return stop, close - stop


def evaluate_symbol(df, interval, symbol, snapshot_row=None,
                    regime_ok=True, require_regime=True):
    """Turns one symbol's candle frame into a trading decision.

    Returns a dict (never raises) describing the newest COMPLETED candle:
        signal        'BUY' | 'SELL' | None
        score         the corroborated confidence score behind that signal
        bull_score / bear_score, rsi, above_ema, volume_ok
        patterns      the pattern names that fired
        stop_loss / take_profit / risk_per_share  (BUY only)
        vetoes        human-readable reasons the symbol is NOT tradable
        reasons       human-readable reasons FOR the signal
    """
    out = {"symbol": str(symbol).upper(), "interval": interval, "signal": None,
           "score": 0, "bull_score": 0, "bear_score": 0, "rsi": None,
           "price": None, "above_ema": False, "volume_ok": False,
           "patterns": [], "strong_pattern": False, "stop_loss": None,
           "take_profit": None, "risk_per_share": None, "atr": None,
           "vetoes": [], "reasons": [], "mtf_votes": None, "mtf_total": None,
           "mtf_agreeing": [], "bars": 0, "bar_time": None, "fresh": False}

    if df is None or len(df) == 0:
        out["vetoes"].append("no candle data")
        return out

    try:
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        completed, dropped = tv.drop_forming_bar(df, interval) if tv else (df, False)
        if len(completed) < 60:
            out["vetoes"].append(f"only {len(completed)} completed bars "
                                 "(needs 60+)")
            return out
        scored, _ = generate_candle_signals(completed, interval)
    except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop a scan
        out["vetoes"].append(f"indicator error: {exc}")
        return out

    if len(scored) < 60:
        out["vetoes"].append("not enough scored bars")
        return out

    last = scored.iloc[-1]
    close = float(last["Close"])
    out.update({
        "price": close,
        "bull_score": int(last["Bullish_Score"]),
        "bear_score": int(last["Bearish_Score"]),
        "rsi": float(last["RSI_14"]),
        "above_ema": bool(close > float(last["EMA_50"])),
        "volume_ok": bool(not pd.isna(last["Vol_SMA_20"])
                          and float(last["Volume"]) > VOLUME_CONFIRM_MULT
                          * float(last["Vol_SMA_20"])),
        "atr": float(last["ATR_14"]) if not pd.isna(last["ATR_14"]) else None,
        "bars": len(scored),
        "bar_time": str(scored.index[-1]),
    })

    # ---- liquidity / tradability vetoes (apply to entries) ----
    price_now = _snapshot_value(snapshot_row, "close") or close
    avg_dollar = None
    if snapshot_row:
        avg_volume = _snapshot_value(snapshot_row, "avg_volume")
        if avg_volume is not None:
            avg_dollar = avg_volume * price_now
    veto = liquidity_veto(price_now, avg_dollar)
    if veto:
        out["vetoes"].append(veto)

    # ---- multi-timeframe confluence (TradingView ratings per timeframe) ----
    if snapshot_row:
        votes, total, agreeing = multi_timeframe_verdict(snapshot_row)
        out.update({"mtf_votes": votes, "mtf_total": total,
                    "mtf_agreeing": agreeing})

    last_ts = scored.index[-1]
    fresh_ok = True
    if tv is not None:
        age = tv.last_completed_bar_age(df, interval)
        if age is not None and age > 3.0 * tv.interval_seconds(interval):
            fresh_ok = False
            out["vetoes"].append(
                f"stale data: newest completed bar is {age/3600:.1f}h old")
    out["fresh"] = fresh_ok

    # ---- entry ----
    if int(last["Buy_Signal"]) == 1:
        reasons = []
        if last["Strong_Bull"]:
            reasons.append("strong bullish pattern")
        else:
            reasons.append("bullish pattern")
        if out["above_ema"]:
            reasons.append("above EMA-50")
        if out["volume_ok"]:
            reasons.append("volume > 1.2x average")
        if out["mtf_total"]:
            reasons.append(f"{out['mtf_votes']}/{out['mtf_total']} timeframes agree")
        out["reasons"] = reasons
        out["patterns"] = list(last["Bullish_Patterns"])
        out["strong_pattern"] = bool(last["Strong_Bull"])
        out["score"] = out["bull_score"]

        if require_regime and not regime_ok:
            out["vetoes"].append("higher-timeframe regime is bearish")
        if out["mtf_total"] and out["mtf_votes"] < AUTO_MIN_AGREEMENT:
            out["vetoes"].append(
                f"only {out['mtf_votes']}/{out['mtf_total']} timeframes agree "
                f"(needs {AUTO_MIN_AGREEMENT})")
        if out["rsi"] is not None and out["rsi"] > 78.0:
            out["vetoes"].append(f"RSI {out['rsi']:.0f} is blow-off overbought")

        stop, risk = _structure_stop(scored, last, "BUY")
        out["stop_loss"] = stop
        out["risk_per_share"] = risk
        out["take_profit"] = close + risk * TAKE_PROFIT_R
        risk_pct = (risk / close) if close else 1.0
        if risk_pct > MAX_STOP_DISTANCE_PCT:
            out["vetoes"].append(
                f"stop is {risk_pct*100:.1f}% away (max "
                f"{MAX_STOP_DISTANCE_PCT*100:.0f}%) - too volatile to size safely")
        elif risk_pct < MIN_STOP_DISTANCE_PCT:
            out["vetoes"].append("stop is unrealistically tight")
        if not out["vetoes"]:
            out["signal"] = "BUY"

    # ---- exit ----
    elif int(last["Sell_Signal"]) == 1:
        out["reasons"] = ["strong bearish pattern" if last["Strong_Bear"]
                          else "bearish pattern",
                          "below EMA-50" if not out["above_ema"] else "trend damage"]
        if out["volume_ok"]:
            out["reasons"].append("volume > 1.2x average")
        out["patterns"] = list(last["Bearish_Patterns"])
        out["strong_pattern"] = bool(last["Strong_Bear"])
        out["score"] = out["bear_score"]
        out["signal"] = "SELL"

    if out["signal"] is None and not out["vetoes"]:
        bull = int(last["Bull_Body_Score"])
        bear = int(last["Bear_Body_Score"])
        if bull and not bool(last["Trend_Gate_Ok"]):
            out["vetoes"].append(
                "bullish pattern but price is below EMA-50 (no uptrend to "
                "confirm it - refusing to catch a falling knife)")
        elif bull and not bool(last["Volume_Gate_Ok"]):
            out["vetoes"].append(
                "bullish pattern but volume is below 1.2x its 20-bar average")
        elif bull and out["bull_score"] < MIN_ENTRY_SCORE:
            out["vetoes"].append(
                f"bullish pattern but score {out['bull_score']} < "
                f"{MIN_ENTRY_SCORE} (needs trend/momentum/volume support)")
        elif bear and out["bear_score"] < MIN_EXIT_SCORE:
            out["vetoes"].append(
                f"bearish pattern but score {out['bear_score']} < {MIN_EXIT_SCORE}")
        else:
            out["vetoes"].append("no candlestick pattern on the last "
                                 "completed candle")
    return out


def analyze_symbol_candles(symbol, interval="1D", snapshot_row=None,
                           regime_ok=True, require_regime=True, bars=None):
    """Fetches one symbol's candles from TradingView and evaluates them."""
    symbol = str(symbol).upper()
    if tv is None or not PANDAS_AVAILABLE:
        return None
    frame = tv.history(symbol, interval, bars or BARS_PER_SCAN)
    if frame is None or frame.empty:
        return {"symbol": symbol, "interval": interval, "signal": None,
                "score": 0, "bull_score": 0, "bear_score": 0, "rsi": None,
                "price": None, "above_ema": False, "volume_ok": False,
                "patterns": [], "strong_pattern": False, "stop_loss": None,
                "take_profit": None, "risk_per_share": None, "atr": None,
                "vetoes": ["no candle data from TradingView"], "reasons": [],
                "mtf_votes": None, "mtf_total": None, "mtf_agreeing": [],
                "bars": 0, "bar_time": None, "fresh": False}
    return evaluate_symbol(frame, interval, symbol, snapshot_row=snapshot_row,
                           regime_ok=regime_ok, require_regime=require_regime)


def regime_allows_long(snapshot_row, symbol_frame=None):
    """Higher-timeframe regime filter: only take new longs when the daily
    backdrop is not broken. Uses TradingView's daily EMA-200 and the weekly
    aggregate rating, falling back to the symbol's own daily EMA-200."""
    if snapshot_row:
        price = _snapshot_value(snapshot_row, "close")
        ema200 = _snapshot_value(snapshot_row, "ema200")
        weekly = _snapshot_value(snapshot_row, "rec_all_1W")
        if price is not None and ema200 is not None:
            if price < ema200 * 0.97:
                # More than 3% below the 200-day average: a broken backdrop.
                if weekly is not None and weekly < 0:
                    return False
                return False
            return True
        if weekly is not None:
            return weekly >= -0.1
    if symbol_frame is not None and len(symbol_frame) >= 200:
        close = symbol_frame["Close"]
        return float(close.iloc[-1]) >= float(close.tail(200).mean()) * 0.97
    return True


def scan_market_candles(symbols, interval="1D", snapshot=None,
                        require_regime=True, bars=None):
    """Whole-universe candle scan from TradingView.

    One batched sweep (~13 websocket connections for all 503 S&P 500 names)
    produces every symbol's decision. Returns (results, prices, timings) where
    results holds one decision dict per symbol that had usable data.
    """
    ordered = []
    for s in symbols:
        s = str(s).upper()
        if s and s not in ordered:
            ordered.append(s)
    results, prices = [], {}
    if not ordered or tv is None or not PANDAS_AVAILABLE:
        return results, prices, {}

    started = time.time()
    frames, errors = tv.history_batch(ordered, interval, bars or BARS_PER_SCAN,
                                     chunk=SWEEP_CHUNK, workers=SWEEP_WORKERS)
    fetch_seconds = time.time() - started

    for symbol in ordered:
        frame = frames.get(symbol)
        if frame is None or frame.empty:
            continue
        row = snapshot.get(symbol) if snapshot else None
        decision = evaluate_symbol(
            frame, interval, symbol, snapshot_row=row,
            regime_ok=regime_allows_long(row, frame),
            require_regime=require_regime)
        results.append(decision)
        if decision.get("price"):
            prices[symbol] = decision["price"]
    return results, prices, {
        "fetch_s": fetch_seconds,
        "scanned": len(results),
        "requested": len(ordered),
        "errors": len(errors),
    }



# ==========================================
# 5b. RISK ENGINE - capital preservation
# ==========================================
# This section is what stands between "a strategy" and "a strategy that can
# lose the account". Three independent mechanisms:
#   1. SIZING   - every entry risks a fixed % of equity, so a wider stop buys
#                 fewer shares and one loss can never be large.
#   2. EXITS    - stop-loss, stop-to-breakeven at +1R, ATR trailing stop and a
#                 1:2 take-profit. Stops only ever move UP.
#   3. BREAKERS - daily loss limit and peak-drawdown halt that stop NEW
#                 entries. Exits are never blocked by anything.
# All of it is pure functions over plain values so it can be unit-tested
# without a database, a UI or a network.


def apply_slippage(price, side, bps=None):
    """Fills are charged realistic adverse slippage: a BUY pays a little more
    than the quoted price, a SELL receives a little less. Without this, paper
    results are systematically flattering."""
    rate = (SLIPPAGE_BPS if bps is None else bps) / 10_000.0
    if str(side).upper() == "BUY":
        return float(price) * (1.0 + rate)
    return float(price) * (1.0 - rate)


def position_size(equity, entry, stop, cash_available,
                  risk_pct=None, max_position_pct=None, cash_floor_pct=None,
                  current_position_value=0.0):
    """Risk-based position sizing - the single most important loss control.

    The number of shares is derived from the DISTANCE TO THE STOP, so the loss
    taken if that stop is hit is (about) equity * risk_pct no matter how
    volatile the stock is:

        shares = (equity * risk_pct) / (entry - stop)

    That raw risk-based size is then capped by the concentration limit and by
    the cash the account is actually allowed to spend. Returns
    (shares, binding_constraint) where shares is a whole number >= 0.
    """
    risk_pct = RISK_PER_TRADE_PCT if risk_pct is None else risk_pct
    max_position_pct = MAX_POSITION_PCT if max_position_pct is None else max_position_pct
    cash_floor_pct = CASH_FLOOR_PCT if cash_floor_pct is None else cash_floor_pct

    try:
        equity = float(equity)
        entry = float(entry)
        stop = float(stop)
        cash_available = float(cash_available)
    except (TypeError, ValueError):
        return 0, "bad inputs"
    if equity <= 0 or entry <= 0:
        return 0, "bad inputs"
    if stop >= entry:
        return 0, "stop is not below entry"

    risk_per_share = entry - stop
    risk_budget = equity * risk_pct
    shares = int(risk_budget / risk_per_share)
    binding = "risk budget"

    # Concentration cap: never let one position exceed max_position_pct of
    # equity (measured on the position's total, not just the new shares).
    room_value = equity * max_position_pct - float(current_position_value or 0.0)
    if room_value <= 0:
        return 0, "position cap already reached"
    cap_shares = int(room_value / entry)
    if cap_shares < shares:
        shares, binding = cap_shares, "position cap"

    # Cash floor: the account must keep cash_floor_pct of equity uninvested.
    spendable = cash_available - equity * cash_floor_pct
    if spendable <= 0:
        return 0, "cash floor reached"
    cash_shares = int(spendable / entry)
    if cash_shares < shares:
        shares, binding = cash_shares, "cash floor"

    return max(0, shares), binding


def _r_multiple(entry, initial_stop, price):
    """How many times the original risk the position is currently up."""
    risk = entry - initial_stop
    if risk <= 0:
        return 0.0
    return (price - entry) / risk


def manage_position(symbol, quantity, avg_price, stop_loss, take_profit,
                    live_price, initial_stop=None, high_water=None,
                    risk_per_share=None, atr=None, breakeven_done=False):
    """Decides what to do with one OPEN position on this cycle.

    Returns a dict:
        exit         True when the position must be closed now
        exit_reason  which rule fired ('stop-loss', 'trailing stop',
                     'take-profit')
        new_stop     the stop to store (only ever higher than the current one)
        new_high_water / breakeven_now  state to persist
        r_multiple   current profit in units of original risk

    Order of checks matters: the STOP is evaluated before the TARGET, because
    when both levels are inside one bar's range the honest assumption is that
    the stop was hit first.
    """
    out = {"exit": False, "exit_reason": "", "new_stop": stop_loss,
           "new_high_water": high_water, "breakeven_now": False,
           "r_multiple": 0.0}
    try:
        qty = float(quantity)
        entry = float(avg_price)
        price = float(live_price)
    except (TypeError, ValueError):
        return out
    if qty <= 0 or entry <= 0 or price <= 0:
        return out

    effective_initial = float(initial_stop) if initial_stop else (
        float(stop_loss) if stop_loss else entry * 0.98)
    effective_risk = float(risk_per_share) if risk_per_share else (
        entry - effective_initial)

    out["r_multiple"] = _r_multiple(entry, effective_initial, price)
    peak = max(float(high_water) if high_water else entry, price)
    out["new_high_water"] = peak

    # 1) Hard stop. The stored stop already includes breakeven/trailing
    #    ratchets from previous cycles.
    if stop_loss is not None and price <= float(stop_loss):
        out["exit"] = True
        out["exit_reason"] = "stop-loss"
        return out
    # 2) Take-profit.
    if take_profit is not None and price >= float(take_profit):
        out["exit"] = True
        out["exit_reason"] = "take-profit"
        return out

    current_stop = float(stop_loss) if stop_loss else None

    # 3) Ratchet the stop to breakeven once the trade has earned its risk.
    #    From this point the trade cannot lose money (barring an overnight
    #    gap straight through the stop, which is why sizing still matters).
    if not breakeven_done and out["r_multiple"] >= BREAKEVEN_AT_R:
        breakeven = entry * (1.0 + 2.0 * SLIPPAGE_BPS / 10_000.0)
        if current_stop is None or breakeven > current_stop:
            current_stop = breakeven
            out["new_stop"] = breakeven
            out["breakeven_now"] = True

    # 4) Trail the stop behind the high-water mark, then never lower it.
    if (breakeven_done or out["breakeven_now"]) and atr and float(atr) > 0:
        trail = peak - TRAIL_ATR_MULT * float(atr)
        ceiling = price * (1.0 - MIN_STOP_DISTANCE_PCT)   # never above price
        trail = min(trail, ceiling)
        if current_stop is None or trail > current_stop:
            current_stop = trail
            out["new_stop"] = trail

    # 5) A position that has NO stop at all must never stay unprotected. This
    #    happens for holdings carried in from an older version of the app (or a
    #    partially-written row); the whole capital-preservation promise rests on
    #    every position having a stop, so one is armed here from the fallback
    #    initial risk, clamped below the live price that it stays valid.
    if current_stop is None:
        armed = min(effective_initial, price * (1.0 - MIN_STOP_DISTANCE_PCT))
        current_stop = armed
        out["new_stop"] = armed

    if current_stop is not None:
        if stop_loss is None or current_stop > float(stop_loss):
            out["new_stop"] = current_stop
        else:
            out["new_stop"] = float(stop_loss)
    return out


def higher_timeframe_pattern_check(symbols, interval, bars=None):
    """Confirms a candidate against a HIGHER timeframe's candlestick picture.

    Multi-timeframe confluence in "Auto" mode is not only TradingView's
    ratings (see multi_timeframe_verdict): the actual pattern engine is run
    one timeframe up on the shortlist, and a strong bearish reversal up there
    vetoes a lower-timeframe long. Only the shortlist is fetched - typically a
    handful of symbols - so this costs one small batched connection rather
    than a second whole-universe sweep.

    Returns {symbol: (ok, reason)}; symbols with no data are simply absent
    (absent means "no objection", because the data layer already refused
    symbols it could not price).
    """
    ordered = []
    for s in symbols:
        s = str(s).upper()
        if s and s not in ordered:
            ordered.append(s)
    if not ordered or tv is None or not PANDAS_AVAILABLE:
        return {}

    frames, _errors = tv.history_batch(ordered, interval, bars or BARS_PER_SCAN,
                                      chunk=SWEEP_CHUNK, workers=SWEEP_WORKERS)
    verdicts = {}
    for symbol, frame in frames.items():
        try:
            completed, _ = tv.drop_forming_bar(frame, interval)
            if len(completed) < 60:
                continue
            scored, _ = generate_candle_signals(completed, interval)
            last = scored.iloc[-1]
            close = float(last["Close"])
            ema50 = float(last["EMA_50"])
            strong_bear = bool(last["Strong_Bear"])
            bear_patterns = list(last["Bearish_Patterns"])
            if strong_bear and close < ema50:
                verdicts[symbol] = (
                    False,
                    f"{interval} shows a strong bearish reversal "
                    f"({'+'.join(bear_patterns[:2])}) below EMA-50")
            elif close < ema50 * 0.95:
                verdicts[symbol] = (
                    False, f"{interval} trend is broken (price well under "
                           f"EMA-50)")
            else:
                verdicts[symbol] = (True, "")
        except Exception:
            continue
    return verdicts


def stop_take_exit_decisions(stops_rows, prices, atrs=None):
    """Risk management for the continuous Trading Mode.

    `stops_rows` are the OPEN positions of this profile, as tuples:
        (symbol, quantity, avg_price, stop_loss, take_profit,
         initial_stop, high_water, risk_per_share, breakeven_done)
    Only symbols you actually hold can ever produce a SELL here.

    Returns (decisions, stop_updates):
      decisions    - SELL orders for positions whose stop or target was hit
      stop_updates - (symbol, new_stop, new_high_water, breakeven_done) rows
                     for positions whose protective stop was ratcheted up.
    """
    decisions, stop_updates = [], []
    atrs = atrs or {}
    for row in stops_rows:
        (symbol, quantity, avg_price, stop_loss, take_profit,
         initial_stop, high_water, risk_per_share, breakeven_done) = row
        symbol = str(symbol).upper()
        price = float(prices.get(symbol, 0.0) or 0.0)
        if price <= 0 or not quantity or quantity <= 0:
            continue

        managed = manage_position(
            symbol, quantity, avg_price, stop_loss, take_profit, price,
            initial_stop=initial_stop, high_water=high_water,
            risk_per_share=risk_per_share, atr=atrs.get(symbol),
            breakeven_done=bool(breakeven_done))

        if managed["exit"]:
            reason_kind = managed["exit_reason"]
            if reason_kind == "take-profit":
                text = (f"🎯 Take-profit hit at {price:.2f} (target "
                        f"{float(take_profit):.2f}) - exit your "
                        f"{float(quantity):.4g}-share position to lock in gains.")
            elif reason_kind == "trailing stop":
                text = (f"📈 Trailing stop hit at {price:.2f} (level "
                        f"{float(stop_loss):.2f}) - exit to bank the gain.")
            else:
                text = (f"🛑 Stop-loss hit at {price:.2f} (level "
                        f"{float(stop_loss):.2f}, {managed['r_multiple']:+.2f}R) - "
                        f"exit your {float(quantity):.4g}-share position to "
                        "protect capital.")
            decisions.append({
                "action": "SELL", "symbol": symbol, "quantity": float(quantity),
                "amount": float(quantity) * price, "kind": "exit",
                "reason": text,
            })
            continue

        # Persist any ratchet so the next cycle enforces the tighter stop.
        new_stop = managed["new_stop"]
        changed = (managed["breakeven_now"]
                   or (new_stop is not None
                       and (stop_loss is None or float(new_stop) > float(stop_loss) + 1e-9))
                   or (managed["new_high_water"] or 0) != (high_water or 0))
        if changed:
            stop_updates.append((symbol, new_stop, managed["new_high_water"],
                                 bool(breakeven_done or managed["breakeven_now"])))
    return decisions, stop_updates


# ---------------------------------------------------------------- breakers
def evaluate_breakers(equity, day_start_equity, peak_equity):
    """Portfolio-level circuit breakers. Returns (halted, reason).

    Two independent limits, both about stopping the bleeding rather than
    predicting it:
      * daily loss limit  - a bad day stops getting worse;
      * drawdown limit    - a bad streak cannot compound into a disaster.
    When halted, NO new position is opened; exits and stops keep working.
    """
    halted, reasons = False, []
    try:
        equity = float(equity)
        day_start_equity = float(day_start_equity or equity)
        peak_equity = float(peak_equity or equity)
    except (TypeError, ValueError):
        return False, ""
    if day_start_equity > 0:
        day_change = equity / day_start_equity - 1.0
        if day_change <= -DAILY_LOSS_LIMIT_PCT:
            halted = True
            reasons.append(f"daily loss limit hit ({day_change*100:+.2f}% today)")
    if peak_equity > 0:
        drawdown = equity / peak_equity - 1.0
        if drawdown <= -MAX_DRAWDOWN_HALT_PCT:
            halted = True
            reasons.append(f"drawdown limit hit ({drawdown*100:+.2f}% from peak "
                           f"{peak_equity:,.2f})")
    return halted, "; ".join(reasons)


def update_risk_state(conn, profile_id, equity, today=None):
    """Reads/creates the profile's risk state and folds today's equity into
    it. Returns a dict with day_start_equity, peak_equity, halted, halt_reason
    and the day's P/L. Called once per cycle, before any trading decision."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    state = {"day": today, "day_start_equity": equity, "peak_equity": equity,
             "halted": False, "halt_reason": "", "day_pnl_pct": 0.0,
             "drawdown_pct": 0.0}
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT day, day_start_equity, peak_equity FROM risk_state "
            "WHERE profile_id = ?", (profile_id,))
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                "INSERT INTO risk_state (profile_id, day, day_start_equity, "
                "peak_equity, halted, halt_reason) VALUES (?, ?, ?, ?, 0, '')",
                (profile_id, today, equity, equity))
        else:
            day, day_start, peak = row
            day_start = float(day_start or equity)
            peak = float(peak or equity)
            if day != today:
                # A new session: yesterday's loss limit no longer applies, and
                # the drawdown peak keeps tracking the all-time high.
                day_start = equity
            peak = max(peak, equity)
            cursor.execute(
                "UPDATE risk_state SET day = ?, day_start_equity = ?, "
                "peak_equity = ? WHERE profile_id = ?",
                (today, day_start, peak, profile_id))
            state.update(day=day, day_start_equity=day_start, peak_equity=peak)
        conn.commit()
    except sqlite3.Error:
        pass

    halted, reason = evaluate_breakers(equity, state["day_start_equity"],
                                       state["peak_equity"])
    state["halted"] = halted
    state["halt_reason"] = reason
    if state["day_start_equity"]:
        state["day_pnl_pct"] = (equity / state["day_start_equity"] - 1.0) * 100.0
    if state["peak_equity"]:
        state["drawdown_pct"] = (equity / state["peak_equity"] - 1.0) * 100.0
    try:
        conn.execute("UPDATE risk_state SET halted = ?, halt_reason = ? "
                     "WHERE profile_id = ?", (1 if halted else 0, reason,
                                              profile_id))
        conn.commit()
    except sqlite3.Error:
        pass
    return state


# ==========================================
# 6. AI ANALYSIS (prompt building + robust parsing)
# ==========================================
# Point-of-view instructions per analysis lens (set by the Fundamental /
# Technical checkboxes in the UI).
_LENS_INSTRUCTIONS = {
    "fundamental": (
        "ANALYSIS LENS - FUNDAMENTAL: judge every stock purely from a company "
        "fundamentals point of view: earnings growth and quality, valuation "
        "(P/E, price/sales, fair value), balance sheet strength (debt versus "
        "cash), free cash flow, dividend reliability, competitive moat and "
        "sector outlook. Favor quality companies trading below fair value; "
        "trim overvalued or fundamentally deteriorating positions. Short-term "
        "price chart action is irrelevant under this lens."
    ),
    "technical": (
        "ANALYSIS LENS - TECHNICAL: judge every stock purely from a technical "
        "point of view: trend direction versus the moving averages "
        "(SMA20/SMA50/SMA200), momentum (RSI-14 and 1/3/6-month returns), "
        "distance from the 52-week high/low, breakouts and breakdowns. Buy "
        "strength emerging from consolidation; reduce on breakdowns, trend "
        "damage, or overbought conditions (RSI-14 above 70). Company "
        "fundamentals are secondary under this lens."
    ),
    "combined": (
        "ANALYSIS LENS - COMBINED: weigh BOTH fundamentals (valuation, "
        "earnings, balance sheet) and technicals (trend versus moving "
        "averages, momentum, RSI-14, 52-week range). The strongest ideas have "
        "fundamental AND technical support; flag conflicts explicitly in the "
        "reason field."
    ),
    "general": (
        "ANALYSIS LENS - GENERAL: apply a conservative capital-preservation "
        "point of view."
    ),
}


def _lens_kind(fundamental, technical):
    """Maps the two UI checkboxes to a lens identifier."""
    if fundamental and technical:
        return "combined"
    if fundamental:
        return "fundamental"
    if technical:
        return "technical"
    return "general"


def _is_cloud_model(model):
    """Ollama cloud models carry a ':cloud' (or '-cloud') marker in the tag."""
    lowered = (model or "").lower()
    return ":cloud" in lowered or lowered.endswith("-cloud")


def _think_param_for(model):
    """Cloud models (e.g. deepseek-v4-pro:cloud) think at MAXIMUM level
    ('high'); local models skip thinking entirely, because with grammar-
    constrained JSON output thinking only adds latency, not quality."""
    return "high" if _is_cloud_model(model) else False


def _to_float(value, default=0.0):
    """Coerces model output like '2 shares', 3, '12.5' into a float."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else default


def _parse_model_json(raw):
    """Defensively parses the model reply into a list. Handles markdown code
    fences, stray text around the JSON, a dict wrapper like
    {"recommendations": [...]}, AND a single bare recommendation object such
    as {"action": ...} (some models emit one object instead of an array).
    Returns [] if nothing usable is found."""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.S)
        if not match:
            # Maybe a bare {...} object with stray text around it.
            obj = re.search(r"\{.*\}", text, re.S)
            if not obj:
                return []
            try:
                parsed = json.loads(obj.group(0))
            except json.JSONDecodeError:
                return []
        else:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                return []

    if isinstance(parsed, dict):
        extracted = None
        for key in ("recommendations", "actions", "recs", "results", "data"):
            if isinstance(parsed.get(key), list):
                extracted = parsed[key]
                break
        if extracted is None:
            # Single bare recommendation object -> wrap it in a list.
            if "action" in parsed or "symbol" in parsed:
                extracted = [parsed]
            else:
                extracted = []
        parsed = extracted
    return parsed if isinstance(parsed, list) else []


def parse_recommendations(raw_recs):
    """Validates and normalizes model recommendations into a safe structure."""
    clean = []
    if not isinstance(raw_recs, list):
        return clean
    for r in raw_recs:
        if not isinstance(r, dict):
            continue
        action = str(r.get("action", "")).strip().upper()
        if action not in ("BUY", "SELL", "HOLD"):
            continue
        symbol = str(r.get("symbol", "")).strip().upper()
        symbol = symbol.split(":")[0].strip(".")
        if not symbol or not re.match(r"^[A-Z0-9.\-^]+$", symbol):
            continue
        clean.append({
            "action": action,
            "symbol": symbol,
            "quantity": _to_float(r.get("quantity"), 0.0),
            "amount": _to_float(r.get("amount"), 0.0),
            "reason": str(r.get("reason", "")).strip() or "No reason provided.",
        })
    return clean


def build_analysis_prompt(profile_name, currency, cash, holdings, candidate_prices,
                          web_context=None, technical_indicators=None, lens="general"):
    """Builds the complete analysis prompt (kept separate from the Ollama
    call so it can be inspected and tested independently)."""
    web_section = ""
    if web_context:
        web_section = (
            "\nLatest Web Search Results (retrieved just now; supplementary "
            "context, may be noisy):\n" + _format_web_context(web_context) + "\n"
        )

    tech_section = ""
    if technical_indicators:
        tech_section = (
            "\nTechnical Indicators (computed locally from one year of daily "
            "prices):\n" + json.dumps(technical_indicators, indent=2) + "\n"
        )

    lens_line = _LENS_INSTRUCTIONS.get(lens, _LENS_INSTRUCTIONS["general"])

    prompt = f"""
You are a conservative, risk-averse financial portfolio advisor specializing
in capital preservation, moderate risk, and loss minimization.

{lens_line}

Current Profile: {profile_name}
Currency: {currency}
Available Cash Balance: {currency} {cash:.2f}

Current Portfolio Holdings:
{json.dumps(holdings, indent=2)}

Candidate Blue-Chip Stock Prices (live):
{json.dumps(candidate_prices, indent=2)}
{tech_section}{web_section}
INSTRUCTIONS:
1. Analyze the portfolio, the live prices, the technical indicators (if
   present), and (if present) the web search results - always through the
   analysis lens stated above.
2. Recommend at most 1 to 3 actions (BUY, SELL, or HOLD) aimed strictly at
   minimizing risk of loss and achieving moderate, steady returns.
3. Prioritize capital preservation, adequate liquidity, and diversification.
4. TRADING DISCIPLINE - recommend a trade ONLY when the portfolio genuinely
   needs it: a clear diversification gap, dangerous over-concentration, a
   deteriorating holding, or a strong concrete signal under the active
   lens. Never recommend a BUY merely because cash is available, and never
   invent trades just to fill the array.
5. If the portfolio is already reasonably balanced and nothing clearly
   needs to change, recommend HOLD (or return an empty array) and explain
   why in the reason field. Doing nothing is a valid conservative choice.
6. NEVER recommend spending more than the available cash, and keep a
   healthy cash reserve (at least 25% of net worth) at all times.
7. "quantity" must be a positive number of shares (fractional allowed) and
   "amount" is the estimated total cost/proceeds.
8. Return ONLY a valid JSON ARRAY of objects - even for a single
   recommendation, wrap it in [ ] - with NO extra text and NO markdown
   formatting. An empty array [] is acceptable when no trade is warranted.

REQUIRED JSON FORMAT:
[
  {{
    "action": "BUY",
    "symbol": "MSFT",
    "quantity": 2,
    "amount": 800.0,
    "reason": "Strong balance sheet, low volatility, adds steady tech exposure."
  }}
]
"""
    return prompt


def query_ollama_for_analysis(profile_name, currency, cash, holdings,
                              candidate_prices, model, web_context=None,
                              technical_indicators=None, lens="general"):
    """Sends portfolio state, live prices, (optionally) computed technical
    indicators and (optionally) fresh web search results to the selected
    Ollama model, framed by the requested analysis lens (fundamental /
    technical / combined / general). Cloud models think at maximum level.
    Returns (raw_recommendations_list, error_message_or_None)."""

    prompt = build_analysis_prompt(profile_name, currency, cash, holdings,
                                   candidate_prices, web_context=web_context,
                                   technical_indicators=technical_indicators,
                                   lens=lens)

    payload = {
        "model": model,
        "messages": [
            {"role": "system",
             "content": "You are a professional financial advisor engine. "
                        "You output only raw JSON arrays, no markdown, no commentary."},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "format": "json",
        # Cloud models think at MAXIMUM level ("high"); local models skip
        # thinking (with grammar-constrained JSON output it only adds latency).
        "think": _think_param_for(model),
        "keep_alive": OLLAMA_KEEP_ALIVE,
    }
    if not _is_cloud_model(model):
        # Local models: low temperature keeps the JSON output stable. Cloud
        # models keep their provider-tuned defaults - forcing a low
        # temperature on deep-reasoning cloud models can trigger repetitive
        # thinking loops and degenerate answers.
        payload["options"] = {"temperature": 0.2}

    try:
        req = urllib.request.Request(
            OLLAMA_CHAT_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_S) as response:
            res_data = json.loads(response.read().decode("utf-8"))
        raw_content = (res_data.get("message") or {}).get("content") or "[]"
        if DEBUG_MODEL_OUTPUT:
            print(f"[debug] Ollama raw content: {raw_content[:2000]}")
        return _parse_model_json(raw_content), None
    except urllib.error.URLError as e:
        return [], (f"Cannot reach Ollama at {OLLAMA_BASE_URL} "
                    f"({getattr(e, 'reason', e)}). Is it running?")
    except Exception as e:
        return [], f"Ollama error: {e}"


# ==========================================
# 7. GRAPHICAL USER INTERFACE (Tkinter)
# ==========================================
# Modern dark palette - applied through pure ttk styling (clam base theme),
# so it stays at native speed with no extra dependencies.
C_BG = "#17181c"        # window background
C_CARD = "#22252b"      # cards / tree rows / buttons
C_FIELD = "#2b2f36"     # input fields, active surfaces
C_STRIPE = "#262931"    # zebra-striped table rows
C_FG = "#e8eaed"        # main text
C_MUTED = "#9aa0a6"     # secondary text
C_ACCENT = "#4f8cff"    # primary accent (buttons, selections)
C_GREEN = "#2ecc71"     # buy
C_RED = "#e74c3c"       # sell
C_GREEN_SOFT = "#7ee787"  # positive P/L text
C_RED_SOFT = "#ff8a80"    # negative P/L text
C_BORDER = "#33363d"


# The class must still be *definable* on a headless host (no Tk, no display),
# because engine.py imports this module for its pure functions. Only creating
# an instance needs a real Tk root, and __init__ says so plainly.
_AppBase = tk.Tk if TK_AVAILABLE else object


class PaperTradingApp(_AppBase):
    def __init__(self):
        if not TK_AVAILABLE:
            raise RuntimeError(TK_ERROR)
        super().__init__()
        self.title("AI Stock Portfolio & Paper Trader (Ollama + Live Web)")
        # Size the window to the actual screen so the bottom controls
        # (recommendations + BUY/SELL buttons) are never cut off.
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        win_w = min(1200, max(980, screen_w - 80))
        win_h = min(880, max(660, screen_h - 160))
        self.geometry(f"{win_w}x{win_h}")
        self.minsize(960, 600)

        init_db()

        self.current_profile_id = None
        self.currency_symbols = {"USD": "$", "EUR": "€", "GBP": "£"}

        # Guards: prevents overlapping refreshes and refresh-during-analysis.
        self._refresh_lock = threading.Lock()
        self._analysis_running = False

        # Continuous candlestick Trading Mode state.
        self._tm_running = False
        self._tm_stop_event = threading.Event()
        # Mirrored copy of the timeframe combobox: Tk variables cannot be
        # read from the loop's background thread (Python 3.13+ tkinter).
        self._tm_timeframe = "1 day"

        # Thread-safe UI dispatch: worker threads put callables on this queue
        # and the main thread runs them. (widget.after() from a worker thread
        # raises RuntimeError on Python 3.13+, so it must never be used from
        # background threads.)
        self._ui_queue = queue.Queue()

        self.setup_ui()
        self.load_profiles_into_dropdown()
        self.load_models_async()

        if not TV_DATA_AVAILABLE:
            self._set_status(f"TradingView data unavailable: {TV_DATA_ERROR}")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(UI_POLL_MS, self._process_ui_queue)
        self.after(AUTO_REFRESH_MS, self._auto_refresh_tick)

    # ---------- small helpers ----------

    def get_db_connection(self):
        return get_db_connection()

    def _engine(self):
        """The shared headless engine (engine.py).

        The import is deferred on purpose: engine.py imports this module
        for its pure functions, so importing it at module level here would
        be a circular import. By the time any method runs, both modules are
        fully loaded.
        """
        import engine
        if getattr(self, '_engine_instance', None) is None:
            self._engine_instance = engine.TradingEngine()
        self._engine_instance.profile_id = self.current_profile_id
        return self._engine_instance

    def _post_ui(self, func):
        """Thread-safe: run func on the UI thread. Worker threads must use
        this instead of widget.after(), which raises RuntimeError when called
        from a non-main thread on Python 3.13+."""
        self._ui_queue.put(func)

    def _process_ui_queue(self):
        """Runs on the main thread only: drains queued UI updates from the
        worker threads, then reschedules itself."""
        try:
            while True:
                try:
                    func = self._ui_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    func()
                except tk.TclError:
                    pass  # window is closing down
                except Exception as e:
                    print(f"UI callback error: {e}")
        finally:
            try:
                self.after(UI_POLL_MS, self._process_ui_queue)
            except tk.TclError:
                pass

    def _set_status(self, text):
        try:
            self.lbl_status.config(text=f"Status: {text}")
        except tk.TclError:
            pass

    def _save_model_selection(self, event=None):
        model = self.model_cb.get().strip()
        if model:
            set_setting("ollama_model", model)

    # ---------- UI construction ----------

    # ---------- theme ----------

    def setup_theme(self):
        """Modern dark skin built on the ttk 'clam' base theme - native
        rendering speed, no external libraries."""
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        base_font = ("Segoe UI", 10)

        style.configure(".", background=C_BG, foreground=C_FG,
                        fieldbackground=C_FIELD, bordercolor=C_BORDER,
                        lightcolor=C_CARD, darkcolor=C_CARD,
                        troughcolor=C_FIELD, font=base_font)

        style.configure("TFrame", background=C_BG)
        style.configure("TLabelframe", background=C_BG, bordercolor=C_BORDER,
                        relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=C_BG,
                        foreground=C_ACCENT, font=("Segoe UI", 10, "bold"))

        style.configure("TLabel", background=C_BG, foreground=C_FG)
        style.configure("Muted.TLabel", background=C_BG, foreground=C_MUTED,
                        font=("Segoe UI", 9))
        style.configure("Header.TLabel", background=C_BG, foreground=C_FG,
                        font=("Segoe UI", 11, "bold"))
        style.configure("PL.TLabel", background=C_BG, foreground=C_FG,
                        font=("Segoe UI", 11, "bold"))

        style.configure("TButton", background=C_CARD, foreground=C_FG,
                        bordercolor=C_BORDER, focusthickness=1,
                        relief="flat", padding=(12, 5))
        style.map("TButton",
                  background=[("pressed", C_BORDER), ("active", C_FIELD),
                              ("disabled", C_CARD)],
                  foreground=[("disabled", "#6b7078")])
        style.configure("Accent.TButton", background=C_ACCENT,
                        foreground="#ffffff", padding=(14, 6))
        style.map("Accent.TButton",
                  background=[("pressed", "#3d74e0"), ("active", "#6b9bff"),
                              ("disabled", C_CARD)],
                  foreground=[("disabled", "#6b7078")])

        style.configure("TCheckbutton", background=C_BG, foreground=C_FG)
        style.map("TCheckbutton",
                  background=[("active", C_BG)],
                  foreground=[("disabled", "#6b7078")])

        style.configure("TEntry", fieldbackground=C_FIELD, foreground=C_FG,
                        insertcolor=C_FG, bordercolor=C_BORDER, padding=3)
        style.configure("TCombobox", fieldbackground=C_FIELD, foreground=C_FG,
                        background=C_CARD, arrowcolor=C_FG, bordercolor=C_BORDER,
                        padding=3)
        style.map("TCombobox",
                  fieldbackground=[("readonly", C_FIELD), ("disabled", C_CARD)],
                  foreground=[("disabled", "#6b7078")])
        # Drop-down list colors (not reachable through ttk styles).
        self.option_add("*TCombobox*Listbox.background", C_CARD)
        self.option_add("*TCombobox*Listbox.foreground", C_FG)
        self.option_add("*TCombobox*Listbox.selectBackground", C_ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.option_add("*TCombobox*Listbox.font", base_font)

        style.configure("Treeview", background=C_CARD, foreground=C_FG,
                        fieldbackground=C_CARD, rowheight=26,
                        bordercolor=C_BORDER, font=base_font)
        style.configure("Treeview.Heading", background=C_FIELD,
                        foreground=C_FG, font=("Segoe UI", 9, "bold"),
                        relief="flat", padding=(4, 4))
        style.map("Treeview",
                  background=[("selected", C_ACCENT)],
                  foreground=[("selected", "#ffffff")])
        style.map("Treeview.Heading", background=[("active", C_BORDER)])

        style.configure("TNotebook", background=C_BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=C_CARD, foreground=C_MUTED,
                        padding=(16, 8), font=("Segoe UI", 10))
        style.map("TNotebook.Tab",
                  background=[("selected", C_FIELD), ("active", C_CARD)],
                  foreground=[("selected", C_FG), ("active", C_FG)])

    def setup_ui(self):
        self.setup_theme()
        self.configure(bg=C_BG)

        # Top Frame: Profile Management
        profile_frame = ttk.LabelFrame(self, text=" Profile Management ")
        profile_frame.pack(fill="x", padx=15, pady=10)

        ttk.Label(profile_frame, text="Select Profile:").pack(side="left", padx=5, pady=5)
        self.profile_cb = ttk.Combobox(profile_frame, state="readonly", width=25)
        self.profile_cb.pack(side="left", padx=5, pady=5)
        self.profile_cb.bind("<<ComboboxSelected>>", self.on_profile_selected)

        ttk.Button(profile_frame, text="➕ Create Profile",
                   command=self.create_profile_dialog).pack(side="left", padx=5, pady=5)
        ttk.Button(profile_frame, text="➕ Deposit Cash",
                   command=self.add_paper_money).pack(side="left", padx=5, pady=5)
        ttk.Button(profile_frame, text="🗑️ Delete Profile",
                   command=self.delete_profile).pack(side="left", padx=5, pady=5)

        # AI Engine frame: model selection + data options
        engine_frame = ttk.LabelFrame(self, text=" AI Engine (Ollama) ")
        engine_frame.pack(fill="x", padx=15, pady=(0, 5))

        model_row = ttk.Frame(engine_frame)
        model_row.pack(fill="x", padx=5, pady=(6, 2))

        ttk.Label(model_row, text="🤖 Model:").pack(side="left", padx=(5, 3))
        self.model_cb = ttk.Combobox(model_row, width=34, state="normal")
        self.model_cb.pack(side="left", padx=3)
        # Editable combobox: user can type ANY exact model name.
        self.model_cb.bind("<Return>", self._save_model_selection)
        self.model_cb.bind("<FocusOut>", self._save_model_selection)
        self.model_cb.bind("<<ComboboxSelected>>", self._save_model_selection)

        ttk.Button(model_row, text="🔄 Refresh Models",
                   command=self.load_models_async).pack(side="left", padx=5)
        self.lbl_model_status = ttk.Label(model_row, text="Loading models from Ollama...",
                                          style="Muted.TLabel")
        self.lbl_model_status.pack(side="left", padx=10)

        # One compact row: web search, live prices and the analysis lens.
        options_row = ttk.Frame(engine_frame)
        options_row.pack(fill="x", padx=5, pady=(1, 5))

        self.web_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_row, text="🌐 Web Search",
                        variable=self.web_var).pack(side="left", padx=(5, 10))

        self.live_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_row, text=f"⏱ Live Prices ({AUTO_REFRESH_MS // 1000}s)",
                        variable=self.live_var).pack(side="left", padx=(0, 15))

        ttk.Label(options_row, text="Analysis lens:").pack(side="left", padx=(0, 3))
        self.fund_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_row, text="📊 Fundamental",
                        variable=self.fund_var).pack(side="left", padx=3)
        self.tech_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_row, text="📈 Technical",
                        variable=self.tech_var).pack(side="left", padx=3)
        ttk.Label(options_row, text="(both = combined)",
                  style="Muted.TLabel").pack(side="left", padx=8)

        # Profile Summary Header
        self.summary_frame = ttk.LabelFrame(self, text=" Account Overview ")
        self.summary_frame.pack(fill="x", padx=15, pady=5)

        # Row 1: balances
        summary_row1 = ttk.Frame(self.summary_frame)
        summary_row1.pack(fill="x", padx=10, pady=(8, 0))

        self.lbl_cash = ttk.Label(summary_row1, text="Cash Balance: -",
                                  style="Header.TLabel")
        self.lbl_cash.pack(side="left", padx=(10, 20))

        self.lbl_equity = ttk.Label(summary_row1, text="Portfolio Value: -",
                                    style="Header.TLabel")
        self.lbl_equity.pack(side="left", padx=20)

        self.lbl_total = ttk.Label(summary_row1, text="Net Worth: -",
                                   style="Header.TLabel")
        self.lbl_total.pack(side="left", padx=20)

        self.lbl_updated = ttk.Label(summary_row1, text="Prices: -",
                                     style="Muted.TLabel")
        self.lbl_updated.pack(side="left", padx=20)

        # Auto Trading Mode Checkbox
        self.auto_var = tk.BooleanVar(value=False)
        self.auto_chk = ttk.Checkbutton(summary_row1, text="🤖 Auto-Trade (Auto Buy/Sell)",
                                        variable=self.auto_var, command=self.toggle_auto_mode)
        self.auto_chk.pack(side="right", padx=10)

        # Row 2: overall profit / loss
        summary_row2 = ttk.Frame(self.summary_frame)
        summary_row2.pack(fill="x", padx=10, pady=(2, 8))

        self.lbl_realized = ttk.Label(summary_row2, text="Realized P/L: -",
                                      style="PL.TLabel")
        self.lbl_realized.pack(side="left", padx=(10, 20))

        self.lbl_unrealized = ttk.Label(summary_row2, text="Unrealized P/L: -",
                                        style="PL.TLabel")
        self.lbl_unrealized.pack(side="left", padx=20)

        self.lbl_overall = ttk.Label(summary_row2, text="Overall P/L: -",
                                     style="PL.TLabel")
        self.lbl_overall.pack(side="left", padx=16)

        self.lbl_avg_pos = ttk.Label(summary_row2, text="Avg position P/L: -",
                                     style="PL.TLabel")
        self.lbl_avg_pos.pack(side="left", padx=16)

        # Analysis Controls
        control_frame = ttk.Frame(self)
        control_frame.pack(fill="x", padx=15, pady=(4, 2))

        self.btn_analyze = ttk.Button(control_frame, text="🚀 Start AI Market Analysis",
                                      style="Accent.TButton",
                                      command=self.start_analysis_thread)
        self.btn_analyze.pack(side="left", padx=5)

        self.btn_trading_mode = ttk.Button(
            control_frame, text="▶️ Trading Mode",
            style="Accent.TButton", command=self._toggle_trading_mode)
        self.btn_trading_mode.pack(side="left", padx=5)

        ttk.Label(control_frame, text="Timeframe:").pack(side="left", padx=(15, 3))
        self.tf_var = tk.StringVar(value="Auto (all timeframes)")
        self.tf_cb = ttk.Combobox(control_frame, textvariable=self.tf_var,
                                  values=list(TRADING_TIMEFRAMES.keys()),
                                  state="readonly", width=22)
        self.tf_cb.pack(side="left")
        self.tf_cb.bind("<<ComboboxSelected>>", self._on_timeframe_changed)

        self.lbl_status = ttk.Label(control_frame, text="Status: Ready",
                                    style="Muted.TLabel")
        self.lbl_status.pack(side="left", padx=15)

        # ------------------------------------------------------------------
        # Risk engine panel: the numbers that decide how much can be lost.
        # Shown permanently so the protections are never a hidden assumption.
        # ------------------------------------------------------------------
        risk_frame = ttk.LabelFrame(self, text=" 🛡️ Risk Engine (capital protection) ")
        risk_frame.pack(fill="x", padx=15, pady=(2, 2))

        risk_row = ttk.Frame(risk_frame)
        risk_row.pack(fill="x", padx=10, pady=(6, 3))

        self.lbl_risk_rules = ttk.Label(
            risk_row,
            text=(f"Risk/trade {RISK_PER_TRADE_PCT*100:.2f}%  •  "
                  f"max {MAX_OPEN_POSITIONS} positions  •  "
                  f"max position {MAX_POSITION_PCT*100:.0f}%  •  "
                  f"cash floor {CASH_FLOOR_PCT*100:.0f}%  •  "
                  f"stop→breakeven at +{BREAKEVEN_AT_R:.0f}R then "
                  f"{TRAIL_ATR_MULT:g}xATR trail  •  "
                  f"target {TAKE_PROFIT_R:.0f}R  •  slip {SLIPPAGE_BPS:g}bps"),
            style="Muted.TLabel")
        self.lbl_risk_rules.pack(side="left")

        self.lbl_risk_state = ttk.Label(risk_row, text="Risk state: -",
                                        style="PL.TLabel")
        self.lbl_risk_state.pack(side="right", padx=8)

        # AI Recommendations & Execution - packed BEFORE the holdings tabs so
        # the suggestions and the BUY/SELL buttons stay visible even on small
        # screens (the tabs below absorb any shortage and have scrollbars).
        recs_frame = ttk.LabelFrame(self, text=" AI Recommendations & Execution ")
        recs_frame.pack(fill="both", expand=True, padx=15, pady=(2, 4))

        rec_cols = ("Action", "Symbol", "Suggested Shares", "Estimated Amount", "Rationale")
        recs_holder, recs_tree = self._tree_with_scrollbar(
            recs_frame, columns=rec_cols, show="headings", height=4)
        self.recs_tree = recs_tree
        for col in rec_cols:
            self.recs_tree.heading(col, text=col)
            self.recs_tree.column(col, anchor="center")
        self.recs_tree.column("Rationale", anchor="w", width=430)
        recs_holder.pack(fill="both", expand=True, padx=5, pady=5)
        self.recs_tree.bind("<<TreeviewSelect>>", self.on_rec_selected)
        self._style_tree(self.recs_tree)

        # Action Buttons Area (Green Buy / Red Sell)
        action_bar = tk.Frame(recs_frame, bg=C_BG)
        action_bar.pack(fill="x", padx=5, pady=5)

        tk.Label(action_bar, text="Symbol:", bg=C_BG, fg=C_FG).pack(side="left", padx=(5, 2))
        self.ent_symbol = ttk.Entry(action_bar, width=9)
        self.ent_symbol.pack(side="left", padx=(0, 10))

        tk.Label(action_bar, text="Qty:", bg=C_BG, fg=C_FG).pack(side="left", padx=5)
        self.ent_trade_val = ttk.Entry(action_bar, width=9)
        self.ent_trade_val.pack(side="left", padx=5)

        self.btn_buy = tk.Button(action_bar, text="🟢 BUY", bg=C_GREEN, fg="white",
                                 activebackground="#27ae60", activeforeground="white",
                                 font=("Segoe UI", 10, "bold"), relief="flat", bd=0,
                                 cursor="hand2",
                                 command=lambda: self.execute_manual_trade("BUY"))
        self.btn_buy.pack(side="left", padx=10, ipady=5, ipadx=12)

        self.btn_sell = tk.Button(action_bar, text="🔴 SELL", bg=C_RED, fg="white",
                                  activebackground="#c0392b", activeforeground="white",
                                  font=("Segoe UI", 10, "bold"), relief="flat", bd=0,
                                  cursor="hand2",
                                  command=lambda: self.execute_manual_trade("SELL"))
        self.btn_sell.pack(side="left", padx=5, ipady=5, ipadx=12)

        # Holdings & Trade History notebook - packed LAST so it absorbs any
        # leftover (or missing) vertical space without ever hiding the
        # recommendation area above.
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=15, pady=(0, 8))

        holdings_tab = ttk.Frame(notebook)
        notebook.add(holdings_tab, text="Current Holdings")

        columns = ("Symbol", "Shares", "Avg Buy Price", "Current Price",
                   "Total Value", "Profit/Loss")
        holdings_holder, holdings_tree = self._tree_with_scrollbar(
            holdings_tab, columns=columns, show="headings", height=5)
        self.holdings_tree = holdings_tree
        for col in columns:
            self.holdings_tree.heading(col, text=col)
            self.holdings_tree.column(col, anchor="center")
        self.holdings_tree.column("Symbol", width=90)
        self.holdings_tree.column("Shares", width=80)
        self.holdings_tree.column("Avg Buy Price", width=130)
        self.holdings_tree.column("Current Price", width=130)
        self.holdings_tree.column("Total Value", width=130)
        self.holdings_tree.column("Profit/Loss", width=110)
        holdings_holder.pack(fill="both", expand=True, padx=5, pady=5)
        self._style_tree(self.holdings_tree)

        history_tab = ttk.Frame(notebook)
        notebook.add(history_tab, text="Trade History")

        hist_cols = ("Time", "Action", "Symbol", "Qty", "Price", "Total",
                     "Realized P/L", "Reason")
        history_holder, history_tree = self._tree_with_scrollbar(
            history_tab, columns=hist_cols, show="headings", height=6)
        self.history_tree = history_tree
        for col in hist_cols:
            self.history_tree.heading(col, text=col)
            self.history_tree.column(col, anchor="center")
        self.history_tree.column("Time", width=150)
        self.history_tree.column("Realized P/L", width=110)
        self.history_tree.column("Reason", anchor="w", width=290)
        history_holder.pack(fill="both", expand=True, padx=5, pady=5)
        self.history_tree.bind("<Double-1>", self.on_history_double_click)
        self._style_tree(self.history_tree)

        # History toolbar: correct the price you actually executed at.
        hist_bar = ttk.Frame(history_tab)
        hist_bar.pack(fill="x", padx=5, pady=(0, 5))
        ttk.Button(hist_bar, text="✏️ Edit Price of Selected Trade",
                   command=self.edit_selected_trade_price).pack(side="left")
        ttk.Label(hist_bar,
                  text="Double-click a trade to correct its execution price - "
                       "cash, average cost and realized P/L are recalculated.",
                  style="Muted.TLabel").pack(side="left", padx=12)

    def _style_tree(self, tree):
        """Zebra striping + profit/loss sign coloring for table rows."""
        tree.tag_configure("odd", background=C_STRIPE)
        tree.tag_configure("even", background=C_CARD)
        tree.tag_configure("pnl_up", foreground=C_GREEN_SOFT)
        tree.tag_configure("pnl_down", foreground=C_RED_SOFT)

    def _tree_with_scrollbar(self, parent, **tree_kwargs):
        """Treeview packed together with a vertical scrollbar, so no row is
        ever unreachable when space runs short."""
        holder = ttk.Frame(parent)
        tree = ttk.Treeview(holder, **tree_kwargs)
        vsb = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        return holder, tree

    # ---------- modern dark dialogs ----------
    # Native messagebox/simpledialog pop-ups kept the old light Windows look,
    # so every prompt and notification is a themed dark dialog instead.

    def _dialog_root(self, title):
        win = tk.Toplevel(self)
        win.title(title)
        win.configure(bg=C_BG)
        win.transient(self)
        win.resizable(False, False)
        return win

    def _center_dialog(self, win):
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _popup_message(self, title, message, kind="info"):
        """Dark replacement for messagebox.showinfo/showwarning/showerror."""
        icons = {"info": "ℹ️", "warning": "⚠️", "error": "⛔"}
        win = self._dialog_root(title)
        body = ttk.Frame(win)
        body.pack(fill="x", padx=24, pady=(20, 12))
        ttk.Label(body, text=icons.get(kind, "ℹ️"),
                  font=("Segoe UI", 20)).pack(side="left", padx=(0, 14))
        ttk.Label(body, text=message, wraplength=330,
                  justify="left").pack(side="left", fill="x")
        ttk.Button(win, text="OK", style="Accent.TButton",
                   command=win.destroy).pack(pady=(0, 18), ipadx=10)
        win.bind("<Return>", lambda e: win.destroy())
        win.bind("<Escape>", lambda e: win.destroy())
        self._center_dialog(win)
        win.grab_set()
        self.wait_window(win)

    def _popup_confirm(self, title, message):
        """Dark replacement for messagebox.askyesno. Returns True/False."""
        result = {"ok": False}
        win = self._dialog_root(title)
        ttk.Label(win, text="❓", font=("Segoe UI", 20)).pack(pady=(20, 6))
        ttk.Label(win, text=message, wraplength=350,
                  justify="center").pack(padx=28)
        bar = ttk.Frame(win)
        bar.pack(pady=(16, 18))

        def yes():
            result["ok"] = True
            win.destroy()

        ttk.Button(bar, text="Yes", style="Accent.TButton",
                   command=yes).pack(side="left", padx=8, ipadx=10)
        ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="left", padx=8, ipadx=6)
        win.bind("<Return>", lambda e: yes())
        win.bind("<Escape>", lambda e: win.destroy())
        self._center_dialog(win)
        win.grab_set()
        self.wait_window(win)
        return result["ok"]

    def _popup_prompt(self, title, message, initial="", as_float=False, minvalue=None):
        """Dark replacement for simpledialog.askstring/askfloat. Returns the
        entered value, or None when cancelled."""
        result = {"value": None}
        win = self._dialog_root(title)
        ttk.Label(win, text=message, wraplength=330,
                  justify="left").pack(padx=24, pady=(20, 8), anchor="w")
        var = tk.StringVar(value=str(initial))
        entry = ttk.Entry(win, textvariable=var, width=30, font=("Segoe UI", 11))
        entry.pack(padx=24, fill="x")
        err_lbl = ttk.Label(win, text="", style="Muted.TLabel", foreground=C_RED_SOFT)
        err_lbl.pack(padx=24, pady=(3, 0), anchor="w")

        def ok():
            raw = var.get().strip()
            if as_float:
                try:
                    value = float(raw.replace(",", "."))
                except ValueError:
                    err_lbl.config(text="Please enter a valid number.")
                    return
                if minvalue is not None and value < minvalue:
                    err_lbl.config(text=f"Value must be at least {minvalue}.")
                    return
                result["value"] = value
            else:
                if not raw:
                    err_lbl.config(text="Please enter a value.")
                    return
                result["value"] = raw
            win.destroy()

        bar = ttk.Frame(win)
        bar.pack(pady=(10, 18))
        ttk.Button(bar, text="OK", style="Accent.TButton",
                   command=ok).pack(side="left", padx=8, ipadx=10)
        ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="left", padx=8, ipadx=6)
        entry.bind("<Return>", lambda e: ok())
        win.bind("<Escape>", lambda e: win.destroy())
        self._center_dialog(win)
        entry.focus_set()
        entry.select_range(0, "end")
        win.grab_set()
        self.wait_window(win)
        return result["value"]

    # ==========================================
    # DATA & PROFILE CONTROLLERS
    # ==========================================
    def load_profiles_into_dropdown(self):
        conn = self.get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name FROM profiles ORDER BY name")
        profiles = cursor.fetchall()
        conn.close()

        self.profile_cb["values"] = [p[1] for p in profiles]

        if not profiles:
            self.current_profile_id = None
            self.clear_ui()
            return

        # Keep the current profile selected if it still exists.
        if self.current_profile_id and self.current_profile_id in {p[0] for p in profiles}:
            for p in profiles:
                if p[0] == self.current_profile_id:
                    self.profile_cb.set(p[1])
            return

        self.profile_cb.current(0)
        self.on_profile_selected()

    def create_profile_dialog(self):
        name = self._popup_prompt("New Profile", "Enter profile name:")
        if not name or not name.strip():
            return
        name = name.strip()

        # Choose currency (account starts EMPTY - deposit cash manually)
        currency_win = self._dialog_root(f"New Profile: {name}")

        ttk.Label(currency_win, text="Choose paper currency:").pack(pady=(16, 5))
        curr_var = tk.StringVar(value="USD")
        curr_cb = ttk.Combobox(currency_win, textvariable=curr_var,
                               values=["USD", "EUR", "GBP"], state="readonly")
        curr_cb.pack(pady=5)
        ttk.Label(currency_win,
                  text="Account starts empty (0.00).\nUse 'Deposit Cash' to add funds.",
                  style="Muted.TLabel", justify="center").pack(pady=4)

        def save_profile():
            curr = curr_var.get()
            try:
                conn = self.get_db_connection()
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO profiles (name, currency, balance, auto_mode) VALUES (?, ?, ?, ?)",
                    (name, curr, 0.0, 0))
                conn.commit()
                conn.close()
            except sqlite3.Error as e:
                try:
                    conn.close()
                except Exception:
                    pass
                self._popup_message("Error",
                                    "A profile with that name already exists!" if isinstance(e, sqlite3.IntegrityError)
                                    else f"Could not create profile: {e}",
                                    kind="error")
                return

            currency_win.destroy()
            self.current_profile_id = None  # force reselection
            self.load_profiles_into_dropdown()
            self.profile_cb.set(name)
            self.on_profile_selected()
            self._set_status(f"Profile '{name}' created with 0.00 {curr} - deposit cash to begin trading.")

        ttk.Button(currency_win, text="Create (empty account)",
                   command=save_profile).pack(pady=(8, 16))
        self._center_dialog(currency_win)
        currency_win.grab_set()

    def add_paper_money(self):
        if not self.current_profile_id:
            self._popup_message("Warning", "Select or create a profile first.", kind="warning")
            return

        amount = self._popup_prompt("Deposit Cash", "Enter paper money amount to add:",
                                    as_float=True, minvalue=0.01)
        if amount and amount > 0:
            try:
                conn = self.get_db_connection()
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE profiles SET balance = balance + ?, "
                    "total_deposited = total_deposited + ? WHERE id = ?",
                    (amount, amount, self.current_profile_id))
                conn.commit()
                conn.close()
            except sqlite3.Error as e:
                self._popup_message("Error", f"Deposit failed: {e}", kind="error")
                return
            self.refresh_profile_data()
            self._set_status(f"Deposited {amount:,.2f} paper money.")

    def delete_profile(self):
        if not self.current_profile_id:
            self._popup_message("Warning", "Select a profile to delete first.", kind="warning")
            return
        name = self.profile_cb.get()
        if not self._popup_confirm(
                "Confirm Delete",
                f"Delete profile '{name}' and ALL its positions and trade history?"):
            return
        try:
            conn = self.get_db_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM profiles WHERE id = ?", (self.current_profile_id,))
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            self._popup_message("Error", f"Delete failed: {e}", kind="error")
            return
        self.current_profile_id = None
        self.auto_var.set(False)
        self.load_profiles_into_dropdown()
        self._set_status(f"Profile '{name}' deleted.")

    def on_profile_selected(self, event=None):
        name = self.profile_cb.get()
        if not name:
            return
        conn = self.get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, auto_mode FROM profiles WHERE name = ?", (name,))
        row = cursor.fetchone()
        conn.close()

        if row:
            self.current_profile_id = row[0]
            self.auto_var.set(bool(row[1]))
            self.refresh_profile_data()
            self.load_trade_history()
        else:
            self.current_profile_id = None
            self.clear_ui()

    def toggle_auto_mode(self):
        if not self.current_profile_id:
            # Don't leave the checkbox visually ON with nothing persisted.
            self.auto_var.set(False)
            self._popup_message("Warning", "Select or create a profile first.", kind="warning")
            return
        auto_val = 1 if self.auto_var.get() else 0
        conn = self.get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE profiles SET auto_mode = ? WHERE id = ?",
                       (auto_val, self.current_profile_id))
        conn.commit()
        conn.close()
        self._set_status("Auto-trade ENABLED - AI orders will execute automatically."
                         if auto_val else "Auto-trade disabled.")

    # ==========================================
    # LIVE DATA REFRESH (background threaded)
    # ==========================================
    def refresh_profile_data(self):
        """Non-blocking refresh: prices are fetched in a worker thread, then
        the UI is updated on the main thread. Skips if a refresh is already
        running."""
        if not self.current_profile_id:
            return
        if not self._refresh_lock.acquire(blocking=False):
            return
        pid = self.current_profile_id

        def worker():
            try:
                conn = self.get_db_connection()
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT name, currency, balance, total_deposited "
                    "FROM profiles WHERE id = ?", (pid,))
                p_row = cursor.fetchone()
                cursor.execute(
                    "SELECT symbol, quantity, avg_price FROM positions "
                    "WHERE profile_id = ? ORDER BY symbol", (pid,))
                pos_rows = cursor.fetchall()
                cursor.execute(
                    "SELECT realized_pnl, total FROM trades "
                    "WHERE profile_id = ? AND action = 'SELL' "
                    "AND realized_pnl IS NOT NULL", (pid,))
                sell_rows = cursor.fetchall()
                realized_pnl = sum(float(r or 0.0) for r, _t in sell_rows)
                # Average percentage return per closed trade (the cost basis of
                # a sold lot = proceeds - realized P/L, so no extra column is
                # needed to recover each trade's return %).
                trade_pcts = []
                for r, t in sell_rows:
                    cost = (t or 0.0) - (r or 0.0)
                    if cost > 0:
                        trade_pcts.append((r or 0.0) / cost * 100.0)
                avg_trade_pct = (sum(trade_pcts) / len(trade_pcts)) if trade_pcts else None
                conn.close()

                if not p_row:
                    return
                symbols = [r[0] for r in pos_rows]
                prices = get_stock_prices(symbols) if symbols else {}
                self._post_ui(
                    lambda: self._apply_profile_data(pid, p_row, pos_rows,
                                                     prices, realized_pnl,
                                                     avg_trade_pct))
            except Exception as e:
                self._post_ui(lambda: self._set_status(f"Price refresh error: {e}"))
            finally:
                self._refresh_lock.release()

        threading.Thread(target=worker, daemon=True).start()

    def _apply_profile_data(self, pid, p_row, pos_rows, prices, realized_pnl=0.0,
                            avg_trade_pct=None):
        # Profile may have been switched/deleted while prices were fetched.
        if self.current_profile_id != pid:
            return

        _p_name, currency, cash, total_deposited = p_row
        symbol_char = self.currency_symbols.get(currency, "$")

        for item in self.holdings_tree.get_children():
            self.holdings_tree.delete(item)

        total_portfolio_value = 0.0
        unrealized_pnl = 0.0
        cost_basis = 0.0
        position_pcts = []
        stale_count = 0
        row_index = 0

        for sym, qty, avg_p in pos_rows:
            curr_p = prices.get(sym, 0.0)
            if curr_p <= 0:
                # Live price unavailable: fall back to the buy price so the
                # table stays truthful rather than showing a fake 0 value.
                curr_p = avg_p
                stale_count += 1
            val = qty * curr_p
            total_portfolio_value += val
            pnl = ((curr_p - avg_p) / avg_p) * 100 if avg_p > 0 else 0.0
            unrealized_pnl += (curr_p - avg_p) * qty
            cost_basis += avg_p * qty
            position_pcts.append(pnl)

            stripe = "odd" if row_index % 2 else "even"
            pnl_tag = "pnl_up" if pnl >= 0 else "pnl_down"
            self.holdings_tree.insert("", "end", values=(
                sym,
                f"{qty:.4g}",
                f"{symbol_char}{avg_p:.2f}",
                f"{symbol_char}{curr_p:.2f}",
                f"{symbol_char}{val:.2f}",
                f"{pnl:+.2f}%"
            ), tags=(stripe, pnl_tag))
            row_index += 1

        net_worth = cash + total_portfolio_value
        self.lbl_cash.config(text=f"Cash Balance: {symbol_char}{cash:,.2f}")
        self.lbl_equity.config(text=f"Portfolio Value: {symbol_char}{total_portfolio_value:,.2f}")
        self.lbl_total.config(text=f"Net Worth: {symbol_char}{net_worth:,.2f}")

        # Overall profit / loss row - money value AND percentages.
        overall_pnl = realized_pnl + unrealized_pnl
        suffix_realized = (f" · avg {avg_trade_pct:+.2f}%/trade"
                           if avg_trade_pct is not None else "")
        suffix_unrealized = (f" · {unrealized_pnl / cost_basis * 100.0:+.2f}% of cost"
                              if cost_basis > 0 else "")
        suffix_overall = (f" · {overall_pnl / total_deposited * 100.0:+.2f}% of deposits"
                          if total_deposited and total_deposited > 0 else "")
        self._set_pl_label(self.lbl_realized, "Realized P/L", realized_pnl,
                           symbol_char, suffix_realized)
        self._set_pl_label(self.lbl_unrealized, "Unrealized P/L", unrealized_pnl,
                           symbol_char, suffix_unrealized)
        self._set_pl_label(self.lbl_overall, "Overall P/L", overall_pnl,
                           symbol_char, suffix_overall)

        if position_pcts:
            avg_pos_pct = sum(position_pcts) / len(position_pcts)
            self.lbl_avg_pos.config(
                text=f"Avg position P/L: {avg_pos_pct:+.2f}%",
                foreground=C_GREEN_SOFT if avg_pos_pct >= 0 else C_RED_SOFT)
        else:
            self.lbl_avg_pos.config(text="Avg position P/L: -", foreground=C_MUTED)

        stamp = datetime.now().strftime("%H:%M:%S")
        self.lbl_updated.config(
            text=f"Prices as of {stamp}" + (" (some unavailable)" if stale_count else ""))

        self._refresh_risk_panel(net_worth, len([1 for _s, q, _a in pos_rows
                                                 if float(q) > 0]))
        self.load_trade_history()

    def _refresh_risk_panel(self, equity, open_positions=0):
        """Shows today's and peak-to-date equity for this profile, plus the
        state of the circuit breakers. Read-only: it never writes risk state,
        because only a trading cycle may advance the day's baseline."""
        if not self.current_profile_id:
            return
        try:
            conn = self.get_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT day, day_start_equity, peak_equity, halted, "
                    "halt_reason FROM risk_state WHERE profile_id = ?",
                    (self.current_profile_id,))
                row = cursor.fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return
        if not row:
            self.lbl_risk_state.config(
                text=f"Equity {equity:,.2f} · {open_positions}/"
                     f"{MAX_OPEN_POSITIONS} positions · breakers armed",
                foreground=C_MUTED)
            return
        day, day_start, peak, _halted, halt_reason = row
        day_start = float(day_start or equity)
        peak = float(peak or equity)
        day_pct = (equity / day_start - 1.0) * 100.0 if day_start else 0.0
        dd_pct = (equity / peak - 1.0) * 100.0 if peak else 0.0
        halted, reason = evaluate_breakers(equity, day_start, peak)
        text = (f"Equity {equity:,.2f} · today {day_pct:+.2f}% · "
                f"from peak {dd_pct:+.2f}% · {open_positions}/"
                f"{MAX_OPEN_POSITIONS} positions · ")
        text += "⛔ NEW ENTRIES HALTED" if halted else "✅ trading enabled"
        self.lbl_risk_state.config(
            text=text + (f" ({reason})" if halted and reason else ""),
            foreground=C_RED_SOFT if halted else C_GREEN_SOFT)

    @staticmethod
    def _set_pl_label(label, title, value, symbol_char, suffix=""):
        label.config(text=f"{title}: {symbol_char}{value:+,.2f}{suffix}",
                     foreground=C_GREEN_SOFT if value >= 0 else C_RED_SOFT)

    def _auto_refresh_tick(self):
        try:
            if (self.live_var.get() and self.current_profile_id
                    and not self._analysis_running):
                self.refresh_profile_data()
        except tk.TclError:
            return
        try:
            self.after(AUTO_REFRESH_MS, self._auto_refresh_tick)
        except tk.TclError:
            pass

    def load_trade_history(self):
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        if not self.current_profile_id:
            return
        try:
            conn = self.get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, trade_time, action, symbol, quantity, price, total, "
                "realized_pnl, reason FROM trades WHERE profile_id = ? "
                "ORDER BY id DESC LIMIT 300", (self.current_profile_id,))
            rows = cursor.fetchall()
            conn.close()
        except sqlite3.Error:
            return
        for row_index, (t_id, t, a, s, q, p, tot, rp, r) in enumerate(rows):
            pnl_text = ""
            tags = ["odd" if row_index % 2 else "even"]
            if a == "SELL" and rp is not None:
                pnl_text = f"{rp:+,.2f}"
                tags.append("pnl_up" if rp >= 0 else "pnl_down")
            # Item id = trade id, so the Edit Price action knows what to change.
            self.history_tree.insert("", "end", iid=str(t_id), values=(
                t, a, s, f"{q:.4g}", f"{p:.2f}", f"{tot:.2f}", pnl_text, r or ""),
                tags=tags)

    # ==========================================
    # TRADE PRICE CORRECTION (edit history)
    # ==========================================
    def on_history_double_click(self, event):
        self.edit_selected_trade_price()

    def edit_selected_trade_price(self):
        """Lets the user correct the price a historical trade actually
        executed at (double-click a row or use the Edit Price button)."""
        if not self.current_profile_id:
            self._popup_message("Warning", "Select or create a profile first.", kind="warning")
            return
        selected = self.history_tree.selection()
        if not selected:
            self._popup_message("Warning", "Select a trade in the history first.",
                                kind="warning")
            return
        trade_id = int(selected[0])  # item id == trade id
        values = self.history_tree.item(selected[0])["values"]
        action, symbol = str(values[1]), str(values[2])
        qty_text, old_price_text = str(values[3]), str(values[4])

        new_price = self._popup_prompt(
            "Edit Trade Price",
            f"{action} {qty_text} {symbol}\nEnter the price you actually executed at:",
            initial=float(old_price_text), as_float=True, minvalue=0.01)
        if new_price is None or abs(new_price - float(old_price_text)) < 1e-9:
            return

        if not self._popup_confirm(
                "Confirm Price Correction",
                f"Update {action} {qty_text} {symbol} from "
                f"{float(old_price_text):,.2f} to {new_price:,.2f}?\n\n"
                "Cash, average cost basis and realized P/L will be "
                "recalculated automatically."):
            return

        if self.edit_trade_price(trade_id, float(new_price)):
            self._set_status(f"Trade corrected: {action} {qty_text} {symbol} "
                             f"@ {new_price:,.2f} (was {float(old_price_text):,.2f}).")
            self.refresh_profile_data()

    def edit_trade_price(self, trade_id, new_price):
        """Corrects the execution price of a historical trade and restores
        consistency everywhere: the trade's total, the cash delta, the
        position's average cost, and the realized P/L of every affected SELL
        of that symbol. Returns True on success."""
        conn = self.get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT action, symbol, quantity, price FROM trades "
                "WHERE id = ? AND profile_id = ?",
                (trade_id, self.current_profile_id))
            trade = cursor.fetchone()
            if not trade:
                self._popup_message("Error", "Trade not found.", kind="error")
                return False
            action, symbol, qty, old_price = trade
            qty, old_price = float(qty), float(old_price)

            # 1) The trade row itself.
            new_total = qty * new_price
            cursor.execute("UPDATE trades SET price = ?, total = ? WHERE id = ?",
                           (new_price, new_total, trade_id))

            # 2) Cash: refund/charge exactly the difference the correction
            #    implies (BUY: paid too much before -> refund; SELL: received
            #    too little before -> top up).
            cursor.execute("SELECT balance FROM profiles WHERE id = ?",
                           (self.current_profile_id,))
            balance = float(cursor.fetchone()[0])
            delta = (new_price - old_price) * qty
            balance += -delta if action == "BUY" else delta
            cursor.execute("UPDATE profiles SET balance = ? WHERE id = ?",
                           (balance, self.current_profile_id))

            # 3) Replay the symbol's whole ledger so the cost basis and every
            #    SELL's realized P/L stay mutually consistent.
            cursor.execute(
                "SELECT id, action, quantity, price FROM trades "
                "WHERE profile_id = ? AND symbol = ? ORDER BY id",
                (self.current_profile_id, symbol))
            ledger = cursor.fetchall()

            pos_qty = 0.0
            pos_cost = 0.0
            for t_id, t_action, t_qty, t_price in ledger:
                t_qty = float(t_qty)
                if t_action == "BUY":
                    pos_cost += t_qty * float(t_price)
                    pos_qty += t_qty
                    cursor.execute("UPDATE trades SET realized_pnl = 0 WHERE id = ?",
                                   (t_id,))
                else:  # SELL
                    if pos_qty > 1e-9:
                        avg = pos_cost / pos_qty
                        sold = min(t_qty, pos_qty)
                        rp = (float(t_price) - avg) * sold
                        pos_cost -= avg * sold
                        pos_qty -= sold
                    else:
                        rp = 0.0
                    cursor.execute("UPDATE trades SET realized_pnl = ? WHERE id = ?",
                                   (rp, t_id))

            # 4) Position: adopt the replayed basis when the ledger matches
            #    the position (normal case). Legacy positions that predate the
            #    trade log keep their quantity; only their cost basis shifts.
            cursor.execute(
                "SELECT quantity, avg_price FROM positions "
                "WHERE profile_id = ? AND symbol = ?",
                (self.current_profile_id, symbol))
            existing = cursor.fetchone()
            if existing is None:
                if pos_qty > 1e-9:
                    cursor.execute(
                        "INSERT INTO positions (profile_id, symbol, quantity, avg_price) "
                        "VALUES (?, ?, ?, ?)",
                        (self.current_profile_id, symbol, pos_qty, pos_cost / pos_qty))
            elif abs(existing[0] - pos_qty) < 1e-6:
                if pos_qty > 1e-9:
                    cursor.execute(
                        "UPDATE positions SET quantity = ?, avg_price = ? "
                        "WHERE profile_id = ? AND symbol = ?",
                        (pos_qty, pos_cost / pos_qty, self.current_profile_id, symbol))
                else:
                    cursor.execute(
                        "DELETE FROM positions WHERE profile_id = ? AND symbol = ?",
                        (self.current_profile_id, symbol))
            elif action == "BUY" and existing[0] > 0:
                # Legacy partial ledger: shift the cost basis by the correction.
                new_avg = (existing[0] * existing[1] + qty * (new_price - old_price)) / existing[0]
                cursor.execute(
                    "UPDATE positions SET avg_price = ? WHERE profile_id = ? AND symbol = ?",
                    (new_avg, self.current_profile_id, symbol))

            conn.commit()
            return True
        except sqlite3.Error as e:
            conn.rollback()
            self._popup_message("Error", f"Could not update trade: {e}", kind="error")
            return False
        finally:
            conn.close()

    def clear_ui(self):
        self.lbl_cash.config(text="Cash Balance: -")
        self.lbl_equity.config(text="Portfolio Value: -")
        self.lbl_total.config(text="Net Worth: -")
        self.lbl_updated.config(text="Prices: -")
        self.lbl_realized.config(text="Realized P/L: -", foreground=C_MUTED)
        self.lbl_unrealized.config(text="Unrealized P/L: -", foreground=C_MUTED)
        self.lbl_overall.config(text="Overall P/L: -", foreground=C_MUTED)
        self.lbl_avg_pos.config(text="Avg position P/L: -", foreground=C_MUTED)
        for tree in (self.holdings_tree, self.recs_tree, self.history_tree):
            for item in tree.get_children():
                tree.delete(item)

    # ==========================================
    # MODEL SELECTION
    # ==========================================
    def load_models_async(self):
        self.lbl_model_status.config(text="Loading models from Ollama...",
                                     foreground=C_MUTED)

        def worker():
            models = list_ollama_models()
            self._post_ui(lambda: self._apply_models(models))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_models(self, models):
        # Cloud models never show up in Ollama's /api/tags listing, so they
        # are appended manually and are always selectable.
        values = list(models)
        for cloud_model in CLOUD_MODELS:
            if cloud_model not in values:
                values.append(cloud_model)
        self.model_cb["values"] = sorted(values)

        current = self.model_cb.get().strip()
        if not current:
            saved = get_setting("ollama_model", "")
            if saved:
                self.model_cb.set(saved)
            elif DEFAULT_MODEL in values:
                self.model_cb.set(DEFAULT_MODEL)
            elif models:
                self.model_cb.set(models[0])
            else:
                self.model_cb.set(DEFAULT_MODEL)

        if models:
            self.lbl_model_status.config(
                text=f"{len(values)} models available (incl. cloud) - "
                     "pick one or type an exact name",
                foreground=C_GREEN_SOFT)
        else:
            self.lbl_model_status.config(
                text="Ollama offline - start it (ollama serve), then Refresh",
                foreground=C_RED_SOFT)

    # ==========================================
    # ANALYSIS & TRADING ENGINE
    # ==========================================
    def start_analysis_thread(self):
        if not self.current_profile_id:
            self._popup_message("Warning", "Select or create a profile first.", kind="warning")
            return
        if self._tm_running:
            self._popup_message("Warning",
                                "Stop Trading Mode before running an AI analysis.",
                                kind="warning")
            return

        model = self.model_cb.get().strip()
        if not model:
            self._popup_message("Warning", "Enter or select an Ollama model name first.",
                                kind="warning")
            return
        set_setting("ollama_model", model)

        use_web = self.web_var.get()
        lens = _lens_kind(self.fund_var.get(), self.tech_var.get())
        lens_desc = {
            "fundamental": "fundamental lens",
            "technical": "technical lens",
            "combined": "fundamental + technical lens",
            "general": "general lens",
        }[lens]

        self._analysis_running = True
        self.btn_analyze.config(state="disabled")
        self.btn_trading_mode.config(state="disabled")
        self._set_status("Fetching live prices"
                         + (" + web search context" if use_web else "")
                         + f", querying {model} ({lens_desc})... "
                           "(UI stays live)")

        threading.Thread(target=self.run_analysis, args=(model, use_web, lens),
                         daemon=True).start()

    def run_analysis(self, model, use_web, lens):
        try:
            pid = self.current_profile_id
            conn = self.get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT name, currency, balance FROM profiles WHERE id = ?", (pid,))
            p_row = cursor.fetchone()
            cursor.execute(
                "SELECT symbol, quantity, avg_price FROM positions WHERE profile_id = ?",
                (pid,))
            pos_rows = cursor.fetchall()
            conn.close()

            if not p_row:
                self._post_ui(lambda: self._set_status("Profile no longer exists."))
                return

            p_name, currency, cash = p_row
            holdings = [{"symbol": r[0], "quantity": r[1], "avg_price": r[2]} for r in pos_rows]

            # Holdings first so price fetch + web search prioritize what the
            # user actually owns, then the watchlist candidates.
            ordered_symbols = []
            for h in holdings:
                if h["symbol"] not in ordered_symbols:
                    ordered_symbols.append(h["symbol"])
            for s in CANDIDATE_STOCKS:
                if s not in ordered_symbols:
                    ordered_symbols.append(s)

            prices = get_stock_prices(ordered_symbols)

            web_context = gather_web_context(ordered_symbols, lens=lens) if use_web else None
            used_web = bool(web_context)

            # The Technical lens (alone or combined) gets real computed
            # indicators injected into the prompt.
            technical_indicators = None
            if lens in ("technical", "combined"):
                technical_indicators = get_technical_indicators(ordered_symbols)

            raw_recs, error = query_ollama_for_analysis(
                p_name, currency, cash, holdings, prices, model, web_context,
                technical_indicators=technical_indicators, lens=lens)
            recs = parse_recommendations(raw_recs)

            self._post_ui(
                lambda: self.display_recommendations(recs, prices, error, model,
                                                     used_web, lens))
        except Exception as e:
            self._post_ui(lambda: self._set_status(f"Analysis failed: {e}"))
        finally:
            def done():
                self._analysis_running = False
                try:
                    self.btn_analyze.config(state="normal")
                    self.btn_trading_mode.config(state="normal")
                except tk.TclError:
                    pass
            self._post_ui(done)

    def _on_timeframe_changed(self, event=None):
        """Mirrors the combobox choice into a plain attribute, which the
        background Trading Mode loop can read safely."""
        self._tm_timeframe = self.tf_var.get()

    # ==========================================
    # CANDLESTICK TRADING MODE (continuous, whole-market scan, no AI)
    # ==========================================
    def _toggle_trading_mode(self):
        """Starts/stops the continuous TradingView engine.

        While running, the WHOLE S&P 500 universe is re-scanned automatically
        on the selected timeframe, and the engine trades clear patterns -
        with every entry sized by the risk engine, every position protected by
        a stop, and the portfolio circuit breakers in charge of when new risk
        may be taken on."""
        if self._tm_running:
            self._tm_stop_event.set()
            self._set_status("🕯️ Trading Mode stopping "
                             "(finishes the current cycle)...")
            return
        if not self.current_profile_id:
            self._popup_message("Warning", "Select or create a profile first.",
                                kind="warning")
            return
        if self._analysis_running:
            self._popup_message("Warning",
                                "Wait for the AI analysis to finish first.",
                                kind="warning")
            return
        if not PANDAS_AVAILABLE or not TV_DATA_AVAILABLE:
            self._popup_message(
                "Trading Mode unavailable",
                "Trading Mode needs pandas, websocket-client and tv_data.py:\n"
                "pip install pandas websocket-client\n\n"
                f"Details: {TV_DATA_ERROR}", kind="error")
            return

        label = self.tf_var.get()
        self._tm_timeframe = label  # readable from the background loop
        cfg = TRADING_TIMEFRAMES.get(label, TRADING_TIMEFRAMES["1 day"])
        interval = cfg["interval"] or f"{AUTO_MIN_AGREEMENT}/{len(AUTO_TIMEFRAMES)} of {', '.join(AUTO_TIMEFRAMES)}"
        self._tm_stop_event.clear()
        self._tm_running = True
        self.btn_analyze.config(state="disabled")
        self.btn_trading_mode.config(text="⏹️ Stop Trading Mode")
        self._set_status(
            f"🕯️ Trading Mode STARTED - continuously scanning the S&P 500 "
            f"universe on '{label}' ({interval}), re-scan every "
            f"{cfg['rescan_min']} min. Risk per trade "
            f"{RISK_PER_TRADE_PCT*100:.2f}%, max {MAX_OPEN_POSITIONS} "
            f"positions, stops and trailing stops enforced every cycle.")
        threading.Thread(target=self._trading_mode_loop, daemon=True).start()

    def _trading_mode_loop(self):
        """Background loop: scan -> decide -> execute -> wait -> repeat,
        until the user stops it or the profile disappears."""
        try:
            cycle = 0
            while not self._tm_stop_event.is_set():
                # The timeframe can be switched live between cycles (read
                # from the plain mirror - never from the Tk variable).
                label = self._tm_timeframe
                cfg = TRADING_TIMEFRAMES.get(label, TRADING_TIMEFRAMES["1 day"])
                pid = self.current_profile_id
                if pid is None:
                    self._post_ui(lambda: self._set_status(
                        "🕯️ Trading Mode stopped: no profile selected."))
                    break
                try:
                    self._trading_mode_cycle(pid, label, cfg, cycle + 1)
                except Exception as e:
                    self._post_ui(lambda err=e: self._set_status(
                        f"🕯️ Trading Mode cycle error: {err} - "
                        "retrying next cycle."))
                cycle += 1
                # Pause between scans; wake immediately when stopped.
                if self._tm_stop_event.wait(cfg["rescan_min"] * 60):
                    break
        finally:
            self._tm_running = False

            def done():
                try:
                    self.btn_trading_mode.config(text="▶️ Trading Mode")
                    self.btn_analyze.config(state="normal")
                except tk.TclError:
                    pass
                self._set_status("🕯️ Trading Mode stopped.")
            self._post_ui(done)

    def _load_trading_mode_state(self, pid):
        """Reads everything one cycle needs about the profile and its
        positions, including the risk-engine columns."""
        conn = self.get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT balance FROM profiles WHERE id = ?", (pid,))
            row = cursor.fetchone()
            cursor.execute(
                "SELECT symbol, quantity, avg_price FROM positions "
                "WHERE profile_id = ?", (pid,))
            positions = cursor.fetchall()
            cursor.execute(
                "SELECT symbol, quantity, avg_price, stop_loss, take_profit, "
                "initial_stop, high_water, risk_per_share, breakeven_done "
                "FROM positions WHERE profile_id = ?", (pid,))
            stops_rows = cursor.fetchall()
        finally:
            conn.close()
        return row, positions, stops_rows

    def _trading_mode_cycle(self, pid, label, cfg, cycle):
        """One scan -> decide -> execute round of the continuous engine."""
        stamp = datetime.now().strftime("%H:%M:%S")
        auto_mode = cfg["interval"] is None
        interval = REGIME_INTERVAL if auto_mode else cfg["interval"]

        self._post_ui(lambda: self._set_status(
            f"🕯️ [{stamp}] cycle {cycle}: scanning the S&P 500 universe on "
            f"'{label}'..."))

        row, positions, stops_rows = self._load_trading_mode_state(pid)
        if not row:
            self._post_ui(lambda: self._set_status(
                "🕯️ Trading Mode stopped: profile no longer exists."))
            self._tm_stop_event.set()
            return
        balance = float(row[0])
        owned = {s.upper(): float(q) for s, q, _a in positions}

        # ---- 1. RISK MANAGEMENT FIRST -------------------------------------
        # Stops and targets are checked against LIVE quotes before anything
        # else, so a stopped-out position is always closed on this cycle even
        # if the rest of the scan fails.
        held = list(owned)
        live_prices = get_stock_prices(held) if held else {}
        atr_map = {}
        if held:
            held_snapshot = get_market_snapshot(held, ("1D",))
            for symbol in held:
                atr_map[symbol] = _snapshot_value(held_snapshot.get(symbol), "atr")
        decisions, stop_updates = stop_take_exit_decisions(
            stops_rows, live_prices, atrs=atr_map)

        def apply_stop_updates(updates):
            if not updates:
                return
            conn = self.get_db_connection()
            try:
                cursor = conn.cursor()
                for symbol, new_stop, high_water, breakeven_done in updates:
                    cursor.execute(
                        "UPDATE positions SET stop_loss = ?, high_water = ?, "
                        "breakeven_done = ? WHERE profile_id = ? AND symbol = ?",
                        (new_stop, high_water, 1 if breakeven_done else 0,
                         pid, symbol))
                conn.commit()
            except sqlite3.Error:
                pass
            finally:
                conn.close()

        decided_symbols = {d["symbol"] for d in decisions}

        # ---- 2. UNIVERSE SCAN --------------------------------------------
        universe = get_market_universe()
        universe = list(dict.fromkeys(universe + [s for s in owned
                                                  if s not in universe]))
        # One scanner request (<1s) gives the multi-timeframe context for every
        # symbol - TradingView's per-timeframe ratings, RSI, EMA-200, ATR and
        # liquidity. It goes first because the candle sweep needs it for the
        # regime filter and the multi-timeframe agreement gate.
        snapshot = get_market_snapshot(universe, AUTO_TIMEFRAMES)
        if not snapshot:
            self._post_ui(lambda: self._set_status(
                f"🕯️ [{stamp}] TradingView scanner unreachable - "
                "scanning candles without multi-timeframe context."))

        results, prices, timings = scan_market_candles(
            universe, interval, snapshot, True, BARS_PER_SCAN)
        if not results:
            self._post_ui(lambda: self._set_status(
                f"🕯️ [{stamp}] candle sweep returned nothing "
                "(TradingView unreachable?) - will retry next cycle."))
            return

        # Execution prices come from the live batched quote route, which is
        # fresher than the last completed candle's close.
        live_quote_map = get_stock_prices(list(dict.fromkeys(
            [r["symbol"] for r in results] + universe + held)))
        for symbol, price in live_quote_map.items():
            if price > 0:
                prices[symbol] = price

        # ---- 3. RISK STATE (circuit breakers) ----------------------------
        equity = balance + sum(
            q * (prices.get(s, 0.0) or a) for s, q, a in positions)
        conn = self.get_db_connection()
        try:
            risk = update_risk_state(conn, pid, equity)
        finally:
            conn.close()

        # ---- 4. EXITS: bearish pattern on a position you actually hold ---
        # Long-only: a bearish signal on a stock you do not own is information,
        # never an order. Exits are never blocked by any breaker.
        for r in results:
            if (r["signal"] == "SELL"
                    and owned.get(r["symbol"], 0.0) > 0
                    and r["symbol"] not in decided_symbols):
                qty = owned[r["symbol"]]
                price = prices.get(r["symbol"], r.get("price")) or r.get("price")
                if not price or price <= 0:
                    continue
                decided_symbols.add(r["symbol"])
                decisions.append({
                    "action": "SELL", "symbol": r["symbol"],
                    "quantity": qty, "amount": qty * price, "kind": "exit",
                    "reason": (f"🕯️ {'+'.join(r['patterns']) or 'bearish patterns'}"
                               f" | bearish score {r['bear_score']}, "
                               f"RSI {r['rsi']:.0f}, "
                               f"{'above' if r['above_ema'] else 'below'} EMA-50"
                               f" - exit the entire {qty:.4g}-share "
                               f"{r['symbol']} position."),
                })

        # ---- 5. ENTRIES: the clearest bullish patterns -------------------
        buys = [r for r in results
                if r["signal"] == "BUY" and r["symbol"] not in owned
                and r["symbol"] not in decided_symbols]
        buys.sort(key=lambda r: (-r["score"], -r["bull_score"],
                                 r["rsi"] if r["rsi"] is not None else 50))

        # "Auto" mode adds a real multi-timeframe PATTERN confirmation: the
        # pattern engine is run one timeframe up on the shortlist, and a
        # strong bearish reversal there vetoes the lower-timeframe long.
        if auto_mode and buys:
            confirm_interval = "1W" if interval == "1D" else "1D"
            verdicts = higher_timeframe_pattern_check(
                [r["symbol"] for r in buys[:TRADING_MODE_MAX_BUYS * 3]],
                confirm_interval)
            kept = []
            for r in buys:
                ok, why = verdicts.get(r["symbol"], (True, ""))
                if ok:
                    kept.append(r)
                else:
                    r["signal"] = None
                    r["vetoes"].append(f"higher-timeframe veto: {why}")
            buys = kept
        if risk["halted"]:
            buys = []
        for r in buys[:TRADING_MODE_MAX_BUYS]:
            price = prices.get(r["symbol"], r["price"]) or r["price"]
            if not price or price <= 0:
                continue
            fill = apply_slippage(price, "BUY")
            stop = r["stop_loss"]
            if not stop or stop >= fill:
                continue
            # Risk-based sizing: the number of shares follows from the stop
            # distance, so a full stop-out costs RISK_PER_TRADE_PCT of equity.
            equity_now = balance + sum(
                q * (prices.get(s, 0.0) or a) for s, q, a in positions)
            shares, binding = position_size(
                equity_now, fill, stop, balance,
                current_position_value=0.0)
            if shares <= 0:
                continue
            decided_symbols.add(r["symbol"])
            risk_money = shares * (fill - stop)
            decisions.append({
                "action": "BUY", "symbol": r["symbol"],
                "quantity": shares, "amount": shares * fill, "kind": "entry",
                "stop_loss": stop,
                "take_profit": r["take_profit"],
                "risk_per_share": fill - stop,
                "reason": (f"🕯️ {'+'.join(r['patterns'])}"
                           f" | bullish score {r['score']}, RSI {r['rsi']:.0f}, "
                           f"{'above' if r['above_ema'] else 'below'} EMA-50, "
                           f"{r['mtf_votes']}/{r['mtf_total']} timeframes agree"
                           f" - new entry sized to risk "
                           f"{RISK_PER_TRADE_PCT*100:.2f}% "
                           f"({risk_money:,.0f} = {shares} x "
                           f"{fill - stop:.2f}); SL {stop:.2f} / "
                           f"TP {r['take_profit']:.2f} [{binding}]"),
            })
            # Plan the next entry against the cash this one consumes.
            balance -= shares * fill

        # Execute on the main thread - and only if the user has not switched
        # to a different profile mid-scan.
        def show():
            if self.current_profile_id != pid:
                self._set_status("🕯️ Trading Mode cycle skipped: profile switched.")
                return
            apply_stop_updates(stop_updates)
            self.display_trading_mode_results(
                decisions, results, prices,
                cycle_info=f"[{stamp}] cycle {cycle} '{label}'", quiet=True,
                cooldown_hours=cfg["cooldown_h"],
                risk_state=risk, timings=timings, auto_mode=auto_mode)

        self._post_ui(show)

    def display_trading_mode_results(self, decisions, results, prices,
                                     cycle_info="", quiet=False,
                                     cooldown_hours=AUTO_TRADE_COOLDOWN_HOURS,
                                     risk_state=None, timings=None,
                                     auto_mode=False):
        """Shows the TradingView decisions, then executes them through the
        risk engine and the auto-trading guard rails."""
        for item in self.recs_tree.get_children():
            self.recs_tree.delete(item)

        engine = ("TA-Lib (61 patterns)" if TALIB_AVAILABLE
                  else "built-in pattern engine")

        for d in decisions:
            # Make the direction unmistakable: exits are always positions you
            # hold; entries are brand-new positions.
            action_txt = d["action"]
            if d.get("kind") == "exit":
                action_txt = "SELL (exit)"
            elif d.get("kind") == "entry":
                action_txt = "BUY (new)"
            self.recs_tree.insert("", "end", values=(
                action_txt, d["symbol"], f"{d['quantity']:.4g}",
                f"{d['amount']:.2f}", d["reason"]))

        scanned = len(results)
        fetch_note = ""
        if timings and timings.get("fetch_s"):
            fetch_note = f", candles in {timings['fetch_s']:.0f}s"

        if decisions:
            exits = sum(1 for d in decisions if d["action"] == "SELL")
            entries = sum(1 for d in decisions if d["action"] == "BUY")
            self._set_status(
                f"🕯️ Trading Mode {cycle_info} [{engine}]: {scanned} of the "
                f"S&P 500 scanned{fetch_note} - executing {len(decisions)} "
                f"decision(s): {entries} new entry(ies), {exits} exit(s) of "
                f"YOUR holdings.")
            self.execute_auto_trades(
                decisions, prices, label="🕯️ Trading Mode",
                title="Trading Mode", quiet=quiet,
                cooldown_hours=cooldown_hours, risk_state=risk_state)
        else:
            # Explain WHY it is flat rather than leaving the user guessing.
            bull_pattern = [r for r in results if r["bull_score"] > 0]
            vetoes = {}
            for r in results:
                for v in r.get("vetoes", []):
                    key = v.split("(")[0].split(":")[0].strip()[:38]
                    vetoes[key] = vetoes.get(key, 0) + 1
            top = sorted(vetoes.items(), key=lambda kv: -kv[1])[:3]
            top_txt = "; ".join(f"{n}x {v}" for v, n in top) if top else "none"
            halted_txt = ""
            if risk_state and risk_state.get("halted"):
                halted_txt = f" ⛔ NEW ENTRIES HALTED: {risk_state['halt_reason']}."
            self._set_status(
                f"🕯️ Trading Mode {cycle_info} [{engine}]: {scanned} of the "
                f"S&P 500 scanned{fetch_note} - no clear signals "
                f"({len(bull_pattern)} carried a bullish pattern, none passed "
                f"every gate). Top blockers: {top_txt}. Staying flat."
                + halted_txt)

    def display_recommendations(self, recs, prices, error, model, used_web,
                                lens="general"):
        for item in self.recs_tree.get_children():
            self.recs_tree.delete(item)

        if error:
            self._set_status(f"❌ {error}")
            self._popup_message("AI Analysis Error", error, kind="error")
            return

        if not recs:
            self._set_status("Analysis complete - no trade currently warranted "
                             "(the AI recommends doing nothing).")
            return

        for r in recs:
            price = prices.get(r["symbol"], 0.0)
            qty = r["quantity"]
            if qty <= 0 and r["amount"] > 0 and price > 0:
                qty = float(max(1, int(r["amount"] / price)))
            amt = r["amount"] if r["amount"] > 0 else qty * price
            self.recs_tree.insert("", "end", values=(
                r["action"], r["symbol"], f"{qty:.4g}", f"{amt:.2f}", r["reason"]))

        lens_desc = {
            "fundamental": "fundamental",
            "technical": "technical",
            "combined": "fundamental+technical",
            "general": "general",
        }.get(lens, "general")
        source = "web + live prices" if used_web else "live prices only"
        self._set_status(f"✅ {len(recs)} suggestion(s) from {model} "
                         f"({lens_desc} lens, {source}).")

        if self.auto_var.get() and self.current_profile_id:
            self.execute_auto_trades(recs, prices)

    def on_rec_selected(self, event):
        selected = self.recs_tree.selection()
        if not selected:
            return
        values = self.recs_tree.item(selected[0])["values"]
        if len(values) >= 3:
            self.ent_symbol.delete(0, tk.END)
            self.ent_symbol.insert(0, str(values[1]))
            self.ent_trade_val.delete(0, tk.END)
            self.ent_trade_val.insert(0, str(values[2]))

    def execute_manual_trade(self, action_type):
        if not self.current_profile_id:
            self._popup_message("Warning", "Select or create a profile first.", kind="warning")
            return

        selected = self.recs_tree.selection()
        sym = self.ent_symbol.get().strip().upper()
        val_str = self.ent_trade_val.get().strip()

        if not sym and selected:
            values = self.recs_tree.item(selected[0])["values"]
            if len(values) > 1:
                sym = str(values[1]).strip().upper()

        if not sym and not val_str:
            self._popup_message("Warning",
                                "Select a recommendation or enter a symbol and quantity.",
                                kind="warning")
            return

        if not sym:
            sym = self._popup_prompt("Stock Symbol", "Enter stock symbol (e.g., AAPL):")
            if not sym:
                return
            sym = sym.strip().upper()

        qty = _to_float(val_str, 0.0) if val_str else 0.0
        if qty <= 0:
            self._popup_message("Error", "Enter a valid positive quantity.", kind="error")
            return

        symbol, quantity = sym, qty
        self._set_status(f"Fetching live price for {symbol}...")

        def worker():
            prices = get_stock_prices([symbol])
            price = prices.get(symbol, 0.0)

            def apply():
                if price <= 0:
                    self._set_status(f"Could not fetch a live price for {symbol}.")
                    self._popup_message(
                        "Error",
                        f"Could not fetch a live market price for {symbol}.",
                        kind="error")
                    return
                ok = self.process_trade(action_type, symbol, quantity, price,
                                        reason="Manual trade")
                if ok:
                    self._set_status(f"{action_type} {quantity:.4g} {symbol} "
                                     f"@ {price:.2f} executed.")

            self._post_ui(apply)

        threading.Thread(target=worker, daemon=True).start()

    def execute_auto_trades(self, recs, prices, label="🤖 Auto-Trade Engine",
                            title="Auto Trading", quiet=False,
                            cash_reserve_pct=AUTO_CASH_RESERVE_PCT,
                            max_position_pct=AUTO_MAX_POSITION_PCT,
                            cooldown_hours=AUTO_TRADE_COOLDOWN_HOURS,
                            risk_state=None):
        """Executes the AI's (or Trading Mode's) BUY/SELL recommendations
        automatically - but only the orders that pass the risk engine and the
        guard rails in _plan_auto_trades(). The AI is told to trade only when
        necessary; these rails make sure of it regardless of what the model
        returns. 'label'/'title' brand the status line and result popup;
        'quiet' suppresses the popup (used by the continuous Trading Mode)."""
        orders, skipped = self._plan_auto_trades(
            recs, prices, cash_reserve_pct=cash_reserve_pct,
            max_position_pct=max_position_pct, cooldown_hours=cooldown_hours,
            risk_state=risk_state)

        executed = 0
        executed_buys = 0
        executed_sells = 0
        for order in orders:
            if self.process_trade(order["action"], order["symbol"], order["qty"],
                                  order["price"], is_auto=True,
                                  reason=order["reason"], refresh=False,
                                  stop_loss=order.get("stop_loss"),
                                  take_profit=order.get("take_profit"),
                                  risk_per_share=order.get("risk_per_share")):
                executed += 1
                if order["action"] == "BUY":
                    executed_buys += 1
                else:
                    executed_sells += 1

        if executed > 0:
            self.refresh_profile_data()
            status = (f"{label}: executed {executed} order(s) - "
                      f"{executed_buys} new position(s), "
                      f"{executed_sells} exit(s) of YOUR holdings")
            self._set_status(status + (f"; {len(skipped)} skipped by the risk "
                                       f"engine/guard rails." if skipped else "."))
            if not quiet:
                msg = f"{label} executed {executed} order(s) successfully."
                if skipped:
                    msg += "\n\nSkipped:\n- " + "\n- ".join(skipped[:12])
                self._popup_message(title, msg)
        else:
            note = (" Skipped: " + "; ".join(skipped[:5])) if skipped else ""
            self._set_status(f"{label}: no trades executed (nothing "
                             "necessary right now)." + note)

    def _plan_auto_trades(self, recs, prices,
                          cash_reserve_pct=AUTO_CASH_RESERVE_PCT,
                          max_position_pct=AUTO_MAX_POSITION_PCT,
                          cooldown_hours=AUTO_TRADE_COOLDOWN_HOURS,
                          risk_state=None):
        """Applies the risk engine and the guard rails to AI/Trading-Mode
        recommendations and returns (orders, skipped): 'orders' holds only the
        trades that are genuinely safe, 'skipped' holds human-readable reasons
        for everything rejected. Pure planning - no writes, no UI.

        Guard rails, in the order they are applied:
        - circuit breakers: when the daily-loss or drawdown limit is hit, NO
          new entry is opened (exits still run);
        - position count: never more than MAX_OPEN_POSITIONS holdings;
        - cash floor: buys never spend below cash_reserve_pct of equity;
        - concentration: no single position above max_position_pct of equity;
        - risk sizing: a Trading Mode entry is re-sized so a stop-out costs
          RISK_PER_TRADE_PCT of equity (a tighter stop is re-capped by the
          position/cash limits rather than silently increasing the risk);
        - cooldown: BUYs on a symbol traded within cooldown_hours are rejected
          (exits always run - risk management must never wait);
        - duplicates: one order per symbol per batch.
        """
        # The real planning lives in engine.py, which the WEB build calls
        # too. Keeping ONE implementation is deliberate - two copies of the
        # risk rules would be two chances for them to disagree, and the order
        # the rails are applied in is itself part of the policy (exits before
        # entries, breakers before sizing).
        return self._engine().plan_auto_trades(
            recs, prices, cash_reserve_pct=cash_reserve_pct,
            max_position_pct=max_position_pct, cooldown_hours=cooldown_hours,
            risk_state=risk_state, profile_id=self.current_profile_id)
    def process_trade(self, action, symbol, qty, price, is_auto=False,
                      reason="", refresh=True, stop_loss=None, take_profit=None,
                      risk_per_share=None):
        """Executes a paper trade atomically and logs it to Trade History.
        stop_loss/take_profit (used by the continuous Trading Mode) are
        stored on the position so every later cycle can enforce them;
        risk_per_share records the original per-share risk so the cycle can
        move the stop to breakeven at +1R and then trail it.
        Returns True on success."""
        if not self.current_profile_id:
            return False
        if qty is None or price is None or qty <= 0 or price <= 0:
            if not is_auto:
                self._popup_message("Invalid Trade",
                                    "Quantity and price must be positive numbers.",
                                    kind="error")
            return False

        action = str(action).upper()
        if action not in ("BUY", "SELL"):
            return False
        symbol = str(symbol).strip().upper()
        total_cost = qty * price

        conn = self.get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT balance FROM profiles WHERE id = ?",
                           (self.current_profile_id,))
            row = cursor.fetchone()
            if not row:
                raise sqlite3.Error("Profile not found.")
            balance = row[0]
            realized_pnl = 0.0  # set for SELL below; BUYs realize no P/L

            if action == "BUY":
                if balance < total_cost:
                    if not is_auto:
                        self._popup_message(
                            "Insufficient Funds",
                            f"Cost {total_cost:,.2f} exceeds cash balance {balance:,.2f}.",
                            kind="error")
                    return False

                new_balance = balance - total_cost
                cursor.execute("UPDATE profiles SET balance = ? WHERE id = ?",
                               (new_balance, self.current_profile_id))

                cursor.execute(
                    "SELECT quantity, avg_price FROM positions "
                    "WHERE profile_id = ? AND symbol = ?",
                    (self.current_profile_id, symbol))
                existing = cursor.fetchone()

                if existing:
                    old_qty, old_avg = existing
                    new_qty = old_qty + qty
                    new_avg = ((old_qty * old_avg) + total_cost) / new_qty
                    cursor.execute(
                        "UPDATE positions SET quantity = ?, avg_price = ? "
                        "WHERE profile_id = ? AND symbol = ?",
                        (new_qty, new_avg, self.current_profile_id, symbol))
                else:
                    cursor.execute(
                        "INSERT INTO positions (profile_id, symbol, quantity, "
                        "avg_price, opened_at) VALUES (?, ?, ?, ?, ?)",
                        (self.current_profile_id, symbol, qty, price,
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

                # Trading Mode entries carry active risk levels; refresh them
                # whenever provided so every later cycle enforces them.
                # initial_stop / high_water / risk_per_share / breakeven_done
                # are the risk engine's memory for the breakeven and trailing
                # ratchets. MAX() keeps the invariant that a protective stop
                # can only ever move UP, even when averaging into a position
                # whose stop had already been ratcheted.
                if stop_loss is not None or take_profit is not None:
                    cursor.execute(
                        "UPDATE positions SET "
                        "stop_loss = MAX(COALESCE(stop_loss, ?), ?), "
                        "take_profit = COALESCE(?, take_profit), "
                        "initial_stop = COALESCE(initial_stop, ?), "
                        "high_water = MAX(COALESCE(high_water, ?), ?), "
                        "risk_per_share = COALESCE(risk_per_share, ?), "
                        "breakeven_done = COALESCE(breakeven_done, 0) "
                        "WHERE profile_id = ? AND symbol = ?",
                        (stop_loss, stop_loss, take_profit, stop_loss, price,
                         price, risk_per_share, self.current_profile_id,
                         symbol))

            else:  # SELL
                cursor.execute(
                    "SELECT quantity, avg_price FROM positions "
                    "WHERE profile_id = ? AND symbol = ?",
                    (self.current_profile_id, symbol))
                existing = cursor.fetchone()

                if not existing or existing[0] < qty:
                    if not is_auto:
                        owned = existing[0] if existing else 0.0
                        self._popup_message(
                            "Error",
                            f"You own {owned:.4g} shares of {symbol}; cannot sell {qty:.4g}.",
                            kind="error")
                    return False

                old_qty, old_avg = existing
                new_qty = old_qty - qty
                new_balance = balance + total_cost
                realized_pnl = (price - old_avg) * qty

                cursor.execute("UPDATE profiles SET balance = ? WHERE id = ?",
                               (new_balance, self.current_profile_id))

                if new_qty <= 1e-9:
                    cursor.execute(
                        "DELETE FROM positions WHERE profile_id = ? AND symbol = ?",
                        (self.current_profile_id, symbol))
                else:
                    cursor.execute(
                        "UPDATE positions SET quantity = ? "
                        "WHERE profile_id = ? AND symbol = ?",
                        (new_qty, self.current_profile_id, symbol))

            cursor.execute(
                "INSERT INTO trades (profile_id, trade_time, action, symbol, "
                "quantity, price, total, reason, realized_pnl) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.current_profile_id,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 action, symbol, qty, price, total_cost, reason,
                 realized_pnl))
            conn.commit()

        except sqlite3.Error as e:
            conn.rollback()
            if not is_auto:
                self._popup_message("Trade Error", f"Database error: {e}", kind="error")
            return False
        finally:
            conn.close()

        if refresh:
            self.refresh_profile_data()
        return True

    def _on_close(self):
        try:
            self._tm_stop_event.set()  # halt the continuous Trading Mode
            self._save_model_selection()
        finally:
            self.destroy()


# ==========================================
# 8. APPLICATION ENTRY POINT
# ==========================================
if __name__ == "__main__":
    app = PaperTradingApp()
    app.mainloop()