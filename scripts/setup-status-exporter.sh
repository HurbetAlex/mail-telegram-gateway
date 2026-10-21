#!/bin/sh
# Installs a systemd timer that runs scripts/status-exporter.sh every minute
# (server status for the Mini App). Run on the server: sudo sh scripts/setup-status-exporter.sh
# Idempotent: re-running overwrites the units and restarts the timer.
set -eu

cd "$(dirname "$0")/.."
DIR=$(pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "Root required: run via sudo" >&2
    exit 1
fi

[ -f scripts/status-exporter.sh ] || { echo "scripts/status-exporter.sh not found (run from the project directory)" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 required" >&2; exit 1; }

echo "==> Directories status/out (root-writable only) and status/requests"
for d in status/out status/requests; do
    if [ -L "$d" ] || { [ -e "$d" ] && [ ! -d "$d" ]; }; then
        echo "$d: symlink or not a directory" >&2
        exit 1
    fi
done
mkdir -p status/out status/requests
chmod 755 status status/out status/requests

echo "==> systemd units"
cat > /etc/systemd/system/newmail-status.service <<UNIT
[Unit]
Description=Mail gateway: export server status for the Mini App
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=$DIR
ExecStart=/bin/sh $DIR/scripts/status-exporter.sh
UNIT

cat > /etc/systemd/system/newmail-status.timer <<'UNIT'
[Unit]
Description=Mail gateway: server status every minute

[Timer]
OnCalendar=minutely
AccuracySec=5s
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now newmail-status.timer

echo "==> Trial run"
systemctl start newmail-status.service
cat "$DIR/status/out/status.json"
echo
systemctl list-timers newmail-status.timer --no-pager
