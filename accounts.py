"""Accounts, and per-user ownership of portfolios.

The trading engine and its SQLite schema come from the desktop app. This module
adds the one thing a hosted, multi-user deployment needs on top of them:

  * a `users` table (email + scrypt password hash),
  * a `user_id` on every profile, so a portfolio belongs to somebody,
  * the ownership check that keeps one account's portfolios invisible - and
    untradeable - from another account,
  * per-user Trading Mode arming, so each account arms its own portfolio while
    the server still runs one shared market scan for everybody.

Nothing here touches the trading logic; it only answers "who is this, and what
are they allowed to see and arm".
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

# The account that adopts whatever single-user data already existed. It is
# created from the password the app prints on first start, so the owner can log
# in immediately and find the portfolios they already had.
DEFAULT_OWNER_EMAIL = "owner@localhost"

USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    email              TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash      TEXT NOT NULL,
    created_at         TEXT,
    is_owner           INTEGER NOT NULL DEFAULT 0,
    current_profile_id INTEGER,
    armed              INTEGER NOT NULL DEFAULT 0,
    armed_profile_id   INTEGER,
    armed_label        TEXT,
    armed_timeframe    TEXT,
    last_login_at      TEXT
);
"""

PROFILE_COLUMNS = ("user_id INTEGER",)

# Columns added to `users` after the first release of the accounts layer. Kept
# as ALTERs so an existing database upgrades in place.
USERS_COLUMNS = ("armed_profile_id INTEGER",)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------
def ensure_schema(conn, owner_password_hash=None, owner_email=None,
                  legacy_profile_id=None, legacy_arm=None):
    """Creates the users table and adopts existing data as the owner's.

    Returns the owner's user id (created on first run from the password the
    single-user build had already generated). Safe to call on every start: it
    never overwrites an existing account.
    """
    conn.executescript(USERS_SCHEMA)
    user_cols = _columns(conn, "users")
    for column in USERS_COLUMNS:
        name = column.split()[0]
        if name not in user_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column}")
    profile_cols = _columns(conn, "profiles")
    for column in PROFILE_COLUMNS:
        name = column.split()[0]
        if name not in profile_cols:
            conn.execute(f"ALTER TABLE profiles ADD COLUMN {column}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_profiles_user "
                 "ON profiles(user_id)")

    row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    if row is None:
        if not owner_password_hash:
            conn.commit()
            return None
        cursor = conn.execute(
            "INSERT INTO users (email, password_hash, created_at, is_owner) "
            "VALUES (?, ?, ?, 1)",
            ((owner_email or DEFAULT_OWNER_EMAIL).strip(),
             owner_password_hash, _now()))
        owner = cursor.lastrowid
    else:
        owner = row[0]

    # Anything created before accounts existed belongs to the owner.
    conn.execute("UPDATE profiles SET user_id = ? WHERE user_id IS NULL",
                 (owner,))
    if legacy_profile_id:
        conn.execute("UPDATE users SET current_profile_id = ? WHERE id = ? "
                     "AND (current_profile_id IS NULL OR current_profile_id "
                     "NOT IN (SELECT id FROM profiles WHERE user_id = ?))",
                     (legacy_profile_id, owner, owner))
    if not _user_row(conn, owner)["current_profile_id"]:
        first = conn.execute("SELECT id FROM profiles WHERE user_id = ? "
                             "ORDER BY id DESC LIMIT 1", (owner,)).fetchone()
        if first:
            conn.execute("UPDATE users SET current_profile_id = ? WHERE id = ?",
                         (first[0], owner))
    # The single-user build could be left armed; that choice carries over to
    # the owner only (never to a new account).
    if legacy_arm:
        conn.execute("UPDATE users SET armed = 1, armed_profile_id = ?, "
                     "armed_label = ?, armed_timeframe = ? WHERE id = ?",
                     (legacy_arm.get("profile_id"), legacy_arm.get("label"),
                      legacy_arm.get("timeframe"), owner))
        if legacy_arm.get("profile_id"):
            conn.execute("UPDATE users SET current_profile_id = ? WHERE id = ?",
                         (legacy_arm["profile_id"], owner))
    conn.commit()
    return owner


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------
def _user_row(conn, uid):
    cursor = conn.execute(
        "SELECT id, email, password_hash, created_at, is_owner, "
        "current_profile_id, armed, armed_profile_id, armed_label, "
        "armed_timeframe, last_login_at "
        "FROM users WHERE id = ?", (uid,))
    row = cursor.fetchone()
    if not row:
        return None
    keys = ("id", "email", "password_hash", "created_at", "is_owner",
            "current_profile_id", "armed", "armed_profile_id", "armed_label",
            "armed_timeframe", "last_login_at")
    return dict(zip(keys, row))


