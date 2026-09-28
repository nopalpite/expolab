# Image d'ansible-web : Flask + Ansible + client git/ssh.
#
# Recette reprise de l'ancien Dockerfile de bastion-ansible (webui/runner
# maison retire lors du passage a Semaphore, lui-meme retire ensuite) -
# meme base eprouvee, seul le contenu applicatif change : ansible-web ne
# publie plus une image auto-suffisante avec le contenu Ansible baque
# dedans, il clone ce contenu a l'execution depuis git-mirror (voir
# app.py, sync_repo()).
FROM python:3.12-slim

# sshpass : necessaire pour l'authentification SSH par mot de passe
# (ansible_ssh_pass) - la cle dediee automatisation est la methode
# "vraie prod", mais les faux Pi de ce lab (fleet/provision-fakepi.sh)
# n'acceptent que le mot de passe par defaut ("pi"/"raspberry"), aucune
# cle n'y est injectee automatiquement.
RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-client git sshpass && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir ansible-core==2.17.* flask==3.0.* pyyaml==6.0.*

WORKDIR /app
COPY app.py .
COPY templates/ templates/
COPY static/ static/

EXPOSE 5055
CMD ["python3", "/app/app.py"]
