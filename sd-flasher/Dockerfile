# Image de sd-flasher : Flask + les outils systeme qui parlent aux disques
# (lsblk/mount/umount/blockdev viennent d'util-linux ; zstd pour les images .zst).
# Le conteneur est PRIVILEGIE (voir la stack) : il ecrit sur des disques bruts.
FROM debian:trixie-slim

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-flask util-linux zstd && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py flasher.py ./
COPY templates/ templates/
COPY static/ static/

EXPOSE 5057
CMD ["python3", "/app/app.py"]