def user(conn, uid):
    return _user_row(conn, uid)


def find_by_email(conn, email):
    if not email:
        return None
    row = conn.execute("SELECT id FROM users WHERE email = ? "
                       "COLLATE NOCASE", (str(email).strip(),)).fetchone()
    return _user_row(conn, row[0]) if row else None


def public(user_row):
    """The part of a user row that is safe to send to a browser."""
    if not user_row:
        return None
    return {"id": user_row["id"], "email": user_row["email"],
            "is_owner": int(user_row["is_owner"] or 0),
            "created_at": user_row["created_at"]}


def create_user(conn, email, password_hash, is_owner=False):
    """Adds an account. Raises sqlite3.IntegrityError if the email is taken."""
    cursor = conn.execute(
        "INSERT INTO users (email, password_hash, created_at, is_owner) "
        "VALUES (?, ?, ?, ?)",
        (str(email).strip(), password_hash, _now(), 1 if is_owner else 0))
    conn.commit()
    return cursor.lastrowid


def set_password(conn, uid, password_hash):
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (password_hash, uid))
    conn.commit()


def owner_id(conn):
    row = conn.execute("SELECT id FROM users WHERE is_owner = 1 "
                       "ORDER BY id LIMIT 1").fetchone()
    if row:
        return row[0]
    row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    return row[0] if row else None


def mark_login(conn, uid):
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                 (_now(), uid))
    conn.commit()


def count(conn):
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


# --------------------------------------------------------------------------
# portfolios (profiles)
# --------------------------------------------------------------------------
def owns(conn, uid, pid):
    """True when this portfolio belongs to this user."""
    if not uid or not pid:
        return False
    row = conn.execute("SELECT user_id FROM profiles WHERE id = ?",
                       (int(pid),)).fetchone()
    return bool(row) and row[0] == int(uid)


def profiles_of(conn, uid):
    """This user's portfolios, newest last, in the shape the UI expects."""
    rows = conn.execute(
        "SELECT id, name, currency, balance, auto_mode, total_deposited "
        "FROM profiles WHERE user_id = ? ORDER BY id", (int(uid),)).fetchall()
    return [{"id": r[0], "name": r[1], "currency": r[2],
             "balance": float(r[3] or 0.0), "auto_mode": int(r[4] or 0),
             "total_deposited": float(r[5] or 0.0)} for r in rows]


def profile_ids_of(conn, uid):
    return [p["id"] for p in profiles_of(conn, uid)]


def unique_profile_name(conn, base):
    """A portfolio name nobody is using yet.

    Profile names are unique across the whole database (that is the desktop
    app's own rule), so two accounts both asking for "Main" would collide.
    This appends a counter instead: Main, Main 2, Main 3...
    """
    base = (str(base or "").strip() or "Main")[:60]
    taken = {r[0].lower() for r in conn.execute("SELECT name FROM profiles")}
    if base.lower() not in taken:
        return base
    for n in range(2, 1000):
        candidate = f"{base} {n}"
        if candidate.lower() not in taken:
            return candidate
    return f"{base} {int(datetime.now().timestamp())}"


