#!/bin/sh
# Generates DKIM keys (selector "mail") inside the rspamd container
# and prints TXT records for DNS. Re-running does not overwrite existing keys.
set -e

DOMAINS="${*:-example.com guest.example.com}"

for domain in $DOMAINS; do
    docker compose exec -T rspamd sh -c "
        mkdir -p /var/lib/rspamd/dkim
        if [ ! -f /var/lib/rspamd/dkim/$domain.mail.key ]; then
            rspamadm dkim_keygen -d $domain -s mail \
                -k /var/lib/rspamd/dkim/$domain.mail.key \
                > /var/lib/rspamd/dkim/$domain.mail.txt
            chown -R _rspamd:_rspamd /var/lib/rspamd/dkim
            chmod 600 /var/lib/rspamd/dkim/$domain.mail.key
        fi
    "
    echo "=== DNS TXT for $domain (mail._domainkey.$domain) ==="
    docker compose exec -T rspamd cat "/var/lib/rspamd/dkim/$domain.mail.txt"
    echo
done
