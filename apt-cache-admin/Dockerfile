# Image d'apt-cache-admin : Flask + client Docker (pour redemarrer le
# conteneur du cache apres un changement de mode ou de miroirs - voir app.py).
FROM debian:trixie-slim

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-flask docker.io && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py .
COPY templates/ templates/
COPY static/ static/

EXPOSE 5056
CMD ["python3", "/app/app.py"]
