"""Publishes this folder to a GitHub repository - without git.

Why this exists: the machine has no git and no GitHub CLI, so the usual
"git init && git push" is not available. GitHub's REST "Git Data" API can create
a repository and commit a whole tree in a handful of calls, so the project can be
published with nothing but a token.

    # 1. a token: https://github.com/settings/tokens  (classic, scopes: repo, workflow)
    $env:GITHUB_TOKEN = "ghp_..."

    # 2. look first - this is the default and it changes nothing anywhere:
    py deploy/push-to-github.py

    # 3. publish:
    py deploy/push-to-github.py --go --repo paper-trader --public

    # 4. also set the two Actions secrets, and start one run:
    py deploy/push-to-github.py --go --repo paper-trader --public --secrets ^
        --database-url "postgresql://..." --web-url "https://...onrender.com" --dispatch

The dry run prints every file that would be uploaded AND asserts that the ledger,
the config, your env file and your deployment script are NOT among them. Read
that list before you use --go.

NOT yet exercised against the live API - it needs a real token, which is the one
thing this script cannot provide itself.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
API = "https://api.github.com"

# Never uploaded, whatever .gitignore says: these are the files that must not
# leave this machine.
NEVER = {
    "paper_trading_app.db",
    "paper_trading_app.db-wal",
    "paper_trading_app.db-shm",
    "web_config.json",
    ".env",
    "deploy/cloud-env.txt",
    "deploy/db-url.txt",
    "deploy/gh-token.txt",
    "deploy/my-deployment.ps1",
}


def ignore_rules():
    """The .gitignore patterns, as (pattern, negated) pairs."""
    path = os.path.join(ROOT, ".gitignore")
    rules = []
    if not os.path.exists(path):
        return rules
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            negated = line.startswith("!")
            if negated:
                line = line[1:]
            rules.append((line.replace("\\", "/"), negated))
    return rules


def ignored(rel, rules):
    """Whether a repo-relative posix path is ignored (git's common cases)."""
    if rel in NEVER:
        return True
    name = rel.rsplit("/", 1)[-1]
    parts = rel.split("/")
    verdict = False
    for pattern, negated in rules:
        hit = False
        if pattern.endswith("/"):
            prefix = pattern.rstrip("/")
            hit = rel == prefix or rel.startswith(prefix + "/") or prefix in parts
        elif "/" in pattern:
            hit = fnmatch.fnmatch(rel, pattern)
        else:
            hit = fnmatch.fnmatch(name, pattern) or any(
                fnmatch.fnmatch(part, pattern) for part in parts)
        if hit:
            verdict = not negated
    return verdict


def collect(rules):
    """Every file that should be published, as (relative posix path, bytes)."""
    files, skipped = [], []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for name in sorted(names):
            full = os.path.join(base, name)
            rel = os.path.relpath(full, ROOT).replace("\\", "/")
            if rel == "deploy/cloud-env.txt" or ignored(rel, rules):
                skipped.append(rel)
                continue
            try:
                with open(full, "rb") as fh:
                    files.append((rel, fh.read()))
            except OSError as exc:
                print(f"  ! could not read {rel}: {exc}")
    return files, skipped


def request(method, path, token, payload=None, ok=(200, 201)):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "paper-trader-deploy",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(body).get("message", body)
        except ValueError:
            detail = body
        if exc.code in ok:
            return exc.code, {}
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from None


def seal(public_key, secret):
    """libsodium sealed box, which is what the secrets API wants."""
    from nacl import encoding, public
    key = public.PublicKey(public_key.encode(), encoding.Base64Encoder())
    box = public.SealedBox(key)
    return base64.b64encode(box.encrypt(secret.encode())).decode()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--go", action="store_true",
                        help="actually publish (without it: dry run)")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN")
                        or os.environ.get("GH_TOKEN") or "")
    parser.add_argument("--repo", default="paper-trader")
    parser.add_argument("--owner", default="", help="defaults to the token's user")
    parser.add_argument("--private", action="store_true",
                        help="private repo (Actions minutes are then limited)")
    parser.add_argument("--public", action="store_true", help="public repo (default)")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--message", default="Paper trader with the free split "
                                            "stack (PostgreSQL + external cycles)")
    parser.add_argument("--secrets", action="store_true",
                        help="set DATABASE_URL and WEB_URL in the repository")
    parser.add_argument("--database-url", default=os.environ.get("PAPER_TRADER_DB_URL")
                        or os.environ.get("DATABASE_URL") or "")
    parser.add_argument("--web-url", default="")
    parser.add_argument("--dispatch", action="store_true",
                        help="start one workflow run after pushing")
    args = parser.parse_args(argv)

    rules = ignore_rules()
    files, skipped = collect(rules)
    total = sum(len(blob) for _, blob in files)

    print("=" * 72)
    print("  Publish to GitHub" + ("" if args.go else "   (DRY RUN - nothing is sent)"))
    print("=" * 72)
    print(f"\n  would upload : {len(files)} file(s), {total / 1024:.0f} KB")
    print(f"  would skip   : {len(skipped)} file(s) matched by .gitignore")

    print("\n  NOT uploaded (checked explicitly):")
    for name in sorted(NEVER):
        present = os.path.exists(os.path.join(ROOT, name.replace("/", os.sep)))
        print(f"    [{'excluded' if name not in [f for f, _ in files] else 'LEAKED!!'}]"
              f" {name}" + ("" if present else "   (not present on disk)"))

    print("\n  the files that would go up:")
    for rel, blob in files:
        print(f"    {len(blob):>9,}  {rel}")

    if not args.go:
        print("\n  Dry run only. Add --go (and a --token or $env:GITHUB_TOKEN) to "
              "publish.\n")
        return 0

    if not args.token:
        print("\n  ! No token. Create one at https://github.com/settings/tokens "
              "(classic,\n    scopes: repo + workflow), then set $env:GITHUB_TOKEN "
              "and run again.\n")
        return 1

    status, me = request("GET", "/user", args.token)
    owner = args.owner or me.get("login")
    if not owner:
        print("\n  ! The token was rejected.\n")
        return 1
    print(f"\n  token belongs to : {owner}")

    private = args.private and not args.public
    status, repo = request("POST", "/user/repos", args.token, {
        "name": args.repo, "private": private, "auto_init": False,
        "description": "Paper trading app with free hosting: PostgreSQL ledger, "
                       "GitHub Actions cycles, Render dashboard.",
    }, ok=(201, 422))
    if status == 422:
        print(f"  repository       : {owner}/{args.repo} already exists, using it")
        status, repo = request("GET", f"/repos/{owner}/{args.repo}", args.token)
    else:
        print(f"  repository       : created {owner}/{args.repo} "
              f"({'private' if private else 'public'})")
    html_url = repo.get("html_url", f"https://github.com/{owner}/{args.repo}")
    full = f"{owner}/{args.repo}"

    # The git-data API refuses to create objects in a repository that has no
    # commits at all ("Git Repository is empty"), so put one small file in
    # first; that also creates the branch. The file is .gitignore, which the
    # upload below replaces with the real one, so nothing stray is left behind.
    try:
        request("PUT", f"/repos/{full}/contents/.gitignore", args.token, {
            "message": "Initialise the repository",
            "content": base64.b64encode(
                b"# placeholder, replaced by the real .gitignore\n").decode()})
        print("  repository       : initialised with one commit")
    except RuntimeError as exc:
        print(f"  ! could not initialise the repository: {exc}")

    print(f"\n  uploading {len(files)} file(s) as one commit...")
    tree = []
    for rel, blob in files:
        status, out = request("POST", f"/repos/{full}/git/blobs", args.token,
                             {"content": base64.b64encode(blob).decode(),
                              "encoding": "base64"})
        tree.append({"path": rel, "mode": "100644", "type": "blob",
                     "sha": out["sha"]})
    status, out = request("POST", f"/repos/{full}/git/trees", args.token,
                          {"tree": tree})
    tree_sha = out["sha"]

    parent = None
    try:
        status, ref = request("GET", f"/repos/{full}/git/ref/heads/{args.branch}",
                              args.token)
        parent = ref["object"]["sha"]
    except RuntimeError:
        parent = None

    commit = {"message": args.message, "tree": tree_sha}
    if parent:
        commit["parents"] = [parent]
    status, out = request("POST", f"/repos/{full}/git/commits", args.token, commit)
    commit_sha = out["sha"]

    if parent:
        request("PATCH", f"/repos/{full}/git/refs/heads/{args.branch}", args.token,
                {"sha": commit_sha, "force": True})
    else:
        request("POST", f"/repos/{full}/git/refs", args.token,
                {"ref": f"refs/heads/{args.branch}", "sha": commit_sha})
    print(f"  committed        : {commit_sha[:12]} on {args.branch}")

    # Scheduled workflows only run from the default branch. A freshly created
    # repository can default to "master", which would leave the cycle never
    # firing, so say which branch is the default explicitly.
    try:
        request("PATCH", f"/repos/{full}", args.token,
                {"default_branch": args.branch})
        print(f"  default branch   : {args.branch}")
    except RuntimeError as exc:
        print(f"  ! could not set the default branch: {exc}")

    # Prove GitHub can see the trading loop: a schedule only runs for a workflow
    # file it finds on the default branch.
    try:
        status, found = request("GET", f"/repos/{full}/actions/workflows",
                                args.token)
        listed = [w.get("path") for w in found.get("workflows", [])]
        print(f"  workflows        : {listed or 'none visible'}")
    except RuntimeError as exc:
        print(f"  ! could not list the workflows: {exc}")

    if args.secrets:
        wanted = {}
        if args.database_url:
            wanted["DATABASE_URL"] = args.database_url
        if args.web_url:
            wanted["WEB_URL"] = args.web_url
        if not wanted:
            print("\n  ! --secrets needs --database-url (and --web-url for the "
                  "keep-alive ping)")
        try:
            from nacl import public                            # noqa: F401
            status, key = request(
                "GET", f"/repos/{full}/actions/secrets/public-key", args.token)
            for name, value in wanted.items():
                request("PUT", f"/repos/{full}/actions/secrets/{name}", args.token,
                        {"encrypted_value": seal(key["key"], value),
                         "key_id": key["key_id"]})
                print(f"  secret set       : {name}")
        except ImportError:
            print("\n  ! PyNaCl is needed to set secrets from here:"
                  "\n      py -m pip install pynacl"
                  f"\n    or add them by hand: {html_url}/settings/secrets/actions")

    if args.dispatch:
        try:
            request("POST",
                    f"/repos/{full}/actions/workflows/cycle.yml/dispatches",
                    args.token, {"ref": args.branch})
            print("  workflow         : one run started")
        except RuntimeError as exc:
            print(f"  ! could not start the workflow: {exc}")

    print(f"\n  repository       : {html_url}")
    print("\n  Next, on Render (https://dashboard.render.com):")
    print("    New -> Blueprint -> pick this repository; it reads render.yaml.")
    print("    Fill in the variables marked sync:false from deploy/cloud-env.txt")
    print("    (DATABASE_URL plus the PAPER_TRADER_* lines printed by --print-env).")
    print("\n  Remember to set PAPER_TRADER_REGISTRATION_CODE before sharing the "
          "URL.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
