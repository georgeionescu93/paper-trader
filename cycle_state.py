"""When each armed portfolio is next due for a cycle.

The desktop build keeps this in memory: Scheduler._armed[pid]["next_at"]. That
is fine for one always-on process, but the split deployment starts a fresh
process for every cron tick, and without a stored due time it would re-scan the
whole S&P 500 on every tick instead of once per rescan interval - fifteen times
more trading and fifteen times more database wake-ups than the app intends.

The table is deliberately tiny (one row per portfolio) and purely advisory:
losing it costs one extra cycle, never a missed one. Every read fails OPEN, so
a database hiccup makes a portfolio due for a cycle rather than silently
suspending it.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

STAMP = "%Y-%m-%d %H:%M:%S"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycle_state (
    profile_id   INTEGER PRIMARY KEY,
    user_id      INTEGER,
    last_run_at  TEXT,
    next_run_at  TEXT,
    last_cycle   INTEGER,
    last_label   TEXT,
    updated_at   TEXT
)
"""

DEFAULT_LABEL = "Auto (all timeframes)"


def _connect():
    import paper_trading_app_TV as app
    return app.get_db_connection()


def _stamp(when=None):
    return (when or datetime.now()).strftime(STAMP)


def ensure_table(conn=None):
    own = conn is None
    conn = conn or _connect()
    try:
        conn.execute(SCHEMA)
        conn.commit()
    finally:
        if own:
            conn.close()


def rescan_minutes(label):
    """The pause between cycles for a timeframe, as the desktop build uses it."""
    try:
        import paper_trading_app_TV as app
        cfg = app.TRADING_TIMEFRAMES.get(label) or {}
        return float(cfg.get("rescan_min") or 15)
    except Exception:                                       # noqa: BLE001
        return 15.0


def next_after(label, now=None, minutes=None):
    """The timestamp this portfolio is next due, for the given timeframe.

    `minutes` overrides the timeframe's own pause, which the job uses to retry a
    failed cycle sooner than the full interval.
    """
    delay = float(minutes) if minutes else rescan_minutes(label)
    return _stamp((now or datetime.now()) + timedelta(minutes=delay))


def reset(profile_id):
    """Forget the schedule of one portfolio (called when it is armed/disarmed).

    Arming a portfolio must run it promptly, exactly like the desktop build,
    instead of waiting out a due time left over from a previous arming.
    """
    if profile_id is None:
        return
    try:
        conn = _connect()
        try:
            ensure_table(conn)
            conn.execute("DELETE FROM cycle_state WHERE profile_id = ?",
                         (int(profile_id),))
            conn.commit()
        finally:
            conn.close()
    except Exception:                                       # noqa: BLE001
        pass                                                # advisory only


def due_profiles(profile_ids, now=None, conn=None):
    """Which of these portfolios are due (never run, or past their due time)."""
    ids = [int(p) for p in profile_ids if p is not None]
    if not ids:
        return set()
    own = conn is None
    try:
        conn = conn or _connect()
        try:
            ensure_table(conn)
            placeholders = ", ".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT profile_id, next_run_at FROM cycle_state "
                f"WHERE profile_id IN ({placeholders})", ids).fetchall()
        finally:
            if own:
                conn.close()
    except Exception:                                       # noqa: BLE001
        return set(ids)                                     # fail open: run them
    stamp = _stamp(now)
    planned = {int(row[0]): (row[1] or "") for row in rows}
    due = set()
    for pid in ids:
        when = planned.get(pid)
        if not when or when <= stamp:
            due.add(pid)
    return due


def mark_ran(profile_id, user_id=None, next_run_at=None, cycle=None, label=None,
             now=None, conn=None):
    """Records that a portfolio just ran, and when it is next due."""
    if profile_id is None:
        return False
    own = conn is None
    try:
        conn = conn or _connect()
        try:
            ensure_table(conn)
            when = next_run_at or next_after(label or DEFAULT_LABEL, now)
            ran_at = _stamp(now)
            previous = conn.execute(
                "SELECT profile_id FROM cycle_state WHERE profile_id = ?",
                (int(profile_id),)).fetchone()
            if previous:
                conn.execute(
                    "UPDATE cycle_state SET user_id = ?, last_run_at = ?, "
                    "next_run_at = ?, last_cycle = ?, last_label = ?, "
                    "updated_at = ? WHERE profile_id = ?",
                    (user_id, ran_at, when, cycle, label, ran_at,
                     int(profile_id)))
            else:
                conn.execute(
                    "INSERT INTO cycle_state (profile_id, user_id, last_run_at,"
                    " next_run_at, last_cycle, last_label, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (int(profile_id), user_id, ran_at, when, cycle, label,
                     ran_at))
            conn.commit()
            return True
        finally:
            if own:
                conn.close()
    except Exception as exc:                                # noqa: BLE001
        print(f"  ! could not record the cycle time: "
              f"{type(exc).__name__}: {exc}")
        return False


def snapshot(conn=None):
    """Every known schedule, for a status display."""
    own = conn is None
    try:
        conn = conn or _connect()
        try:
            ensure_table(conn)
            rows = conn.execute(
                "SELECT profile_id, user_id, last_run_at, next_run_at, "
                "last_cycle, last_label FROM cycle_state "
                "ORDER BY profile_id").fetchall()
            return [{"profile_id": r[0], "user_id": r[1], "last_run_at": r[2],
                     "next_run_at": r[3], "last_cycle": r[4], "label": r[5]}
                    for r in rows]
        finally:
            if own:
                conn.close()
    except Exception:                                       # noqa: BLE001
        return []


def describe(profile_ids, now=None):
    """A one-line summary of what is due, for the job's output."""
    ids = [int(p) for p in profile_ids if p is not None]
    if not ids:
        return "nothing is armed"
    due = due_profiles(ids)
    if len(due) == len(ids):
        return f"all {len(ids)} armed portfolio(s) are due"
    if not due:
        return (f"none of the {len(ids)} armed portfolio(s) are due yet "
                f"(the next is scheduled later)")
    return f"{len(due)} of {len(ids)} armed portfolio(s) are due"
