"""
Web server for the paper-trading app - zero dependencies beyond the app's own.

WHY THE STANDARD LIBRARY
========================
No FastAPI, no Flask, no Starlette. The whole point of this build is that the
user can upload it somewhere and run it with one command, on any Python 3.9+
without a pip install that might fail, conflict or be unavailable on cheap
hosting. http.server's ThreadingHTTPServer is a thread-per-connection server,
which is far more than a single-user trading dashboard needs, and it means the
only requirements stay `pandas` + `websocket-client` (for the trading engine).

WHAT IT DOES
=============
  * serves the single-page app in web/,
  * exposes the JSON API frozen in web/API.md,
  * runs the trading engine on a background thread so it keeps scanning the
    S&P 500 whether or not a browser is open - closing the tab must never stop
    it, and that is the main difference from the desktop build,
  * gates everything behind a password with signed session cookies.

SECURITY
========
This listens on 127.0.0.1 by default. Exposing it to the internet is a
deliberate act: pass --host 0.0.0.0 and put it behind a TLS reverse proxy
(Caddy/nginx/Cloudflare). Plain HTTP over the open internet would expose the
session cookie. The password is hashed with scrypt and stored only as a hash;
sessions are HMAC-signed with `secrets.compare_digest` verification, and login
attempts are rate-limited.

Run
===
    py app_web.py                     # http://127.0.0.1:8080
    py app_web.py --host 0.0.0.0      # reachable from your phone on the LAN
    py app_web.py --port 9000
"""

import argparse
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# ---------------------------------------------------------------------------
# Database backend, chosen before anything imports sqlite3.
#
# When PAPER_TRADER_DB_URL (or DATABASE_URL) points at PostgreSQL, `sqlite3` is
# rebound to pg_compat, so this file, accounts.py, engine.py and the frozen
# trading engine all speak PostgreSQL without one edit to their SQL. With no
# URL set this is a no-op and the app keeps using SQLite exactly as before.
# ---------------------------------------------------------------------------
try:
    import pg_compat
    POSTGRES = pg_compat.auto_install(verbose=False)
except ImportError:                                         # pragma: no cover
    pg_compat = None
    POSTGRES = False

import sqlite3            # the shim when PostgreSQL is on, sqlite3 otherwise

import accounts                                   # noqa: E402
import engine as engine_mod                      # noqa: E402
import paper_trading_app_TV as app               # noqa: E402

# ---------------------------------------------------------------------------
# Where Ollama lives. The built-in default (http://localhost:11434) assumes
# Ollama runs on the same machine as the website, which is true locally but
# usually false on a VPS that points at a separate inference box. These are
# module globals read at call time by query_ollama_for_analysis and
# list_ollama_models, so overriding them here is enough - no edit to the
# desktop module, and the desktop build keeps its own default.
# ---------------------------------------------------------------------------
_OLLAMA_BASE = (os.environ.get("OLLAMA_BASE_URL") or "").strip().rstrip("/")
if _OLLAMA_BASE:
    app.OLLAMA_BASE_URL = _OLLAMA_BASE
    app.OLLAMA_CHAT_URL = _OLLAMA_BASE + "/api/chat"
    app.OLLAMA_TAGS_URL = _OLLAMA_BASE + "/api/tags"

_OLLAMA_TIMEOUT = (os.environ.get("OLLAMA_TIMEOUT_S") or "").strip()
if _OLLAMA_TIMEOUT.isdigit() and int(_OLLAMA_TIMEOUT) > 0:
    app.OLLAMA_TIMEOUT_S = int(_OLLAMA_TIMEOUT)

# ---------------------------------------------------------------------------
# Optional OpenAI-compatible endpoint. With LLM_BASE_URL set, the analysis runs
# against that API instead of Ollama, so the app works on a server that has no
# local models. The prompt is still built by the desktop module, so the model
# sees the same instructions either way.
# ---------------------------------------------------------------------------
import cloud_llm                                   # noqa: E402

CLOUD_LLM = cloud_llm.config()


def list_models():
    """The model names the configured backend offers."""
    if cloud_llm.enabled():
        return cloud_llm.list_models()
    return app.list_ollama_models()


def _install_cloud_analysis():
    """Points the desktop module's analysis call at the cloud endpoint."""
    if not cloud_llm.enabled():
        return False

    def cloud_query(profile_name, currency, cash, holdings, candidate_prices,
                    model, web_context=None, technical_indicators=None,
                    lens="general"):
        prompt = app.build_analysis_prompt(
            profile_name, currency, cash, holdings, candidate_prices,
            web_context=web_context, technical_indicators=technical_indicators,
            lens=lens)
        try:
            text = cloud_llm.chat(prompt, model=CLOUD_LLM["model"] or model)
        except Exception as exc:                        # noqa: BLE001
            # No type prefix: run_analysis adds it, and "RuntimeError:
            # RuntimeError: ..." reads like two different problems.
            return [], str(exc)
        # The caller expects a LIST (the Ollama path returns
        # _parse_model_json(content)), and the same defensive parse is used here
        # so a markdown fence, stray prose or a {"recommendations": [...]}
        # wrapper cannot silently turn an answer into "no suggestions".
        return app._parse_model_json(text), None

    app.query_ollama_for_analysis = cloud_query
    return True


CLOUD_ANALYSIS = _install_cloud_analysis()


WEB_DIR = os.path.join(HERE, "web")
# web_config.json holds the password hash, the session secret and the
# auto-start choice. On a container it must NOT live inside the image (a
# recreate would silently reset the password and forget that the engine was
# running), so its location is overridable.
CONFIG_FILE = os.environ.get("PAPER_TRADER_CONFIG") or os.path.join(
    HERE, "web_config.json")
# The database is the whole account: balance, positions, ledger, risk state.
# Its location is overridable so a container or a systemd unit can keep it on a
# volume instead of inside the deploy directory (a container recreate would
# otherwise start from an empty account without saying so).
DB_FILE = os.environ.get("PAPER_TRADER_DB") or os.path.join(
    HERE, "paper_trading_app.db")

SESSION_COOKIE = "pt_session"
SESSION_DAYS = 30
STATE_CACHE_S = 2.0          # collapses bursty polling into one price fetch
LOGIN_MAX_ATTEMPTS = 8       # per client, per window
LOGIN_WINDOW_S = 300


# ===========================================================================
# 1. CONFIG: password hash + session secret
# ===========================================================================
def _scrypt(password, salt, n=2 ** 14, r=8, p=1):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt,
                          n=n, r=r, p=p, dklen=32)


def hash_password(password):
    """scrypt with a random salt, stored as salt$hash. Memory-hard, stdlib."""
    salt = secrets.token_bytes(16)
    return salt.hex() + "$" + _scrypt(password, salt).hex()


def verify_password(password, stored):
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    # Constant-time: never leak how much of the hash matched.
    return hmac.compare_digest(_scrypt(password, salt), expected)


def load_config():
    """Reads web_config.json, creating it (and a password) on first run.

    On a host with an ephemeral filesystem - Render's free tier, for instance -
    this file is wiped on every restart, which would rotate the owner password
    and invalidate every session each time the service wakes up. Setting
    PAPER_TRADER_PASSWORD_HASH and PAPER_TRADER_SESSION_SECRET makes the file
    unnecessary, which is what the free stack does.
    """
    env_hash = (os.environ.get("PAPER_TRADER_PASSWORD_HASH") or "").strip()
    env_secret = (os.environ.get("PAPER_TRADER_SESSION_SECRET") or "").strip()
    if env_hash and env_secret:
        return {"password_hash": env_hash,
                "session_secret": env_secret,
                "auto_start": False,
                "auto_start_timeframe": "Auto (all timeframes)",
                "from_env": True}, False

    if os.path.exists(CONFIG_FILE):
        try:
            with io.open(CONFIG_FILE, encoding="utf-8") as fh:
                cfg = json.load(fh)
            if cfg.get("password_hash") and cfg.get("session_secret"):
                return cfg, False
        except (ValueError, OSError) as exc:
            print(f"  ! web_config.json is unreadable ({exc}); recreating it.")

    password = secrets.token_urlsafe(12)
    cfg = {
        "password_hash": hash_password(password),
        "session_secret": secrets.token_hex(32),
        # OFF until the user arms it. Auto-starting Trading Mode on a fresh
        # install would mean a brand-new deployment starts buying and selling
        # shares the moment it boots, before anyone has even logged in. The
        # flag exists to remember the user's own Start/Stop decision across
        # reboots and container recreates, not to make that decision for them.
        "auto_start": False,
        "auto_start_timeframe": "Auto (all timeframes)",
    }
    with io.open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass
    return cfg, password


