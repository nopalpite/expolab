#!/usr/bin/env bash
# Test de image/machine-setup.sh dans un faux systeme de fichiers (aucun
# root, aucun systeme reel touche). Lance en CI et a la main.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SETUP="$REPO/image/machine-setup.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "ECHEC : $*" >&2; exit 1; }
contains() { grep -qF -- "$2" "$1" || fail "'$2' absent de $1"; }

export MACHINE_SETUP_ROOT="$WORK/root"
export MACHINE_SETUP_HOME="$WORK/root/home/pi"
mkdir -p "$WORK/root/etc/systemd" "$MACHINE_SETUP_HOME"
echo "ssh-ed25519 AAAATESTKEY expolab-test" > "$WORK/key.pub"

# --- etape reseau ---
bash "$SETUP" --stage network --server-ip 10.42.0.1 > /dev/null
contains "$WORK/root/etc/apt/apt.conf.d/01expolab-proxy" 'Acquire::http::Proxy "http://10.42.0.1:3142";'
contains "$WORK/root/etc/systemd/timesyncd.conf.d/expolab.conf" "NTP=10.42.0.1"
# Systeme avec chrony (Ubuntu 26.04) : le serveur du lab est AJOUTE aux sources, jamais substitue.
[ ! -e "$WORK/root/etc/chrony/sources.d/expolab.sources" ] || fail "source chrony ecrite sur un systeme sans chrony"
mkdir -p "$WORK/root/etc/chrony"
bash "$SETUP" --stage network --server-ip 10.42.0.1 > /dev/null
contains "$WORK/root/etc/chrony/sources.d/expolab.sources" "server 10.42.0.1 iburst prefer"
[ ! -e "$WORK/root/etc/chrony/chrony.conf" ] || fail "chrony.conf modifie : le serveur du lab doit s'ajouter, pas remplacer"

# Les machines n'ont pas internet : pas de repli en connexion directe, et
# l'ancien mecanisme de detection (version precedente) est retire.
! grep -q "DIRECT\|Auto-Detect" "$WORK/root/etc/apt/apt.conf.d/01expolab-proxy" || fail "repli direct present"
mkdir -p "$WORK/root/usr/local/bin" && echo old > "$WORK/root/usr/local/bin/expolab-apt-proxy"
bash "$SETUP" --stage network > /dev/null
[ ! -e "$WORK/root/usr/local/bin/expolab-apt-proxy" ] || fail "ancien script de detection non retire"

# --- etape acces ---
bash "$SETUP" --stage access --user pi --pubkey-file "$WORK/key.pub" > /dev/null
contains "$MACHINE_SETUP_HOME/.ssh/authorized_keys" "AAAATESTKEY"
contains "$WORK/root/etc/sudoers.d/90-expolab-automation" "pi ALL=(ALL) NOPASSWD:ALL"

# --- idempotence : un second passage ne duplique rien ---
bash "$SETUP" --stage all --user pi --pubkey-file "$WORK/key.pub" > /dev/null
[ "$(grep -c AAAATESTKEY "$MACHINE_SETUP_HOME/.ssh/authorized_keys")" = "1" ] || fail "cle dupliquee"
[ "$(wc -l < "$WORK/root/etc/sudoers.d/90-expolab-automation")" = "1" ] || fail "sudoers duplique"

# --- erreurs d'usage ---
bash "$SETUP" --stage access --user pi > /dev/null 2>&1 && fail "--pubkey-file devrait etre exige"
bash "$SETUP" --stage nimporte > /dev/null 2>&1 && fail "etape inconnue devrait echouer"

echo "OK : image/machine-setup.sh"
