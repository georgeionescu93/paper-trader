"""
TradingView market-data layer for the paper trader - the single source of
live prices, history and multi-timeframe technical context. Replaces
yfinance completely.

Two independent TradingView routes are used, each for what it is best at:

1. SCANNER API  (HTTPS, batched)
   https://scanner.tradingview.com/america/scan
   Evaluates many symbols in ONE request and returns live prices plus
   indicators for up to ten timeframes (RSI, EMA20/50/200, ATR, ADX, MACD,
   Stochastic, CCI, Williams %R, Bollinger, plus TradingView's own aggregate
   technical rating "Recommend.All"). A full 503-symbol S&P 500 sweep with
   multi-timeframe indicators is a single request that answers in <1s - this
   is what makes a *continuous* whole-universe scan affordable.

2. CHART WEBSOCKET  (wss://data.tradingview.com/socket.io/websocket)
   Streams real OHLCV candle series, which the candlestick pattern engine
   needs. Many symbols are batched per connection and several connections run
   in parallel: a full 503-symbol daily sweep takes ~11s, 5-minute ~13s.

Batching is deliberate: it keeps the load on TradingView low (13 requests for
a whole-universe sweep instead of 503) and keeps the scan fast.

Only `websocket-client` and `pandas` are imported beyond the standard library.
`websocket-client` is used for the chart stream; the scanner route needs
nothing but urllib, so live quotes keep working even if the socket route is
unavailable (in that case candle patterns are disabled and the caller is told).

Timeframe codes used across this module are the keys of INTERVALS:
"1m", "5m", "15m", "30m", "1h", "2h", "4h", "1D", "1W", "1M".
"""

from __future__ import annotations

import json
import os
import random
import re
import string
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:  # pragma: no cover - pandas is a hard requirement in practice
    pd = None
    PANDAS_AVAILABLE = False

try:
    import websocket  # provided by the "websocket-client" package
    WEBSOCKET_AVAILABLE = True
except ImportError:  # pragma: no cover
    websocket = None
    WEBSOCKET_AVAILABLE = False


# ==========================================
# ENDPOINTS & CONSTANTS
# ==========================================
SCAN_URL = "https://scanner.tradingview.com/america/scan"
WS_URL = ("wss://data.tradingview.com/socket.io/websocket"
          "?from=chart%2F&type=chart")
SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_SP500_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "tv_sp500_cache.json")
_SYMBOL_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "tv_symbol_cache.json")

_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

ET = ZoneInfo("America/New_York")

# Full feature set needs pandas (DataFrames for the pattern engine) and
# websocket-client (candles). Live quotes only need urllib.
TV_AVAILABLE = PANDAS_AVAILABLE and WEBSOCKET_AVAILABLE

# interval code -> (TradingView websocket interval, scanner column suffix,
#                   bar length in seconds)
INTERVALS = {
    "1m":  ("1",   "|1",   60),
    "5m":  ("5",   "|5",   300),
    "15m": ("15",  "|15",  900),
    "30m": ("30",  "|30",  1800),
    "1h":  ("60",  "|60",  3600),
    "2h":  ("120", "|120", 7200),
    "4h":  ("240", "|240", 14400),
    "1D":  ("1D",  "",     86400),
    "1W":  ("1W",  "|1W",  604800),
    "1M":  ("1M",  "|1M",  2592000),
}


def scan_suffix(interval):
    """Scanner-API column suffix for an interval code.

    NOTE: TradingView's screener uses NO suffix for daily bars and does not
    accept "|1D" at all (it silently returns nulls) - the daily columns are
    plain "RSI", "EMA50", ...  This asymmetry is the reason this function
    exists instead of callers building column names by hand.
    """
    if interval not in INTERVALS:
        raise KeyError(f"unknown interval {interval!r}")
    return INTERVALS[interval][1]


def ws_interval(interval):
    """Chart-websocket interval string for an interval code."""
    if interval not in INTERVALS:
        raise KeyError(f"unknown interval {interval!r}")
    return INTERVALS[interval][0]


def interval_seconds(interval):
    return INTERVALS[interval][2]


