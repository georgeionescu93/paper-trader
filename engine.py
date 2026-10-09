"""
Headless trading engine for the paper-trading app.

WHY THIS MODULE EXISTS
======================
The desktop build (paper_trading_app_TV.py) mixes the trading logic into a Tk
class, so the engine could only ever run with a window open. The web build
needs the same engine running on a server, 24/7, with nobody watching.

Rather than write a second copy of the trading logic - which would mean two
places where "risk 0.5%" could drift apart, exactly the kind of divergence that
loses money - this module holds the ONE implementation of:

  * the scan -> decide -> execute cycle, including the stop/target checks and
    the circuit breakers,
  * the auto-trade guard rails (position cap, cash floor, cooldown, sizing),
  * trade execution and the cash/position accounting,
  * the JSON snapshot the web dashboard renders,
  * profile management, and the AI analysis run.

It imports every pure function (signal engine, risk engine, TradingView data
layer) from paper_trading_app_TV, which deliberately does no GUI work at import
time. Nothing in here imports tkinter, so it runs on a headless server.

paper_trading_app_TV.py delegates its own cycle/trade methods here, so the
desktop app and the web app cannot disagree.

THREADING
=========
A TradingEngine instance owns no threads. The web server runs the continuous
loop on one background thread and calls run_cycle() from it; the desktop app
does the equivalent from its own loop. All public methods are safe to call from
any thread: every database operation opens its own short-lived connection, and
the small amount of shared state is guarded by a lock.
"""

import os
import sqlite3
import threading
import time
from datetime import datetime

import paper_trading_app_TV as app

# Re-exported so callers do not have to reach into the GUI module.
TRADING_TIMEFRAMES = app.TRADING_TIMEFRAMES

CURRENCY_SYMBOLS = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3"}


def currency_symbol(currency):
    return CURRENCY_SYMBOLS.get(currency or "USD", "$")


