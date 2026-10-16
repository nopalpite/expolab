# Portail expolab : connexion unique, tiroir de services, verification de
# session pour Caddy (forward_auth). Voir app.py.
FROM debian:trixie-slim

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-flask python3-yaml python3-bcrypt && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py plans.py ./
COPY templates/ templates/
COPY static/ static/

CMD ["python3", "/app/app.py"]
