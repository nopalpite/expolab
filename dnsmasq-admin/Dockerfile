# Image de dnsmasq-admin : Flask seul, aucun acces reseau/socket special -
# lecture seule d'un fichier monte (voir server/stacks/dnsmasq-admin/docker-compose.yml).
FROM debian:trixie-slim

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-flask && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py .
COPY templates/ templates/
COPY static/ static/

EXPOSE 5051
CMD ["python3", "/app/app.py"]
