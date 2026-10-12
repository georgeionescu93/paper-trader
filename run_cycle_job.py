"""Runs the paper-trading cycle for an external scheduler.

WHY THIS FILE EXISTS
====================
The free stack has no process that stays awake: Render's free web service sleeps
when nobody visits, and its filesystem is wiped on every restart. So the trading
loop cannot live in the web process. Instead an external scheduler - GitHub
Actions on a cron - calls this script:

    python run_cycle_job.py                    # one cycle, only if due
    python run_cycle_job.py --loop              # keep cycling until the close
    python run_cycle_job.py --force             # scan even when nothing is armed
    python run_cycle_job.py --ignore-market-hours   # run outside session hours
    python run_cycle_job.py --json              # machine-readable summary

WHEN IT RUNS, AND WHEN IT DELIBERATELY DOES NOTHING
===================================================
Entries are only ever made during the US regular session (Mon-Fri 09:30-16:00
ET, see tv_data.market_is_open). Outside those hours every entry point stops
before it touches the database: nothing is scanned, nothing is logged to the
feed, nothing is traded. "The market is closed" is a normal, green outcome - not
a failure - so a closed-market run never shows up as a red run.

Inside the session, --loop keeps working instead of exiting after one pass: it
polls every --every-seconds (default 60) and runs a cycle whenever a portfolio is
actually due - cycle_state owns that decision, so a poll that finds nothing due
costs a millisecond and changes nothing. The loop stops when the session closes
or when --max-hours is reached (default 5.0, comfortably inside GitHub's 6-hour
job limit); the next scheduled run then picks the session back up. Pass
--ignore-market-hours, or --force after hours, to override the gate.

It does exactly what Scheduler._tick() does in the web build: read the armed
accounts from the database, scan the market ONCE, and apply that one scan to
every armed portfolio - each with its own cash, positions and rules.

Two safety properties matter more than speed here:

  * A PostgreSQL advisory lock means two schedulers (or a scheduler plus a
    manual cycle pressed in the browser) can never trade at the same time. It is
    taken per cycle, never held while sleeping, so a browser-triggered cycle can
    run between two of this loop's ticks.
  * The process exits non-zero when a cycle fails, so a broken run shows up as a
    red run in GitHub Actions instead of failing silently.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, time as dt_time, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Any stable 64-bit number; this one spells "paper" in ASCII.
LOCK_KEY = 0x7061706572


class CycleLock:
    """A cross-process lock, so only one cycle runs at any moment.

    PostgreSQL advisory locks are session-scoped and are released even if the
    process is killed, which makes them exactly right for a scheduled job. With
    SQLite (local runs) there is only ever one process, so this does nothing.
    """

    def __init__(self, enabled, timeout=0.0):
        self.enabled = enabled
        self.timeout = timeout
        self.conn = None
        self.held = False

    def __enter__(self):
        if not self.enabled:
            self.held = True
            return self
        import paper_trading_app_TV as app
        self.conn = app.get_db_connection()
        deadline = time.time() + max(0.0, self.timeout)
        while True:
            row = self.conn.execute(
                "SELECT pg_try_advisory_lock(?)", (LOCK_KEY,)).fetchone()
            if row and row[0]:
                self.held = True
                return self
            if time.time() >= deadline:
                self.held = False
                return self
            time.sleep(2.0)

    def __exit__(self, *exc_info):
        if self.enabled and self.conn is not None:
            try:
                if self.held:
                    self.conn.execute("SELECT pg_advisory_unlock(?)",
                                      (LOCK_KEY,))
                    self.conn.commit()
            except Exception:                               # noqa: BLE001
                pass
            finally:
                try:
                    self.conn.close()
                except Exception:                           # noqa: BLE001
                    pass
        self.held = False
        return False


# ---------------------------------------------------------------------------
# Market hours
# ---------------------------------------------------------------------------
def market_is_open(now_utc=None):
    """True during the US regular session; unknown clocks count as closed.

    tv_data owns the rule (Mon-Fri 09:30-16:00 ET). If it cannot be imported we
    say "closed", because doing nothing is the safe answer: a missed scan costs
    one interval, an after-hours scan places orders at stale prices.
    """
    try:
        import tv_data
        return bool(tv_data.market_is_open(now_utc or datetime.now(timezone.utc)))
    except Exception:                                       # noqa: BLE001
        return False


def seconds_until_open(now_utc=None):
    """Seconds until the next weekday 09:30 ET open, or None if unknowable.

    Used to turn a trigger that lands before the bell into useful cover instead
    of a wasted slot: the run waits, then trades the session. Holidays are not
    modelled (see tv_data), so a holiday looks like a normal day here.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    if market_is_open(now_utc):
        return 0.0
    try:
        import tv_data
        eastern = tv_data.ET
    except Exception:                                       # noqa: BLE001
        return None
    local = now_utc.astimezone(eastern)
    for day in range(0, 8):
        bell = datetime.combine(local.date() + timedelta(days=day),
                                dt_time(9, 30), tzinfo=eastern)
        if bell.weekday() < 5 and bell > local:
            return (bell - local).total_seconds()
    return None


