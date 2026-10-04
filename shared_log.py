"""One shared activity feed, for a deployment split across two hosts.

engine.py keeps its activity in memory (TradingEngine._log). That is fine when
one process both trades and serves the page, which is how this app was built.
The free stack splits those roles: GitHub Actions runs the cycles while Render
serves the dashboard. Without this module the browser would show an empty
Activity panel while a remote engine quietly traded - the worst possible thing
for trust in an unattended deployment.

Installing it makes TradingEngine.log() also append to a small engine_log table
and log_since() read that table, so every process shares one feed.

The SQL below is written in SQLite dialect on purpose: pg_compat translates it
when PostgreSQL is the backend, and it is plain SQLite locally. Nothing here
needs to know which database it is talking to.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime

TABLE = "engine_log"
DDL = ("CREATE TABLE IF NOT EXISTS engine_log ("
       "n INTEGER PRIMARY KEY AUTOINCREMENT, "
       "t TEXT, text TEXT, at TEXT)")
KEEP = 800            # rows retained; oldest are trimmed
_installed = False
_lock = threading.Lock()


def _conn():
    import paper_trading_app_TV as app
    return app.get_db_connection()


def _clock():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_table():
    """Creates the table if needed. Safe to call on every start."""
    conn = _conn()
    try:
        conn.execute(DDL)
        conn.commit()
    finally:
        conn.close()


def append(text):
    """Adds one line to the shared feed."""
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO engine_log (t, text, at) VALUES (?, ?, ?)",
            (datetime.now().strftime("%H:%M:%S"), str(text), _clock()))
        conn.commit()
        conn.execute(
            "DELETE FROM engine_log WHERE n <= "
            "(SELECT MAX(n) FROM engine_log) - ?", (KEEP,))
        conn.commit()
    finally:
        conn.close()


def since(n=0, limit=400):
    """Lines newer than n, oldest first, as {n, t, text} - the shape
    /api/log already returns to the browser."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT n, t, text FROM engine_log WHERE n > ? ORDER BY n",
            (int(n or 0),)).fetchall()
    finally:
        conn.close()
    return [{"n": row[0], "t": row[1], "text": row[2]}
            for row in rows][-limit:]


def install(verbose=True):
    """Patches TradingEngine so its log is shared through the database.

    Best effort by design: if the database write fails, the in-memory log still
    works and a warning goes to stderr. Logging must never break trading.
    """
    global _installed
    with _lock:
        if _installed:
            return True
        try:
            import engine as engine_mod
        except Exception as exc:                            # noqa: BLE001
            sys.stderr.write(f"shared_log: engine import failed: {exc}\n")
            return False

        original_log = engine_mod.TradingEngine.log
        original_since = engine_mod.TradingEngine.log_since

        def log(self, text):
            original_log(self, text)
            try:
                append(text)
            except Exception as exc:                        # noqa: BLE001
                sys.stderr.write(f"shared_log: could not persist a log line "
                                 f"({type(exc).__name__}: {exc})\n")

        def log_since(self, since_n=0):
            try:
                rows = since(since_n)
                if rows:
                    return rows
            except Exception as exc:                        # noqa: BLE001
                sys.stderr.write(f"shared_log: could not read the shared log "
                                 f"({type(exc).__name__}: {exc})\n")
            return original_since(self, since_n)

        engine_mod.TradingEngine.log = log
        engine_mod.TradingEngine.log_since = log_since
        _installed = True
        if verbose:
            print("  Activity : shared through the database "
                  "(both hosts show one feed)")
        return True


def is_installed():
    return _installed
