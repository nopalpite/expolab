#!/usr/bin/env bash
# Installe l'agent audio sur UNE machine de l'expo (Ubuntu Server) : PipeWire, acces au son pour
# l'utilisateur, session utilisateur persistante, agent et service systemd.
#
# Usage (sur la machine, en root) :
#   sudo AGENT_TOKEN=<jeton> ./install.sh [http://10.42.0.1:5063] [nom-de-la-machine]
#
#   AGENT_TOKEN       le jeton du serveur : server/stacks/audio-admin/data/agent.token (jamais en argument :
#                     il resterait visible dans la liste des processus)
#   AGENT_USER        utilisateur qui joue le son et fait tourner l'agent (defaut : l'utilisateur qui a lance sudo)
#   AGENT_SINK_MATCH  morceau du nom de la sortie audio (defaut : usb = la carte audio USB ; « default » pour
#                     essayer sur la sortie par defaut, par exemple le jack d'un Pi 3 ou 4)
#
# Idempotent : relancer met a jour l'agent et la configuration sans rien dupliquer. Les applications qui jouent du
# son doivent tourner sous le MEME utilisateur (PipeWire est une session par utilisateur) et, pour ne pas demarrer a
# un niveau inconnu, declarer `After=expolab-agent.service` dans leur unite.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi
HUB="${1:-http://10.42.0.1:5063}"
NAME="${2:-$(hostname)}"
USER_NAME="${AGENT_USER:-${SUDO_USER:-pi}}"
SINK_MATCH="${AGENT_SINK_MATCH:-usb}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -z "${AGENT_TOKEN:-}" ]; then
    echo "AGENT_TOKEN absent : lancer avec  sudo AGENT_TOKEN=<jeton> $0" >&2
    echo "(le jeton est dans server/stacks/audio-admin/data/agent.token sur le serveur)" >&2
    exit 2
fi
if ! id "$USER_NAME" >/dev/null 2>&1; then
    echo "Utilisateur '$USER_NAME' introuvable." >&2
    exit 1
fi
UID_N="$(id -u "$USER_NAME")"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"

echo "[+] Paquets audio (PipeWire, outils ALSA)..."
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq pipewire-audio pipewire-bin alsa-utils >/dev/null

echo "[+] $USER_NAME : acces au son et session persistante..."
usermod -aG audio "$USER_NAME"        # sans session graphique, le groupe audio donne l'acces aux cartes son
loginctl enable-linger "$USER_NAME"   # PipeWire et l'agent tournent des le demarrage, sans connexion

echo "[+] Agent et configuration (jeton en mode 600)..."
install -d -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR/audio-agent" "$HOME_DIR/.config" "$HOME_DIR/.config/systemd/user"
install -m 755 -o "$USER_NAME" -g "$USER_NAME" "$SCRIPT_DIR/agent.py" "$HOME_DIR/audio-agent/agent.py"
(umask 077 && cat > "$HOME_DIR/.config/expolab-agent.conf" <<EOF
AGENT_HUB=$HUB
AGENT_TOKEN=$AGENT_TOKEN
AGENT_NAME=$NAME
AGENT_SINK_MATCH=$SINK_MATCH
EOF
)
chown "$USER_NAME:$USER_NAME" "$HOME_DIR/.config/expolab-agent.conf"

cat > "$HOME_DIR/.config/systemd/user/expolab-agent.service" <<EOF
[Unit]
Description=Agent audio de l'expo (niveau voulu par le serveur, repris au demarrage meme sans reseau)
After=pipewire.service wireplumber.service

[Service]
Type=notify
NotifyAccess=main
TimeoutStartSec=60
ExecStart=/usr/bin/python3 $HOME_DIR/audio-agent/agent.py
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF
chown "$USER_NAME:$USER_NAME" "$HOME_DIR/.config/systemd/user/expolab-agent.service"

echo "[+] Demarrage (la session de $USER_NAME est relancee pour prendre en compte le groupe audio)..."
systemctl restart "user@$UID_N.service"
sleep 3
run_user() {
    runuser -u "$USER_NAME" -- env XDG_RUNTIME_DIR="/run/user/$UID_N" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$UID_N/bus" "$@"
}
run_user systemctl --user daemon-reload
run_user systemctl --user enable --now pipewire.socket pipewire-pulse.socket wireplumber.service expolab-agent.service

cat <<EOF

[+] Agent audio installe pour '$USER_NAME' sur cette machine ($NAME).
    Etat :            systemctl --user status expolab-agent     (en tant que $USER_NAME)
    Journal :         journalctl --user -u expolab-agent -f
    Sorties visibles: wpctl status
    La machine apparait dans la console audio (https://audio.<domaine>) : il reste a la VALIDER.
    Ses applications sonores : declarer After=expolab-agent.service dans leur unite.
EOF