def unavailable_reason():
    """Human-readable reason the full data layer is unavailable, or ''."""
    if not PANDAS_AVAILABLE:
        return "pandas is not installed - run: pip install pandas"
    if not WEBSOCKET_AVAILABLE:
        return ("websocket-client is not installed - run: "
                "pip install websocket-client")
    return ""


# ==========================================
# SMALL HTTP HELPER
# ==========================================
def _post_json(url, payload, timeout=30, attempts=3):
    """POSTs JSON and returns the decoded body. Retries transient failures
    with a short backoff - a whole-market scan is worthless if one flaky
    connection aborts it."""
    body = json.dumps(payload).encode("utf-8")
    last_error = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"User-Agent": _USER_AGENT,
                         "Content-Type": "application/json",
                         "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"TradingView request failed: {last_error}")


def _get_text(url, timeout=30, attempts=2):
    last_error = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read().decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(0.5)
    raise RuntimeError(f"request to {url} failed: {last_error}")


# ==========================================
# 1. SYMBOL RESOLUTION  (plain ticker -> "EXCHANGE:TICKER")
# ==========================================
_symbol_lock = threading.Lock()
_symbol_cache = {}          # {"AAPL": "NASDAQ:AAPL"}
_symbol_cache_loaded = False


def _load_symbol_cache():
    global _symbol_cache_loaded
    if _symbol_cache_loaded:
        return
    _symbol_cache_loaded = True
    try:
        with open(_SYMBOL_CACHE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _symbol_cache.update({str(k).upper(): str(v) for k, v in data.items()})
    except Exception:
        pass


def _save_symbol_cache():
    try:
        with _symbol_lock:
            snapshot = dict(_symbol_cache)
        with open(_SYMBOL_CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh)
    except Exception:
        pass


def _chunks(seq, size):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def resolve_symbols(names, use_cache=True):
    """Maps plain tickers to TradingView symbols ('AAPL' -> 'NASDAQ:AAPL').

    The scanner resolves tickers for us, so no separate symbol-search call is
    needed. Results are cached in memory and on disk; unresolved tickers are
    simply absent from the returned dict.
    """
    _load_symbol_cache()
    wanted = []
    for raw in names:
        name = str(raw or "").strip().upper()
        if name and name not in wanted:
            wanted.append(name)

    out = {}
    if use_cache:
        with _symbol_lock:
            for name in wanted:
                if name in _symbol_cache:
                    out[name] = _symbol_cache[name]

    missing = [n for n in wanted if n not in out]
    if not missing:
        return out

    # Already-qualified symbols ("NASDAQ:AAPL") pass straight through.
    qualified = {}
    for name in list(missing):
        if ":" in name:
            qualified[name.split(":")[-1]] = name
            missing.remove(name)
    newly = dict(qualified)

    for batch in _chunks(missing, 400):
        try:
            data = _post_json(SCAN_URL, {
                "filter": [{"left": "name", "operation": "in_range",
                            "right": batch}],
                "options": {"lang": "en"},
                "columns": ["name"],
                "range": [0, len(batch) + 50],
            })
        except RuntimeError:
            continue
        for row in data.get("data", []) or []:
            full = row.get("s")
            if not full:
                continue
            ticker = str(full).split(":")[-1].upper()
            if ticker in batch:
                newly[ticker] = str(full)

    if newly:
        with _symbol_lock:
            _symbol_cache.update(newly)
        out.update(newly)
        _save_symbol_cache()
    return out


# ==========================================
# 2. SCANNER API  (live quotes + multi-timeframe context)
# ==========================================
_quote_lock = threading.Lock()
_quote_cache = {}            # {"AAPL": (timestamp, price)}
QUOTE_TTL = 15.0             # seconds


def quotes(names, ttl=QUOTE_TTL):
    """Live last price per symbol, batched into a single scanner request.

    Returns {ticker: price}; symbols that could not be priced are absent.
    Cached briefly so parallel UI refreshes do not re-hit the network.
    """
    wanted = []
    for raw in names:
        name = str(raw or "").strip().upper()
        if name and name not in wanted:
            wanted.append(name)
    if not wanted:
        return {}

    now = time.time()
    prices, stale = {}, []
    with _quote_lock:
        for name in wanted:
            hit = _quote_cache.get(name)
            if hit and (now - hit[0]) < ttl and hit[1] > 0:
                prices[name] = hit[1]
            else:
                stale.append(name)
    if not stale:
        return prices

    fields = ["name", "close", "change", "volume", "average_volume_10d_calc",
              "update_mode"]
    fresh = {}
    for batch in _chunks(stale, 400):
        try:
            data = _post_json(SCAN_URL, {
                "filter": [{"left": "name", "operation": "in_range",
                            "right": batch}],
                "options": {"lang": "en"},
                "columns": fields,
                "range": [0, len(batch) + 50],
            })
        except RuntimeError:
            continue
        for row in data.get("data", []) or []:
            values = dict(zip(fields, row.get("d") or []))
            ticker = str(row.get("s", "")).split(":")[-1].upper()
            price = values.get("close")
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            if price > 0:
                fresh[ticker] = price

    if fresh:
        with _quote_lock:
            for name, price in fresh.items():
                _quote_cache[name] = (now, price)
    prices.update(fresh)
    return prices


# Canonical scanner fields the app consumes, with their types.
_BASE_FIELDS = [
    "name", "description", "close", "change", "volume",
    "average_volume_10d_calc", "market_cap_basic", "sector",
    "pricescale", "update_mode", "open", "high", "low",
]
# Per-timeframe indicator fields. For the DAILY timeframe these are requested
# without a suffix (see scan_suffix) and mirrored onto the "_1D" keys, so
# callers can address every timeframe uniformly.
_TF_FIELDS = ["RSI", "EMA20", "EMA50", "ATR", "ADX", "Recommend.All",
              "MACD.macd", "MACD.signal", "Stoch.K"]
# Fields requested WITHOUT a timeframe suffix: the daily context used by the
# regime filter (EMA200, 52-week position, trend strength, volatility).
_DAILY_FIELDS = list(_TF_FIELDS) + [
    "EMA200", "SMA50", "SMA200", "Volatility.D", "Perf.W", "Perf.1M",
    "Perf.3M", "Recommend.MA", "Recommend.Other", "BB.upper", "BB.lower",
]


def snapshot(names, timeframes=("1D", "1W"), batch=400):
    """Multi-timeframe technical snapshot for many symbols in one request.

    Returns {ticker: {...}} where the dict holds flat keys:
        close, change, volume, avg_volume, market_cap, sector, update_mode,
        rsi, ema20, ema50, ema200, atr, adx, rec_all (daily, no suffix)
        and per-timeframe values as "<key>_<interval>", e.g. rsi_1W, rec_1W,
        ema50_4h, atr_60m-style keys for every requested timeframe.

    This is the cheap broad filter: it answers "where is the whole universe
    trending on each timeframe" without downloading a single candle.
    """
    wanted = []
    for raw in names:
        name = str(raw or "").strip().upper()
        if name and name not in wanted:
            wanted.append(name)
    if not wanted:
        return {}

    columns = list(_BASE_FIELDS) + [c for c in _DAILY_FIELDS if c not in _BASE_FIELDS]
    # Map scanner column -> canonical key we hand back.
    key_for = {
        "name": "name", "description": "description", "close": "close",
        "change": "change", "volume": "volume",
        "average_volume_10d_calc": "avg_volume",
        "market_cap_basic": "market_cap", "sector": "sector",
        "pricescale": "pricescale", "update_mode": "update_mode",
        "open": "open", "high": "high", "low": "low",
        "RSI": "rsi", "EMA20": "ema20", "EMA50": "ema50", "EMA200": "ema200",
        "ATR": "atr", "ADX": "adx", "Recommend.All": "rec_all",
        "Recommend.MA": "rec_ma", "Recommend.Other": "rec_other",
        "MACD.macd": "macd", "MACD.signal": "macd_signal", "Stoch.K": "stoch_k",
        "SMA50": "sma50", "SMA200": "sma200",
        "Volatility.D": "volatility_d", "Perf.W": "perf_w",
        "Perf.1M": "perf_1m", "Perf.3M": "perf_3m",
        "BB.upper": "bb_upper", "BB.lower": "bb_lower",
    }
    plain = {
        "RSI": "rsi", "EMA20": "ema20", "EMA50": "ema50", "ATR": "atr",
        "ADX": "adx", "Recommend.All": "rec_all", "MACD.macd": "macd",
        "MACD.signal": "macd_signal", "Stoch.K": "stoch_k",
    }

    # Non-daily timeframes get their own suffixed columns; daily already has
    # the unsuffixed ones and is mirrored onto "_1D" after parsing.
    extra_tfs = [tf for tf in timeframes if tf != "1D"]
    tf_cols = []
    for interval in extra_tfs:
        suffix = scan_suffix(interval)
        for field in _TF_FIELDS:
            col = field + suffix
            if col in columns:
                continue
            columns.append(col)
            tf_cols.append((col, f"{plain[field]}_{interval}"))
    mirror_daily = "1D" in timeframes

    out = {}
    for batch_names in _chunks(wanted, batch):
        try:
            data = _post_json(SCAN_URL, {
                "filter": [{"left": "name", "operation": "in_range",
                            "right": batch_names},
                           {"left": "type", "operation": "equal",
                            "right": "stock"}],
                "options": {"lang": "en"},
                "columns": columns,
                "range": [0, len(batch_names) + 50],
            }, timeout=45)
        except RuntimeError:
            continue
        for row in data.get("data", []) or []:
            values = dict(zip(columns, row.get("d") or []))
            ticker = str(row.get("s", "")).split(":")[-1].upper()
            entry = {"tv_symbol": row.get("s", "")}
            for col, key in key_for.items():
                entry[key] = values.get(col)
            for col, key in tf_cols:
                entry[key] = values.get(col)
            if mirror_daily:
                for field, key in plain.items():
                    entry[f"{key}_1D"] = entry.get(key)
            out[ticker] = entry
    return out


# ==========================================
# 3. CHART WEBSOCKET  (real OHLCV candles)
# ==========================================
def _tvp(func, args):
    payload = json.dumps({"m": func, "p": args}, separators=(",", ":"))
    return f"~m~{len(payload)}~m~{payload}"


def _rand_id(prefix, n=12):
    return prefix + "_" + "".join(
        random.choices(string.ascii_lowercase + string.digits, k=n))


def parse_frames(buffer):
    """Incrementally splits a TradingView socket payload into frames.

    Returns (frames, leftover) where a frame is ("h", heartbeat_text) or
    ("m", json_text). TradingView frames messages as `~m~<len>~m~<payload>`
    and interleaves `~h~<n>` heartbeats that must be echoed back. A socket
    read can split a frame in half, hence the leftover buffer.
    """
    frames, i, n = [], 0, len(buffer)
    while i < n:
        if buffer.startswith("~h~", i):
            end = buffer.find("~", i + 3)
            if end == -1:
                break
            frames.append(("h", buffer[i:end + 1]))
            i = end + 1
        elif buffer.startswith("~m~", i):
            sep = buffer.find("~m~", i + 3)
            if sep == -1:
                break
            try:
                length = int(buffer[i + 3:sep])
            except ValueError:
                break
            if sep + 3 + length > n:
                break
            frames.append(("m", buffer[sep + 3:sep + 3 + length]))
            i = sep + 3 + length
        else:
            candidates = [p for p in (buffer.find("~m~", i), buffer.find("~h~", i))
                          if p != -1]
            if not candidates:
                break
            i = min(candidates)
    return frames, buffer[i:]


class _ChartStream:
    """One TradingView chart websocket, reused for many symbols.

    Bars are merged by their index within the series, because TradingView
    sends a series in several `timescale_update` chunks and a partially
    received series would otherwise look complete at 1 bar. `series_completed`
    is the authoritative "this series is done" signal.
    """

    def __init__(self, timeout=25):
        self.ws = websocket.create_connection(
            WS_URL, timeout=timeout,
            header={"Origin": "https://data.tradingview.com"},
            suppress_origin=True)
        self.ws.send(_tvp("set_auth_token", ["unauthorized_user_token"]))
        self._buffer = ""
        self.symbols = {}        # chart session -> ticker
        self.bars = {}           # ticker -> {bar_index: [ts,o,h,l,c,v]}
        self.completed = set()
        self.errors = {}

    def add_series(self, tv_symbol, interval, bars):
        chart = _rand_id("cs")
        ticker = tv_symbol.split(":")[-1].upper()
        self.symbols[chart] = ticker
        self.bars.setdefault(ticker, {})
        self.ws.send(_tvp("chart_create_session", [chart, ""]))
        self.ws.send(_tvp("resolve_symbol", [
            chart, "symbol_1",
            '={"symbol":"%s","adjustment":"splits","session":"regular"}'
            % tv_symbol]))
        self.ws.send(_tvp("create_series", [
            chart, "s1", "s1", "symbol_1", ws_interval(interval), int(bars), ""]))
        return ticker

    def pump(self, deadline):
        """Reads until all series are complete or the deadline passes."""
        while time.time() < deadline:
            if self.symbols and len(self.completed) >= len(self.symbols):
                return
            try:
                raw = self.ws.recv()
            except Exception:
                return
            if not raw:
                return
            self._buffer += raw
            frames, self._buffer = parse_frames(self._buffer)
            for kind, payload in frames:
                if kind == "h":
                    try:
                        self.ws.send(payload)
                    except Exception:
                        return
                    continue
                try:
                    message = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                self._handle(message)

    def _handle(self, message):
        kind = message.get("m")
        params = message.get("p") or []
        if kind in ("timescale_update", "du"):
            if not params:
                return
            ticker = self.symbols.get(params[0])
            if not ticker or len(params) < 2:
                return
            series = (params[1] or {}).get("s1") or {}
            for bar in series.get("s") or []:
                try:
                    self.bars[ticker][int(bar["i"])] = bar["v"]
                except (KeyError, TypeError, ValueError):
                    continue
        elif kind == "series_completed":
            if params:
                ticker = self.symbols.get(params[0])
                if ticker:
                    self.completed.add(ticker)
        elif kind in ("series_error", "symbol_error", "critical_error",
                      "protocol_error"):
            ticker = self.symbols.get(params[0]) if params else None
            if ticker:
                self.errors[ticker] = str(message)[:200]
            elif params:
                self.errors[str(params[0])] = str(message)[:200]

    def rows(self):
        return {ticker: [value for _i, value in sorted(by_index.items())]
                for ticker, by_index in self.bars.items() if by_index}

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _fetch_chunk(tv_symbols, interval, bars, timeout_s=60):
    """Fetches one batch of symbols over a single websocket connection."""
    stream = None
    try:
        stream = _ChartStream(timeout=25)
        for symbol in tv_symbols:
            stream.add_series(symbol, interval, bars)
        stream.pump(time.time() + timeout_s)
        return stream.rows(), dict(stream.errors)
    except Exception as exc:  # noqa: BLE001
        return {}, {"_chunk": f"{type(exc).__name__}: {exc}"}
    finally:
        if stream is not None:
            stream.close()


def bars_to_frame(rows):
    """Converts raw [ts, open, high, low, close, volume] rows into the
    DataFrame shape the rest of the app expects (UTC DatetimeIndex)."""
    if not rows or not PANDAS_AVAILABLE:
        return None
    frame = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close",
                                        "Volume"])
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    if frame.empty:
        return None
    frame["ts"] = pd.to_datetime(frame["ts"], unit="s", utc=True)
    frame = frame.drop_duplicates(subset=["ts"]).set_index("ts").sort_index()
    frame["Volume"] = pd.to_numeric(frame["Volume"], errors="coerce").fillna(0.0)
    return frame


