# Image de audio-admin : Flask + PyYAML (lecture des salles de Bastion). La logique des niveaux est dans hub.py.
FROM debian:trixie-slim

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-flask python3-yaml && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py hub.py bastion.py ./
COPY templates/ templates/
COPY static/ static/

EXPOSE 5062 5063
CMD ["python3", "/app/app.py"]