def save_config(cfg):
    with io.open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


# ===========================================================================
# 2. SESSIONS
# ===========================================================================
class Sessions:
    """Stateless signed cookies: <expiry>.<user_id>.<hmac>. The user id is
    inside the signature, so a cookie cannot be replayed as somebody else, and
    no server-side store is needed - a restart never logs anyone out."""

    def __init__(self, secret):
        self.secret = secret.encode("utf-8")

    def issue(self, user_id, now=None):
        expiry = int((now or time.time()) + SESSION_DAYS * 86400)
        payload = f"{expiry}.{int(user_id)}"
        sig = hmac.new(self.secret, payload.encode("ascii"),
                       hashlib.sha256).hexdigest()
        return f"{payload}.{sig}"

    def valid(self, token, now=None):
        """Returns the user id a valid cookie belongs to, or None."""
        if not token or token.count(".") != 2:
            return None
        expiry, _, rest = token.partition(".")
        user_id, _, sig = rest.partition(".")
        payload = f"{expiry}.{user_id}"
        expected = hmac.new(self.secret, payload.encode("ascii"),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            if int(expiry) <= (now or time.time()):
                return None
            return int(user_id)
        except ValueError:
            return None


class LoginThrottle:
    """Simple per-client attempt counter, to make brute force impractical."""

    def __init__(self):
        self._lock = threading.Lock()
        self._hits = {}

    def blocked(self, client):
        with self._lock:
            hits = [t for t in self._hits.get(client, [])
                    if time.time() - t < LOGIN_WINDOW_S]
            self._hits[client] = hits
            return len(hits) >= LOGIN_MAX_ATTEMPTS

    def record(self, client):
        with self._lock:
            self._hits.setdefault(client, []).append(time.time())

    def clear(self, client):
        with self._lock:
            self._hits.pop(client, None)


# ===========================================================================
# 3. THE CONTINUOUS SCHEDULER
# ===========================================================================
class Scheduler(threading.Thread):
    """Drives the engine's scan cycle on its own thread, forever.

    This is what makes the web build different from the desktop build: the
    loop lives in the server process, not in a window, so it keeps trading
    while every browser is closed. The thread is a daemon so Ctrl+C and normal
    interpreter exit are never blocked by a long scan in flight.
    """

    def __init__(self, engine, config):
        super().__init__(name="trading-scheduler", daemon=True)
        self.engine = engine
        self.config = config
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.next_cycle_at = None
        self._lock = threading.Lock()
        self._generation = 0        # bumped to abandon an in-flight wait
        self._armed = {}            # profile_id -> armed portfolio

    # -- control -------------------------------------------------------------
    def arm(self, profile_id, label, user_id, tag=""):
        """Arms one portfolio for its owner.

        Several accounts can be armed at the same time: the loop scans the
        market once per cycle and applies that one scan to every armed
        portfolio, each with its own rules, cash and positions.
        """
        profile_id = int(profile_id)
        with self._lock:
            # One armed portfolio per account: arming a second one moves the
            # engine instead of running two portfolios for the same person.
            for other in [p for p, e in self._armed.items()
                          if e["user_id"] == int(user_id)
                          and p != profile_id]:
                self._armed.pop(other, None)
            self._armed[profile_id] = {
                "profile_id": profile_id, "label": label,
                "user_id": int(user_id), "tag": tag,
                "next_at": time.time(),
            }
            self.engine.running = True
            self.engine.profile_id = profile_id
            self.engine.label = label
            self.engine.started_at = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S")
            self._generation += 1
        cfg = app.TRADING_TIMEFRAMES.get(label)
        rescan = cfg["rescan_min"] if cfg else 15
        who = f" for {tag}" if tag else ""
        if _engine_is_external():
            # No loop lives in this process, so say where the cycles come from
            # instead of promising that "it runs on the server".
            tail = ("Cycles are executed by the external scheduler against "
                    "this same database.")
        else:
            tail = ("It runs on the server, so closing this page will not "
                    "stop it.")
        self.engine.log(f"\u25b6\ufe0f Trading Mode armed{who} on '{label}' "
                        f"(rescan every {rescan} min, {len(self._armed)} "
                        f"portfolio(s) armed). {tail}")
        self.wake()

    def disarm_user(self, user_id, reason="stopped"):
        """Disarms every portfolio owned by one account. Other accounts keep
        trading."""
        with self._lock:
            gone = [pid for pid, entry in self._armed.items()
                    if entry["user_id"] == int(user_id)]
            for pid in gone:
                self._armed.pop(pid, None)
            if not self._armed:
                self.engine.running = False
                self.next_cycle_at = None
            self._generation += 1
        if gone:
            self.engine.log(f"\u23f9\ufe0f Trading Mode {reason} "
                            f"({len(gone)} portfolio(s)).")
        self.wake()
        return len(gone)

    def disarm_all(self, reason="stopped (server shutting down)"):
        with self._lock:
            count = len(self._armed)
            self._armed.clear()
            self.engine.running = False
            self.next_cycle_at = None
            self._generation += 1
        if count:
            self.engine.log(f"\u23f9\ufe0f Trading Mode {reason}.")
        self.wake()
        return count

    def armed_entries(self):
        with self._lock:
            return [dict(entry) for entry in self._armed.values()]

    def armed_profiles(self):
        with self._lock:
            return sorted(self._armed)

    # Kept for the single-portfolio call sites: start == arm, stop == disarm.
    def start_engine(self, profile_id, label, user_id=0, tag=""):
        return self.arm(profile_id, label, user_id, tag)

    def stop_engine(self, reason="stopped"):
        return self.disarm_all(reason)

    def wake(self):
        self._wake.set()

    def shutdown(self):
        self._stop.set()
        self.wake()

    # -- the loop ------------------------------------------------------------
    def run(self):
        # The body is wrapped so that NO single failure can end the loop: if
        # this thread dies, Trading Mode silently stops and the server looks
        # perfectly healthy while nothing is trading. That is the worst
        # possible failure mode for an unattended deployment, so every cycle is
        # isolated and a broken one backs off instead of killing the thread.
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:                    # noqa: BLE001
                self.engine.last_error = f"{type(exc).__name__}: {exc}"
                self.engine.log(f"\u274c scheduler error (retrying in 60s): "
                                f"{self.engine.last_error}")
                sys.stderr.write("".join(traceback.format_exc()))
                with self._lock:
                    for entry in self._armed.values():
                        entry["next_at"] = max(entry["next_at"],
                                               time.time() + 60)
                if self._wake.wait(5.0):
                    self._wake.clear()

    def _tick(self):
        with self._lock:
            entries = [dict(e) for e in self._armed.values()]
            generation = self._generation
        if not entries:
            self._wake.wait(1.0)
            self._wake.clear()
            return

        now = time.time()
        due = [e for e in entries if e["next_at"] <= now]
        if not due:
            soonest = min(e["next_at"] for e in entries)
            # Sleep in short slices so arming/disarming is honoured
            # promptly instead of after a 15-minute wait.
            if self._wake.wait(max(0.5, min(30.0, soonest - now))):
                self._wake.clear()
            return

        cycle = self.engine.cycle_count + 1
        results = self.engine.run_cycle_many(due, cycle)
        self._reschedule(due, results, generation, cycle)
        for res in results.get("profiles", []):
            self._publish_decisions(res)

    def _reschedule(self, due, results, generation, cycle):
        """Books the next cycle for every armed portfolio that is still
        armed, and forgets the ones whose profile disappeared."""
        with self._lock:
            if generation != self._generation:
                # The armed set changed while the cycle was running; whatever
                # survived is due again immediately.
                for entry in due:
                    if entry["profile_id"] in self._armed:
                        self._armed[entry["profile_id"]]["next_at"] = time.time()
                return
            for res in results.get("profiles", []):
                pid = res.get("profile_id")
                entry = self._armed.get(pid)
                if entry is None:
                    continue
                if res.get("reason") == "profile no longer exists":
                    self._armed.pop(pid, None)
                    continue
                cfg = app.TRADING_TIMEFRAMES.get(entry["label"])
                rescan = cfg["rescan_min"] if cfg else 15
                entry["next_at"] = time.time() + rescan * 60
            self.engine.cycle_count = cycle
            if self._armed:
                self.next_cycle_at = min(e["next_at"]
                                         for e in self._armed.values())
            else:
                self.next_cycle_at = None
                self.engine.running = False

    def _publish_decisions(self, result):
        """Puts this cycle's decisions in the recommendations table.

        The desktop build fills its "AI Recommendations & Execution" tree from
        every Trading Mode cycle, labelling each row "BUY (new)" or
        "SELL (exit)" so the direction is unmistakable. The web build reads the
        same table through /api/state, so a cycle has to publish its rows here;
        without this the browser would only ever see the AI's suggestions and
        the continuous engine would look silent.
        """
        if not isinstance(result, dict) or "decisions" not in result:
            return
        state = getattr(self, "app_state", None)
        if state is None:
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for decision in result.get("decisions") or []:
            action = str(decision.get("action") or "")
            kind = decision.get("kind")
            if kind == "exit":
                action = "SELL (exit)"
            elif kind == "entry":
                action = "BUY (new)"
            rows.append({
                "action": action,
                "symbol": decision.get("symbol") or "",
                "quantity": decision.get("quantity"),
                "amount": decision.get("amount"),
                "reason": decision.get("reason") or "",
                "kind": kind,
                "created_at": stamp,
            })
        # An empty list is meaningful: like the desktop tree it CLEARS the
        # table, which is how "the scan found nothing worth doing" is shown.
        # Rows are stored per portfolio, so one account never sees another's.
        state.set_recommendations(rows, result.get("profile_id"))

    def next_in(self):
        with self._lock:
            if not self._armed or not self.next_cycle_at:
                return None
            return max(0, int(self.next_cycle_at - time.time()))


# ===========================================================================
# 4. APPLICATION STATE (what /api/state returns)
# ===========================================================================
class WebApp:
    """Owns the engine, the scheduler and the HTTP-facing state assembly."""

    def __init__(self, db_file=DB_FILE, config=None):
        self.config = config or load_config()[0]
        self.db_file = db_file
        self.engine = engine_mod.TradingEngine()
        self.engine.set_db_file(db_file)
        self.sessions = Sessions(self.config["session_secret"])
        self.throttle = LoginThrottle()
        self.scheduler = Scheduler(self.engine, self.config)
        self.scheduler.app_state = self   # lets a cycle publish its rows
        self._state_lock = threading.Lock()
        self._state_cache = {}            # user id -> (built at, payload)
        self._analysis_lock = threading.Lock()
        self.analysis = {"running": False, "last_at": None, "last_error": None}
        self._recommendations = {}        # profile id -> rows
        self._recs_lock = threading.Lock()
        self.owner_id = None
        self.current_profile_id = None    # the owner's portfolio

    # -- accounts ------------------------------------------------------------
    def connect(self):
        """A short-lived connection to the app database."""
        return app.get_db_connection()

    def init_accounts(self):
        """Creates the users table and adopts pre-accounts data as the owner's.

        Called once at startup, before the scheduler runs. Everything that
        existed under the single-user build (the printed password, the
        portfolios) ends up on the owner account, so nothing is lost.
        """
        conn = self.connect()
        try:
            # The one-time migration of the old single-user "auto_start" flag.
            # Resuming is what the always-on deployment is for, but this flag
            # predates accounts and was written by the old build, so it is NOT
            # trusted silently: the upgrade starts idle unless the operator asks
            # for the resume, and the notice below says how. Every arming
            # decision made from now on lives in the database and IS resumed.
            legacy_arm = None
            if self.config.get("auto_start"):
                if _legacy_resume_wanted():
                    legacy_arm = {
                        "label": self.config.get("auto_start_timeframe"),
                        "timeframe": self.config.get("auto_start_timeframe"),
                        "profile_id": self.config.get("auto_start_profile_id"),
                    }
                else:
                    self.legacy_arm_notice = (
                        f"web_config.json still says auto_start for portfolio "
                        f"{self.config.get('auto_start_profile_id')}; the "
                        f"upgrade starts IDLE. Press Trading Mode in the page, "
                        f"or start with PAPER_TRADER_RESUME_ARM=1, to resume it.")
            owner = accounts.ensure_schema(
                conn,
                owner_password_hash=self.config.get("password_hash"),
                owner_email=(os.environ.get("PAPER_TRADER_OWNER_EMAIL")
                             or self.config.get("owner_email")),
                legacy_profile_id=self.config.get("auto_start_profile_id"),
                legacy_arm=legacy_arm)
            self.owner_id = owner
            self.current_profile_id = (accounts.current_pid(conn, owner)
                                       if owner else None)
            if self.current_profile_id:
                self.engine.profile_id = self.current_profile_id
            return owner
        finally:
            conn.close()

    # -- startup -------------------------------------------------------------
    def start(self):
        """Starts the in-process trading loop, unless it is switched off.

        PAPER_TRADER_ENGINE=off is how the split deployment works: the web host
        may sleep and lose its disk at any moment, so the cycles are executed by
        an external scheduler (run_cycle_job.py on GitHub Actions) against the
        same database, while this process only serves the dashboard.
        """
        external = _engine_is_external()
        if not external:
            self.scheduler.start()
        resumed = []
        conn = self.connect()
        try:
            for entry in accounts.armed_accounts(conn):
                label = entry["label"]
                if label not in app.TRADING_TIMEFRAMES:
                    label = "Auto (all timeframes)"
                # Armed in BOTH modes. When the engine is external this process
                # runs no loop, but the dashboard's "Trading Mode" state has to
                # reflect the database - otherwise every restart of a sleeping
                # web host would show an actively traded portfolio as idle.
                self.scheduler.arm(entry["profile_id"], label,
                                   entry["user_id"], entry["email"])
                resumed.append(f"{entry['email']} -> {entry['profile_name']}")
        finally:
            conn.close()
        if external:
            self.engine.log(
                "\u23f8\ufe0f In-process scheduler is OFF "
                "(PAPER_TRADER_ENGINE=off). Cycles run from the external "
                "scheduler against this same database"
                + (f"; {len(resumed)} portfolio(s) are armed and waiting: "
                   + "; ".join(resumed) if resumed else
                   "; nothing is armed yet") + ".")
            return resumed
        if resumed:
            # Armed means armed: an account that left Trading Mode running gets
            # it back after a reboot, and the log names every one of them.
            self.engine.log("\U0001f504 Trading Mode resumed at startup for: "
                            + "; ".join(resumed))
        else:
            self.engine.log("\U0001f504 Trading Mode is idle - nothing is "
                            "armed. Arm it from the page when you want it to "
                            "trade.")
        return resumed

    def stop(self):
        self.scheduler.disarm_all("stopped (server shutting down)")
        self.scheduler.shutdown()

    # -- recommendations (per portfolio, never global) -----------------------
    def set_recommendations(self, recs, profile_id=None):
        with self._recs_lock:
            self._recommendations[int(profile_id or 0)] = list(recs or [])

    def get_recommendations(self, profile_id=None):
        with self._recs_lock:
            # Engine decisions are the primary source; AI suggestions fill in
            # when the engine has not produced any this cycle.
            return list(self._recommendations.get(int(profile_id or 0), []))

    # -- the master snapshot -------------------------------------------------
    def state(self, user_id=None, force=False):
        """One user's dashboard. Never contains another account's rows."""
        with self._state_lock:
            key = int(user_id or 0)
            cached = self._state_cache.get(key)
            if (not force and cached
                    and time.time() - cached[0] < STATE_CACHE_S):
                return cached[1]
            payload = self._build_state(user_id)
            self._state_cache[key] = (time.time(), payload)
            return payload

    def _build_state(self, user_id=None):
        conn = self.connect()
        try:
            user_row = accounts.user(conn, user_id) if user_id else None
            pid = accounts.current_pid(conn, user_id) if user_id else None
            profiles = accounts.profiles_of(conn, user_id) if user_id else []
            # The portfolio that is actually trading for this account. It is
            # stored on the account, so switching the view to another portfolio
            # does not change what the engine is running.
            armed_pid = ((user_row or {}).get("armed_profile_id")
                         if (user_row or {}).get("armed") else None)
        finally:
            conn.close()

        snap = self.engine.profile_snapshot(pid) if pid else None
        risk = self.engine.risk_state(pid) if pid else None
        if risk:
            risk["rules"] = self.engine.risk_rules()

        armed = self.scheduler.armed_profiles()
        engine_state = dict(self.engine.status(),
                            next_cycle_in_s=self.scheduler.next_in(),
                            armed_profiles=armed,
                            your_profile_armed=bool(armed_pid
                                                    and armed_pid in armed),
                            your_armed_profile_id=armed_pid)
        return {
            "ok": True,
            "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "market_open": app.tv.market_is_open() if app.tv else None,
            "engine": engine_state,
            "user": accounts.public(user_row),
            "profiles": profiles,
            "current_profile_id": pid,
            "account": (snap or {}).get("account"),
            "positions": (snap or {}).get("positions", []),
            "risk": risk,
            "recommendations": self.get_recommendations(pid),
            "last_scan": self.engine.last_scan,
            "trading_timeframes": self.engine.timeframe_options(),
            "analysis": dict(self.analysis),
            "auto_mode": next((p["auto_mode"] for p in profiles
                               if p["id"] == pid), 0),
        }

    def invalidate(self):
        with self._state_lock:
            self._state_cache = {}


# ===========================================================================
# 5. AI ANALYSIS (Ollama) - runs off the request thread
# ===========================================================================
def run_analysis(app_state, pid, lens="general", use_web=True, is_current=True):
    eng = app_state.engine
    with app_state._analysis_lock:
        if app_state.analysis["running"]:
            return False
        app_state.analysis["running"] = True
    eng.log(f"\U0001f916 AI analysis started ({lens} lens)...")

    def worker():
        try:
            conn = app.get_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT name, currency, balance FROM profiles "
                               "WHERE id = ?", (pid,))
                prow = cursor.fetchone()
                cursor.execute("SELECT symbol, quantity, avg_price FROM "
                               "positions WHERE profile_id = ?", (pid,))
                holding_rows = cursor.fetchall()
            finally:
                conn.close()
            if not prow:
                raise ValueError("Profile not found.")

            name, currency, cash = prow[0], prow[1], float(prow[2])
            # A LIST of dicts with "symbol" in each one, exactly the shape the
            # desktop build hands to the prompt builder.
            holdings = [{"symbol": s, "quantity": float(q),
                         "avg_price": float(a)} for s, q, a in holding_rows]
            # Holdings first, then the blue-chip watchlist - the same symbol set
            # in the same order the desktop build feeds to the model, so the
            # prompt is identical whichever front end asked for it.
            symbols = [h["symbol"] for h in holdings]
            for candidate in app.CANDIDATE_STOCKS:
                if candidate not in symbols:
                    symbols.append(candidate)

            prices = app.get_stock_prices(symbols) if symbols else {}
            # The desktop injects real computed indicators whenever the lens is
            # technical or combined, and passes None otherwise. Same rule here:
            # an empty dict would look like "indicators were computed".
            indicators = (app.get_technical_indicators(symbols)
                          if lens in ("technical", "combined") and symbols
                          else None)
            model = app.get_setting("ollama_model", "") or app.DEFAULT_MODEL
            used_web = False
            web_context = None
            if use_web:
                try:
                    web_context = app.gather_web_context(symbols, lens=lens)
                    used_web = bool(web_context)
                except Exception:                      # noqa: BLE001
                    web_context = None

            # query_ollama_for_analysis builds the prompt itself; it returns
            # (recommendations, error) and the error must not be swallowed.
            raw_recs, error = app.query_ollama_for_analysis(
                name, currency, cash, holdings, prices, model, web_context,
                technical_indicators=indicators, lens=lens)
            if error:
                raise RuntimeError(error)
            recs = app.parse_recommendations(raw_recs)
            app_state.set_recommendations(recs, pid)
            for r in recs:
                r.setdefault("created_at",
                             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            eng.log(f"\u2705 {len(recs)} suggestion(s) from {model} "
                    f"({lens} lens, {'web + live prices' if used_web else 'live prices only'}).")
            app_state.analysis["last_error"] = None

            # If the profile has auto_mode on, the AI's suggestions go through
            # exactly the same guard rails as the engine's own decisions. Only
            # the portfolio the caller is actually looking at is touched.
            if recs and is_current:
                conn = app.get_db_connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute("SELECT auto_mode FROM profiles WHERE id = ?",
                                   (pid,))
                    row = cursor.fetchone()
                finally:
                    conn.close()
                if row and int(row[0] or 0):
                    eng.profile_id = pid
                    summary = eng.execute_auto_trades(
                        recs, prices, label="\U0001f916 AI auto-trade",
                        risk_state=eng.risk_state(pid), quiet=True,
                        profile_id=pid)
                    if summary["executed"]:
                        eng.log(f"\U0001f916 AI auto-trade executed "
                                f"{summary['executed']} order(s).")
        except Exception as exc:                       # noqa: BLE001
            app_state.analysis["last_error"] = f"{type(exc).__name__}: {exc}"
            eng.log(f"\u274c AI analysis failed: {exc}")
        finally:
            app_state.analysis["running"] = False
            app_state.analysis["last_at"] = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S")
            app_state.invalidate()

    threading.Thread(target=worker, name="ai-analysis", daemon=True).start()
    return True


# ===========================================================================
# 6. HTTP LAYER
# ===========================================================================
class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


class Router:
    """Tiny explicit router: (method, regex) -> handler. No magic, no deps, and
    the route table doubles as the documentation of what the API does."""

    def __init__(self):
        self.routes = []

    def add(self, method, pattern, handler, auth=True):
        self.routes.append((method, re.compile("^" + pattern + "$"),
                            handler, auth))

    def match(self, method, path):
        for m, regex, handler, auth in self.routes:
            if m != method:
                continue
            found = regex.match(path)
            if found:
                return handler, found.groupdict(), auth
        return None, None, None


def json_bytes(obj):
    return json.dumps(obj, default=str).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "PaperTraderWeb/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------------
    def log_message(self, fmt, *args):
        # Default logging writes every request to stderr, which is noisy while
        # the dashboard polls every 5 seconds.
        if self.server.verbose:
            sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status, body, content_type="application/json",
              extra_headers=None):
        if isinstance(body, (dict, list)):
            body = json_bytes(body)
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status=200, extra_headers=None):
        self._send(status, obj, "application/json; charset=utf-8",
                   extra_headers)

    def _error(self, message, status=400):
        self._json({"ok": False, "error": message}, status)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError("Bad Content-Length")
        if length <= 0:
            return {}
        if length > 1_000_000:
            # Refusing without draining would desynchronise a keep-alive
            # connection, so this one answer closes it.
            self.close_connection = True
            raise ApiError("Request body too large", 413)
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError("Body must be JSON")
        if not isinstance(data, dict):
            raise ApiError("Body must be a JSON object")
        return data

    def _cookie(self, name):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:                              # noqa: BLE001
            return None
        morsel = jar.get(name)
        return morsel.value if morsel else None

    def _authed(self):
        """The signed-in user id carried by the cookie, or None."""
        return self.server.state.sessions.valid(self._cookie(SESSION_COOKIE))

    def _client(self):
        return self.client_address[0] if self.client_address else "?"

    # -- dispatch ------------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        try:
            if path.startswith("/api/"):
                self._api(method, path, parsed)
            elif method in ("GET", "HEAD"):
                self._static(path)
            else:
                self._error("Method not allowed", 405)
        except ApiError as exc:
            self._error(exc.message, exc.status)
        except BrokenPipeError:
            pass
        except Exception as exc:                       # noqa: BLE001
            sys.stderr.write("".join(traceback.format_exc()))
            self._error(f"Internal error: {type(exc).__name__}: {exc}", 500)

    # -- API -----------------------------------------------------------------
    def _api(self, method, path, parsed):
        # The body is read BEFORE routing and auth on purpose. On a keep-alive
        # connection (HTTP/1.1 is the default here) an unread body is not
        # discarded: it is parsed as the first line of the NEXT request, so a
        # browser that got a 401/404 on a POST would desynchronise the socket
        # and see "Unsupported method" on the following call.
        body = self._body() if method in ("POST", "DELETE") else {}
        router = self.server.router
        handler, params, auth_required = router.match(method, path)
        if handler is None:
            self._error(f"No such endpoint: {method} {path}", 404)
            return
        # Who is asking. Every handler that touches a portfolio re-checks that
        # the portfolio belongs to this id.
        self.user_id = self._authed()
        if auth_required and not self.user_id:
            self._error("Not authenticated", 401)
            return
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        handler(self, body, params, query)

    # -- static files --------------------------------------------------------
    def _static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        # Reject traversal outright rather than trying to sanitise it.
        relative = os.path.normpath(path.lstrip("/")).replace("\\", "/")
        if relative.startswith("..") or os.path.isabs(relative):
            self._error("Forbidden", 403)
            return
        full = os.path.join(WEB_DIR, relative)
        if not os.path.abspath(full).startswith(os.path.abspath(WEB_DIR)):
            self._error("Forbidden", 403)
            return
        if not os.path.isfile(full):
            if "." not in relative:
                # SPA deep link: serve the shell and let the client route.
                full = os.path.join(WEB_DIR, "index.html")
                if not os.path.isfile(full):
                    self._error("index.html is missing from web/", 500)
                    return
            else:
                self._error(f"Not found: {path}", 404)
                return
        ctype, _enc = mimetypes.guess_type(full)
        if full.endswith(".js"):
            ctype = "application/javascript"
        elif full.endswith(".css"):
            ctype = "text/css"
        elif full.endswith(".svg"):
            ctype = "image/svg+xml"
        with open(full, "rb") as fh:
            data = fh.read()
        # The shell and its assets must never be cached: a stale client bundle
        # against a newer API is a confusing failure mode.
        self._send(200, data, (ctype or "application/octet-stream") +
                   ("; charset=utf-8" if (ctype or "").startswith("text/")
                    or full.endswith(".js") else ""),
                   {"Cache-Control": "no-store"})


