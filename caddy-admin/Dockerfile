# Image de caddy-admin : Flask + client Docker (pour detecter les
# conteneurs a ports publies non encore exposes via Caddy - voir app.py,
# /api/discover). Le rechargement de Caddy lui-meme continue de passer
# par son admin API HTTP, pas par le socket.
#
# python3-ruamel.yaml (pas PyYAML) pour lire/ecrire services.yaml depuis
# app.py : seul ruamel (mode round-trip) preserve les commentaires du
# fichier lors d'une reecriture - PyYAML les perd systematiquement des
# la premiere modification. python3-yaml reste installe pour
# render-caddyfile.py (lecture seule, execute en subprocess depuis ce
# meme conteneur - safe_load suffit largement pour ce cas-la).
FROM debian:trixie-slim

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-flask python3-yaml python3-ruamel.yaml docker.io && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py .
COPY templates/ templates/
COPY static/ static/

EXPOSE 5052
CMD ["python3", "/app/app.py"]
