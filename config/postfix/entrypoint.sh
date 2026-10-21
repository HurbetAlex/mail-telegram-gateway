#!/bin/sh
set -e

echo "Starting Postfix container entrypoint..."

# =========================
# Basic files/directories
# =========================

mkdir -p /etc/sasl2
mkdir -p /var/run/saslauthd
mkdir -p /var/spool/postfix/etc

# =========================
# SMTP AUTH via saslauthd + PAM
# =========================

echo "Configuring Cyrus SASL for Postfix..."

cat > /etc/sasl2/smtpd.conf <<'EOF'
pwcheck_method: saslauthd
saslauthd_path: /var/run/saslauthd/mux
mech_list: PLAIN LOGIN
log_level: 7
EOF

# PAM service used by testsaslauthd -s smtp
cat > /etc/pam.d/smtp <<'EOF'
auth required pam_unix.so
account required pam_unix.so
EOF

# PAM service used by testsaslauthd -s smtpd / Postfix smtpd
cat > /etc/pam.d/smtpd <<'EOF'
auth required pam_unix.so
account required pam_unix.so
EOF

# =========================
# Create SMTP auth users
# =========================

if [ -f /etc/postfix/sasl_users ]; then
    echo "Creating SMTP AUTH users from /etc/postfix/sasl_users..."

    while IFS=':' read -r user pass; do
        # Remove Windows CRLF symbols if file was edited on Windows
        user="$(printf '%s' "$user" | tr -d '\r')"
        pass="$(printf '%s' "$pass" | tr -d '\r')"

        # Skip empty/comment lines
        case "$user" in
            ""|\#*) continue ;;
        esac

        if [ -n "$user" ] && [ -n "$pass" ]; then
            echo "Creating SMTP auth user: $user"

            if ! id "$user" >/dev/null 2>&1; then
                useradd -M -s /usr/sbin/nologin "$user"
            fi

            printf '%s:%s\n' "$user" "$pass" | chpasswd
        fi
    done < /etc/postfix/sasl_users
else
    echo "Warning: /etc/postfix/sasl_users not found."
fi

# =========================
# Start saslauthd
# =========================

echo "Starting saslauthd with PAM backend..."

# Kill old saslauthd if container was restarted without full recreation
pkill saslauthd 2>/dev/null || true

mkdir -p /var/run/saslauthd

saslauthd -a pam -m /var/run/saslauthd -n 2

# Allow postfix process to access saslauthd socket
chgrp -R sasl /var/run/saslauthd || true
chmod -R 710 /var/run/saslauthd || true

# =========================
# Generate self-signed TLS certificate if missing
# =========================

if [ ! -f /etc/postfix/certs/cert.pem ] || [ ! -f /etc/postfix/certs/key.pem ]; then
    echo "Generating self-signed certificate..."

    mkdir -p /etc/postfix/certs

    openssl req -new -x509 -days 3650 -nodes \
        -out /etc/postfix/certs/cert.pem \
        -keyout /etc/postfix/certs/key.pem \
        -subj "/C=US/ST=State/L=City/O=MailServer/CN=mail.example.com"

    chmod 600 /etc/postfix/certs/key.pem
    chmod 644 /etc/postfix/certs/cert.pem
fi

# =========================
# Build Postfix maps
# =========================

if [ -f /etc/postfix/sender_login ]; then
    echo "Building sender_login map..."
    postmap /etc/postfix/sender_login || true
fi

if [ -f /etc/postfix/virtual_mailboxes ]; then
    echo "Building virtual_mailboxes map..."
    postmap /etc/postfix/virtual_mailboxes || true
fi

if [ -f /etc/postfix/virtual_aliases ]; then
    echo "Building virtual_aliases map..."
    postmap /etc/postfix/virtual_aliases || true
fi

# =========================
# Fix permissions
# =========================

chown -R root:root /etc/postfix || true

# sender_login/sasl_users may be mounted from host with different owner;
# Postfix only warns, not fatal. Keep files readable.
chmod 644 /etc/postfix/sender_login 2>/dev/null || true
chmod 644 /etc/postfix/sender_login.db 2>/dev/null || true
chmod 600 /etc/postfix/sasl_users 2>/dev/null || true

# =========================
# Debug output
# =========================

echo "===== /etc/sasl2/smtpd.conf ====="
cat /etc/sasl2/smtpd.conf || true

echo "===== saslauthd processes ====="
pgrep -a saslauthd || true

echo "===== SMTP auth users from /etc/passwd ====="
if [ -f /etc/postfix/sasl_users ]; then
    cut -d ':' -f 1 /etc/postfix/sasl_users | while read -r u; do
        u="$(printf '%s' "$u" | tr -d '\r')"
        [ -n "$u" ] && id "$u" 2>/dev/null || true
    done
fi

echo "===== Postfix SASL config ====="
postconf -n | grep -E 'sasl|sender|restriction|myhostname|mydomain' || true

# =========================
# Start Postfix
# =========================

echo "Starting Postfix..."

exec postfix start-fg
