"""Copies the local SQLite database into the PostgreSQL database.

The app itself never needs this - it creates whatever tables it is missing. This
exists so that the ledger you already have (portfolios, positions, all 31 trades,
the risk state, the accounts) arrives intact, with its row ids preserved so that
current_profile_id / armed_profile_id / profile_id still point at the same rows.

    # PowerShell
    $env:PAPER_TRADER_DB_URL = "postgresql://user:pw@host/db?sslmode=require"
    py migrate_to_postgres.py --check        # read the source, touch nothing
    py migrate_to_postgres.py --apply        # create the schema and copy

What it guarantees
==================
  * the source SQLite file is opened READ-ONLY and is never modified,
  * a non-empty target is refused instead of being mixed with or duplicated
    into (use --append to add anyway, or --drop to start the target over),
  * every table, row count, and the two sums that matter (cash and traded
    value) are compared afterwards, and the script exits non-zero if any of
    them disagrees,
  * identity sequences are reset, so the next INSERT from the app does not
    collide with a copied id.

Columns the app no longer uses (positions.highest_price, atr_at_entry,
trail_at_entry, risk_at_entry exist in old databases only) are reported and
skipped rather than silently dropped.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# The REAL sqlite3 is imported before anything else, because pg_compat will
# later rebind the name "sqlite3" for the app modules. This script needs both:
# real SQLite to read the source, PostgreSQL (through the shim) to write.
import sqlite3                                             # noqa: E402

DEFAULT_SOURCE = os.path.join(HERE, "paper_trading_app.db")

# Copy order respects the foreign keys: positions / risk_state / trades all
# reference profiles. `reset_id` marks tables whose id sequence must be fixed.
TABLES = [
    ("profiles", True),
    ("users", True),
    ("positions", True),
    ("risk_state", False),
    ("trades", True),
    ("settings", False),
    ("engine_log", True),
]
DROP_ORDER = [name for name, _ in reversed(TABLES)]


def fail(message, code=1):
    print(f"\n  ! {message}\n")
    sys.exit(code)


def source_inventory(path):
    if not os.path.exists(path):
        fail(f"source database not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        inventory = {}
        for table, _ in TABLES:
            if table not in tables:
                continue
            columns = [row[1] for row in conn.execute(
                f'PRAGMA table_info("{table}")')]
            count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            inventory[table] = {"columns": columns, "rows": count}
        extra = sorted(tables - {name for name, _ in TABLES}
                       - {"sqlite_sequence"})
        return inventory, extra
    finally:
        conn.close()


def read_rows(path, table, columns):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        quoted = ", ".join(f'"{c}"' for c in columns)
        return conn.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Copy the SQLite database into PostgreSQL.")
    parser.add_argument("--source", default=DEFAULT_SOURCE,
                        help="the SQLite file to read (never modified)")
    parser.add_argument("--check", action="store_true",
                        help="show what would be copied and exit")
    parser.add_argument("--apply", action="store_true",
                        help="create the schema and copy the rows")
    parser.add_argument("--drop", action="store_true",
                        help="DROP the target tables first (destructive!)")
    parser.add_argument("--append", action="store_true",
                        help="copy into a target that already has rows")
    args = parser.parse_args(argv)

    print("=" * 72)
    print("  SQLite  ->  PostgreSQL")
    print("=" * 72)
    inventory, extra = source_inventory(args.source)
    print(f"\n  source : {args.source}")
    for table, info in inventory.items():
        print(f"    {table:12} {info['rows']:>6} row(s)  "
              f"[{len(info['columns'])} columns]")
    if extra:
        print(f"    ! not migrated (unknown to this app): {', '.join(extra)}")

    if not args.check and not args.apply:
        print("\n  Nothing was changed. Re-run with --check or --apply.\n")
        return 0
    if args.check:
        print("\n  --check only: nothing was written.\n")
        return 0

    # ---- from here on, PostgreSQL is the target ---------------------------
    try:
        import pg_compat
    except ImportError:
        fail("pg_compat.py is missing next to this script.")
    url = pg_compat.resolve_url()
    if not url:
        fail("No database URL. Set PAPER_TRADER_DB_URL (or DATABASE_URL) to "
             "the PostgreSQL connection string first.")
    print(f"\n  target : {pg_compat.re.sub(r'://[^@]*@', '://***@', url)}")

    try:
        import psycopg2                                     # noqa: F401
    except ImportError:
        fail("psycopg2 is not installed. Run: pip install psycopg2-binary")

    pg_compat.install(verbose=False)
    import paper_trading_app_TV as app
    import accounts

    # ---- schema -----------------------------------------------------------
    try:
        if args.drop:
            conn = app.get_db_connection()
            try:
                for table in DROP_ORDER:
                    conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
                conn.commit()
                print("\n  dropped the target tables (--drop)")
            finally:
                conn.close()

        app.init_db()                       # profiles/positions/trades/...
        conn = app.get_db_connection()
        try:
            accounts.ensure_schema(conn)    # users + its migrations
        finally:
            conn.close()
        print("  schema is ready")
    except Exception as exc:                                # noqa: BLE001
        fail(f"could not prepare the schema: {type(exc).__name__}: {exc}")

    # ---- is the target empty? --------------------------------------------
    conn = app.get_db_connection()
    try:
        try:
            existing = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
        except Exception:                                   # noqa: BLE001
            existing = 0
        if existing and not (args.append or args.drop):
            conn.close()
            fail(f"the target already has {existing} portfolio(s). Refusing to "
                 f"mix two ledgers. Re-run with --append to add the rows anyway "
                 f"(ids may clash) or --drop to clear the target first.")

        # ---- copy --------------------------------------------------------
        copied, skipped_columns = {}, {}
        for table, reset_id in TABLES:
            info = inventory.get(table)
            if not info or not info["rows"]:
                continue
            target_cols = [row[1] for row in conn.execute(
                f"PRAGMA table_info({table})")]
            if not target_cols:
                print(f"  - {table}: not present in the target, skipped")
                continue
            columns = [c for c in info["columns"] if c in target_cols]
            missing = [c for c in info["columns"] if c not in target_cols]
            if missing:
                skipped_columns[table] = missing
            if not columns:
                continue
            rows = read_rows(args.source, table, columns)
            placeholders = ", ".join("?" for _ in columns)
            statement = (f'INSERT INTO {table} '
                         f'({", ".join(columns)}) VALUES ({placeholders})')
            conn.executemany(statement, rows)
            conn.commit()
            copied[table] = len(rows)
            print(f"  + {table:12} {len(rows):>6} row(s) copied")
        if skipped_columns:
            for table, cols in skipped_columns.items():
                print(f"  ! {table}: columns not in the target were left behind "
                      f"({', '.join(cols)}) - the current app does not use them")

        # ---- identity sequences ------------------------------------------
        for table, reset_id in TABLES:
            if not reset_id or table not in copied:
                continue
            try:
                conn.execute(
                    "SELECT setval(pg_get_serial_sequence(?, 'id'), "
                    f"(SELECT COALESCE(MAX(id), 1) FROM {table}))", (table,))
                conn.commit()
            except Exception as exc:                        # noqa: BLE001
                print(f"  ! could not reset the {table} id sequence "
                      f"({type(exc).__name__}: {exc}); new rows may collide")

        # ---- verify ------------------------------------------------------
        print("\n  verifying:")
        problems = []
        for table, info in inventory.items():
            if table not in copied and info["rows"] == 0:
                continue
            try:
                got = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception as exc:                        # noqa: BLE001
                problems.append(f"{table}: cannot count ({exc})")
                continue
            want = info["rows"]
            mark = "ok  " if got == want else "MISMATCH"
            print(f"    [{mark}] {table:12} source {want:>6}  target {got:>6}")
            if got != want:
                problems.append(f"{table}: {want} -> {got}")

        src = sqlite3.connect(f"file:{args.source}?mode=ro", uri=True)
        try:
            want_cash = src.execute(
                "SELECT COALESCE(SUM(balance), 0), COALESCE(SUM(total_deposited), 0)"
                " FROM profiles").fetchone()
            want_trades = src.execute(
                "SELECT COUNT(*), COALESCE(SUM(total), 0), "
                "COALESCE(SUM(realized_pnl), 0) FROM trades").fetchone()
            want_positions = src.execute(
                "SELECT COUNT(*), COALESCE(SUM(quantity), 0) FROM positions"
            ).fetchone()
        finally:
            src.close()
        got_cash = conn.execute(
            "SELECT COALESCE(SUM(balance), 0), COALESCE(SUM(total_deposited), 0)"
            " FROM profiles").fetchone()
        got_trades = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(total), 0), "
            "COALESCE(SUM(realized_pnl), 0) FROM trades").fetchone()
        got_positions = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(quantity), 0) FROM positions"
        ).fetchone()

        def compare(label, want, got, tolerance=0.01):
            same = all(abs(float(a) - float(b)) <= tolerance
                       for a, b in zip(want, got))
            print(f"    [{'ok  ' if same else 'MISMATCH'}] {label:12} "
                  f"source {tuple(round(float(v), 2) for v in want)}  "
                  f"target {tuple(round(float(v), 2) for v in got)}")
            if not same:
                problems.append(label)

        compare("cash", want_cash, got_cash)
        compare("trades", want_trades, got_trades)
        compare("positions", want_positions, got_positions)
    finally:
        conn.close()

    print()
    if problems:
        fail("migration finished with problems: " + "; ".join(problems))
    print("  Migration verified: every table, row count and total matches.")
    print("  Next: set PAPER_TRADER_ENGINE=off on the web host and let "
          "run_cycle_job.py do the trading.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
