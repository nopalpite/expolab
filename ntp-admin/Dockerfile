# Image de ntp-admin : Flask + chronyc (le client de chrony, pour lire l'etat du serveur
# de temps par sa socket). Le redemarrage du conteneur ntp passe par l'API Docker, sans
# client docker : voir app.py.
FROM debian:trixie-slim

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-flask chrony && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py .
COPY templates/ templates/
COPY static/ static/

EXPOSE 5058
CMD ["python3", "/app/app.py"]
