#!/usr/bin/env bash
#
# Creates everything the Paper Trader needs on Oracle Cloud, from scratch:
# the VCN, an internet gateway, the route table, a security list that allows
# SSH + HTTP + HTTPS, a public subnet, and an Always Free instance - then prints
# the public IP you deploy to.
#
# WHERE TO RUN IT: Oracle Cloud Shell, not your PC. It is the terminal button
# ("`>_" icon, top right) in the Oracle console: it is already authenticated with
# your account, needs no API keys and no local install. Nothing secret leaves
# your tenancy.
#
# WHAT YOU NEED FIRST (both are one click to copy in the console):
#   COMPARTMENT_ID    Identity -> Compartments -> the root compartment of your
#                     tenancy -> Copy OCID. (Or leave it: the script falls back
#                     to the tenancy compartment discovered from your user.)
#   SSH_PUBLIC_KEY    the text inside the .pub file created by
#                     deploy/deploy-from-windows.ps1 (it prints the path).
#
# Example:
#   COMPARTMENT_ID=ocid1.tenancy.oc1..aaaa \
#   SSH_PUBLIC_KEY="$(cat <<'KEY'
#   ssh-ed25519 AAAAC3... paper-trader-deploy
#   KEY
#   )" bash oci-create-instance.sh
#
# Options: SHAPE (default VM.Standard.A1.Flex, 1 OCPU / 6 GB - the ARM free
# tier), OCPUS, MEMORY_GB, INSTANCE_NAME, VCN_NAME, REGION.
#
set -euo pipefail

COMPARTMENT_ID="${COMPARTMENT_ID:-}"
SSH_PUBLIC_KEY="${SSH_PUBLIC_KEY:-}"
SHAPE="${SHAPE:-VM.Standard.A1.Flex}"
OCPUS="${OCPUS:-1}"
MEMORY_GB="${MEMORY_GB:-6}"
INSTANCE_NAME="${INSTANCE_NAME:-paper-trader}"
VCN_NAME="${VCN_NAME:-paper-trader-vcn}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m  x %s\033[0m\n' "$*" >&2; exit 1; }
q()    { oci "$@" --raw-output; }         # run oci, print just the value

command -v oci >/dev/null 2>&1 || die "the oci CLI is not installed. Use Oracle Cloud Shell (the '>_' button in the console), which has it ready."

[ -n "$SSH_PUBLIC_KEY" ] || die "SSH_PUBLIC_KEY is empty. Paste the contents of your .pub file, e.g.:
    SSH_PUBLIC_KEY=\"\$(cat \$HOME/.ssh/oracle-paper-trader.pub)\" bash $0"

# ---------------------------------------------------------------------------
say "Checking your tenancy"
if [ -z "$COMPARTMENT_ID" ]; then
  # Every user lives in the tenancy's root compartment, so this finds it
  # without asking you to hunt for the OCID.
  COMPARTMENT_ID="$(q iam user list --query 'data[0]."compartment-id"' 2>/dev/null || true)"
  [ -n "$COMPARTMENT_ID" ] && echo "  using the tenancy compartment: $COMPARTMENT_ID"
fi
[ -n "$COMPARTMENT_ID" ] || die "could not work out the compartment. Copy it from Identity -> Compartments and pass COMPARTMENT_ID=..."

REGION="${REGION:-$(q iam region list --query 'data[0].name' 2>/dev/null || echo unknown)}"
echo "  region: $REGION"

# Already done? Then do not build a second one.
EXISTING="$(q compute instance list --compartment-id "$COMPARTMENT_ID" \
  --display-name "$INSTANCE_NAME" --lifecycle-state RUNNING \
  --query 'data[0].id' 2>/dev/null || true)"
if [ -n "$EXISTING" ] && [ "$EXISTING" != "null" ]; then
  IP="$(q compute instance list-vnics --instance-id "$EXISTING" \
        --query 'data[0]."public-ip"' 2>/dev/null || true)"
  warn "an instance called '$INSTANCE_NAME' is already running."
  echo "  public IP: ${IP:-none}"
  echo "  Deploy to it with:  .\\deploy\\deploy-from-windows.ps1 -Server $IP ..."
  exit 0
fi

ADS="$(q iam availability-domain list --compartment-id "$COMPARTMENT_ID" \
      --query 'data[].name' 2>/dev/null || true)"
[ -n "$ADS" ] || die "could not list availability domains"
echo "  availability domains: $(echo "$ADS" | tr -d '[]"' | tr '\n' ' ')"

# ---------------------------------------------------------------------------
say "Creating the network"
VCN_ID="$(q network vcn create --compartment-id "$COMPARTMENT_ID" \
  --cidr-block 10.0.0.0/16 --display-name "$VCN_NAME" --dns-label ptvcn \
  --wait-for-state AVAILABLE --query 'data.id')"
echo "  VCN: $VCN_ID"

IGW_ID="$(q network internet-gateway create --compartment-id "$COMPARTMENT_ID" \
  --vcn-id "$VCN_ID" --is-enabled true --display-name "$VCN_NAME-igw" \
  --wait-for-state AVAILABLE --query 'data.id')"
echo "  internet gateway: $IGW_ID"

