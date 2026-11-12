"""Failure-mode regressions: temporary SQLite and mocks, never production."""
from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import paper_trading_app_TV as app
import app_web
import accounts
import cycle_state
import engine
import pg_compat
import run_cycle_job


AUTO = "Auto (all timeframes)"


def candidate(symbol="AAA", price=100.0):
    return {
        "symbol": symbol, "signal": "BUY", "price": price,
        "bull_score": 8, "bear_score": 0, "score": 8, "rsi": 50,
        "above_ema": True, "patterns": ["bullish test"], "vetoes": [],
        "mtf_votes": len(app.AUTO_TIMEFRAMES),
        "mtf_total": len(app.AUTO_TIMEFRAMES),
        "stop_loss": price - 5, "take_profit": price + 10,
    }


def snapshot(symbol="AAA", price=100.0):
    return {symbol: {"close": price, "ema200": price - 10, "rec_all_1W": 0.5}}


def scan_for(rec, live=None, ok=True, mtf=True):
    live = dict(live or {})
    return {
        "ok": ok, "results": [rec] if rec else [], "prices": dict(live),
        "live_prices": live, "timings": None, "fetch_s": None,
        "reason": "" if ok else "test outage", "mtf_ok": mtf,
        "snapshot": snapshot(rec["symbol"], rec["price"]) if rec else {},
    }


