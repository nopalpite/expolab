# Caddy avec le plugin caddy-dns/ovh compile en dur - necessaire pour un
# certificat signe Let's Encrypt via DNS-01 (voir TLS_MODE dans
# deploy-server.sh). Toujours utilise, meme en TLS_MODE=internal (le
# plugin reste present mais inactif tant que render-caddyfile.py n'emet
# aucun bloc "dns ovh") : un seul Dockerfile a maintenir pour les deux
# modes plutot que deux images.
FROM caddy:2-builder AS builder
RUN xcaddy build --with github.com/caddy-dns/ovh

FROM caddy:2
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