def history_batch(names, interval="1D", bars=210, chunk=40, workers=6,
                  timeout_s=60):
    """Candle history for many symbols, batched over parallel connections.

    Returns ({ticker: DataFrame}, {ticker_or_reason: error_message}).
    A full S&P 500 sweep is ~13 connections for 503 symbols.
    """
    if not TV_AVAILABLE:
        return {}, {"_unavailable": unavailable_reason()}

    mapping = resolve_symbols(names)
    if not mapping:
        return {}, {"_resolve": "no symbols could be resolved by TradingView"}

    items = list(mapping.items())
    batches = list(_chunks(items, max(1, int(chunk))))
    frames, errors = {}, {}

    def worker(batch):
        tickers = [t for t, _ in batch]
        tv_symbols = [s for _, s in batch]
        rows, errs = _fetch_chunk(tv_symbols, interval, bars, timeout_s)
        return tickers, rows, errs

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        for tickers, rows, errs in pool.map(worker, batches):
            for ticker in tickers:
                data = rows.get(ticker)
                frame = bars_to_frame(data) if data else None
                if frame is not None and not frame.empty:
                    frames[ticker] = frame
            errors.update(errs)

    missing = [t for t in mapping if t not in frames]
    for ticker in missing:
        errors.setdefault(ticker, "no candle data returned")
    return frames, errors


