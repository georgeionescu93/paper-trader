"""Regression tests for hosted, unattended trading.

These use temporary SQLite databases and mocks only. They never open the real
ledger and never contact TradingView, Neon, GitHub or Render.
"""
from __future__ import annotations

import math
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import paper_trading_app_TV as app
import app_web
import engine
import tv_data


class MarketHoursTests(unittest.TestCase):
    def at(self, text):
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)

    def test_normal_session(self):
        self.assertTrue(tv_data.market_is_open(self.at("2026-11-12T15:00:00")))
        self.assertFalse(tv_data.market_is_open(self.at("2026-11-12T14:29:00")))
        self.assertFalse(tv_data.market_is_open(self.at("2026-11-12T21:00:00")))

    def test_good_friday_is_closed(self):
        self.assertFalse(tv_data.market_is_open(self.at("2026-04-03T15:00:00")))

    def test_observed_independence_day_is_closed(self):
        self.assertFalse(tv_data.market_is_open(self.at("2026-07-03T15:00:00")))

    def test_day_after_thanksgiving_closes_at_one(self):
        self.assertTrue(tv_data.market_is_open(self.at("2026-11-27T17:30:00")))
        self.assertFalse(tv_data.market_is_open(self.at("2026-11-27T18:30:00")))

    def test_christmas_eve_early_close(self):
        self.assertTrue(tv_data.market_is_open(self.at("2026-12-24T17:30:00")))
        self.assertFalse(tv_data.market_is_open(self.at("2026-12-24T18:30:00")))

    def test_new_year_saturday_has_no_observed_close(self):
        # NYSE explicitly observes no New Year's Day holiday in 2028.
        self.assertTrue(tv_data.market_is_open(self.at("2027-12-31T15:00:00")))


class TemporaryLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paper-trader-tests-")
        self.db = os.path.join(self.tmp.name, "ledger.db")
        app.DB_FILE = self.db
        app.init_db()
        self.eng = engine.TradingEngine(self.db)
        self.pid = self.eng.create_profile("Concurrency test", "USD", 100.0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_non_finite_funds_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.eng.adjust_funds(self.pid, value)

    def test_non_finite_initial_balance_is_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.eng.create_profile("Bad balance", "USD", value)

    def test_simultaneous_buys_cannot_create_cash(self):
        """Both workers try to spend 50 of the same 100 at the same time."""
        barrier = threading.Barrier(2)
        original = sqlite3.connect

        class Cursor:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            def execute(self, sql, params=()):
                if sql.startswith("SELECT balance FROM profiles"):
                    # The old implementation reads before it writes. Make both
                    # workers finish that read before either can continue.
                    barrier.wait(timeout=5)
                return self._wrapped.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

        class Connection:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            def cursor(self):
                return Cursor(self._wrapped.cursor())

            def execute(self, sql, params=()):
                return self._wrapped.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

        def connection():
            return Connection(original(self.db, timeout=10))

        results = []
        errors = []

        def buy(symbol):
            try:
                results.append(self.eng.process_trade(
                    "BUY", symbol, 50, 1.0, profile_id=self.pid))
            except Exception as exc:  # the assertion below reports it
                errors.append(exc)

        with mock.patch.object(app, "get_db_connection", side_effect=connection):
            threads = [threading.Thread(target=buy, args=(symbol,))
                       for symbol in ("AAA", "BBB")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)

        self.assertFalse(errors)
        self.assertEqual(results, [True, True])
        conn = original(self.db)
        try:
            balance = conn.execute(
                "SELECT balance FROM profiles WHERE id=?", (self.pid,)).fetchone()[0]
            positions = conn.execute(
                "SELECT count(*) FROM positions WHERE profile_id=?",
                (self.pid,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(positions, 2)
        self.assertAlmostEqual(balance, 0.0, places=6)


class WebRegressionTests(unittest.TestCase):
    def test_secure_cookie_on_https(self):
        header = app_web._cookie_header("token", secure=True)
        self.assertIn("Secure", header)
        self.assertIn("HttpOnly", header)
        self.assertIn("SameSite=Lax", header)

    def test_log_cursor_uses_shared_database_sequence(self):
        fake_engine = SimpleNamespace(
            log_since=lambda since: [{"n": 105, "t": "now", "text": "line"}],
            status=lambda: {"log_seq": 2},
        )
        state = SimpleNamespace(engine=fake_engine)
        # Owner bypasses the log-redaction database lookup.
        handler = SimpleNamespace(
            server=SimpleNamespace(state=state), user_id=1,
            _json=lambda payload: setattr(handler, "payload", payload))
        router = app_web.build_router()
        route, _params, _auth = router.match("GET", "/api/log")
        with mock.patch.object(app_web, "_redact_log", side_effect=lambda _s, lines, _u: lines):
            route(handler, {}, {}, {"since": "100"})
        self.assertEqual(handler.payload["next"], 105)


class DegradedScanTests(unittest.TestCase):
    def test_risk_management_still_runs_when_candles_fail(self):
        eng = engine.TradingEngine()
        label = next(iter(app.TRADING_TIMEFRAMES))
        entry = {"profile_id": 123, "label": label, "tag": "test"}
        called = []
        degraded = {"ok": False, "results": [], "prices": {}, "timings": None,
                    "reason": "test outage", "fetch_s": None}
        with mock.patch.object(eng, "held_symbols", return_value=[]), \
             mock.patch.object(eng, "scan_universe", return_value=degraded), \
             mock.patch.object(eng, "apply_cycle",
                               side_effect=lambda *args, **kwargs: called.append(args) or {
                                   "ok": False, "profile_id": 123,
                                   "decisions": [], "reason": "test outage"}):
            out = eng.run_cycle_many([entry], 1)
        self.assertEqual(len(called), 1)
        self.assertFalse(out["ok"])
        self.assertEqual(out["profiles"][0]["reason"], "test outage")


if __name__ == "__main__":
    unittest.main()
