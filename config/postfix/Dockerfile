FROM mwader/postfix-relay:latest

# Install SASL libraries and OpenSSL (Debian/Ubuntu based)
# Reverting to apt-get as the base image appears to be Debian-based (apk not found)
RUN apt-get update && \
    apt-get install -y \
    libsasl2-modules \
    sasl2-bin \
    openssl \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Create directory for sasl2 config and certs
RUN mkdir -p /etc/sasl2 && \
    mkdir -p /etc/postfix/certs

# Postfix needs to read sasldb (add postfix user to sasl group)
RUN usermod -aG sasl postfix
