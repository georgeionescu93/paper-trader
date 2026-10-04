"""Proves the hosted-database side works before you trust it with the ledger.

    # PowerShell
    $env:PAPER_TRADER_DB_URL = "postgresql://...neon.tech/neondb?sslmode=require"
    py -m pip install psycopg2-binary
    py verify_free_stack.py

It checks, in order:
  1. the URL and the driver are usable,
  2. the connection the app itself makes,
  3. the schema the app builds (idempotent - safe on a live database),
  4. the SQL the app relies on, translated for PostgreSQL,
  5. that this connection supports the cycle lock, which is what tells you the
     string is the DIRECT one and not a transaction-pooled one,
  6. the two tables the split stack adds (engine_log, cycle_state),
  7. what the scheduled job would do right now (who is armed, who is due).

Nothing here trades or deletes: it only creates missing tables/columns and reads.
Exit code 0 means the whole hosted side is ready.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

FAILS = []


def check(label, condition, detail="", hint=""):
    print(f"  [{'ok  ' if condition else 'FAIL'}] {label}"
          f"{(' -> ' + str(detail)) if detail else ''}")
    if not condition:
        FAILS.append(label)
        if hint:
            print(f"       fix: {hint}")
    return condition


def redact(url):
    return __import__("re").sub(r"://[^@]*@", "://***@", url or "")


def main():
    print("=" * 72)
    print("  Free stack self-check: is the hosted database ready?")
    print("=" * 72)

    print("\n1. the URL and the driver")
    try:
        import pg_compat
    except ImportError:
        check("pg_compat.py is present", False, "run this from the project folder")
        return 1
    url = pg_compat.resolve_url()
    if not check("a PostgreSQL URL is configured", bool(url),
                 hint="set PAPER_TRADER_DB_URL (or DATABASE_URL) to the "
                      "connection string"):
        return 1
    print(f"       {redact(url)}")
    try:
        import psycopg2                                     # noqa: F401
        check("psycopg2 is installed", True)
    except ImportError:
        check("psycopg2 is installed", False,
              hint="py -m pip install psycopg2-binary")
        return 1

    print("\n2. the connection the app makes")
    pg_compat.install(verbose=False)
    import paper_trading_app_TV as app
    import accounts
    import shared_log
    import cycle_state
    try:
        conn = app.get_db_connection()
    except Exception as exc:                                # noqa: BLE001
        check("the app can connect", False, f"{type(exc).__name__}: {exc}")
        return 1
    try:
        check("the app can connect", True)
        version = conn.execute("SELECT version()").fetchone()[0]
        print(f"       {version.split(',')[0]}")
        check("it answers a query", conn.execute("SELECT 1").fetchone()[0] == 1)

        print("\n3. the schema the app builds")
        app.init_db()
        accounts.ensure_schema(conn)
        rows = conn.execute("SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'public' ORDER BY 1").fetchall()
        tables = sorted(r[0] for r in rows)
        print(f"       tables: {', '.join(tables)}")
        for want in ("profiles", "positions", "risk_state", "trades", "settings",
                     "users"):
            check(f"table {want} exists", want in tables)
        counts = {t: conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                  for t in ("profiles", "positions", "trades", "users")}
        check("the accounts table is usable", counts["users"] >= 0, counts)
        print(f"       rows: {counts}")

        print("\n4. the SQL the app relies on")
        traps = [
            ("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
             ("auto_start", "1")),
            ("SELECT id FROM users WHERE email = ? COLLATE NOCASE",
             ("a@b.com",)),
            ("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, "
             "cash REAL)", None),
            ("UPDATE users SET armed = 1 WHERE id = ?", (1,)),
            ("SELECT * FROM positions WHERE profile_id = ? ORDER BY id DESC", (1,)),
        ]
        good = 0
        for sql, params in traps:
            try:
                statement = pg_compat.translate(sql, params)
                good += 1
                print(f"       ok: {statement.kind:10} "
                      f"{' '.join(statement.sql.split())[:58]}")
            except Exception as exc:                        # noqa: BLE001
                check(f"translates: {sql[:44]}", False,
                      f"{type(exc).__name__}: {exc}")
        check("every representative statement translates", good == len(traps),
              f"{good}/{len(traps)}")

        print("\n5. the cycle lock (this is what a pooled URL breaks)")
        key = 0x7061706572
        second = app.get_db_connection()
        try:
            first_got = conn.execute("SELECT pg_try_advisory_lock(?)",
                                     (key,)).fetchone()[0]
            second_got = second.execute("SELECT pg_try_advisory_lock(?)",
                                        (key,)).fetchone()[0]
            check("one process can take the lock", first_got is True, first_got)
            check("a second process is locked out - safe against double trading",
                  second_got is False,
                  hint="this URL is transaction-pooled; use the DIRECT "
                       "connection string from Neon (without -pooler)")
            if second_got:
                second.execute("SELECT pg_advisory_unlock(?)", (key,))
            conn.execute("SELECT pg_advisory_unlock(?)", (key,))
        finally:
            second.close()

        print("\n6. the tables the split stack adds")
        shared_log.ensure_table()
        cycle_state.ensure_table()
        rows = conn.execute("SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'public' AND table_name IN "
                            "('engine_log', 'cycle_state')").fetchall()
        names = {r[0] for r in rows}
        check("engine_log exists (the dashboard's shared activity feed)",
              "engine_log" in names, names)
        check("cycle_state exists (due times for the external job)",
              "cycle_state" in names, names)

        print("\n7. what the scheduled job would do right now")
        armed = accounts.armed_accounts(conn)
        if not armed:
            print("       nothing is armed yet - arm a portfolio in the "
                  "dashboard and the next job tick will trade it")
        else:
            ids = [row["profile_id"] for row in armed]
            due = cycle_state.due_profiles(ids, conn=conn)
            for row in armed:
                mark = "due" if row["profile_id"] in due else "waiting"
                print(f"       portfolio {row['profile_id']} "
                      f"({row['label']}, {row['email']}): {mark}")
            check("the armed portfolios are readable", len(ids) == len(armed))
    finally:
        conn.close()

    print("\n" + "=" * 72)
    if FAILS:
        print(f"NOT READY ({len(FAILS)} problem(s)):")
        for label in FAILS:
            print(f"  - {label}")
        print("\nFix those and run this again before deploying.")
        return 1
    print("READY. This database can host the app:")
    print("  * set DATABASE_URL to it on the web host and in GitHub Actions,")
    print("  * copy your old ledger in, or leave it empty and start fresh:")
    print("      py migrate_to_postgres.py --apply")
    print("  * keep PAPER_TRADER_ENGINE=off on the web host")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
