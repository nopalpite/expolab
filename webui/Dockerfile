# Image de la webui expolab : Flask + le client Incus (pour piloter le
# socket monte depuis l'hote, cf incus/profiles/expo-apps.yaml).
FROM debian:trixie-slim

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        python3 python3-flask python3-yaml bash \
        ca-certificates curl gnupg && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://pkgs.zabbly.com/key.asc -o /etc/apt/keyrings/zabbly.asc && \
    . /etc/os-release && \
    echo "deb [signed-by=/etc/apt/keyrings/zabbly.asc] https://pkgs.zabbly.com/incus/stable ${VERSION_CODENAME:-trixie} main" \
        > /etc/apt/sources.list.d/zabbly-incus-stable.list && \
    apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq incus-client && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py .
COPY templates/ templates/
COPY static/ static/

EXPOSE 5050
CMD ["python3", "/app/app.py"]
