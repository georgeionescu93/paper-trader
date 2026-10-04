#!/usr/bin/env bash
#
# Paper Trader - one-shot setup for an Oracle Cloud "Always Free" VM.
#
# Oracle's free tier is the only free hosting that gives this app what it
# actually needs: a machine that never sleeps, a real disk for SQLite, and
# outbound HTTPS to TradingView. This script turns a fresh Ubuntu instance into
# a running, auto-restarting deployment in about five minutes.
#
# BEFORE YOU RUN THIS, in the Oracle web console:
#   1. Create the instance: shape "VM.Standard.E2.1.Micro" (AMD, always free)
#      or "VM.Standard.A1.Flex" (Ampere ARM, always free, 4 OCPU / 24 GB).
#      Image: Canonical Ubuntu 22.04 or 24.04.
#   2. Save the SSH key it offers you, then connect:
#         ssh ubuntu@<public-ip>
#   3. Networking -> Virtual Cloud Networks -> your VCN -> Security Lists ->
#      Default Security List -> Add Ingress Rules:
#         Source 0.0.0.0/0, IP Protocol TCP, Destination Port 80
#         Source 0.0.0.0/0, IP Protocol TCP, Destination Port 443
#      (Port 22 is already open.) This is the rule people forget: the site is
#      unreachable until BOTH this and the instance firewall below allow it.
#   4. Copy this project to the instance (scp -r, or git clone), then:
#         cd <project>/deploy
#         DOMAIN=trader.yourdomain.com ./oracle-cloud-setup.sh
#
# Variables (all optional except that a DOMAIN is strongly recommended):
#   DOMAIN                   hostname pointing at this VM; Caddy then obtains
#                            a free Let's Encrypt certificate. Without it the
#                            app is served over plain HTTP and your password
#                            would travel in clear text.
#   DUCKDNS_SUBDOMAIN        free DuckDNS name (just the label, e.g. "mytrader"
#   DUCKDNS_TOKEN            for mytrader.duckdns.org, plus the account token
#                            from duckdns.org). With both set, this script
#                            points the name at this VM, keeps it updated every
#                            5 minutes, and uses it as DOMAIN automatically - so
#                            you get HTTPS without owning a domain.
#   OWNER_EMAIL              account that adopts pre-existing portfolios
#                            (default owner@localhost)
#   REGISTRATION_CODE        set a word to stop strangers registering
#                            (default: open registration)
#   LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
#                            OpenAI-compatible endpoint for the AI analysis,
#                            because a free VM cannot run a model locally.
#   APP_DIR                  install directory (default /opt/paper-trader)
#   WITH_TALIB=1             build the container with the full 61-pattern
#                            TA-Lib engine (adds a few minutes to the build)
#
# Run it like this (sudo passes leading VAR=value assignments through):
#   sudo DOMAIN=trader.yourdomain.com ./oracle-cloud-setup.sh
#   sudo DUCKDNS_SUBDOMAIN=mytrader DUCKDNS_TOKEN=xxxx ./oracle-cloud-setup.sh
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/paper-trader}"
DOMAIN="${DOMAIN:-}"
DUCKDNS_SUBDOMAIN="${DUCKDNS_SUBDOMAIN:-}"
DUCKDNS_TOKEN="${DUCKDNS_TOKEN:-}"
OWNER_EMAIL="${OWNER_EMAIL:-owner@localhost}"
REGISTRATION_CODE="${REGISTRATION_CODE:-}"
LLM_BASE_URL="${LLM_BASE_URL:-}"
LLM_API_KEY="${LLM_API_KEY:-}"
LLM_MODEL="${LLM_MODEL:-}"
WITH_TALIB="${WITH_TALIB:-0}"
TZ="${TZ:-Europe/Bucharest}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m  x %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "run this as root:  sudo DOMAIN=... $0"

# ---------------------------------------------------------------------------
say "Base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl rsync iptables-persistent >/dev/null

# ---------------------------------------------------------------------------
say "Docker"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
systemctl enable --now docker >/dev/null 2>&1 || true

if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  die "docker compose is not available"
fi
echo "  using: $COMPOSE"