# ===========================================================================
# 7. ROUTES
# ===========================================================================
# ---------------------------------------------------------------------------
# Request helpers: who is asking, and which portfolio they may touch
# ---------------------------------------------------------------------------
MIN_PASSWORD_LEN = 8

# E-mail addresses inside log lines, with or without the engine's [brackets].
EMAIL_RE = re.compile(r"[^\[\]\s@]+@[^\[\]\s@.]+(?:\.[^\[\]\s@.]+)+")


def _legacy_resume_wanted():
    """Whether the pre-upgrade auto_start flag may arm an account on the first
    start after the upgrade. Off unless explicitly asked for."""
    return (os.environ.get("PAPER_TRADER_RESUME_ARM") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _registration_code():
    """Empty means open registration. Set PAPER_TRADER_REGISTRATION_CODE (or
    registration_code in the config) to require an invite code instead."""
    return (os.environ.get("PAPER_TRADER_REGISTRATION_CODE")
            or os.environ.get("PT_REGISTRATION_CODE") or "").strip()


def _engine_is_external():
    """True when cycles are run by an external scheduler, not by this process.

    PAPER_TRADER_ENGINE=off is for the split deployment: a host that sleeps,
    restarts or loses its disk cannot be trusted to keep a loop alive, so
    run_cycle_job.py (on GitHub Actions or any cron) does the trading and this
    process only serves the dashboard. Arming a portfolio is a database write
    either way, so the two halves need no extra coordination.
    """
    return (os.environ.get("PAPER_TRADER_ENGINE") or "").strip().lower() in (
        "off", "0", "false", "no", "none", "external")


def _cycle_lock(timeout=0.0):
    """A cross-process lock around a cycle.

    With PostgreSQL this is an advisory lock, so the external scheduler and a
    manual cycle pressed in the browser can never trade at the same moment -
    two simultaneous cycles on the same portfolio would double its orders.
    """
    try:
        import run_cycle_job
        return run_cycle_job.CycleLock(bool(POSTGRES), timeout)
    except Exception:                                       # noqa: BLE001
        class _NoLock:
            held = True

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        return _NoLock()


def _valid_email(value):
    return bool(re.match(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$", value or ""))


def _cookie_header(token, max_age=SESSION_DAYS * 86400):
    if not token:
        return f"{SESSION_COOKIE}=; Path=/; HttpOnly; Max-Age=0"
    return (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={max_age}")


def _user_of(state, uid):
    conn = state.connect()
    try:
        return accounts.public(accounts.user(conn, uid)) if uid else None
    finally:
        conn.close()


def _pid_for(h, wanted=None):
    """The portfolio this request is about, checked against the session user.

    Somebody else's portfolio is reported as missing rather than forbidden, so
    the API never confirms that another account's portfolio exists.
    """
    state = h.server.state
    conn = state.connect()
    try:
        if wanted:
            try:
                pid = int(wanted)
            except (TypeError, ValueError):
                raise ApiError("No such profile", 404)
            if not accounts.owns(conn, h.user_id, pid):
                raise ApiError("No such profile", 404)
            return pid
        pid = accounts.current_pid(conn, h.user_id)
    finally:
        conn.close()
    if not pid:
        raise ApiError("Create a profile first.")
    return pid


def _redact_log(state, lines, uid, me_email=None):
    """Hides other accounts' e-mail addresses in the shared activity log.

    The log is the server's own operational log, so it covers every account's
    cycles, and the engine tags its lines with the account they belong to. The
    addresses are only shown to the account they belong to (and to the owner),
    so one user cannot read another's address - or their trading - out of it.
    """
    if me_email is None:
        conn = state.connect()
        try:
            me = accounts.user(conn, uid) or {}
        finally:
            conn.close()
        me_email = me.get("email")
        if int(me.get("is_owner") or 0):
            return lines
    elif not me_email:
        return lines

    def swap(match):
        found = match.group(0)
        return found if found == me_email else "another account"

    out = []
    for line in lines:
        text = str(line.get("text") or "")
        if "@" in text:
            text = EMAIL_RE.sub(swap, text)
        out.append(dict(line, text=text))
    return out


def build_router():
    r = Router()
    st = lambda h: h.server.state  # noqa: E731

    def route(method, pattern, auth=True):
        def wrap(fn):
            r.add(method, pattern, fn, auth=auth)
            return fn
        return wrap

    # ---- session -----------------------------------------------------------
    @route("GET", r"/api/session", auth=False)
    def session(h, body, params, query):
        user = _user_of(h.server.state, h._authed())
        h._json({"ok": True, "authed": bool(user), "user": user,
                 "registration_open": not _registration_code()})

    @route("POST", r"/api/login", auth=False)
    def login(h, body, params, query):
        state = h.server.state
        client = h._client()
        if state.throttle.blocked(client):
            raise ApiError("Too many attempts. Wait 5 minutes and try again.",
                           429)
        email = str(body.get("email") or "").strip()
        password = str(body.get("password") or "")
        conn = state.connect()
        try:
            user = accounts.find_by_email(conn, email) if email else None
            if user is None and not email:
                # The password the app prints on first start still signs the
                # owner in, without an e-mail address.
                user = accounts.find_by_email(
                    conn, state.config.get("owner_email")
                    or os.environ.get("PAPER_TRADER_OWNER_EMAIL")
                    or accounts.DEFAULT_OWNER_EMAIL)
            # Always hash something: a missing account and a wrong password
            # must take the same time.
            stored = ((user or {}).get("password_hash")
                      or state.config["password_hash"])
            if not verify_password(password, stored) or user is None:
                state.throttle.record(client)
                raise ApiError("Wrong e-mail or password", 401)
            accounts.mark_login(conn, user["id"])
        finally:
            conn.close()
        state.throttle.clear(client)
        token = state.sessions.issue(user["id"])
        state.invalidate()
        h._json({"ok": True, "user": accounts.public(user)},
                extra_headers={"Set-Cookie": _cookie_header(token)})

    @route("POST", r"/api/register", auth=False)
    def register(h, body, params, query):
        """Open self-registration: every account gets its own portfolios.

        Set PAPER_TRADER_REGISTRATION_CODE to require an invite code instead.
        """
        state = h.server.state
        client = h._client()
        if state.throttle.blocked(client):
            raise ApiError("Too many attempts. Wait 5 minutes and try again.",
                           429)
        code = _registration_code()
        if code and str(body.get("code") or "").strip() != code:
            state.throttle.record(client)
            raise ApiError("That invite code is not right.", 403)
        email = str(body.get("email") or "").strip()
        password = str(body.get("password") or "")
        if not _valid_email(email):
            raise ApiError("Enter a valid e-mail address.")
        if len(password) < MIN_PASSWORD_LEN:
            raise ApiError(f"Use at least {MIN_PASSWORD_LEN} characters for "
                           "your password.")
        currency = str(body.get("currency") or "USD").upper()
        if currency not in ("USD", "EUR", "GBP"):
            currency = "USD"
        conn = state.connect()
        try:
            try:
                uid = accounts.create_user(conn, email, hash_password(password))
            except sqlite3.IntegrityError:
                raise ApiError("That e-mail already has an account.", 409)
            # One portfolio to start with, so the dashboard is usable at once.
            # The balance is always 0 - Deposit Cash is how money gets in, the
            # same as the desktop build.
            base = email.split("@")[0] or "Main"
            name = accounts.unique_profile_name(conn, base)
            pid = state.engine.create_profile(name, currency, 0.0)
            accounts.claim(conn, uid, pid)
            accounts.set_current_pid(conn, uid, pid)
            user = accounts.user(conn, uid)
        finally:
            conn.close()
        state.throttle.clear(client)
        token = state.sessions.issue(uid)
        state.invalidate()
        h._json({"ok": True, "user": accounts.public(user),
                 "profile": {"id": pid, "name": name}},
                extra_headers={"Set-Cookie": _cookie_header(token)})

    @route("POST", r"/api/logout", auth=False)
    def logout(h, body, params, query):
        h._json({"ok": True}, extra_headers={
            "Set-Cookie": _cookie_header(None)})

    @route("GET", r"/api/health", auth=False)
    def health(h, body, params, query):
        state = h.server.state
        conn = state.connect()
        try:
            users = accounts.count(conn)
        finally:
            conn.close()
        h._json({"ok": True, "engine_running": state.engine.running,
                 "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "talib": bool(app.TALIB_AVAILABLE),
                 "pandas": bool(app.PANDAS_AVAILABLE),
                 "tv_available": bool(app.TV_DATA_AVAILABLE),
                 "users": users,
                 "registration_open": not _registration_code()})

    # ---- state -------------------------------------------------------------
    @route("GET", r"/api/state")
    def state_endpoint(h, body, params, query):
        h._json(h.server.state.state(h.user_id,
                                     force=query.get("force") == "1"))

    @route("GET", r"/api/log")
    def log_endpoint(h, body, params, query):
        eng = h.server.state.engine
        since = int(query.get("since") or 0)
        lines = _redact_log(h.server.state, eng.log_since(since), h.user_id)
        h._json({"ok": True, "lines": lines,
                 "next": eng.status()["log_seq"]})

    # ---- engine ------------------------------------------------------------
    @route("POST", r"/api/engine/start")
    def engine_start(h, body, params, query):
        """Arms THIS account's portfolio. Other accounts keep their own."""
        state = h.server.state
        pid = _pid_for(h, body.get("profile_id"))
        label = body.get("timeframe") or "Auto (all timeframes)"
        if label not in app.TRADING_TIMEFRAMES:
            raise ApiError(f"Unknown timeframe: {label}")
        conn = state.connect()
        try:
            accounts.set_current_pid(conn, h.user_id, pid)
            accounts.arm(conn, h.user_id, pid, label)
            user = accounts.user(conn, h.user_id)
        finally:
            conn.close()
        # Remember the INTENT, not just the live flag: a reboot or a container
        # recreate must resume the accounts that were trading, and only those.
        state.scheduler.arm(pid, label, h.user_id, (user or {}).get("email", ""))
        state.invalidate()
        h._json({"ok": True,
                 "engine": state.state(h.user_id, force=True)["engine"],
                 "armed_profile_id": pid})

    @route("POST", r"/api/engine/stop")
    def engine_stop(h, body, params, query):
        state = h.server.state
        conn = state.connect()
        try:
            accounts.disarm(conn, h.user_id)
        finally:
            conn.close()
        state.scheduler.disarm_user(h.user_id, "stopped")
        state.invalidate()
        h._json({"ok": True})

    @route("POST", r"/api/engine/cycle")
    def engine_cycle(h, body, params, query):
        """One cycle now, without disturbing the continuous loop."""
        state = h.server.state
        pid = _pid_for(h, body.get("profile_id"))
        label = (body.get("timeframe") or state.engine.label
                 or "Auto (all timeframes)")
        cfg = app.TRADING_TIMEFRAMES.get(label)
        if cfg is None:
            raise ApiError(f"Unknown timeframe: {label}")

        def once():
            lock = _cycle_lock()
            with lock:
                if not lock.held:
                    state.engine.log("\u26a0\ufe0f another cycle is already "
                                     "running (the external scheduler), so "
                                     "this manual cycle was skipped.")
                    return
                state.engine.profile_id = pid
                state.engine.label = label
                result = state.engine.run_cycle_safe(pid, label, cfg,
                                                     state.engine.cycle_count + 1)
                # Publish exactly the way the continuous loop does, so a manual
                # cycle fills the recommendations table with its "BUY (new)" /
                # "SELL (exit)" rows too - the desktop build fills its tree from
                # the result whatever started the cycle.
                state.scheduler._publish_decisions(result)
                state.invalidate()
        threading.Thread(target=once, name="manual-cycle",
                         daemon=True).start()
        h._json({"ok": True})

    # ---- profiles ----------------------------------------------------------
    @route("POST", r"/api/profiles")
    def create_profile(h, body, params, query):
        state = h.server.state
        currency = body.get("currency") or "USD"
        try:
            pid = state.engine.create_profile(
                body.get("name"), currency,
                body.get("initial_balance") or 0.0)
        except sqlite3.IntegrityError:
            raise ApiError("That portfolio name is already taken - try "
                           "another one.", 409)
        except ValueError as exc:
            raise ApiError(str(exc))
        conn = state.connect()
        try:
            if not accounts.claim(conn, h.user_id, pid):
                state.engine.delete_profile(pid)
                raise ApiError("Could not create that portfolio", 500)
            accounts.set_current_pid(conn, h.user_id, pid)
            profile = next((p for p in accounts.profiles_of(conn, h.user_id)
                            if p["id"] == pid), None)
        finally:
            conn.close()
        state.engine.profile_id = pid
        state.invalidate()
        h._json({"ok": True, "profile": profile})

    @route("POST", r"/api/profiles/select")
    def select_profile(h, body, params, query):
        state = h.server.state
        pid = _pid_for(h, body.get("profile_id"))
        conn = state.connect()
        try:
            accounts.set_current_pid(conn, h.user_id, pid)
        finally:
            conn.close()
        state.engine.profile_id = pid
        state.invalidate()
        h._json({"ok": True})

    @route("DELETE", r"/api/profiles/(?P<pid>\d+)")
    def delete_profile(h, body, params, query):
        state = h.server.state
        pid = _pid_for(h, params["pid"])
        conn = state.connect()
        try:
            if pid in state.scheduler.armed_profiles():
                state.scheduler.disarm_user(h.user_id,
                                            "stopped: portfolio deleted")
                accounts.disarm(conn, h.user_id)
            ok = state.engine.delete_profile(pid)
            if ok:
                accounts.current_pid(conn, h.user_id)   # repairs the pointer
        finally:
            conn.close()
        if not ok:
            raise ApiError("No such profile", 404)
        state.invalidate()
        h._json({"ok": True})

    @route("POST", r"/api/profiles/(?P<pid>\d+)/funds")
    def funds(h, body, params, query):
        state = h.server.state
        pid = _pid_for(h, params["pid"])
        try:
            new_balance = state.engine.adjust_funds(
                pid, body.get("amount"), body.get("note"))
        except ValueError as exc:
            raise ApiError(str(exc))
        state.invalidate()
        h._json({"ok": True, "balance": new_balance})

    @route("POST", r"/api/profiles/(?P<pid>\d+)/auto_mode")
    def auto_mode(h, body, params, query):
        """The Auto-Trade switch: lets the AI's own suggestions execute.

        It only ever writes that flag - arming the continuous engine is a
        separate, explicit decision (POST /api/engine/start).
        """
        state = h.server.state
        pid = _pid_for(h, params["pid"])
        ok = state.engine.set_auto_mode(pid, bool(body.get("enabled")))
        state.invalidate()
        h._json({"ok": True, "auto_mode": 1 if body.get("enabled") else 0,
                 "updated": ok})

    # ---- trading -----------------------------------------------------------
    @route("POST", r"/api/trade")
    def trade(h, body, params, query):
        state = h.server.state
        pid = _pid_for(h)
        action = str(body.get("action") or "").upper()
        if action not in ("BUY", "SELL"):
            raise ApiError("action must be BUY or SELL")
        symbol = str(body.get("symbol") or "").strip().upper()
        if not symbol:
            raise ApiError("Symbol is required")
        try:
            qty = float(body.get("quantity") or 0)
        except (TypeError, ValueError):
            raise ApiError("Quantity must be a number")
        if qty <= 0:
            raise ApiError("Quantity must be greater than zero")

        prices = app.get_stock_prices([symbol])
        price = prices.get(symbol, 0.0) or 0.0
        if price <= 0:
            raise ApiError(f"Could not fetch a live TradingView price for "
                           f"{symbol}. Check the symbol.", 502)
        price = app.apply_slippage(price, action)

        state.engine.profile_id = pid
        ok = state.engine.process_trade(action, symbol, qty, price,
                                        reason="Manual trade", profile_id=pid)
        if not ok:
            raise ApiError(f"{action} rejected: insufficient cash or not "
                           f"enough shares held.", 400)
        state.invalidate()
        h._json({"ok": True, "price": price, "quantity": qty,
                 "total": qty * price, "action": action, "symbol": symbol})

    @route("GET", r"/api/history")
    def history(h, body, params, query):
        state = h.server.state
        pid = _pid_for(h, query.get("profile_id"))
        h._json({"ok": True, "trades": state.engine.trade_history(pid)})

    @route("POST", r"/api/history/clear")
    def history_clear(h, body, params, query):
        state = h.server.state
        pid = _pid_for(h, body.get("profile_id"))
        conn = app.get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM trades WHERE profile_id = ?", (pid,))
            deleted = cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        state.invalidate()
        h._json({"ok": True, "deleted": deleted})

    @route("POST", r"/api/history/(?P<tid>\d+)/price")
    def history_price(h, body, params, query):
        state = h.server.state
        tid = int(params["tid"])
        conn = state.connect()
        try:
            row = conn.execute("SELECT profile_id FROM trades WHERE id = ?",
                               (tid,)).fetchone()
            if not row or not accounts.owns(conn, h.user_id, row[0]):
                raise ApiError("No such trade", 404)
        finally:
            conn.close()
        try:
            new_price = float(body.get("price"))
        except (TypeError, ValueError):
            raise ApiError("price must be a number")
        if new_price <= 0:
            raise ApiError("price must be greater than zero")
        ok, error = edit_trade_price(tid, new_price)
        if not ok:
            raise ApiError(error or "Could not correct that trade", 400)
        state.invalidate()
        h._json({"ok": True})

    # ---- AI ----------------------------------------------------------------
    @route("POST", r"/api/analyse")
    def analyse(h, body, params, query):
        state = h.server.state
        pid = _pid_for(h, body.get("profile_id"))
        if state.analysis["running"]:
            raise ApiError("An analysis is already running.", 409)
        lens = str(body.get("lens") or "general")
        if lens not in ("general", "technical", "fundamental", "combined"):
            lens = "general"
        # The "Web Search" checkbox of the desktop build: absent means on.
        use_web = body.get("web")
        use_web = True if use_web is None else bool(use_web)
        conn = state.connect()
        try:
            is_current = accounts.current_pid(conn, h.user_id) == pid
        finally:
            conn.close()
        run_analysis(state, pid, lens, use_web, is_current)
        state.invalidate()
        h._json({"ok": True})

    @route("GET", r"/api/models")
    def models(h, body, params, query):
        available = list_models()
        selected = app.get_setting("ollama_model", "") or ""
        if CLOUD_LLM.get("model") and CLOUD_LLM.get("base"):
            selected = selected or CLOUD_LLM["model"]
        h._json({"ok": True, "models": available, "selected": selected,
                 "engine": "openai-compatible" if CLOUD_LLM.get("base")
                 else "ollama"})

    @route("POST", r"/api/models")
    def set_model(h, body, params, query):
        model = str(body.get("model") or "").strip()
        if not model:
            raise ApiError("model is required")
        app.set_setting("ollama_model", model)
        h._json({"ok": True, "selected": model})

    return r


def edit_trade_price(trade_id, new_price):
    """Corrects a historical trade's price, keeping cash and positions
    consistent: the cash delta is re-applied, the average cost is recomputed
    for BUYs, and the realized P/L is recomputed for SELLs."""
    conn = app.get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT profile_id, action, symbol, quantity, price, "
                       "total FROM trades WHERE id = ?", (trade_id,))
        row = cursor.fetchone()
        if not row:
            return False, "That trade no longer exists."
        pid, action, symbol, qty, old_price, old_total = row
        qty = float(qty or 0.0)
        old_price = float(old_price or 0.0)
        old_total = float(old_total or 0.0)
        if qty <= 0:
            return False, ("That row carries no quantity (it is a cash "
                           "deposit/withdrawal), so it has no price to fix.")
        new_total = qty * new_price
        delta = new_total - old_total

        cursor.execute("SELECT balance, total_deposited FROM profiles "
                       "WHERE id = ?", (pid,))
        prow = cursor.fetchone()
        if not prow:
            return False, "The owning profile no longer exists."
        balance, deposited = float(prow[0]), float(prow[1] or 0.0)
        # A BUY that now costs more must have been affordable; refuse an edit
        # that would leave the account overdrawn rather than silently
        # corrupting the balance.
        new_balance = balance - delta if action == "BUY" else balance + delta
        if new_balance < 0:
            return False, (f"That price would overdraw the account by "
                           f"{abs(new_balance):,.2f}; deposit cash first.")

        cursor.execute("UPDATE profiles SET balance = ? WHERE id = ?",
                       (new_balance, pid))
        cursor.execute("UPDATE trades SET price = ?, total = ? WHERE id = ?",
                       (new_price, new_total, trade_id))

        if action == "BUY":
            # Recompute the average cost of the open lot from its trades.
            cursor.execute("SELECT id, quantity, price FROM trades WHERE "
                           "profile_id = ? AND symbol = ? AND action = 'BUY' "
                           "ORDER BY id", (pid, symbol))
            buys = cursor.fetchall()
            cursor.execute("SELECT id, quantity, price FROM trades WHERE "
                           "profile_id = ? AND symbol = ? AND action = 'SELL' "
                           "ORDER BY id", (pid, symbol))
            sells = cursor.fetchall()
            remaining = sum(float(q or 0) for _i, q, _p in buys) - \
                sum(float(q or 0) for _i, q, _p in sells)
            if remaining > 1e-9:
                # NOTE: the cost must be summed from the price column, which is
                # the third field of each row - unpacking it as `_p` and then
                # using a bare `p` raised NameError and made every BUY
                # correction fail.
                cost = sum(float(q or 0) * float(price_i or 0)
                           for _i, q, price_i in buys) - \
                    sum(float(q or 0) * float(price_i or 0)
                        for _i, q, price_i in sells)
                cursor.execute("UPDATE positions SET avg_price = ? WHERE "
                               "profile_id = ? AND symbol = ?",
                               (cost / remaining, pid, symbol))
        elif action == "SELL":
            # Realized P/L needs the average cost of the lot at sale time.
            cursor.execute("SELECT avg_price FROM positions WHERE profile_id = ? "
                           "AND symbol = ?", (pid, symbol))
            pos = cursor.fetchone()
            avg = float(pos[0]) if pos else None
            if avg is None:
                cursor.execute("SELECT quantity, price FROM trades WHERE "
                               "profile_id = ? AND symbol = ? AND "
                               "action = 'BUY' ORDER BY id", (pid, symbol))
                buys = cursor.fetchall()
                total_qty = sum(float(q or 0) for q, _p in buys)
                avg = (sum(float(q or 0) * float(price_i or 0)
                           for q, price_i in buys) / total_qty
                       ) if total_qty > 0 else 0.0
            cursor.execute("UPDATE trades SET realized_pnl = ? WHERE id = ?",
                           ((new_price - avg) * qty, trade_id))
        conn.commit()
        return True, None
    except Exception as exc:                            # noqa: BLE001
        # Never fail silently: a correction that does nothing looks to the user
        # exactly like a button that is broken.
        sys.stderr.write(f"  ! trade price correction failed for trade "
                         f"{trade_id}: {type(exc).__name__}: {exc}\n")
        sys.stderr.write("".join(traceback.format_exc()))
        conn.rollback()
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


# ===========================================================================
# 8. ENTRY POINT
# ===========================================================================
def enable_wal():
    """Puts SQLite in WAL mode for a server that never stops.

    The HTTP threads read the dashboard while the engine thread writes trades
    into the same file. In the default rollback journal a writer blocks every
    reader (and vice versa), which shows up as "database is locked" on a busy
    cycle; WAL lets readers and one writer proceed at once and survives a crash
    better. The setting lives in the database file, so this is a one-off change
    and it is safe on an existing database.
    """
    try:
        conn = app.get_db_connection()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:                            # noqa: BLE001
        print(f"  ! could not enable WAL mode ({exc}); continuing anyway.")


def _lan_ip():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return None


def print_hosted_env(args):
    """Prints the environment a hosted deployment needs, and exits.

    A host without a permanent disk (Render's free tier) cannot keep
    web_config.json, so the owner's password hash and the session secret have to
    come from the environment. The hash printed here is the one the account
    ALREADY has, so the password you know keeps working after the move - if the
    account does not exist yet, a new one is generated and shown once.
    """
    print("=" * 66)
    print("  Environment for a hosted deployment")
    print("=" * 66)

    cfg = {}
    try:
        with io.open(CONFIG_FILE, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        cfg = {}

    conn = app.get_db_connection()
    try:
        # On a database that does not exist yet this also builds the schema,
        # which is exactly what a fresh hosted database needs before the first
        # cycle runs.
        app.init_db()
        accounts.ensure_schema(conn)
        owner = accounts.user(conn, accounts.owner_id(conn))
    finally:
        conn.close()

    if owner:
        password_hash = owner["password_hash"]
        owner_email = owner["email"]
        password = None
    else:
        password = secrets.token_urlsafe(12)
        password_hash = hash_password(password)
        owner_email = (os.environ.get("PAPER_TRADER_OWNER_EMAIL")
                       or cfg.get("owner_email") or "owner@localhost")

    secret = secrets.token_hex(32)
    print("\n  Copy these into the host's environment settings:\n")
    print(f"  PAPER_TRADER_ENGINE=off")
    print(f"  PAPER_TRADER_PASSWORD_HASH={password_hash}")
    print(f"  PAPER_TRADER_SESSION_SECRET={secret}")
    print(f"  PAPER_TRADER_OWNER_EMAIL={owner_email}")
    print(f"  PAPER_TRADER_CONFIG=/tmp/web_config.json")
    print(f"  DATABASE_URL=postgresql://...   (from Neon/Supabase)")
    print(f"  PAPER_TRADER_REGISTRATION_CODE=  (empty = open sign-up)")
    print(f"  LLM_BASE_URL= / LLM_API_KEY= / LLM_MODEL=   (optional AI)")
    print()
    if password:
        print("  " + "-" * 62)
        print("  OWNER PASSWORD (shown once; there was no account yet):")
        print(f"      {password}")
        print(f"  Sign in as {owner_email} with it.")
        print("  " + "-" * 62)
    else:
        print(f"  The hash above is {owner_email}'s existing one, so the")
        print("  password you already use keeps working on the new host.")
    print()
    return 0


def main(argv=None):
    # When stdout is a pipe - systemd, Docker, or `python app_web.py > log.txt` -
    # Python block-buffers it, so the banner AND the one-time password would sit
    # invisible in the buffer until the process exits. Line buffering makes the
    # first run usable however it was started.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError, OSError):
        pass

    parser = argparse.ArgumentParser(
        description="Paper-trading web app (TradingView data, S&P 500).")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address; 0.0.0.0 exposes it to your LAN "
                             "(default 127.0.0.1, localhost only)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default=DB_FILE)
    parser.add_argument("--verbose", action="store_true",
                        help="log every HTTP request")
    parser.add_argument("--reset-password", metavar="EMAIL", nargs="?",
                        const="", default=None,
                        help="generate a new password for an account (the "
                             "owner's by default) and exit")
    parser.add_argument("--print-env", action="store_true",
                        help="print the environment variables a hosted "
                             "deployment needs (password hash, session secret) "
                             "and exit")
    args = parser.parse_args(argv)

    # Single source of truth for the database path. The locked desktop module
    # keeps its own module-level DB_FILE and app.get_db_connection() reads it,
    # so it MUST be pointed at the chosen database before anything opens a
    # connection - otherwise --reset-password (and every route that uses
    # app.get_db_connection directly) would work on "paper_trading_app.db"
    # relative to the current directory instead of --db / PAPER_TRADER_DB.
    # That is exactly the bug that would bite a container, where the database
    # lives on the /data volume and the working directory is /app.
    # (WebApp.__init__ also does this through engine.set_db_file, which is the
    # method that assigns app.DB_FILE.)
    app.DB_FILE = args.db

    if args.print_env:
        return print_hosted_env(args)

    config, new_password = load_config()
    if args.reset_password is not None:
        password = secrets.token_urlsafe(12)
        password_hash = hash_password(password)
        conn = app.get_db_connection()
        try:
            accounts.ensure_schema(
                conn, owner_password_hash=config.get("password_hash"),
                owner_email=(os.environ.get("PAPER_TRADER_OWNER_EMAIL")
                             or config.get("owner_email")))
            wanted = (args.reset_password or "").strip()
            user = (accounts.find_by_email(conn, wanted) if wanted
                    else accounts.user(conn, accounts.owner_id(conn)))
            if not user:
                print(f"\n  ! no account found for "
                      f"{wanted or 'the owner account'}\n")
                return 1
            accounts.set_password(conn, user["id"], password_hash)
            if int(user["is_owner"] or 0):
                # Keep the password-less owner sign-in in step too.
                config["password_hash"] = password_hash
                save_config(config)
        finally:
            conn.close()
        print(f"\nNew password for {user['email']}: {password}\n")
        return 0

    print("=" * 66)
    print("  Paper Trading web app  -  TradingView data, S&P 500 universe")
    print("=" * 66)
    if not app.TV_DATA_AVAILABLE:
        print(f"  ! TradingView data layer problem: {app.TV_DATA_ERROR}")
    if POSTGRES:
        _safe_url = re.sub(r"://[^@]*@", "://***@", pg_compat.resolve_url())
        print(f"  Database : PostgreSQL  ({_safe_url})")
    else:
        print(f"  Database : {args.db}")
    print("  Engine   : "
          + ("EXTERNAL scheduler (PAPER_TRADER_ENGINE=off) - "
             "run_cycle_job.py does the trading"
             if _engine_is_external() else
             "in-process, keeps running with every browser closed"))
    if _engine_is_external() and not (
            os.environ.get("PAPER_TRADER_PASSWORD_HASH") or "").strip():
        # On a host with an ephemeral disk the config file is rewritten on every
        # boot, so the split deployment is supposed to get this from the
        # environment. Without it a throwaway password is generated - which
        # silently does nothing if the database already has the owner account,
        # because that account keeps the password stored in the database.
        print("  ! PAPER_TRADER_PASSWORD_HASH is not set, so a temporary")
        print("    password was generated for this boot. If this database")
        print("    already has the owner account, it keeps the password stored")
        print("    in the database and the generated one will NOT work. Copy the")
        print("    real values from:  py app_web.py --print-env")
    print(f"  Patterns : {'TA-Lib (61 patterns)' if app.TALIB_AVAILABLE else 'built-in engine'}")
    print(f"  Analysis : "
          + (f"OpenAI-compatible API at {CLOUD_LLM['base']}"
             if CLOUD_ANALYSIS else
             f"Ollama at {app.OLLAMA_BASE_URL}"))

    state = WebApp(db_file=args.db, config=config)
    enable_wal()
    try:
        owner = state.init_accounts()
    except Exception as exc:                            # noqa: BLE001
        print(f"  ! could not prepare the accounts table: "
              f"{type(exc).__name__}: {exc}")
        return 1

    # The activity feed is shared through the database, so the dashboard shows
    # what the external scheduler did while this process was asleep.
    try:
        import shared_log
        shared_log.ensure_table()
        shared_log.install()
    except Exception as exc:                            # noqa: BLE001
        print(f"  ! shared activity log unavailable: "
              f"{type(exc).__name__}: {exc}")

    conn = state.connect()
    try:
        users = accounts.count(conn)
        owner_row = accounts.user(conn, owner) if owner else None
        resynced = False
        if (new_password and owner_row
                and not verify_password(new_password,
                                        owner_row["password_hash"])):
            # The config had no password hash (a first start, or web_config.json
            # was lost or was never on this database's volume) while the
            # database already has an owner. Adopt the password we are about to
            # print: printing one that cannot be used is worse than useless, and
            # anyone able to replace the config could edit the database anyway.
            # The config's own hash is reused so the two never drift apart.
            accounts.set_password(conn, owner_row["id"],
                                  config["password_hash"])
            resynced = True
    finally:
        conn.close()
    print(f"  Accounts : {users} registered - "
          + ("invite code required to sign up" if _registration_code()
             else "anyone with the link can sign up"))
    if owner_row:
        print(f"  Owner    : {owner_row['email']} "
              "(your existing portfolios live here)")
    elif new_password:
        print("  Owner    : created on the first start that has a password")
    notice = getattr(state, "legacy_arm_notice", None)
    if notice:
        print()
        print(f"  ! {notice}")

    if new_password:
        print()
        print("  " + "-" * 60)
        print("  OWNER PASSWORD (shown once, lives hashed in web_config.json):")
        print(f"      {new_password}")
        if resynced:
            print("  This account already existed, so the password above")
            print("  replaces its old one - web_config.json was missing from")
            print("  this database's volume. Nothing else changed.")
        print("  Log in with it and any e-mail field you like.")
        print("  " + "-" * 60)
    print()
    print(f"  Open this in your browser:  http://127.0.0.1:{args.port}")
    if args.host == "0.0.0.0":
        ip = _lan_ip()
        if ip:
            print(f"  From another device on this network:  "
                  f"http://{ip}:{args.port}")
    print()
    print("  The engine runs on this server, so it keeps scanning the S&P 500")
    print("  whether or not a browser is open. It starts IDLE - nothing is")
    print("  armed until an account presses Trading Mode. Press Ctrl+C to stop.")
    print("=" * 66)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.state = state
    httpd.router = build_router()
    httpd.verbose = args.verbose
    httpd.daemon_threads = True

    state.start()

    def bye(_signum, _frame):
        print("\n  Shutting down: stopping the engine and closing the server...")
        state.stop()
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    try:
        signal.signal(signal.SIGINT, bye)
        signal.signal(signal.SIGTERM, bye)
    except (ValueError, OSError):
        pass

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        state.stop()
    finally:
        httpd.server_close()
    print("  Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
