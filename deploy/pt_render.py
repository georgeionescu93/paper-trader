"""Small client for the Render API, so the key stays out of command lines.

    py pt_render.py services
    py pt_render.py env <service-id>
    py pt_render.py deploy <service-id>
    py pt_render.py status <service-id> [deploy-id]
    py pt_render.py setenv <service-id> KEY=VALUE [KEY=VALUE...]

Values are never printed: env vars are shown as a key, a length and a short
fingerprint, which is enough to compare against the local secret files.
"""
import hashlib
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(r"C:\Users\iones\Desktop\aplicatie de facut bani")
API = "https://api.render.com/v1"


def key():
    data = (ROOT / "deploy" / "render-key.txt").read_bytes()
    text = data.decode("utf-8", "replace")
    match = re.search(r"rnd_[A-Za-z0-9]+", text)
    if not match:
        raise SystemExit("no Render API key found in deploy/render-key.txt")
    return match.group(0)


def call(method, path, body=None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + key(),
                 "Accept": "application/json",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


def unwrap(payload, field):
    """Render wraps list responses as [{"service": {...}, "cursor": ...}]."""
    if not isinstance(payload, list):
        return []
    out = []
    for item in payload:
        out.append(item[field] if isinstance(item, dict) and field in item
                   else item)
    return out


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()[:10]


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]

    if cmd == "services":
        status, payload = call("GET", "/services?limit=100")
        if status != 200:
            print("HTTP", status, payload)
            return 1
        for svc in unwrap(payload, "service"):
            details = svc.get("serviceDetails") or {}
            print(f"  {svc.get('id')}  {svc.get('name')}  type={svc.get('type')}  "
                  f"autoDeploy={details.get('autoDeploy')}")
            print(f"      url={details.get('url')}  repo={svc.get('repo')}  "
                  f"branch={svc.get('branch')}")
            print(f"      suspended={svc.get('suspended')}  "
                  f"plan={details.get('plan')}  "
                  f"updated={svc.get('updatedAt')}")
        return 0

    if cmd == "env":
        status, payload = call("GET", f"/services/{argv[1]}/env-vars?limit=100")
        if status != 200:
            print("HTTP", status, payload)
            return 1
        rows = []
        for item in unwrap(payload, "envVar"):
            val = item.get("value") or ""
            rows.append((item.get("key"), len(val), fingerprint(val)))
        for name, length, fp in sorted(rows):
            print(f"  {name:<34} len={length:<4} sha={fp}")
        return 0

    if cmd == "deploy":
        status, payload = call("POST", f"/services/{argv[1]}/deploys",
                                {"clearCache": "do_not_clear"})
        if status not in (200, 201, 202):
            print("HTTP", status, payload)
            return 1
        payload = payload or {}
        print("  deploy", payload.get("id", "(accepted)"), "->",
              payload.get("status", "accepted"))
        return 0

    if cmd == "status":
        sid = argv[1]
        did = argv[2] if len(argv) > 2 else None
        for attempt in range(60):
            path = (f"/services/{sid}/deploys/{did}" if did
                    else f"/services/{sid}/deploys?limit=3")
            status, payload = call("GET", path)
            if status != 200:
                print("HTTP", status, payload)
                return 1
            if did:
                dep = payload
            else:
                deps = unwrap(payload, "deploy")
                dep = deps[0] if deps else {}
            print(f"  {dep.get('id')}  {dep.get('status'):<22} "
                  f"created={dep.get('createdAt')}  finished={dep.get('finishedAt')}")
            if dep.get("status") in ("live", "build_failed", "update_failed",
                                     "canceled", "deactivated"):
                return 0 if dep.get("status") == "live" else 1
            time.sleep(10)
        print("  still going after 10 minutes")
        return 1

    if cmd == "setenv":
        sid = argv[1]
        pairs = [a.split("=", 1) for a in argv[2:]]
        for name, value in pairs:
            status, payload = call(
                "PUT", f"/services/{sid}/env-vars/{name}", {"value": value})
            how = "updated"
            if status == 404:
                status, payload = call("POST", f"/services/{sid}/env-vars",
                                       [{"key": name, "value": value}])
                how = "created"
            if status not in (200, 201):
                print(f"  {name}: FAILED HTTP {status} {payload}")
                return 1
            print(f"  {name}: {how} (len={len(value)} sha={fingerprint(value)})")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