# ---------------------------------------------------------------------------
say "Instance firewall"
# Oracle images ship an iptables INPUT chain that REJECTs everything except
# SSH. The VCN Security List you set in the console is a SECOND, independent
# layer: both must allow the port or nothing answers.
if command -v iptables >/dev/null 2>&1; then
  for port in 80 443; do
    if iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
      echo "  tcp/$port already allowed"
      continue
    fi
    reject_line="$(iptables -L INPUT --line-numbers -n 2>/dev/null \
      | awk '/REJECT|DROP/ {print $1; exit}')"
    if [ -n "${reject_line:-}" ]; then
      iptables -I INPUT "$reject_line" -p tcp --dport "$port" -j ACCEPT
    else
      iptables -A INPUT -p tcp --dport "$port" -j ACCEPT
    fi
    echo "  tcp/$port allowed"
  done
  netfilter-persistent save >/dev/null 2>&1 || true
else
  warn "iptables not found - make sure 80/443 are open at the OS level too"
fi

# ---------------------------------------------------------------------------
# DuckDNS first: Caddy can only get a certificate once the name resolves here.
say "DNS name"
if [ -n "$DUCKDNS_SUBDOMAIN" ] && [ -n "$DUCKDNS_TOKEN" ]; then
  PUBLIC_IP="$(curl -fsS https://api.ipify.org 2>/dev/null || true)"
  [ -n "$PUBLIC_IP" ] || PUBLIC_IP="$(hostname -I | awk '{print $1}')"
  echo "  this VM's public address: $PUBLIC_IP"
  REPLY="$(curl -fsS "https://www.duckdns.org/update?domains=$DUCKDNS_SUBDOMAIN&token=$DUCKDNS_TOKEN&ip=$PUBLIC_IP" 2>/dev/null || echo KO)"
  if [ "$REPLY" = "OK" ]; then
    echo "  $DUCKDNS_SUBDOMAIN.duckdns.org -> $PUBLIC_IP"
  else
    warn "DuckDNS answered '$REPLY' - check the subdomain and token."
  fi
  # Keep it pointed here even if the VM's address ever changes. The file holds
  # the token, so root-only.
  cat > /etc/cron.d/duckdns <<EOF
# Keeps $DUCKDNS_SUBDOMAIN.duckdns.org pointed at this machine.
*/5 * * * * root curl -fsS "https://www.duckdns.org/update?domains=$DUCKDNS_SUBDOMAIN&token=$DUCKDNS_TOKEN&ip=" >/dev/null 2>&1
EOF
  chmod 600 /etc/cron.d/duckdns
  echo "  a 5-minute update job is installed in /etc/cron.d/duckdns"
  if systemctl list-unit-files 2>/dev/null | grep -q '^cron\.service'; then
    systemctl enable --now cron >/dev/null 2>&1 || true
    systemctl is-active --quiet cron || warn "cron is not running, so the refresh job will not fire (the name stays correct as long as your IP does not change)."
  else
    warn "no cron on this image - update the DuckDNS name yourself if your IP ever changes."
  fi
  if [ -z "$DOMAIN" ]; then
    DOMAIN="$DUCKDNS_SUBDOMAIN.duckdns.org"
    echo "  using it as DOMAIN: $DOMAIN"
  fi
elif [ -n "$DOMAIN" ]; then
  PUBLIC_IP="$(curl -fsS https://api.ipify.org 2>/dev/null || true)"
  echo "  using the DNS name you gave: $DOMAIN"
  if [ -n "$PUBLIC_IP" ]; then
    # Catching a wrong A record here is much friendlier than watching Caddy
    # fail to get a certificate two minutes later.
    RESOLVED="$( (command -v dig >/dev/null 2>&1 && dig +short "$DOMAIN") \
      || getent hosts "$DOMAIN" | awk '{print $1}' || true)"
    RESOLVED="$(printf '%s\n' "$RESOLVED" | tail -1)"
    if [ "$RESOLVED" = "$PUBLIC_IP" ]; then
      echo "  it already points at this VM - good."
    else
      warn "$DOMAIN resolves to '${RESOLVED:-nothing}', not $PUBLIC_IP."
      warn "Point it here first, or the certificate request will fail."
    fi
  fi
