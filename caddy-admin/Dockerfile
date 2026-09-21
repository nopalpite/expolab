# Image de caddy-admin : Flask + PyYAML + client Docker (pour detecter
# les conteneurs a ports publies non encore exposes via Caddy - voir
# app.py, /api/discover). Le rechargement de Caddy lui-meme continue de
# passer par son admin API HTTP, pas par le socket.
FROM debian:trixie-slim

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-flask python3-yaml docker.io && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py .
COPY templates/ templates/
COPY static/ static/

EXPOSE 5052
CMD ["python3", "/app/app.py"]