def history(name, interval="1D", bars=210, timeout_s=60):
    """Candle history for a single symbol (DataFrame or None)."""
    frames, _errors = history_batch([name], interval=interval, bars=bars,
                                    chunk=1, workers=1, timeout_s=timeout_s)
    return frames.get(str(name).strip().upper())


# ==========================================
# 4. BAR COMPLETENESS  (never trade a forming candle)
# ==========================================
def _month_end(open_et):
    """Last calendar day of the month containing open_et."""
    first_next = (open_et.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first_next - timedelta(days=1)


def bar_close_time(bar_open_utc, interval):
    """UTC instant at which the candle that opened at bar_open_utc is final.

    TradingView anchors intraday bars on the period boundary, but daily bars
    on the 09:30 ET session open, weekly bars on Monday 09:30 ET and monthly
    bars on the 1st at 09:30 ET - so the *session close* (16:00 ET), not the
    period end, is when those bars stop changing. Getting this wrong is how a
    half-formed bar (with a fraction of its volume) leaks a signal, so it is
    computed explicitly per interval.
    """
    if interval == "1D":
        open_et = bar_open_utc.astimezone(ET)
        close_et = open_et.replace(hour=16, minute=0, second=0, microsecond=0)
        return close_et.astimezone(timezone.utc)
    if interval == "1W":
        open_et = bar_open_utc.astimezone(ET)
        friday = (open_et + timedelta(days=4)).replace(
            hour=16, minute=0, second=0, microsecond=0)
        return friday.astimezone(timezone.utc)
    if interval == "1M":
        open_et = bar_open_utc.astimezone(ET)
        last = _month_end(open_et).replace(hour=16, minute=0, second=0,
                                           microsecond=0)
        return last.astimezone(timezone.utc)
    return bar_open_utc + timedelta(seconds=interval_seconds(interval))


def bar_is_complete(bar_open_utc, interval, now_utc=None):
    now = now_utc or datetime.now(timezone.utc)
    if bar_open_utc.tzinfo is None:
        bar_open_utc = bar_open_utc.replace(tzinfo=timezone.utc)
    return now >= bar_close_time(bar_open_utc, interval)


def drop_forming_bar(frame, interval, now_utc=None):
    """Returns (frame_of_completed_bars, dropped_flag).

    Only the final bar of a series can still be forming, so at most one row
    is removed. Decisions must come from completed candles: an in-progress
    bar has partial volume, which would veto nearly every volume-filtered
    signal during market hours.
    """
    if frame is None or frame.empty:
        return frame, False
    last_ts = frame.index[-1]
    if last_ts.tzinfo is None:
        last_ts = last_ts.tz_localize("UTC")
    if bar_is_complete(last_ts.to_pydatetime(), interval, now_utc):
        return frame, False
    return frame.iloc[:-1], True


def last_completed_bar_age(frame, interval, now_utc=None):
    """Age in seconds of the newest completed bar's open, or None."""
    if frame is None or frame.empty:
        return None
    now = now_utc or datetime.now(timezone.utc)
    for position in range(len(frame) - 1, max(len(frame) - 3, -1), -1):
        ts = frame.index[position]
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        if bar_is_complete(ts.to_pydatetime(), interval, now):
            return (now - ts.to_pydatetime()).total_seconds()
    return None


def market_is_open(now_utc=None):
    """US regular session (Mon-Fri 09:30-16:00 ET). Holidays are not modelled
    here on purpose - freshness checks below catch a closed market instead,
    which also covers unscheduled halts."""
    now = (now_utc or datetime.now(timezone.utc)).astimezone(ET)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= minutes < (16 * 60)


# ==========================================
# 5. S&P 500 UNIVERSE
# ==========================================
# Bundled fallback so the app works offline / when Wikipedia is unreachable.
# Refreshed automatically from Wikipedia (weekly, cached on disk).
SP500_FALLBACK = """
MMM AOS ABT ABBV ACN ADBE AMD AES AFL A APD ABNB AKAM ALB ARE ALGN ALLE
LNT ALL GOOGL GOOG MO AMZN AMCR AEE AEP AXP AIG AMT AWK AMP AME AMGN
APH ADI AON APA APO AAPL AMAT APP APTV ACGL ADM ARES ANET AJG AIZ T ATO
ADSK ADP AZO AVY AXON BKR BALL BAC BAX BDX BRK.B BBY TECH BIIB BLK BX
XYZ BE BNY BA BKNG BSX BMY AVGO BR BRO BF.B BG BXP CHRW CDNS CPT COF
CAH CCL CARR CVNA CASY CAT CBOE CBRE CDW COR CNC CNP CF CRL SCHW CHTR
CVX CMG CB CHD CIEN CI CINF CTAS CSCO C CFG CLX CME CMS KO CTSH COHR
COIN CL CMCSA FIX COP ED STZ CEG COO CPRT GLW CPAY CTVA CSGP COST CRH
CRWD CCI CSX CMI CVS DHR DRI DDOG DVA DECK DE DELL DAL DVN DXCM FANG
DLR DG DLTR D DPZ DASH DOV DOW DHI DTE DUK DD ETN EBAY ECHO ECL EIX EW
ELV EME EMR ETR EOG EQT EFX EQIX ERIE ESS EL EG EVRG P ES EXC EXE EXPE
EXPD EXR XOM FFIV FDS FICO FAST FRT FDX FDXF FERG FIS FITB FSLR FE FISV
FLEX F FTNT FTV FOXA FOX BEN FCX GRMN IT GE GEHC GEV GEN GNRC GD GIS GM
GPC GILD GPN GL GDDY GS HAL HIG HAS HCA DOC HSIC HSY HPE HLT HD HONA
HON HRL HST HWM HPQ HUBB HUM HBAN HII IBM IEX IDXX ITW ILMN INCY IR
PODD INTC IBKR ICE IFF IP INTU ISRG IVZ INVH IQV IRM JBHT JBL JKHY J
JNJ JCI JPM KVUE KDP KEY KEYS KMB KIM KMI KKR KLAC KHC KR LHX LH LRCX
LVS LDOS LEN LII LLY LIN LYV LMT L LOW LULU LITE LYB MTB MPC MAR MRSH
MLM MRVL MAS MA MKC MCD MCK MDT MRK META MET MTD MGM MCHP MU MSFT MAA
MRNA MDLZ MPWR MNST MCO MS MOS MSI MSCI NDAQ NTAP NFLX NEM NWSA NWS NEE
NKE NI NDSN NSC NTRS NOC NCLH NRG NUE NVDA NVR NXPI ORLY OXY ODFL OMC
ON OKE ORCL OTIS PCAR PKG PLTR PANW PSKY PH PAYX PYPL PNR PEP PFE PCG
PM PSX PNW PNC PPG PPL PFG PG PGR PLD PRU PEG PTC PSA PHM PWR QCOM DGX
Q RL RJF RDDT RTX O REG REGN RF RSG RMD RVTY HOOD ROK ROL ROP ROST RCL
SPGI CRM SNDK SBAC SLB STX SRE NOW SHW SPG SWKS SJM SW SNA SOLV SO LUV
SWK SBUX STT STLD STE SYK SMCI SYF SNPS SYY TMUS TROW TTWO TPR TRGP TGT
TEL TDY TER TSLA TXN TPL TXT TMO TJX TKO TSCO TT TDG TRV TRMB TFC TYL
TSN USB UBER UDR ULTA UNP UAL UPS URI UNH UHS VLO VEEV VTR VLTO VRSN
VRSK VZ VRTX VRT VTRS VICI V VST VMRK VMC WRB GWW WAB WMT DIS WBD WM
WAT WEC WFC WELL WST WDC WY WSM WMB WTW WDAY WYNN XEL XYL YUM ZBRA ZBH
ZTS
"""

_universe_lock = threading.Lock()
_sp500_cache = {"symbols": None, "fetched_at": 0.0}
SP500_CACHE_SECONDS = 7 * 24 * 3600


def _fallback_universe():
    return [s for s in SP500_FALLBACK.split() if s]


def _parse_sp500_wikipedia(html):
    """Extracts tickers from the 'constituents' table.

    Rows carry attributes (`<tr id="...">`), so rows are split on `<tr`, not
    on the bare tag - the naive split silently yields an empty list.
    """
    start = html.find('id="constituents"')
    if start == -1:
        return []
    table = html[start:html.find("</table>", start)]
    symbols = []
    for row in re.split(r"<tr", table)[1:]:
        cell = re.search(r"<td[^>]*>(.*?)</td>", row, re.S)
        if not cell:
            continue
        text = re.sub(r"<[^>]+>", " ", cell.group(1))
        text = text.replace("&amp;", "&").replace("&#160;", " ").strip()
        if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,6}", text) and text not in symbols:
            symbols.append(text)
    return symbols


