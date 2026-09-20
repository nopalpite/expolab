# Image de vpn-admin : Flask + client Docker (pour `docker exec wireguard
# wg ...`, comme add-peer.sh/remove-peer.sh eux-memes) + qrcode (QR code
# du fichier client genere, pour import mobile en un scan).
FROM debian:trixie-slim

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        python3 python3-flask python3-yaml python3-qrcode python3-pil \
        docker.io bash && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py .
COPY templates/ templates/
COPY static/ static/

EXPOSE 5053
CMD ["python3", "/app/app.py"]
