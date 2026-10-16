# Image de prises-admin : Flask + aiohttp (client de l'API WebSocket de matter-server) +
# client SSH (arret propre d'un module avant de couper sa prise).
FROM debian:trixie-slim

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-flask python3-aiohttp python3-yaml openssh-client && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py plugs.py matter_client.py ./
COPY templates/ templates/
COPY static/ static/

EXPOSE 5058
CMD ["python3", "/app/app.py"]