def get_sp500_symbols(force_refresh=False):
    """The S&P 500 universe: disk cache -> Wikipedia -> bundled list."""
    with _universe_lock:
        now = time.time()
        if (not force_refresh and _sp500_cache["symbols"]
                and now - _sp500_cache["fetched_at"] < SP500_CACHE_SECONDS):
            return list(_sp500_cache["symbols"])

        if not force_refresh:
            try:
                with open(_SP500_CACHE_FILE, "r", encoding="utf-8") as fh:
                    cached = json.load(fh)
                symbols = cached.get("symbols") or []
                if symbols and now - float(cached.get("fetched_at", 0)) < SP500_CACHE_SECONDS:
                    _sp500_cache.update(symbols=symbols, fetched_at=float(
                        cached.get("fetched_at", now)))
                    return list(symbols)
            except Exception:
                pass

        symbols = []
        try:
            html = _get_text(SP500_WIKI_URL, timeout=30)
            symbols = _parse_sp500_wikipedia(html)
        except Exception:
            symbols = []

        if len(symbols) < 400:
            symbols = _fallback_universe()

        _sp500_cache.update(symbols=symbols, fetched_at=now)
        try:
            with open(_SP500_CACHE_FILE, "w", encoding="utf-8") as fh:
                json.dump({"symbols": symbols, "fetched_at": now}, fh)
        except Exception:
            pass
        return list(symbols)