else
  warn "No DOMAIN and no DuckDNS settings: the app will only answer on this VM."
  warn "Add DUCKDNS_SUBDOMAIN=myname DUCKDNS_TOKEN=... for free HTTPS."
fi

# ---------------------------------------------------------------------------
say "Installing the app into $APP_DIR"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$SRC_DIR/app_web.py" ] || die "app_web.py not found next to deploy/ - copy the whole project first"
mkdir -p "$APP_DIR/data"
# --delete keeps the install in step with the source, but never touches data/
rsync -a --delete \
  --exclude '.git' --exclude '__pycache__' --exclude 'data' \
  --exclude '*.pyc' --exclude '.venv' \
  "$SRC_DIR"/ "$APP_DIR"/

# ---------------------------------------------------------------------------
say "Writing $APP_DIR/.env"
cat > "$APP_DIR/.env" <<EOF
# Written by deploy/oracle-cloud-setup.sh - edit freely, then:
#   cd $APP_DIR && $COMPOSE up -d
PAPER_TRADER_OWNER_EMAIL=$OWNER_EMAIL
PAPER_TRADER_REGISTRATION_CODE=$REGISTRATION_CODE
LLM_BASE_URL=$LLM_BASE_URL
LLM_API_KEY=$LLM_API_KEY
LLM_MODEL=$LLM_MODEL
TZ=$TZ
EOF
chmod 600 "$APP_DIR/.env"

# ---------------------------------------------------------------------------
say "Building and starting the trader"
cd "$APP_DIR"
if [ "$WITH_TALIB" = "1" ]; then
  $COMPOSE build --build-arg WITH_TALIB=1
fi
$COMPOSE up -d --build

# ---------------------------------------------------------------------------
if [ -n "$DOMAIN" ]; then
  say "TLS for $DOMAIN (Caddy, automatic Let's Encrypt)"
  docker rm -f paper-caddy >/dev/null 2>&1 || true
  docker run -d --name paper-caddy --restart unless-stopped --network host \
    -e DOMAIN="$DOMAIN" \
    -v "$APP_DIR/deploy/Caddyfile:/etc/caddy/Caddyfile:ro" \
    -v paper-caddy-data:/data -v paper-caddy-config:/config \
    caddy:2 >/dev/null
  echo "  Caddy is running; the certificate is fetched on first request."
else
  warn "No DOMAIN given: the app is only reachable on this VM's loopback."
  warn "Re-run with DOMAIN=trader.yourdomain.com for an automatic HTTPS"
  warn "certificate (a free DuckDNS subdomain works fine as the DNS name)."
fi

# ---------------------------------------------------------------------------
say "Waiting for the app to answer"
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8080/api/health >/dev/null 2>&1; then
    echo "  healthy"
    break
  fi
  sleep 2
done

# ---------------------------------------------------------------------------
say "Done"
URL="http://127.0.0.1:8080"
[ -n "$DOMAIN" ] && URL="https://$DOMAIN"
cat <<EOF

  Open:            $URL
  Owner password:  cd $APP_DIR && $COMPOSE logs paper-trader | head -40
                   (printed once, on the very first start; it signs in the
                    $OWNER_EMAIL account, which owns any portfolios that
                    existed before multi-user support)
  Registration:    $( [ -n "$REGISTRATION_CODE" ] && echo "invite code required" || echo "OPEN - anyone with the link can register" )
  Logs:            cd $APP_DIR && $COMPOSE logs -f
  Restart:         cd $APP_DIR && $COMPOSE restart
  Update:          cd $APP_DIR && $COMPOSE up -d --build
  Stop trading:    press "Stop" in the page, or $COMPOSE down

  Trading Mode starts IDLE and survives reboots: whoever presses Trading Mode
  is resumed on the next start, and only they are.

  If the page does not load, check the two firewalls in this order:
    1. the VCN Security List (Oracle console) allows 80 and 443
    2. this instance's iptables - the script above opened them, verify with
       sudo iptables -L INPUT --line-numbers -n | head
EOF