def claim(conn, uid, pid):
    """Gives an unowned (or already-owned-by-them) portfolio to this user."""
    if owns(conn, uid, pid):
        return True
    row = conn.execute("SELECT user_id FROM profiles WHERE id = ?",
                       (int(pid),)).fetchone()
    if not row or row[0] is not None:
        return False
    conn.execute("UPDATE profiles SET user_id = ? WHERE id = ?",
                 (int(uid), int(pid)))
    conn.commit()
    return True


def current_pid(conn, uid):
    """The portfolio this user is looking at. Falls back to their newest one,
    and repairs the stored pointer when the portfolio was deleted."""
    row = conn.execute("SELECT current_profile_id FROM users WHERE id = ?",
                       (int(uid),)).fetchone()
    owned = profile_ids_of(conn, uid)
    if not owned:
        if row and row[0]:
            conn.execute("UPDATE users SET current_profile_id = NULL "
                         "WHERE id = ?", (int(uid),))
            conn.commit()
        return None
    if row and row[0] in owned:
        return row[0]
    pid = owned[-1]
    set_current_pid(conn, uid, pid)
    return pid


def set_current_pid(conn, uid, pid):
    if not owns(conn, uid, pid):
        return False
    conn.execute("UPDATE users SET current_profile_id = ? WHERE id = ?",
                 (int(pid), int(uid)))
    conn.commit()
    return True


# --------------------------------------------------------------------------
# per-user Trading Mode
# --------------------------------------------------------------------------
def arm(conn, uid, pid, label):
    """Marks this user's portfolio as armed, and remembers it for restarts.

    The portfolio is stored explicitly rather than being read back from
    `current_profile_id`: switching to another portfolio afterwards must not
    silently move what is trading, and a restart must resume the portfolio that
    was actually armed.
    """
    conn.execute("UPDATE users SET armed = 1, armed_profile_id = ?, "
                 "armed_label = ?, armed_timeframe = ?, "
                 "current_profile_id = ? WHERE id = ?",
                 (int(pid), label, label, int(pid), int(uid)))
    conn.commit()
    # Forget any due time left over from an earlier arming, so pressing Trading
    # Mode starts a cycle promptly - the same thing the desktop build does by
    # setting next_at to "now" when it arms. Best-effort: the schedule is
    # advisory state and cycle_state.py is not always present (desktop build).
    try:
        import cycle_state
        cycle_state.reset(pid)
    except Exception:                                       # noqa: BLE001
        pass
    return True


def disarm(conn, uid):
    # Read the portfolio BEFORE clearing it: the schedule to forget belongs to
    # the portfolio this user was trading.
    row = conn.execute("SELECT armed_profile_id FROM users WHERE id = ?",
                       (int(uid),)).fetchone()
    conn.execute("UPDATE users SET armed = 0, armed_profile_id = NULL "
                 "WHERE id = ?", (int(uid),))
    conn.commit()
    try:
        import cycle_state
        cycle_state.reset(row[0] if row else None)
    except Exception:                                       # noqa: BLE001
        pass


def armed_accounts(conn):
    """Every armed account with a portfolio that still exists.

    Used at startup to restore Trading Mode exactly where it was left, so a
    reboot resumes each user's own engine and nobody else's. An account whose
    armed portfolio was deleted is skipped and disarmed, because there is
    nothing left to trade.
    """
    rows = conn.execute(
        "SELECT u.id, u.email, u.armed_profile_id, u.armed_label, "
        "u.armed_timeframe, p.name FROM users u "
        "JOIN profiles p ON p.id = u.armed_profile_id "
        "WHERE u.armed = 1").fetchall()
    out = []
    for uid, email, pid, label, timeframe, name in rows:
        tf = label or timeframe or "Auto (all timeframes)"
        out.append({"user_id": uid, "email": email, "profile_id": pid,
                    "label": tf, "profile_name": name})
    # Clear the flag of anyone whose armed portfolio disappeared.
    conn.execute("UPDATE users SET armed = 0, armed_profile_id = NULL "
                 "WHERE armed = 1 AND armed_profile_id IS NOT NULL "
                 "AND armed_profile_id NOT IN (SELECT id FROM profiles)")
    conn.commit()
    return out
