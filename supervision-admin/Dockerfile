# Image de supervision-admin : Flask + PyYAML (machines lues dans Bastion) + Apprise (canaux d'alerte), paquets Debian.
FROM debian:trixie-slim

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends python3 python3-flask python3-yaml apprise && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py checks.py fleet.py history.py notify.py programme.py scrape.py supervisor.py ./
COPY templates/ templates/
COPY static/ static/

EXPOSE 5064
CMD ["python3", "/app/app.py"]
