# Image de git-mirror : Flask + git en ligne de commande (clone --mirror /
# fetch --prune executes en subprocess depuis app.py, meme pattern que
# caddy-admin pour render-caddyfile.py).
FROM debian:trixie-slim

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-flask python3-yaml git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py .
COPY templates/ templates/
COPY static/ static/

EXPOSE 5054
CMD ["python3", "/app/app.py"]