# ===========================================================================
# 1. TRADING ENGINE
# ===========================================================================
class TradingEngine:
    """Runs the scan/decide/execute cycle headlessly and reports what it did.

    One instance serves one database. It is deliberately ignorant of HTTP,
    threads and Tk: callers drive it and read the results back.
    """

    def __init__(self, db_file=None):
        self._lock = threading.RLock()
        self.reset_runtime()
        if db_file:
            self.set_db_file(db_file)

    # -- setup ---------------------------------------------------------------
    def set_db_file(self, db_file):
        """Points the engine (and the whole app module) at a database."""
        app.DB_FILE = db_file
        app.init_db()

    # -- runtime state -------------------------------------------------------
    def reset_runtime(self):
        self.running = False
        self.profile_id = None
        self.label = None
        self.cycle_count = 0
        self.started_at = None
        self.last_cycle_at = None
        self.last_cycle_s = None
        self.last_status = "Idle."
        self.last_error = None
        self.scanned = 0
        self.fetch_s = None
        self.decisions = 0
        self.last_scan = None
        self._log = []
        self._log_seq = 0

    # -- logging -------------------------------------------------------------
    def log(self, text):
        """Records a line for the web activity panel and the desktop status."""
        with self._lock:
            self._log_seq += 1
            self._log.append({"n": self._log_seq,
                              "t": datetime.now().strftime("%H:%M:%S"),
                              "text": str(text)})
            if len(self._log) > 400:
                del self._log[:200]
        self.last_status = str(text)

    def log_since(self, since=0):
        with self._lock:
            return [line for line in self._log if line["n"] > int(since or 0)]

    def status(self):
        with self._lock:
            return {
                "running": self.running,
                "profile_id": self.profile_id,
                "label": self.label,
                "cycle": self.cycle_count,
                "started_at": self.started_at,
                "last_cycle_at": self.last_cycle_at,
                "last_cycle_s": self.last_cycle_s,
                "last_status": self.last_status,
                "last_error": self.last_error,
                "scanned": self.scanned,
                "fetch_s": self.fetch_s,
                "decisions": self.decisions,
                "data_available": app.TV_DATA_AVAILABLE,
                "data_error": app.TV_DATA_ERROR or "",
                "log_seq": self._log_seq,
            }

    # -- profile helpers -----------------------------------------------------
    def load_trading_mode_state(self, pid):
        """Everything one cycle needs about the profile and its positions."""
        conn = app.get_db_connection()
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

    def apply_stop_updates(self, pid, updates):
        """Persists ratcheted protective stops. A stop only ever moves up."""
        if not updates:
            return 0
        conn = app.get_db_connection()
        try:
            cursor = conn.cursor()
            for symbol, new_stop, high_water, breakeven_done in updates:
                cursor.execute(
                    "UPDATE positions SET stop_loss = ?, high_water = ?, "
                    "breakeven_done = ? WHERE profile_id = ? AND symbol = ?",
                    (new_stop, high_water, 1 if breakeven_done else 0,
                     pid, symbol))
            conn.commit()
            return len(updates)
        except sqlite3.Error:
            return 0
        finally:
            conn.close()

    # -- THE CYCLE -----------------------------------------------------------
    # A cycle is deliberately split into two halves:
    #   scan_universe() is the expensive, profile-independent half - the
    #     universe list, the multi-timeframe snapshot, the candle sweep and the
    #     live quotes used as execution prices.
    #   apply_cycle() is the per-portfolio half - stop/target management on the
    #     positions that portfolio actually holds, the circuit breakers, exits,
    #     entries and execution.
    # That split is what lets one sweep serve every armed portfolio in the same
    # cycle (run_cycle_many) instead of hammering TradingView once per user,
    # while run_cycle() keeps the single-portfolio behaviour unchanged.
    @staticmethod
    def resolved_interval(cfg):
        """'Auto (all timeframes)' resolves to the regime interval."""
        return (app.REGIME_INTERVAL if cfg["interval"] is None
                else cfg["interval"])

    def held_symbols(self, pid):
        """The symbols one portfolio holds, for the shared universe."""
        conn = app.get_db_connection()
        try:
            rows = conn.execute("SELECT symbol FROM positions WHERE "
                                "profile_id = ?", (pid,)).fetchall()
            return [str(r[0]).upper() for r in rows if r and r[0]]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def scan_universe(self, interval, extra_symbols=(), label=""):
        """The shared half of a cycle: market data, fetched exactly once.

        Never reads or writes a profile, so several portfolios can reuse the
        result. Returns {ok, universe, snapshot, results, prices, timings,
        fetch_s, reason}.
        """
        stamp = datetime.now().strftime("%H:%M:%S")
        scan = {"ok": False, "universe": [], "snapshot": {}, "results": [],
                "prices": {}, "timings": None, "fetch_s": None, "reason": ""}
        extra = [str(s).upper() for s in extra_symbols if s]
        universe = app.get_market_universe()
        universe = list(dict.fromkeys(list(universe) + extra))
        scan["universe"] = universe

        snapshot = app.get_market_snapshot(universe, app.AUTO_TIMEFRAMES)
        scan["snapshot"] = snapshot or {}
        if not snapshot:
            self.log(f"\U0001f56f\ufe0f [{stamp}] TradingView scanner "
                     "unreachable - scanning candles without "
                     "multi-timeframe context.")

        results, prices, timings = app.scan_market_candles(
            universe, interval, snapshot, True, app.BARS_PER_SCAN)
        scan["timings"] = timings
        scan["fetch_s"] = (timings or {}).get("fetch_s")
        if not results:
            scan["reason"] = "candle sweep returned nothing"
            self.log(f"\U0001f56f\ufe0f [{stamp}] candle sweep returned "
                     "nothing (TradingView unreachable?) - will retry "
                     "next cycle.")
            return scan

        # Execution prices come from the live quote route, which is fresher
        # than the last completed candle's close.
        live_quote_map = app.get_stock_prices(list(dict.fromkeys(
            [r["symbol"] for r in results] + universe + extra)))
        for symbol, price in live_quote_map.items():
            if price > 0:
                prices[symbol] = price

        scan.update({"ok": True, "results": results, "prices": prices})
        return scan

    def apply_cycle(self, pid, label, cfg, cycle, scan, tag=""):
        """One portfolio's half of a cycle, against an existing scan.

        The order is not arbitrary and must not be rearranged:
          1. risk management on existing positions, against LIVE quotes, so a
             stopped-out position is always closed even if the rest fails,
          2. the circuit breakers,
          3. exits before entries.
        """
        started = time.time()
        stamp = datetime.now().strftime("%H:%M:%S")
        auto_mode = cfg["interval"] is None
        interval = self.resolved_interval(cfg)
        who = f"[{tag}] " if tag else ""
        results = scan["results"]
        prices = scan["prices"]
        timings = scan["timings"]
        result = {"ok": False, "decisions": [], "results": results,
                  "prices": prices, "timings": timings, "risk": None,
                  "scanned": len(results), "reason": "", "status": "",
                  "executed": 0, "profile_id": pid, "label": label,
                  "tag": tag}

        row, positions, stops_rows = self.load_trading_mode_state(pid)
        if not row:
            self.log("\U0001f56f\ufe0f Trading Mode stopped: profile no "
                     "longer exists.")
            result["reason"] = "profile no longer exists"
            self.running = False
            return result
        balance = float(row[0])
        owned = {s.upper(): float(q) for s, q, _a in positions}

        try:
            # ---- 1. RISK MANAGEMENT FIRST ---------------------------------
            held = list(owned)
            live_prices = app.get_stock_prices(held) if held else {}
            atr_map = {}
            if held:
                held_snapshot = app.get_market_snapshot(held, ("1D",))
                for symbol in held:
                    atr_map[symbol] = app._snapshot_value(
                        held_snapshot.get(symbol), "atr")
            decisions, stop_updates = app.stop_take_exit_decisions(
                stops_rows, live_prices, atrs=atr_map)
            self.apply_stop_updates(pid, stop_updates)
            decided_symbols = {d["symbol"] for d in decisions}

            # ---- 2. UNIVERSE SCAN -----------------------------------------
            # Already done for this cycle: results/prices/timings arrived in
            # `scan`, shared by every armed portfolio. This portfolio costs one
            # ATR lookup from here on, no extra market sweep.

            # ---- 3. RISK STATE (circuit breakers) ------------------------
            equity = balance + sum(
                q * (prices.get(s, 0.0) or a) for s, q, a in positions)
            conn = app.get_db_connection()
            try:
                risk = app.update_risk_state(conn, pid, equity)
            finally:
                conn.close()

            # ---- 4. EXITS on positions you actually hold -----------------
            # Long-only: a bearish signal on a stock you do not own is
            # information, never an order. Exits are never blocked.
            for r in results:
                if (r["signal"] == "SELL"
                        and owned.get(r["symbol"], 0.0) > 0
                        and r["symbol"] not in decided_symbols):
                    qty = owned[r["symbol"]]
                    price = (prices.get(r["symbol"], r.get("price"))
                             or r.get("price"))
                    if not price or price <= 0:
                        continue
                    decided_symbols.add(r["symbol"])
                    decisions.append({
                        "action": "SELL", "symbol": r["symbol"],
                        "quantity": qty, "amount": qty * price, "kind": "exit",
                        "reason": (
                            f"\U0001f56f\ufe0f "
                            f"{'+'.join(r['patterns']) or 'bearish patterns'}"
                            f" | bearish score {r['bear_score']}, "
                            f"RSI {r['rsi']:.0f}, "
                            f"{'above' if r['above_ema'] else 'below'} EMA-50"
                            f" - exit the entire {qty:.4g}-share "
                            f"{r['symbol']} position."),
                    })

            # ---- 5. ENTRIES: the clearest bullish patterns ---------------
            buys = [r for r in results
                    if r["signal"] == "BUY" and r["symbol"] not in owned
                    and r["symbol"] not in decided_symbols]
            buys.sort(key=lambda r: (-r["score"], -r["bull_score"],
                                     r["rsi"] if r["rsi"] is not None else 50))

            # "Auto" adds a real multi-timeframe PATTERN confirmation: the
            # pattern engine runs one timeframe up on the shortlist, and a
            # strong bearish reversal there vetoes the lower-timeframe long.
            if auto_mode and buys:
                confirm = "1W" if interval == "1D" else "1D"
                verdicts = app.higher_timeframe_pattern_check(
                    [r["symbol"] for r in buys[:app.TRADING_MODE_MAX_BUYS * 3]],
                    confirm)
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

            for r in buys[:app.TRADING_MODE_MAX_BUYS]:
                price = prices.get(r["symbol"], r["price"]) or r["price"]
                if not price or price <= 0:
                    continue
                fill = app.apply_slippage(price, "BUY")
                stop = r["stop_loss"]
                if not stop or stop >= fill:
                    continue
                equity_now = balance + sum(
                    q * (prices.get(s, 0.0) or a) for s, q, a in positions)
                shares, binding = app.position_size(
                    equity_now, fill, stop, balance,
                    current_position_value=0.0)
                if shares <= 0:
                    continue
                decided_symbols.add(r["symbol"])
                risk_money = shares * (fill - stop)
                decisions.append({
                    "action": "BUY", "symbol": r["symbol"],
                    "quantity": shares, "amount": shares * fill,
                    "kind": "entry", "stop_loss": stop,
                    "take_profit": r["take_profit"],
                    "risk_per_share": fill - stop,
                    "reason": (
                        f"\U0001f56f\ufe0f {'+'.join(r['patterns'])}"
                        f" | bullish score {r['score']}, "
                        f"RSI {r['rsi']:.0f}, "
                        f"{'above' if r['above_ema'] else 'below'} EMA-50, "
                        f"{r['mtf_votes']}/{r['mtf_total']} timeframes agree"
                        f" - new entry sized to risk "
                        f"{app.RISK_PER_TRADE_PCT*100:.2f}% "
                        f"({risk_money:,.0f} = {shares} x {fill - stop:.2f}); "
                        f"SL {stop:.2f} / TP {r['take_profit']:.2f} "
                        f"[{binding}]"),
                })
                # Plan the next entry against the cash this one consumes.
                balance -= shares * fill

            # ---- 6. EXECUTE -----------------------------------------------
            summary = self.execute_auto_trades(
                decisions, prices, label="\U0001f56f\ufe0f Trading Mode",
                cooldown_hours=cfg["cooldown_h"], risk_state=risk,
                quiet=True, profile_id=pid)
            executed = summary["executed"]

            scanned = len(results)
            fetch_note = ""
            if timings and timings.get("fetch_s"):
                fetch_note = f", candles in {timings['fetch_s']:.0f}s"
            engine_name = ("TA-Lib (61 patterns)" if app.TALIB_AVAILABLE
                           else "built-in pattern engine")

            blockers = {}
            for r in results:
                for v in r.get("vetoes", []):
                    key = v.split("(")[0].split(":")[0].strip()[:38]
                    blockers[key] = blockers.get(key, 0) + 1
            top = sorted(blockers.items(), key=lambda kv: -kv[1])[:3]

            if decisions:
                exits = sum(1 for d in decisions if d["action"] == "SELL")
                entries = sum(1 for d in decisions if d["action"] == "BUY")
                status = (f"\U0001f56f\ufe0f Trading Mode {who}[{stamp}] cycle "
                          f"{cycle} '{label}' [{engine_name}]: {scanned} of the "
                          f"S&P 500 scanned{fetch_note} - executed "
                          f"{executed} trade(s) from {len(decisions)} "
                          f"decision(s): {entries} new entry(ies), {exits} "
                          f"exit(s) of YOUR holdings.")
                flat = ""
            else:
                bull = sum(1 for r in results if r["bull_score"] > 0)
                top_txt = ("; ".join(f"{n}x {v}" for v, n in top)
                           if top else "none")
                flat = (f"no clear signals ({bull} carried a bullish pattern, "
                        f"none passed every gate). Top blockers: {top_txt}. "
                        f"Staying flat.")
                status = (f"\U0001f56f\ufe0f Trading Mode {who}[{stamp}] cycle "
                          f"{cycle} '{label}' [{engine_name}]: {scanned} of "
                          f"the S&P 500 scanned{fetch_note} - {flat}")
                if risk.get("halted"):
                    status += (f" \u26d4 NEW ENTRIES HALTED: "
                               f"{risk['halt_reason']}.")

            self.log(status)
            with self._lock:
                self.scanned = scanned
                self.fetch_s = (timings or {}).get("fetch_s")
                self.decisions = len(decisions)
                self.last_scan = {
                    "scanned": scanned,
                    "fetch_s": (timings or {}).get("fetch_s"),
                    "bullish_patterns": sum(1 for r in results
                                            if r["bull_score"] > 0),
                    "flat_reason": flat,
                    "top_blockers": [[v, n] for v, n in top],
                }

            result.update({"ok": True, "decisions": decisions,
                           "results": results, "prices": prices,
                           "timings": timings, "risk": risk,
                           "scanned": scanned, "status": status,
                           "executed": executed})
            return result
        except Exception as exc:                      # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.log(f"\u274c cycle failed: {self.last_error}")
            result["reason"] = self.last_error
            return result
        finally:
            with self._lock:
                self.last_cycle_s = round(time.time() - started, 1)
                self.last_cycle_at = datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S")

    def run_cycle(self, pid, label, cfg, cycle):
        """One scan -> decide -> execute round for a single portfolio.

        This is the original single-portfolio entry point and its behaviour is
        unchanged; it simply scans for itself. The scheduler uses
        run_cycle_many() so several portfolios share one scan instead.
        """
        started = time.time()
        stamp = datetime.now().strftime("%H:%M:%S")
        result = {"ok": False, "decisions": [], "results": [], "prices": {},
                  "timings": None, "risk": None, "scanned": 0,
                  "reason": "", "status": ""}

        self.log(f"\U0001f56f\ufe0f [{stamp}] cycle {cycle}: scanning the "
                 f"S&P 500 universe on '{label}'...")
        try:
            # A missing profile must not cost a market sweep.
            row, _positions, _stops = self.load_trading_mode_state(pid)
            if not row:
                self.log("\U0001f56f\ufe0f Trading Mode stopped: profile no "
                         "longer exists.")
                result["reason"] = "profile no longer exists"
                self.running = False
                return result

            scan = self.scan_universe(self.resolved_interval(cfg),
                                      self.held_symbols(pid), label)
            if not scan["ok"]:
                result.update({"results": scan["results"],
                               "prices": scan["prices"],
                               "timings": scan["timings"],
                               "reason": scan["reason"]})
                return result
            return self.apply_cycle(pid, label, cfg, cycle, scan)
        except Exception as exc:                      # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.log(f"\u274c cycle failed: {self.last_error}")
            result["reason"] = self.last_error
            return result
        finally:
            with self._lock:
                self.last_cycle_s = round(time.time() - started, 1)
                self.last_cycle_at = datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S")

    def run_cycle_many(self, armed, cycle):
        """One shared sweep per timeframe, applied to every armed portfolio.

        `armed` is a list of dicts: {"profile_id", "label", "cfg"?, "tag"?}.
        The timeframe config is optional - when only a label is given it is
        looked up here, because the scheduler stores what the user chose, not
        the parsed config. An unknown label is reported and skipped instead of
        taking the whole sweep down with it.
        Portfolios are grouped by the interval their timeframe resolves to,
        because one candle sweep can only answer one timeframe; with the usual
        "Auto (all timeframes)" setting there is exactly one group. A failure
        for one portfolio never stops the others.
        """
        started = time.time()
        stamp = datetime.now().strftime("%H:%M:%S")
        out = {"ok": False, "scanned": 0, "fetch_s": None, "decisions": 0,
               "profiles": [], "reason": ""}
        groups = {}
        for entry in armed:
            cfg = entry.get("cfg") or app.TRADING_TIMEFRAMES.get(
                str(entry.get("label")))
            if not cfg:
                reason = f"unknown timeframe: {entry.get('label')!r}"
                self.log(f"\u26a0\ufe0f skipping portfolio "
                         f"{entry.get('profile_id')} ({reason})")
                out["profiles"].append({"ok": False, "decisions": [],
                                        "profile_id": entry.get("profile_id"),
                                        "reason": reason})
                continue
            groups.setdefault(self.resolved_interval(cfg),
                              []).append(dict(entry, cfg=cfg))

        total = 0
        for interval, entries in groups.items():
            labels = ", ".join(sorted({str(e["label"]) for e in entries}))
            symbols = []
            for entry in entries:
                symbols.extend(self.held_symbols(entry["profile_id"]))
            self.log(f"\U0001f56f\ufe0f [{stamp}] cycle {cycle}: scanning the "
                     f"S&P 500 universe on '{interval}' for "
                     f"{len(entries)} portfolio(s) ({labels})...")
            try:
                scan = self.scan_universe(interval, symbols, labels)
            except Exception as exc:                  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.log(f"\u274c scan failed: {self.last_error}")
                out["reason"] = self.last_error
                for entry in entries:
                    out["profiles"].append(
                        {"ok": False, "profile_id": entry["profile_id"],
                         "decisions": [], "reason": self.last_error})
                continue

            out["scanned"] = max(out["scanned"], len(scan["results"]))
            if scan["fetch_s"] is not None:
                out["fetch_s"] = scan["fetch_s"]
            if not scan["ok"]:
                out["reason"] = scan["reason"]
                for entry in entries:
                    out["profiles"].append(
                        {"ok": False, "profile_id": entry["profile_id"],
                         "decisions": [], "reason": scan["reason"]})
                continue

            for entry in entries:
                tag = entry.get("tag", "")
                try:
                    res = self.apply_cycle(entry["profile_id"], entry["label"],
                                           entry["cfg"], cycle, scan, tag)
                except Exception as exc:              # noqa: BLE001
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self.log(f"\u274c cycle failed for "
                             f"{tag or entry['label']}: {self.last_error}")
                    res = {"ok": False, "decisions": [],
                           "profile_id": entry["profile_id"],
                           "reason": self.last_error}
                total += len(res.get("decisions") or [])
                out["profiles"].append(res)
                out["ok"] = out["ok"] or bool(res.get("ok"))

        with self._lock:
            self.decisions = total
            self.cycle_count = cycle
            self.last_cycle_s = round(time.time() - started, 1)
            self.last_cycle_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        out["decisions"] = total
        return out

    def run_cycle_safe(self, pid, label, cfg, cycle):
        """run_cycle wrapped so it can never raise into the scheduler thread."""
        with self._lock:
            self.cycle_count = cycle
        try:
            return self.run_cycle(pid, label, cfg, cycle)
        except Exception as exc:                      # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.log(f"\u274c cycle crashed: {self.last_error}")
            return {"ok": False, "reason": self.last_error, "decisions": []}

    # -- trade planning / execution -----------------------------------------
    def plan_auto_trades(self, recs, prices, cash_reserve_pct=None,
                         max_position_pct=None,
                         cooldown_hours=app.AUTO_TRADE_COOLDOWN_HOURS,
                         risk_state=None, profile_id=None):
        """Applies the risk engine and the guard rails to recommendations.

        Returns (orders, skipped): 'orders' holds only the trades that are
        genuinely safe, 'skipped' holds human-readable reasons for everything
        rejected. Pure planning - no writes, no UI.

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

        This is a verbatim port of the desktop implementation; the desktop now
        calls this method, so the two front ends cannot drift apart.
        """
        pid = profile_id if profile_id is not None else self.profile_id
        if cash_reserve_pct is None:
            cash_reserve_pct = app.AUTO_CASH_RESERVE_PCT
        if max_position_pct is None:
            max_position_pct = app.AUTO_MAX_POSITION_PCT

        orders = []
        skipped = []
        if not pid:
            return orders, skipped

        conn = app.get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT balance FROM profiles WHERE id = ?", (pid,))
            row = cursor.fetchone()
            if not row:
                return orders, skipped
            balance = float(row[0])
            cursor.execute("SELECT symbol, quantity, avg_price FROM positions "
                           "WHERE profile_id = ?", (pid,))
            positions = cursor.fetchall()
            cursor.execute("SELECT symbol, MAX(trade_time) FROM trades "
                           "WHERE profile_id = ? GROUP BY symbol", (pid,))
            last_trade = dict(cursor.fetchall())
        finally:
            conn.close()

        owned_qty = {s: float(q) for s, q, _a in positions}
        # Live value per holding (fall back to cost when the price is stale).
        live_value = {s: float(q) * (prices.get(s, 0.0) or a)
                      for s, q, a in positions}
        net_worth = balance + sum(live_value.values())
        min_cash = net_worth * cash_reserve_pct
        planned_symbols = set()
        open_positions = len([1 for s, q, _a in positions if float(q) > 0])
        halted = bool(risk_state and risk_state.get("halted"))
        halt_reason = (risk_state or {}).get("halt_reason", "")

        def hours_since(timestr):
            try:
                t = datetime.strptime(timestr, "%Y-%m-%d %H:%M:%S")
                return (datetime.now() - t).total_seconds() / 3600.0
            except (ValueError, TypeError):
                return 1e9            # unparseable timestamp -> no cooldown

        for r in recs:
            action = str(r.get("action", "")).upper()
            if action not in ("BUY", "SELL"):
                continue
            sym = str(r.get("symbol", "")).strip().upper()
            if not sym:
                continue
            price = prices.get(sym, 0.0) or r.get("price") or 0.0
            if price <= 0:
                skipped.append(f"{sym}: no live price")
                continue
            if sym in planned_symbols:
                skipped.append(f"{sym}: already traded in this batch")
                continue

            if action == "SELL":
                owned = owned_qty.get(sym, 0.0)
                if owned <= 0:
                    skipped.append(f"{sym}: not owned (long-only, no shorting)")
                    continue
                qty = min(float(r.get("quantity") or 0.0), owned) or owned
                if qty <= 0:
                    skipped.append(f"{sym}: no valid quantity")
                    continue
                planned_symbols.add(sym)
                orders.append({
                    "action": "SELL", "symbol": sym, "qty": qty,
                    # Exits are charged adverse slippage too.
                    "price": app.apply_slippage(price, "SELL"),
                    "reason": r.get("reason", ""), "kind": r.get("kind", "exit"),
                    "stop_loss": None, "take_profit": None,
                    "risk_per_share": None})
                balance += qty * price
                owned_qty[sym] = owned - qty
                live_value[sym] = owned_qty[sym] * price
                continue

            # ---------------- BUY ----------------
            # Circuit breakers: no NEW risk while the account is in a protected
            # state. Exits above were never affected.
            if halted:
                skipped.append(f"{sym}: new entries halted - {halt_reason}")
                continue
            if open_positions >= app.MAX_OPEN_POSITIONS:
                skipped.append(f"{sym}: already holding {open_positions} "
                               f"positions (max {app.MAX_OPEN_POSITIONS})")
                continue

            last = last_trade.get(sym)
            if last is not None:
                age = hours_since(last)
                if age < cooldown_hours:
                    skipped.append(f"{sym}: traded {age:.1f}h ago (cooldown "
                                   f"{cooldown_hours:g}h)")
                    continue

            fill = app.apply_slippage(price, "BUY")
            stop = r.get("stop_loss")
            qty = float(r.get("quantity") or 0)

            if stop and float(stop) < fill:
                # Risk-based sizing, re-checked here so the engine's numbers are
                # enforced at execution time as well.
                sized, _binding = app.position_size(
                    net_worth, fill, float(stop), balance,
                    risk_pct=app.RISK_PER_TRADE_PCT,
                    max_position_pct=max_position_pct,
                    cash_floor_pct=cash_reserve_pct,
                    current_position_value=live_value.get(sym, 0.0))
                if qty <= 0:
                    qty = float(sized)
                else:
                    qty = float(min(qty, sized))
            if qty <= 0:
                skipped.append(f"{sym}: no valid quantity")
                continue

            spendable = balance - min_cash
            room = net_worth * max_position_pct - live_value.get(sym, 0.0)
            cap_qty = min(int(spendable / fill) if spendable > 0 else 0,
                          int(room / fill) if room > 0 else 0)
            if cap_qty <= 0:
                skipped.append(f"{sym}: cash floor or position cap reached")
                continue
            if qty > cap_qty:
                qty = float(cap_qty)
            if qty < 1:
                skipped.append(f"{sym}: risk-based size under 1 share")
                continue

            planned_symbols.add(sym)
            orders.append({
                "action": "BUY", "symbol": sym, "qty": qty, "price": fill,
                "reason": r.get("reason", ""), "kind": r.get("kind", "entry"),
                "stop_loss": stop, "take_profit": r.get("take_profit"),
                "risk_per_share": (fill - float(stop)) if stop else None})

            balance -= qty * fill
            owned_qty[sym] = owned_qty.get(sym, 0.0) + qty
            live_value[sym] = owned_qty[sym] * fill
            if sym not in {s for s, _q, _a in positions}:
                open_positions += 1
        return orders, skipped

    def execute_auto_trades(self, recs, prices, label=None,
                            cooldown_hours=app.AUTO_TRADE_COOLDOWN_HOURS,
                            risk_state=None, quiet=True, profile_id=None,
                            cash_reserve_pct=None, max_position_pct=None):
        """Executes the recommendations that survive the guard rails.

        Returns a summary dict: {executed, buys, sells, skipped[]}.
        The desktop app keeps its own popups/status line; this method only
        decides and books, so both front ends share the identical logic.
        """
        pid = profile_id if profile_id is not None else self.profile_id
        orders, skipped = self.plan_auto_trades(
            recs, prices, cash_reserve_pct=cash_reserve_pct,
            max_position_pct=max_position_pct, cooldown_hours=cooldown_hours,
            risk_state=risk_state, profile_id=pid)

        executed = buys = sells = 0
        for order in orders:
            if self.process_trade(order["action"], order["symbol"], order["qty"],
                                  order["price"], is_auto=True,
                                  reason=order["reason"], refresh=False,
                                  stop_loss=order.get("stop_loss"),
                                  take_profit=order.get("take_profit"),
                                  risk_per_share=order.get("risk_per_share"),
                                  profile_id=pid):
                executed += 1
                if order["action"] == "BUY":
                    buys += 1
                else:
                    sells += 1

        summary = {"executed": executed, "buys": buys, "sells": sells,
                   "skipped": skipped, "planned": len(orders)}
        if executed or skipped:
            detail = (f"executed {executed} order(s) - {buys} new position(s), "
                      f"{sells} exit(s) of YOUR holdings")
            if skipped:
                detail += (f"; {len(skipped)} skipped by the risk engine/guard "
                           "rails.")
            self.log(f"{label or 'Auto'}: {detail}")
        elif planned:
            # Planned trades that booked nothing is never normal: say so loudly
            # instead of leaving a cycle that looks like it simply found nothing.
            self.log(f"\u26a0\ufe0f {label or 'Auto'}: {planned} order(s) planned "
                     f"but none could be booked - see the warnings above.")
        return summary

    # -- trade booking -------------------------------------------------------
    def process_trade(self, action, symbol, qty, price, is_auto=False,
                      reason="", refresh=False, stop_loss=None,
                      take_profit=None, risk_per_share=None, profile_id=None):
        """Books a trade: moves cash, updates the position, writes history.

        This is the only place cash and positions change, and it is the same
        code the desktop app uses. A BUY can never overdraw the account; a SELL
        can never sell more than is held. Both are enforced here rather than
        being assumed upstream.
        """
        pid = profile_id if profile_id is not None else self.profile_id
        if not pid:
            return False
        if qty is None or price is None or qty <= 0 or price <= 0:
            return False

        action = str(action).upper()
        if action not in ("BUY", "SELL"):
            return False
        symbol = str(symbol).strip().upper()
        if not symbol:
            return False
        total_cost = qty * price

        conn = app.get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT balance FROM profiles WHERE id = ?", (pid,))
            row = cursor.fetchone()
            if not row:
                raise sqlite3.Error("Profile not found.")
            balance = row[0]
            realized_pnl = 0.0

            if action == "BUY":
                if balance < total_cost:
                    return False
                new_balance = balance - total_cost
                cursor.execute("UPDATE profiles SET balance = ? WHERE id = ?",
                               (new_balance, pid))
                cursor.execute(
                    "SELECT quantity, avg_price FROM positions "
                    "WHERE profile_id = ? AND symbol = ?", (pid, symbol))
                existing = cursor.fetchone()
                if existing:
                    old_qty, old_avg = existing
                    new_qty = old_qty + qty
                    new_avg = ((old_qty * old_avg) + total_cost) / new_qty
                    cursor.execute(
                        "UPDATE positions SET quantity = ?, avg_price = ? "
                        "WHERE profile_id = ? AND symbol = ?",
                        (new_qty, new_avg, pid, symbol))
                else:
                    cursor.execute(
                        "INSERT INTO positions (profile_id, symbol, quantity, "
                        "avg_price, opened_at) VALUES (?, ?, ?, ?, ?)",
                        (pid, symbol, qty, price,
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                # Trading Mode entries carry active risk levels; MAX() keeps the
                # invariant that a protective stop can only ever move UP.
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
                         price, risk_per_share, pid, symbol))
            else:                                        # SELL
                cursor.execute(
                    "SELECT quantity, avg_price FROM positions "
                    "WHERE profile_id = ? AND symbol = ?", (pid, symbol))
                existing = cursor.fetchone()
                if not existing or existing[0] < qty:
                    return False
                old_qty, old_avg = existing
                new_qty = old_qty - qty
                new_balance = balance + total_cost
                realized_pnl = (price - old_avg) * qty
                cursor.execute("UPDATE profiles SET balance = ? WHERE id = ?",
                               (new_balance, pid))
                if new_qty <= 1e-9:
                    cursor.execute("DELETE FROM positions WHERE profile_id = ? "
                                   "AND symbol = ?", (pid, symbol))
                else:
                    cursor.execute("UPDATE positions SET quantity = ? "
                                   "WHERE profile_id = ? AND symbol = ?",
                                   (new_qty, pid, symbol))

            cursor.execute(
                "INSERT INTO trades (profile_id, trade_time, action, symbol, "
                "quantity, price, total, reason, realized_pnl) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action,
                 symbol, qty, price, total_cost, reason, realized_pnl))
            conn.commit()
            return True
        except sqlite3.Error as exc:
            conn.rollback()
            # Never fail silently: a swallowed error here means a trade the user
            # was told about never booked. This is exactly how the hosted build
            # lost every entry for five days (SQLite's two-argument MAX() is not
            # a PostgreSQL function, so the protective-stop UPDATE raised).
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.log(f"\u26a0\ufe0f could not book {action} {symbol}: "
                     f"{self.last_error}")
            return False
        finally:
            conn.close()

    # -- profiles ------------------------------------------------------------
    def list_profiles(self):
        conn = app.get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, currency, balance, auto_mode, "
                           "total_deposited FROM profiles ORDER BY id")
            return [{"id": r[0], "name": r[1], "currency": r[2],
                     "balance": float(r[3] or 0.0), "auto_mode": int(r[4] or 0),
                     "total_deposited": float(r[5] or 0.0)}
                    for r in cursor.fetchall()]
        finally:
            conn.close()

    def create_profile(self, name, currency="USD", initial_balance=100000.0):
        name = str(name or "").strip()
        if not name:
            raise ValueError("Profile name cannot be empty.")
        balance = float(initial_balance or 0.0)
        if balance < 0:
            raise ValueError("Initial balance cannot be negative.")
        conn = app.get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO profiles (name, currency, balance, auto_mode, "
                "total_deposited) VALUES (?, ?, ?, 0, ?)",
                (name, currency, balance, balance))
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def delete_profile(self, pid):
        conn = app.get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM profiles WHERE id = ?", (pid,))
            conn.commit()
            deleted = cursor.rowcount > 0
        finally:
            conn.close()
        with self._lock:
            if deleted and self.profile_id == pid:
                self.running = False
                self.profile_id = None
        return deleted

    def adjust_funds(self, pid, amount, note=None):
        """Deposits (positive) or withdraws (negative). Never goes negative."""
        amount = float(amount or 0.0)
        if amount == 0:
            raise ValueError("Amount must not be zero.")
        conn = app.get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT balance, currency, total_deposited "
                           "FROM profiles WHERE id = ?", (pid,))
            row = cursor.fetchone()
            if not row:
                raise ValueError("Profile not found.")
            balance, currency, deposited = float(row[0]), row[1], float(row[2] or 0)
            new_balance = balance + amount
            if new_balance < 0:
                raise ValueError(
                    f"Cannot withdraw {abs(amount):,.2f}: cash balance is only "
                    f"{balance:,.2f}.")
            new_deposited = deposited + amount if amount > 0 else deposited
            cursor.execute("UPDATE profiles SET balance = ?, total_deposited = ?"
                           " WHERE id = ?", (new_balance, new_deposited, pid))
            cursor.execute(
                "INSERT INTO trades (profile_id, trade_time, action, symbol, "
                "quantity, price, total, reason, realized_pnl) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "DEPOSIT" if amount > 0 else "WITHDRAW",
                 (note or ("Deposit" if amount > 0 else "Withdrawal"))[:40],
                 0.0, 0.0, amount,
                 note or ("Cash deposit" if amount > 0 else "Cash withdrawal"),
                 0.0))
            conn.commit()
            return new_balance
        finally:
            conn.close()

    def set_auto_mode(self, pid, enabled):
        conn = app.get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE profiles SET auto_mode = ? WHERE id = ?",
                           (1 if enabled else 0, pid))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    # -- dashboard snapshot --------------------------------------------------
    def trade_history(self, pid, limit=500):
        conn = app.get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, trade_time, action, symbol, quantity, price, "
                "total, reason, realized_pnl FROM trades WHERE profile_id = ? "
                "ORDER BY id DESC LIMIT ?", (pid, int(limit)))
            cols = ("id", "trade_time", "action", "symbol", "quantity", "price",
                    "total", "reason", "realized_pnl")
            out = []
            for row in cursor.fetchall():
                item = dict(zip(cols, row))
                for key in ("quantity", "price", "total", "realized_pnl"):
                    item[key] = (None if item[key] is None
                                 else float(item[key]))
                out.append(item)
            return out
        finally:
            conn.close()

    def profile_snapshot(self, pid, with_prices=True):
        """Everything the dashboard needs about one profile, as plain JSON.

        This mirrors the arithmetic the desktop UI performs so both front ends
        show identical numbers: cash, position value, net worth, realized and
        unrealized P/L, and the per-trade averages.
        """
        if not pid:
            return None
        conn = app.get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name, currency, balance, total_deposited "
                           "FROM profiles WHERE id = ?", (pid,))
            prow = cursor.fetchone()
            if not prow:
                return None
            name, currency, cash, deposited = prow
            cash = float(cash or 0.0)
            deposited = float(deposited or 0.0)
            cursor.execute(
                "SELECT symbol, quantity, avg_price, stop_loss, take_profit, "
                "initial_stop, high_water, risk_per_share, breakeven_done, "
                "opened_at FROM positions WHERE profile_id = ? "
                "ORDER BY symbol", (pid,))
            pos_rows = cursor.fetchall()
            cursor.execute(
                "SELECT realized_pnl, total FROM trades WHERE profile_id = ? "
                "AND action = 'SELL' AND realized_pnl IS NOT NULL", (pid,))
            sell_rows = cursor.fetchall()
        finally:
            conn.close()

        realized_pnl = sum(float(r or 0.0) for r, _t in sell_rows)
        trade_pcts = []
        for r, t in sell_rows:
            cost = (t or 0.0) - (r or 0.0)
            if cost > 0:
                trade_pcts.append((r or 0.0) / cost * 100.0)
        avg_trade_pct = (sum(trade_pcts) / len(trade_pcts)) if trade_pcts else None

        symbols = [r[0] for r in pos_rows]
        prices = app.get_stock_prices(symbols) if (symbols and with_prices) else {}

        positions, total_value, unrealized, cost_basis = [], 0.0, 0.0, 0.0
        stale = 0
        for (sym, qty, avg_p, sl, tp, init_sl, hw, rps, be, opened) in pos_rows:
            qty, avg_p = float(qty or 0.0), float(avg_p or 0.0)
            live = float(prices.get(sym, 0.0) or 0.0)
            if live <= 0:
                # Fall back to the buy price so the table stays truthful rather
                # than showing a fake zero.
                live = avg_p
                stale += 1
            value = qty * live
            pnl_money = (live - avg_p) * qty
            pnl_pct = ((live - avg_p) / avg_p * 100.0) if avg_p > 0 else 0.0
            total_value += value
            unrealized += pnl_money
            cost_basis += avg_p * qty
            positions.append({
                "symbol": sym, "quantity": qty, "avg_price": avg_p,
                "price": live, "value": value,
                "pnl_pct": pnl_pct, "pnl_money": pnl_money,
                "stop_loss": None if sl is None else float(sl),
                "take_profit": None if tp is None else float(tp),
                "initial_stop": None if init_sl is None else float(init_sl),
                "high_water": None if hw is None else float(hw),
                "risk_per_share": None if rps is None else float(rps),
                "breakeven_done": int(be or 0),
                "opened_at": opened,
            })

        net_worth = cash + total_value
        overall = realized_pnl + unrealized
        return {
            "profile_id": pid, "name": name, "currency": currency,
            "symbol": currency_symbol(currency),
            "account": {
                "currency": currency, "symbol": currency_symbol(currency),
                "cash": cash, "total_portfolio_value": total_value,
                "net_worth": net_worth, "realized_pnl": realized_pnl,
                "unrealized_pnl": unrealized, "overall_pnl": overall,
                "avg_trade_pct": avg_trade_pct,
                "avg_position_pct": (sum(p["pnl_pct"] for p in positions)
                                     / len(positions)) if positions else None,
                "total_deposited": deposited,
                "cost_basis": cost_basis,
                "stale_count": stale,
                "prices_as_of": datetime.now().strftime("%H:%M:%S"),
            },
            "positions": positions,
        }

    def risk_state(self, pid):
        """Reads (and creates on first use) the breaker state for a profile."""
        conn = app.get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT day, day_start_equity, peak_equity, halted, "
                           "halt_reason FROM risk_state WHERE profile_id = ?",
                           (pid,))
            row = cursor.fetchone()
        finally:
            conn.close()
        today = datetime.now().strftime("%Y-%m-%d")
        if not row:
            return {"day": today, "day_start_equity": None, "peak_equity": None,
                    "halted": False, "halt_reason": "", "day_pnl_pct": 0.0,
                    "drawdown_pct": 0.0}
        day, day_start, peak, halted, reason = row
        snap = self.profile_snapshot(pid)
        equity = snap["account"]["net_worth"] if snap else 0.0
        day_pnl = drawdown = 0.0
        if day_start:
            day_pnl = (equity - float(day_start)) / float(day_start) * 100.0
        if peak:
            drawdown = (equity - float(peak)) / float(peak) * 100.0
        return {"day": day, "day_start_equity": (None if day_start is None
                                                 else float(day_start)),
                "peak_equity": None if peak is None else float(peak),
                "halted": bool(halted), "halt_reason": reason or "",
                "day_pnl_pct": day_pnl, "drawdown_pct": drawdown}

    def risk_rules(self):
        """The live risk parameters, so the UI never hard-codes them."""
        return {
            "risk_per_trade_pct": app.RISK_PER_TRADE_PCT * 100.0,
            "max_open_positions": app.MAX_OPEN_POSITIONS,
            "max_position_pct": app.MAX_POSITION_PCT * 100.0,
            "cash_floor_pct": app.CASH_FLOOR_PCT * 100.0,
            "daily_loss_limit_pct": app.DAILY_LOSS_LIMIT_PCT * 100.0,
            "max_drawdown_halt_pct": app.MAX_DRAWDOWN_HALT_PCT * 100.0,
            "take_profit_r": app.TAKE_PROFIT_R,
            "breakeven_at_r": app.BREAKEVEN_AT_R,
            "trail_atr_mult": app.TRAIL_ATR_MULT,
            "slippage_bps": app.SLIPPAGE_BPS,
            "min_entry_score": app.MIN_ENTRY_SCORE,
            "min_exit_score": app.MIN_EXIT_SCORE,
            "min_price": app.MIN_PRICE,
            "min_avg_dollar_volume": app.MIN_AVG_DOLLAR_VOLUME,
        }

    def timeframe_options(self):
        return [{"label": label, "interval": cfg["interval"],
                 "rescan_min": cfg["rescan_min"],
                 "cooldown_h": cfg["cooldown_h"]}
                for label, cfg in app.TRADING_TIMEFRAMES.items()]
