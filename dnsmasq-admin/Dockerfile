# Image de dnsmasq-admin : Flask + client Docker (pour `docker kill
# --signal=HUP expolab-dnsmasq`, seule facon de faire relire a dnsmasq
# ses reservations/enregistrements sans le redemarrer).
FROM debian:trixie-slim

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-flask docker.io && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py .
COPY templates/ templates/
COPY static/ static/

EXPOSE 5051
CMD ["python3", "/app/app.py"]
