#!/bin/sh
# Server status exporter for the Mini App (admin tab).
# Run as root on the host from the project directory (usually by a systemd timer,
# see scripts/setup-status-exporter.sh):
#   1) writes ./status/status.json (atomically): fail2ban, postfix queue, containers;
#   2) handles unban requests ./status/requests/unban-<ip>.
# No external dependencies except python3: python builds the JSON.
set -eu
umask 022

cd "$(dirname "$0")/.."

PROJECT="${COMPOSE_PROJECT:-mailgateway}"
JAIL=postfix-sasl-docker
STATUS_DIR="${STATUS_DIR:-./status}"
# out: written only by the host (root), mounted read-only into the container.
# requests: the container puts request files there; their contents are not trusted.
OUT_DIR="$STATUS_DIR/out"
REQ_DIR="$STATUS_DIR/requests"
# Temp files only in a root-only directory (mktemp -d gives mode 700)
RUN_BASE="${RUN_BASE:-/run}"

mkdir -p "$STATUS_DIR"
if [ -L "$OUT_DIR" ] || { [ -e "$OUT_DIR" ] && [ ! -d "$OUT_DIR" ]; }; then
    echo "status-exporter: $OUT_DIR is not a directory or is a symlink, aborting" >&2
    exit 1
fi
mkdir -p "$OUT_DIR"

# --- Data collection (each error -> empty string = null) ---

F2B=""
if command -v fail2ban-client >/dev/null 2>&1; then
    F2B=$(fail2ban-client status "$JAIL" 2>/dev/null) || F2B=""
fi

QUEUE=""
# postqueue -j prints one JSON line per email; an empty queue = 0 lines
if QUEUE=$(docker exec postfix postqueue -j 2>/dev/null); then
    QUEUE=$(printf '%s' "$QUEUE" | grep -c . || true)
else
    QUEUE=""
fi

CONT_OK=1
CONTAINERS=$(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" \
    --format '{{.Names}}	{{.State}}	{{.Status}}' 2>/dev/null) || { CONTAINERS=""; CONT_OK=""; }

TMPD=$(mktemp -d "$RUN_BASE/newmail-status.XXXXXX")
trap 'rm -rf "$TMPD"' EXIT
TMP="$TMPD/status.json"

F2B="$F2B" QUEUE="$QUEUE" CONTAINERS="$CONTAINERS" CONT_OK="$CONT_OK" python3 - >"$TMP" <<'PY'
import json, os, re
from datetime import datetime, timezone

# fail2ban-client status <jail>: «Total failed:\t12», «Banned IP list:\t1.2.3.4 5.6.7.8»
f2b = None
raw = os.environ.get("F2B", "")
if raw.strip():
    m_total = re.search(r"Total failed:\s*(\d+)", raw)
    m_ips = re.search(r"Banned IP list:[ \t]*(.*)", raw)
    f2b = {
        "banned": m_ips.group(1).split() if m_ips else [],
        "total_failed": int(m_total.group(1)) if m_total else 0,
    }

q = os.environ.get("QUEUE", "").strip()
queue = int(q) if q.isdigit() else None

containers = [] if os.environ.get("CONT_OK") else None
for line in os.environ.get("CONTAINERS", "").splitlines():
    parts = line.split("\t")
    if containers is not None and len(parts) >= 3:
        containers.append({"name": parts[0], "state": parts[1], "status": parts[2]})
if containers:
    containers.sort(key=lambda c: c["name"])

print(json.dumps({
    "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "fail2ban": f2b,
    "postfix_queue": queue,
    "containers": containers,
}, ensure_ascii=False))
PY
# install within out (owner root, 755), then an atomic mv
install -m 644 "$TMP" "$OUT_DIR/status.json.new"
mv -f "$OUT_DIR/status.json.new" "$OUT_DIR/status.json"

# --- Unban requests ---
# File name unban-<ip>; for IPv6 ":" is written as "_". Validation is done by python
# (ipaddress); the file name is never interpolated into shell commands. Nothing is read
# from the files, symlinks are only removed (rm does not follow the link).
if [ -L "$REQ_DIR" ] || { [ -e "$REQ_DIR" ] && [ ! -d "$REQ_DIR" ]; }; then
    echo "status-exporter: $REQ_DIR is a symlink or not a directory, requests skipped" >&2
    exit 0
fi
[ -d "$REQ_DIR" ] || mkdir "$REQ_DIR"

for f in "$REQ_DIR"/unban-*; do
    if [ -L "$f" ]; then
        echo "status-exporter: symlink '$f' in requests, removed" >&2
        rm -f -- "$f"
        continue
    fi
    if [ ! -f "$f" ]; then
        [ -e "$f" ] && echo "status-exporter: '$f' is not a regular file, skipped" >&2
        continue
    fi
    name=$(basename "$f")
    if IP=$(python3 -c '
import ipaddress, sys
s = sys.argv[1]
if not s.startswith("unban-"):
    sys.exit(1)
print(ipaddress.ip_address(s[len("unban-"):].replace("_", ":")))
' "$name" 2>/dev/null); then
        if command -v fail2ban-client >/dev/null 2>&1; then
            fail2ban-client set "$JAIL" unbanip "$IP" >/dev/null 2>&1 \
                || echo "status-exporter: unbanip $IP failed" >&2
        else
            echo "status-exporter: fail2ban-client not found, request $name dropped" >&2
        fi
    else
        echo "status-exporter: invalid unban request '$name', removed" >&2
    fi
    rm -f -- "$f"
done
