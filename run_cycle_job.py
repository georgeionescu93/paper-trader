"""Runs exactly ONE trading cycle, for an external scheduler.

WHY THIS FILE EXISTS
====================
The free stack has no process that stays awake: Render's free web service sleeps
when nobody visits, and its filesystem is wiped on every restart. So the trading
loop cannot live in the web process. Instead an external scheduler - GitHub
Actions on a cron, or any pinging service - calls this script every few minutes.
It runs one full cycle and exits:

    python run_cycle_job.py             # one cycle, only if something is armed
    python run_cycle_job.py --force     # scan even with nothing armed
    python run_cycle_job.py --json      # machine-readable summary

It does exactly what Scheduler._tick() does in the web build: read the armed
accounts from the database, scan the market ONCE, and apply that one scan to
every armed portfolio - each with its own cash, positions and rules.

Two safety properties matter more than speed here:

  * A PostgreSQL advisory lock means two schedulers (or a scheduler plus a
    manual cycle pressed in the browser) can never trade at the same time.
  * The process exits non-zero when the cycle fails, so a broken run shows up
    as a red run in GitHub Actions instead of failing silently.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

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


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run one paper-trading cycle for every armed portfolio.")
    parser.add_argument("--db", default=os.environ.get("PAPER_TRADER_DB")
                        or os.path.join(HERE, "paper_trading_app.db"),
                        help="SQLite file; ignored when a PostgreSQL URL is set")
    parser.add_argument("--force", action="store_true",
                        help="scan even when no account is armed")
    parser.add_argument("--ignore-schedule", action="store_true",
                        help="scan even when no portfolio is due yet")
    parser.add_argument("--json", action="store_true",
                        help="print a JSON summary as the last line")
    parser.add_argument("--lock-timeout", type=float, default=0.0,
                        help="seconds to wait for another cycle to finish")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    started = time.time()
    summary = {"ok": False, "armed": 0, "due": 0, "waiting": [], "profiles": [],
               "scanned": 0, "decisions": 0, "executed": 0, "seconds": 0.0,
               "database": "sqlite", "skipped": None, "error": None}

    # 1. PostgreSQL backend when one is configured, plain SQLite otherwise.
    try:
        import pg_compat
        using_pg = pg_compat.auto_install(verbose=not args.quiet)
    except ImportError:
        using_pg = False
    if using_pg:
        summary["database"] = "postgresql"

    import paper_trading_app_TV as app
    import accounts
    import cycle_state
    import engine as engine_mod
    import shared_log

    app.DB_FILE = args.db          # meaningless for PostgreSQL, truthful for SQLite

    if not app.TV_DATA_AVAILABLE:
        summary["error"] = f"market data unavailable: {app.TV_DATA_ERROR}"
        _report(summary, args)
        return 1

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
        _report(summary, args)
        return 1

    # 3. One cycle at a time, across every process that could start one.
    with CycleLock(using_pg, args.lock_timeout) as lock:
        if not lock.held:
            summary["ok"] = True
            summary["skipped"] = "another cycle is already running"
            _report(summary, args)
            return 0

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
            _report(summary, args)
            return 0

        if armed and not entries and not args.ignore_schedule:
            # The normal case for most ticks: armed, but waiting for its own
            # rescan interval. Cheap on purpose - no market data is fetched.
            summary["ok"] = True
            summary["skipped"] = "no portfolio is due yet"
            if not args.quiet:
                for row in summary["waiting"]:
                    print(f"  waiting: portfolio {row['profile_id']} "
                          f"({row['label']}, {row['email']}) is not due yet")
                print("No portfolio is due - nothing to do.")
            _report(summary, args)
            return 0

        eng = engine_mod.TradingEngine()
        eng.set_db_file(args.db)
        source = os.environ.get("PAPER_TRADER_JOB") or "the external scheduler"
        try:
            eng.log(f"\U0001f504 scheduled cycle from {source} "
                    f"({len(entries)} portfolio(s) armed)")
        except Exception:                                   # noqa: BLE001
            pass

        cycle = eng.cycle_count + 1
        try:
            results = eng.run_cycle_many(entries, cycle)
        except Exception as exc:                            # noqa: BLE001
            summary["error"] = f"{type(exc).__name__}: {exc}"
            try:
                eng.log(f"\u274c scheduled cycle failed: {summary['error']}")
            except Exception:                               # noqa: BLE001
                pass
            _report(summary, args)
            return 1

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
    _report(summary, args)
    return 0 if summary["ok"] else 1


def _report(summary, args):
    if args.json:
        print("CYCLE_SUMMARY " + json.dumps(summary))
    # GitHub Actions renders this in the run summary.
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("### Paper trader cycle\n\n")
                fh.write(f"- database: `{summary['database']}`\n")
                fh.write(f"- armed portfolios: **{summary['armed']}**\n")
                fh.write(f"- due for a cycle: **{summary.get('due', 0)}**\n")
                fh.write(f"- symbols scanned: **{summary['scanned']}**\n")
                fh.write(f"- orders placed: **{summary['executed']}**\n")
                fh.write(f"- took: {summary['seconds']}s\n")
                if summary.get("skipped"):
                    fh.write(f"- skipped: {summary['skipped']}\n")
                for row in summary.get("waiting") or []:
                    fh.write(f"- waiting: portfolio `{row['profile_id']}` "
                             f"({row['label']}) is not due yet\n")
                for prof in summary["profiles"]:
                    fh.write(f"- portfolio `{prof['label']}`: "
                             f"{prof['decisions']} decision(s), "
                             f"{prof['executed']} executed\n")
                if summary.get("error"):
                    fh.write(f"\n**error:** {summary['error']}\n")
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
