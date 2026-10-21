#!/bin/sh
# Installs and configures fail2ban for postfix SMTP authentication (docker).
# Run on the server from the project directory: sudo sh scripts/setup-fail2ban.sh
# Idempotent: re-running updates the filter/jail and reloads the config.
# Other jails (including sshd) are left untouched.
set -eu

cd "$(dirname "$0")/.."

SRC=config/fail2ban
DST=/etc/fail2ban
JAIL=postfix-sasl-docker

if [ "$(id -u)" -ne 0 ]; then
    echo "Root required: run via sudo" >&2
    exit 1
fi

for f in "$SRC/filter.d/$JAIL.conf" "$SRC/jail.d/$JAIL.local"; do
    [ -f "$f" ] || { echo "File $f not found (run from the project directory)" >&2; exit 1; }
done

echo "==> Installing fail2ban"
# python3-systemd is needed for backend = systemd (reading journald)
if ! command -v fail2ban-client >/dev/null 2>&1 || ! dpkg -s python3-systemd >/dev/null 2>&1; then
    apt-get update
    apt-get install -y fail2ban python3-systemd
fi

echo "==> Copying filter and jail"
install -D -m 644 "$SRC/filter.d/$JAIL.conf" "$DST/filter.d/$JAIL.conf"
install -D -m 644 "$SRC/jail.d/$JAIL.local" "$DST/jail.d/$JAIL.local"

# The ban rule goes into the DOCKER-USER chain, which docker creates:
# fail2ban must start after it.
echo "==> fail2ban starts after docker"
install -d /etc/systemd/system/fail2ban.service.d
cat > /etc/systemd/system/fail2ban.service.d/after-docker.conf <<'UNIT'
[Unit]
After=docker.service
UNIT
systemctl daemon-reload

echo "==> Checking configuration"
fail2ban-client -t

echo "==> Starting"
systemctl enable --now fail2ban
fail2ban-client reload

# the jail does not start instantly
sleep 2
echo "==> Status"
fail2ban-client status "$JAIL"