# ==========================================
# 6. SELF-TEST
# ==========================================
if __name__ == "__main__":
    print(f"pandas={PANDAS_AVAILABLE} websocket-client={WEBSOCKET_AVAILABLE} "
          f"TV_AVAILABLE={TV_AVAILABLE}")
    if not TV_AVAILABLE:
        print("unavailable:", unavailable_reason())

    syms = get_sp500_symbols()
    print(f"\nS&P 500 universe: {len(syms)} symbols (e.g. {syms[:6]})")

    print("\nresolving symbols...")
    mapping = resolve_symbols(["AAPL", "MSFT", "BRK.B", "NVDA"])
    print(" ", mapping)

    print("\nquotes (batched)...")
    t0 = time.time()
    prices = quotes(["AAPL", "MSFT", "NVDA", "JPM"])
    print(f"  {prices}  in {time.time()-t0:.2f}s")

    print("\nsnapshot (daily + weekly)...")
    t0 = time.time()
    snap = snapshot(["AAPL", "MSFT"], ("1D", "1W"))
    aapl = snap.get("AAPL", {})
    print(f"  in {time.time()-t0:.2f}s: close={aapl.get('close')} "
          f"rsi={aapl.get('rsi')} rsi_1W={aapl.get('rsi_1W')} "
          f"rec_all={aapl.get('rec_all')} ema200={aapl.get('ema200')}")

    print("\nhistory (single symbol, daily)...")
    t0 = time.time()
    frame = history("AAPL", "1D", 210)
    print(f"  {None if frame is None else len(frame)} bars in {time.time()-t0:.2f}s")
    if frame is not None:
        completed, dropped = drop_forming_bar(frame, "1D")
        print(f"  completed bars: {len(completed)} (dropped forming: {dropped})")
        print(f"  last row:\n{completed.tail(1)}")