def _blank_summary(database="sqlite"):
    return {"ok": False, "armed": 0, "due": 0, "waiting": [], "profiles": [],
            "scanned": 0, "decisions": 0, "executed": 0, "seconds": 0.0,
            "database": database, "skipped": None, "error": None,
            "market": "unknown", "cycle": 0}


def run_tick(args, database="sqlite", cycle=1):
    """One cycle. Returns (summary, exit code). Green means "nothing to do"."""
    started = time.time()
    summary = _blank_summary(database)
    summary["cycle"] = cycle
    summary["market"] = "open"

    import paper_trading_app_TV as app
    import accounts
    import cycle_state
    import engine as engine_mod
    import shared_log

    app.DB_FILE = args.db          # meaningless for PostgreSQL, truthful for SQLite

    if not app.TV_DATA_AVAILABLE:
        summary["error"] = f"market data unavailable: {app.TV_DATA_ERROR}"
        _report(summary, args, cycle)
        return summary, 1

    # 2. Schema is guaranteed before anything else touches the database, so the
    #    very first scheduled run on an empty PostgreSQL database works.
    try:
        app.init_db()
        conn = app.get_db_connection()
        try:
            accounts.ensure_schema(conn)
        finally:
            conn.close()
        shared_log.ensure_table()
        shared_log.install(verbose=not args.quiet)
    except Exception as exc:                                # noqa: BLE001
        summary["error"] = f"{type(exc).__name__}: {exc}"
        _report(summary, args, cycle)
        return summary, 1

    # 3. One cycle at a time, across every process that could start one.
    with CycleLock(database == "postgresql", args.lock_timeout) as lock:
        if not lock.held:
            summary["ok"] = True
            summary["skipped"] = "another cycle is already running"
            _report(summary, args, cycle)
            return summary, 0

        conn = app.get_db_connection()
        try:
            armed = accounts.armed_accounts(conn)
            # Why a due time is stored at all: this process is new on every cron
            # tick, so without it every tick would re-scan the whole universe
            # instead of once per timeframe interval (see cycle_state.py).
            cycle_state.ensure_table(conn)
            due = cycle_state.due_profiles(
                [row["profile_id"] for row in armed], conn=conn)
        finally:
            conn.close()
        summary["armed"] = len(armed)
        entries = [{"profile_id": row["profile_id"], "label": row["label"],
                    "tag": row["email"], "user_id": row["user_id"]}
                   for row in armed if row["profile_id"] in due]
        owners = {entry["profile_id"]: entry["user_id"] for entry in entries}
        summary["due"] = len(entries)
        summary["waiting"] = [
            {"profile_id": row["profile_id"], "label": row["label"],
             "email": row["email"]}
            for row in armed if row["profile_id"] not in due]

        if not armed and not args.force:
            summary["ok"] = True
            summary["skipped"] = "nothing is armed"
            if not args.quiet:
                print("Nothing is armed - no cycle needed. Arm a portfolio in "
                      "the dashboard and the next scheduled run will trade it.")
            _report(summary, args, cycle)
            return summary, 0

        if armed and not entries and not args.ignore_schedule:
            # The normal case for most polls: armed, but waiting for its own
            # rescan interval. Cheap on purpose - no market data is fetched.
            summary["ok"] = True
            summary["skipped"] = "no portfolio is due yet"
            if not args.quiet:
                for row in summary["waiting"]:
                    print(f"  waiting: portfolio {row['profile_id']} "
                          f"({row['label']}, {row['email']}) is not due yet")
                print("No portfolio is due - nothing to do.")
            _report(summary, args, cycle)
            return summary, 0

        eng = engine_mod.TradingEngine()
        eng.set_db_file(args.db)
        source = os.environ.get("PAPER_TRADER_JOB") or "the external scheduler"
        try:
            eng.log(f"\U0001f504 scheduled cycle from {source} "
                    f"({len(entries)} portfolio(s) armed)")
        except Exception:                                   # noqa: BLE001
            pass

        try:
            results = eng.run_cycle_many(entries, cycle)
        except Exception as exc:                            # noqa: BLE001
            summary["error"] = f"{type(exc).__name__}: {exc}"
            try:
                eng.log(f"\u274c scheduled cycle failed: {summary['error']}")
            except Exception:                               # noqa: BLE001
                pass
            _report(summary, args, cycle)
            return summary, 1

    for res in results.get("profiles", []):
        reason = res.get("reason") or ""
        failed = (not res.get("ok")) and reason not in ("",
                                                        "profile no longer exists")
        summary["profiles"].append({
            "profile_id": res.get("profile_id"),
            "label": res.get("label"),
            "decisions": len(res.get("decisions") or []),
            "executed": int(res.get("executed") or 0),
            "reason": reason,
            "error": reason if failed else None,
        })
        # Schedule the next run for this portfolio. A cycle that ran but failed
        # is retried much sooner than the full interval instead of waiting out
        # an hour or a day with nothing trading.
        label = res.get("label") or cycle_state.DEFAULT_LABEL
        try:
            if failed:
                when = cycle_state.next_after(
                    label, minutes=min(cycle_state.rescan_minutes(label), 15))
            else:
                when = cycle_state.next_after(label)
            cycle_state.mark_ran(res.get("profile_id"),
                                 owners.get(res.get("profile_id")),
                                 next_run_at=when, cycle=cycle, label=label)
        except Exception as exc:                            # noqa: BLE001
            print(f"  ! could not schedule the next run: "
                  f"{type(exc).__name__}: {exc}")
    summary["scanned"] = int(results.get("scanned") or 0)
    summary["decisions"] = int(results.get("decisions") or 0)
    summary["executed"] = sum(p["executed"] for p in summary["profiles"])
    summary["ok"] = not any(p.get("error") for p in summary["profiles"])
    summary["seconds"] = round(time.time() - started, 1)

    if not args.quiet:
        print(f"Cycle finished in {summary['seconds']}s: "
              f"{summary['scanned']} symbols scanned, "
              f"{len(summary['profiles'])} portfolio(s), "
              f"{summary['executed']} order(s) placed.")
        for prof in summary["profiles"]:
            print(f"  portfolio {prof['label'] or prof['profile_id']}: "
                  f"{prof['decisions']} decision(s), "
                  f"{prof['executed']} executed"
                  + (f" - {prof['reason']}" if prof.get("reason") else "")
                  + (f" - ERROR {prof['error']}" if prof.get("error") else ""))
    _report(summary, args, cycle)
    return summary, 0 if summary["ok"] else 1


