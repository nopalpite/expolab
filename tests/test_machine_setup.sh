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
contains "$WORK/root/etc/apt/apt.conf.d/01expolab-proxy" 'Proxy-Auto-Detect "/usr/local/bin/expolab-apt-proxy"'
contains "$WORK/root/usr/local/bin/expolab-apt-proxy" "10.42.0.1/3142"
contains "$WORK/root/usr/local/bin/expolab-apt-proxy" 'echo "DIRECT"'
[ -x "$WORK/root/usr/local/bin/expolab-apt-proxy" ] || fail "script de detection non executable"
contains "$WORK/root/etc/systemd/timesyncd.conf.d/expolab.conf" "NTP=10.42.0.1"

# Le script de detection doit repondre DIRECT quand le cache est injoignable
# (port 1 : jamais ouvert) - c'est le repli qui rend l'installation initiale
# possible avant que le serveur existe.
sed "s#10.42.0.1/3142#127.0.0.1/1#" "$WORK/root/usr/local/bin/expolab-apt-proxy" > "$WORK/detect-closed.sh"
[ "$(bash "$WORK/detect-closed.sh")" = "DIRECT" ] || fail "repli DIRECT attendu quand le cache ne repond pas"

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
