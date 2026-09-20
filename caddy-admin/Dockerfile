# Image de caddy-admin : Flask + PyYAML seuls - pas de client Docker, le
# rechargement passe par l'admin API HTTP de Caddy lui-meme (voir app.py).
FROM debian:trixie-slim

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-flask python3-yaml && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py .
COPY templates/ templates/
COPY static/ static/

EXPOSE 5052
CMD ["python3", "/app/app.py"]