def _sleep(seconds):
    """Sleep in slices so a Ctrl-C or a closing market is noticed promptly."""
    end = time.time() + max(0.0, seconds)
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 5.0))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run paper-trading cycles for every armed portfolio.")
    parser.add_argument("--db", default=os.environ.get("PAPER_TRADER_DB")
                        or os.path.join(HERE, "paper_trading_app.db"),
                        help="SQLite file; ignored when a PostgreSQL URL is set")
    parser.add_argument("--force", action="store_true",
                        help="scan even when no account is armed")
    parser.add_argument("--ignore-schedule", action="store_true",
                        help="scan even when no portfolio is due yet")
    parser.add_argument("--loop", action="store_true",
                        help="keep cycling until the market closes or "
                             "--max-hours is reached")
    parser.add_argument("--every-seconds", type=float,
                        default=float(os.environ.get("PAPER_TRADER_EVERY_SECONDS")
                                      or 60),
                        help="how often --loop re-checks whether a portfolio "
                             "is due (default 60)")
    parser.add_argument("--max-hours", type=float,
                        default=float(os.environ.get("PAPER_TRADER_MAX_HOURS")
                                      or 5.0),
                        help="give up --loop after this many hours so the run "
                             "stays inside GitHub's 6-hour job limit "
                             "(default 5)")
    parser.add_argument("--max-wait-hours", type=float,
                        default=float(os.environ.get("PAPER_TRADER_MAX_WAIT_HOURS")
                                      or 4.0),
                        help="with --loop, wait up to this long for the market "
                             "to open instead of exiting (default 4)")
    parser.add_argument("--ignore-market-hours", action="store_true",
                        help="run even when the US session is closed "
                             "(testing and manual runs only)")
    parser.add_argument("--json", action="store_true",
                        help="print a JSON summary for every cycle")
    parser.add_argument("--lock-timeout", type=float, default=0.0,
                        help="seconds to wait for another cycle to finish")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    database = "sqlite"
    # 1. PostgreSQL backend when one is configured, plain SQLite otherwise.
    try:
        import pg_compat
        if pg_compat.auto_install(verbose=not args.quiet):
            database = "postgresql"
    except ImportError:
        pass

    started = time.time()
    deadline = started + max(0.0, args.max_hours) * 3600.0
    cycles = []
    exit_code = 0
    closed_at_start = not market_is_open()
    waited = 0.0

    if closed_at_start and not args.ignore_market_hours:
        # The whole point of the gate: outside the session this job touches
        # nothing at all - no scan, no feed entry, no order - and reports
        # success, so the scheduler can call it as often as it likes without
        # waking the database or placing stale orders. A loop that arrives
        # before the bell waits for it instead of wasting the slot, but never
        # beyond --max-wait-hours or its own --max-hours budget.
        wait = seconds_until_open() if args.loop else None
        affordable = (wait is not None and 0 < wait
                       and wait <= args.max_wait_hours * 3600.0
                       and time.time() + wait < deadline)
        if not affordable:
            _nothing_to_do(database, args)
            return 0
        waited = wait
        if not args.quiet:
            print(f"The US market is closed (Mon-Fri 09:30-16:00 ET). Waiting "
                  f"{wait / 60:.0f} min for the 09:30 ET open - nothing is "
                  f"scanned, logged or traded until then.")
        _sleep(wait)
        if not market_is_open():
            _nothing_to_do(database, args)
            return 0

    while True:
        market_open = market_is_open()
        if not market_open and not args.ignore_market_hours:
            if args.loop and not args.quiet:
                print("The US market has closed - finishing this run.")
            break
        if cycles and not args.ignore_market_hours and time.time() >= deadline:
            if not args.quiet:
                print(f"Reached --max-hours ({args.max_hours}h) with the market "
                      f"still open - the next scheduled run takes over.")
            break
        if cycles and args.ignore_market_hours and time.time() >= deadline:
            break

        summary, code = run_tick(args, database=database, cycle=len(cycles) + 1)
        summary["market"] = "open" if market_open else "closed"
        if waited and not cycles:
            summary["waited_seconds"] = round(waited, 1)
        cycles.append(summary)
        exit_code = code if code != 0 else exit_code

        if not args.loop or not market_open:
            break
        if time.time() + max(1.0, args.every_seconds) >= deadline:
            continue                      # the top of the loop ends the run
        _sleep(max(1.0, args.every_seconds))

    # Every tick already printed its own CYCLE_SUMMARY line; add the run summary.
    if cycles:
        _step_summary(cycles[-1], cycles, len(cycles) if len(cycles) > 1 else None)
    return exit_code