RT_ID="$(q network route-table create --compartment-id "$COMPARTMENT_ID" \
  --vcn-id "$VCN_ID" --display-name "$VCN_NAME-rt" \
  --route-rules "[{\"destination\":\"0.0.0.0/0\",\"networkEntityId\":\"$IGW_ID\"}]" \
  --wait-for-state AVAILABLE --query 'data.id')"
echo "  route table (0.0.0.0/0 -> gateway): $RT_ID"

# This is THE rule people forget: without it the site is unreachable no matter
# how the instance itself is configured.
SL_ID="$(q network security-list create --compartment-id "$COMPARTMENT_ID" \
  --vcn-id "$VCN_ID" --display-name "$VCN_NAME-sl" \
  --egress-security-rules '[{"destination":"0.0.0.0/0","protocol":"all","isStateless":false}]' \
  --ingress-security-rules '[
    {"source":"0.0.0.0/0","protocol":"6","isStateless":false,"description":"SSH","tcpOptions":{"destinationPortRange":{"min":22,"max":22}}},
    {"source":"0.0.0.0/0","protocol":"6","isStateless":false,"description":"HTTP","tcpOptions":{"destinationPortRange":{"min":80,"max":80}}},
    {"source":"0.0.0.0/0","protocol":"6","isStateless":false,"description":"HTTPS","tcpOptions":{"destinationPortRange":{"min":443,"max":443}}}]' \
  --wait-for-state AVAILABLE --query 'data.id')"
echo "  security list (22, 80, 443 open): $SL_ID"

SUBNET_ID="$(q network subnet create --compartment-id "$COMPARTMENT_ID" \
  --vcn-id "$VCN_ID" --cidr-block 10.0.0.0/24 --display-name "$VCN_NAME-subnet" \
  --dns-label ptsubnet --route-table-ids "[\"$RT_ID\"]" \
  --security-list-ids "[\"$SL_ID\"]" \
  --wait-for-state AVAILABLE --query 'data.id')"
echo "  public subnet: $SUBNET_ID"

# ---------------------------------------------------------------------------
say "Finding an Ubuntu image for $SHAPE"
IMAGE_ID="$(q compute image list --compartment-id "$COMPARTMENT_ID" \
  --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
  --shape "$SHAPE" --sort-by TIMECREATED --sort-order DESC \
  --query 'data[0].id')"
if [ -z "$IMAGE_ID" ] || [ "$IMAGE_ID" = "null" ]; then
  IMAGE_ID="$(q compute image list --compartment-id "$COMPARTMENT_ID" \
    --operating-system "Canonical Ubuntu" --shape "$SHAPE" \
    --sort-by TIMECREATED --sort-order DESC --query 'data[0].id')"
fi
[ -n "$IMAGE_ID" ] && [ "$IMAGE_ID" != "null" ] || die "no Ubuntu image found for $SHAPE"
echo "  image: $IMAGE_ID"

METADATA="$(printf '{"ssh_authorized_keys":"%s"}' "$SSH_PUBLIC_KEY")"

# ---------------------------------------------------------------------------
say "Launching the instance"
# "Out of host capacity" is normal on the free ARM shape: each availability
# domain is a separate pool, so we simply try the next one.
INSTANCE_ID=""
for AD in $(echo "$ADS" | tr -d '[]",' ); do
  [ -n "$AD" ] || continue
  echo "  trying $AD ..."
  if [ "$SHAPE" = "VM.Standard.A1.Flex" ]; then
    LAUNCH_ARGS=(--shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEMORY_GB}")
  else
    LAUNCH_ARGS=()
  fi
  if OUT="$(oci compute instance launch --compartment-id "$COMPARTMENT_ID" \
        --availability-domain "$AD" --display-name "$INSTANCE_NAME" \
        --shape "$SHAPE" "${LAUNCH_ARGS[@]}" --image-id "$IMAGE_ID" \
        --subnet-id "$SUBNET_ID" --assign-public-ip true \
        --metadata "$METADATA" --wait-for-state RUNNING \
        --query 'data.id' --raw-output 2>&1)"; then
    INSTANCE_ID="$OUT"
    echo "  running in $AD"
    break
  fi
  if echo "$OUT" | grep -qi "capacity"; then
    warn "$AD has no free capacity right now - trying the next one"
  else
    warn "$AD failed: $(echo "$OUT" | tail -3)"
  fi
done
[ -n "$INSTANCE_ID" ] || die "the free shape is full in every availability domain right now.
    Try again in a few minutes, or run with:
      SHAPE=VM.Standard.E2.1.Micro bash $0"

# ---------------------------------------------------------------------------
say "Waiting for a public address"
IP=""
for _ in $(seq 1 30); do
  IP="$(q compute instance list-vnics --instance-id "$INSTANCE_ID" \
        --query 'data[0]."public-ip"' 2>/dev/null || true)"
  [ -n "$IP" ] && [ "$IP" != "null" ] && break
  sleep 5
done
[ -n "$IP" ] && [ "$IP" != "null" ] || die "the instance has no public IP. Delete it and create a new one WITH 'Assign a public IPv4 address' ticked."

cat <<EOF

========================================================================
  The server exists and port 22/80/443 are open.

  Public IP:        $IP
  Instance OCID:    $INSTANCE_ID
  Compartment:      $COMPARTMENT_ID
  SSH:              ssh -i ~/.ssh/oracle-paper-trader ubuntu@$IP

  Next, on your own computer (this Windows machine):

    cd "$(pwd)"
    .\\deploy\\deploy-from-windows.ps1 -Server $IP \`
        -DuckDnsName mytrader -DuckDnsToken YOUR-DUCKDNS-TOKEN \`
        -OwnerEmail you@example.com -RegistrationCode a-word-only-you-know

  That uploads the project, installs Docker, opens the instance firewall,
  points DuckDNS here, gets the HTTPS certificate and prints your password.
========================================================================
EOF