class EngineFailureModes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paper-trader-failure-tests-")
        self.db = os.path.join(self.tmp.name, "ledger.db")
        app.DB_FILE = self.db
        app.init_db()
        self.eng = engine.TradingEngine(self.db)
        self.pid = self.eng.create_profile("Failure mode", "USD", 1000.0)
        self.cfg = app.TRADING_TIMEFRAMES[AUTO]

    def tearDown(self):
        self.tmp.cleanup()

    def buy(self, symbol="AAA", qty=10, price=100):
        self.assertTrue(self.eng.process_trade(
            "BUY", symbol, qty, price, profile_id=self.pid))

    def position(self, symbol="AAA"):
        conn = app.get_db_connection()
        try:
            return conn.execute(
                "SELECT quantity, avg_price, stop_loss, take_profit FROM positions "
                "WHERE profile_id = ? AND symbol = ?", (self.pid, symbol)).fetchone()
        finally:
            conn.close()

    def cash(self):
        conn = app.get_db_connection()
        try:
            return float(conn.execute("SELECT balance FROM profiles WHERE id = ?",
                                      (self.pid,)).fetchone()[0])
        finally:
            conn.close()

    def prepare_stop(self):
        self.buy()
        conn = app.get_db_connection()
        try:
            conn.execute("UPDATE positions SET stop_loss = 95, initial_stop = 95, "
                         "take_profit = 200, high_water = 100, risk_per_share = 5 "
                         "WHERE profile_id = ? AND symbol = 'AAA'", (self.pid,))
            conn.commit()
        finally:
            conn.close()

    def test_degraded_candles_still_book_protective_sell(self):
        self.prepare_stop()
        scan = scan_for(None, ok=False)
        with mock.patch.object(app, "get_stock_prices", return_value={"AAA": 90}), \
             mock.patch.object(app, "get_market_snapshot", return_value={}):
            result = self.eng.apply_cycle(self.pid, AUTO, self.cfg, 1, scan)
        self.assertFalse(result["ok"])
        self.assertEqual(result["executed"], 1)
        self.assertIsNone(self.position())
        self.assertAlmostEqual(self.cash(), 899.55, places=2)

    def test_breaker_failure_blocks_buys_not_breached_stop(self):
        self.prepare_stop()
        scan = scan_for(None, live={"AAA": 90})
        with mock.patch.object(app, "get_stock_prices", return_value={"AAA": 90}), \
             mock.patch.object(app, "get_market_snapshot", return_value={}), \
             mock.patch.object(self.eng, "update_risk_state_checked",
                               side_effect=sqlite3.OperationalError("write failed")):
            result = self.eng.apply_cycle(self.pid, AUTO, self.cfg, 1, scan)
        self.assertFalse(result["ok"])
        self.assertIn("circuit-breaker", result["reason"])
        self.assertEqual(result["executed"], 1)
        self.assertIsNone(self.position())
        self.assertAlmostEqual(self.cash(), 899.55, places=2)

    def test_schedule_token_prevents_duplicate_buy_and_sell(self):
        key = "external:Auto:2026-11-12 20:19:00"
        self.assertTrue(self.eng.process_trade(
            "BUY", "AAA", 2, 10, profile_id=self.pid,
            order_key=key + ":BUY:AAA"))
        self.assertTrue(self.eng.process_trade(
            "BUY", "AAA", 2, 10, profile_id=self.pid,
            order_key=key + ":BUY:AAA"))
        self.assertAlmostEqual(self.cash(), 980, places=6)
        self.assertAlmostEqual(float(self.position()[0]), 2, places=6)
        self.assertTrue(self.eng.process_trade(
            "SELL", "AAA", 2, 12, profile_id=self.pid,
            order_key=key + ":SELL:AAA"))
        self.assertTrue(self.eng.process_trade(
            "SELL", "AAA", 2, 12, profile_id=self.pid,
            order_key=key + ":SELL:AAA"))
        self.assertAlmostEqual(self.cash(), 1004, places=6)
        self.assertIsNone(self.position())
        conn = app.get_db_connection()
        try:
            count = conn.execute("SELECT COUNT(*) FROM trades WHERE profile_id = ?",
                                 (self.pid,)).fetchone()[0]
            keys = conn.execute("SELECT COUNT(*) FROM automatic_order_keys "
                                "WHERE profile_id = ?", (self.pid,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 2)
        self.assertEqual(keys, 2)

    def test_generic_candle_price_is_not_executable(self):
        rec = candidate()
        scan = scan_for(rec)                 # deliberately NO live_prices
        scan["prices"] = {"AAA": 100}        # legacy/candle-only channel
        with mock.patch.object(app, "higher_timeframe_pattern_check",
                               return_value={"AAA": (True, "")}):
            result = self.eng.apply_cycle(self.pid, AUTO, self.cfg, 1, scan)
        self.assertFalse(result["ok"])
        self.assertEqual(result["executed"], 0)
        self.assertIn("no live execution price", result["reason"])
        self.assertIsNone(self.position())

    def test_gap_up_recomputes_target_from_actual_fill(self):
        self.eng.adjust_funds(self.pid, 9000)
        rec = candidate(price=100)
        rec.update({"stop_loss": 95, "take_profit": 110})
        scan = scan_for(rec, live={"AAA": 120})
        with mock.patch.object(app, "higher_timeframe_pattern_check",
                               return_value={"AAA": (True, "")}):
            result = self.eng.apply_cycle(self.pid, AUTO, self.cfg, 1, scan)
        self.assertTrue(result["ok"], result.get("reason"))
        self.assertEqual(result["executed"], 1)
        row = self.position()
        self.assertIsNotNone(row)
        fill = app.apply_slippage(120, "BUY")
        expected = fill + (fill - 95) * app.TAKE_PROFIT_R
        self.assertAlmostEqual(float(row[3]), expected, places=6)
        self.assertGreater(float(row[3]), fill)

    def test_missing_held_quote_blocks_new_risk_and_is_degraded(self):
        self.buy("AAA", qty=1, price=100)
        rec = candidate("BBB", 100)
        scan = scan_for(rec, live={"BBB": 100})
        with mock.patch.object(app, "get_stock_prices", return_value={}), \
             mock.patch.object(app, "get_market_snapshot", return_value={}), \
             mock.patch.object(app, "higher_timeframe_pattern_check",
                               return_value={"BBB": (True, "")}):
            result = self.eng.apply_cycle(self.pid, AUTO, self.cfg, 1, scan)
        self.assertFalse(result["ok"])
        self.assertIn("no live held-position quote", result["reason"])
        self.assertIsNone(self.position("BBB"))

    def test_missing_mtf_or_higher_timeframe_is_visible_failure(self):
        for higher in ({"AAA": (True, "")}, {}):
            with self.subTest(higher=higher):
                self.eng.adjust_funds(self.pid, 10000)
                rec = candidate()
                scan = scan_for(rec, live={"AAA": 100}, mtf=bool(not higher))
                if not higher:
                    scan["snapshot"] = snapshot()
                with mock.patch.object(app, "higher_timeframe_pattern_check",
                                       return_value=higher):
                    result = self.eng.apply_cycle(self.pid, AUTO, self.cfg, 1, scan)
                self.assertFalse(result["ok"])
                self.assertEqual(result["executed"], 0)
                self.assertIsNone(self.position())


class HistoryReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paper-trader-history-tests-")
        app.DB_FILE = os.path.join(self.tmp.name, "ledger.db")
        app.init_db()
        self.eng = engine.TradingEngine(app.DB_FILE)
        self.pid = self.eng.create_profile("Replay", "USD", 1000)

    def tearDown(self):
        self.tmp.cleanup()

    def test_price_correction_replays_cost_and_realized_pnl(self):
        self.assertTrue(self.eng.process_trade("BUY", "AAA", 10, 10, profile_id=self.pid))
        self.assertTrue(self.eng.process_trade("BUY", "AAA", 10, 20, profile_id=self.pid))
        self.assertTrue(self.eng.process_trade("SELL", "AAA", 10, 30, profile_id=self.pid))
        conn = app.get_db_connection()
        try:
            second_buy = conn.execute(
                "SELECT id FROM trades WHERE profile_id = ? AND action = 'BUY' "
                "AND price = 20", (self.pid,)).fetchone()[0]
        finally:
            conn.close()
        ok, error = app_web.edit_trade_price(second_buy, 40, profile_id=self.pid)
        self.assertTrue(ok, error)
        conn = app.get_db_connection()
        try:
            cash = conn.execute("SELECT balance FROM profiles WHERE id = ?",
                                (self.pid,)).fetchone()[0]
            qty, avg = conn.execute(
                "SELECT quantity, avg_price FROM positions WHERE profile_id = ? "
                "AND symbol = 'AAA'", (self.pid,)).fetchone()
            realized = conn.execute(
                "SELECT realized_pnl FROM trades WHERE profile_id = ? "
                "AND action = 'SELL'", (self.pid,)).fetchone()[0]
        finally:
            conn.close()
        self.assertAlmostEqual(cash, 800, places=6)
        self.assertAlmostEqual(qty, 10, places=6)
        self.assertAlmostEqual(avg, 25, places=6)
        self.assertAlmostEqual(realized, 50, places=6)
        self.assertFalse(app_web.edit_trade_price(second_buy, float("nan"),
                                                  profile_id=self.pid)[0])


class CompatibilityAndSchedulerTests(unittest.TestCase):
    def test_nocase_translation_folds_both_operands(self):
        statement = pg_compat.translate(
            "SELECT id FROM users WHERE email = ? COLLATE NOCASE", ("A@B.COM",))
        self.assertIn("lower(email) = lower(%s)", statement.sql)

    def test_dirty_connection_never_reconnects_for_select(self):
        class RawCursor:
            rowcount = 1
            description = None

            def execute(self, sql, params=None):
                if str(sql).startswith("SELECT"):
                    raise pg_compat.OperationalError("server closed the connection")

            def fetchone(self):
                return None

        class RawConnection:
            def cursor(self):
                return RawCursor()

        class FakeConnection:
            def __init__(self):
                self._conn = RawConnection()
                self._dirty = False
                self.reconnects = 0

            def _note_write(self, kind):
                if kind in ("write", "ddl", "replace"):
                    self._dirty = True

            def _has_column(self, table, column):
                return False

            def _reconnect(self):
                self.reconnects += 1

        fake = FakeConnection()
        cursor = pg_compat.Cursor(fake)
        cursor.execute("UPDATE profiles SET balance = 1")
        with self.assertRaises(pg_compat.OperationalError):
            cursor.execute("SELECT 1")
        self.assertEqual(fake.reconnects, 0)

    def test_due_schedule_read_failure_is_not_everything_due(self):
        class Broken:
            def execute(self, *args):
                raise sqlite3.OperationalError("temporary read failure")

        with self.assertRaises(RuntimeError):
            cycle_state.due_profiles([1], conn=Broken())

    def test_schedule_publish_failure_is_red_and_happens_under_lock(self):
        args = SimpleNamespace(db=":memory:", lock_timeout=0, quiet=True,
                               json=False, force=False, ignore_schedule=False)
        lock = SimpleNamespace(held=True, exited=False)

        class Guard:
            def __enter__(self):
                return lock

            def __exit__(self, *exc):
                lock.exited = not marked["called"]
                return False

        marked = {"called": False}
        fake_engine = SimpleNamespace(
            set_db_file=lambda _db: None, log=lambda _text: None,
            run_cycle_many=lambda entries, cycle: {
                "ok": True, "scanned": 1, "decisions": 0,
                "profiles": [{"ok": True, "profile_id": 1, "label": cycle_state.DEFAULT_LABEL,
                              "decisions": [], "executed": 0, "reason": ""}],
            })

        def failed_mark(*args, **kwargs):
            marked["called"] = True
            return False

        with tempfile.TemporaryDirectory(prefix="paper-trader-schedule-tests-") as tmp, \
             mock.patch.object(app, "TV_DATA_AVAILABLE", True), \
             mock.patch.object(app, "init_db", return_value=None), \
             mock.patch.object(accounts, "ensure_schema", return_value=None), \
             mock.patch.object(accounts, "armed_accounts", return_value=[
                 {"profile_id": 1, "label": cycle_state.DEFAULT_LABEL,
                  "email": "nobody@example.test", "user_id": 1}]), \
             mock.patch.object(cycle_state, "ensure_table", return_value=None), \
             mock.patch.object(cycle_state, "due_profile_tokens",
                               return_value={1: "2026-11-12 20:00:00"}), \
             mock.patch.object(cycle_state, "next_after", return_value="tomorrow"), \
             mock.patch.object(cycle_state, "mark_ran", side_effect=failed_mark), \
             mock.patch.object(engine, "TradingEngine", return_value=fake_engine), \
             mock.patch.object(run_cycle_job, "CycleLock", return_value=Guard()), \
             mock.patch("shared_log.ensure_table", return_value=None), \
             mock.patch("shared_log.install", return_value=True):
            args.db = os.path.join(tmp, "schedule.db")
            summary, code = run_cycle_job.run_tick(args, database="sqlite", cycle=1)
        self.assertEqual(code, 1)
        self.assertFalse(summary["ok"])
        self.assertTrue(marked["called"])
        self.assertFalse(lock.exited)
        self.assertIn("could not schedule", summary["profiles"][0]["error"])

    def test_relay_delay_is_bounded_and_does_not_run_overnight(self):
        with mock.patch.object(run_cycle_job, "market_is_open", return_value=True):
            self.assertEqual(run_cycle_job.relay_delay(failure=False), 0)
            self.assertEqual(run_cycle_job.relay_delay(failure=True), 300)
        with mock.patch.object(run_cycle_job, "market_is_open", return_value=False), \
             mock.patch.object(run_cycle_job, "seconds_until_open", return_value=3600):
            self.assertEqual(run_cycle_job.relay_delay(failure=False), 3540)
            self.assertIsNone(run_cycle_job.relay_delay(failure=True))
        with mock.patch.object(run_cycle_job, "market_is_open", return_value=False), \
             mock.patch.object(run_cycle_job, "seconds_until_open", return_value=20000):
            self.assertIsNone(run_cycle_job.relay_delay())

    def test_production_worker_requires_postgres_before_market_gate(self):
        with tempfile.TemporaryDirectory(prefix="paper-trader-config-tests-") as tmp:
            old_cwd = os.getcwd()
            try:
                with mock.patch.dict(os.environ, {"PAPER_TRADER_REQUIRE_POSTGRES": "1",
                                                 "DATABASE_URL": "",
                                                 "PAPER_TRADER_DB_URL": ""}, clear=False), \
                     mock.patch.object(pg_compat, "auto_install", return_value=False), \
                     mock.patch.object(run_cycle_job, "market_is_open",
                                       side_effect=AssertionError("market gate must not run")):
                    os.chdir(tmp)
                    code = run_cycle_job.main(["--quiet"])
            finally:
                os.chdir(old_cwd)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