def _nothing_to_do(database, args):
    """The green no-op: outside the session the job touches nothing at all."""
    summary = _blank_summary(database)
    summary.update({"ok": True, "market": "closed", "seconds": 0.0,
                    "skipped": "the US market is closed - nothing to do"})
    if not args.quiet:
        print("The US market is closed (Mon-Fri 09:30-16:00 ET). Nothing to "
              "scan, nothing to trade - exiting without touching the database.")
    _report(summary, args, 0, final=True, cycles=0)
    return summary


def _report(summary, args, cycle, final=False, cycles=1, history=None):
    if args.json:
        print("CYCLE_SUMMARY " + json.dumps(summary))
    if final:
        _step_summary(summary, history or [summary], cycle if cycles > 1 else None)


def _step_summary(summary, history, loop_cycles):
    """GitHub Actions renders this in the run summary."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("### Paper trader\n\n")
            if loop_cycles:
                placed = sum(h.get("executed") or 0 for h in history)
                scanned = sum(h.get("scanned") or 0 for h in history)
                fh.write(f"- cycles in this run: **{loop_cycles}**\n")
                fh.write(f"- orders placed: **{placed}**\n")
                fh.write(f"- symbols scanned in total: {scanned}\n")
            else:
                fh.write(f"- database: `{summary['database']}`\n")
                fh.write(f"- armed portfolios: **{summary['armed']}**\n")
                fh.write(f"- due for a cycle: **{summary.get('due', 0)}**\n")
                fh.write(f"- symbols scanned: **{summary['scanned']}**\n")
                fh.write(f"- orders placed: **{summary['executed']}**\n")
            fh.write(f"- market: {summary.get('market', 'unknown')}\n")
            fh.write(f"- took: {summary.get('seconds', 0.0)}s\n")
            if summary.get("skipped"):
                fh.write(f"- skipped: {summary['skipped']}\n")
            for row in summary.get("waiting") or []:
                fh.write(f"- waiting: portfolio `{row['profile_id']}` "
                         f"({row['label']}) is not due yet\n")
            for prof in history:
                for p in prof.get("profiles") or []:
                    fh.write(f"- portfolio `{p['label']}`: {p['decisions']} "
                             f"decision(s), {p['executed']} executed\n")
                if prof.get("error"):
                    fh.write(f"\n**error:** {prof['error']}\n")
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
