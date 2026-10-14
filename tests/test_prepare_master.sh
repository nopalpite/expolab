#!/usr/bin/env bash
# Test de image/prepare-master.sh dans un faux systeme de fichiers : ce qui doit disparaitre
# (graine du flasheur avec le mot de passe Wi-Fi, identite, historiques, journaux) disparait,
# ce qui doit rester (configuration SSH, fichiers du systeme) reste, et rien n'est touche sans
# confirmation. Aucune commande systeme (PREPARE_MASTER_DRY). Lance en CI et a la main.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO/image/prepare-master.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail() { echo "ECHEC : $*" >&2; exit 1; }

export PREPARE_MASTER_ROOT="$WORK/root" PREPARE_MASTER_DRY=1
R="$PREPARE_MASTER_ROOT"

build_tree() {
    rm -rf "$R"
    mkdir -p "$R/boot/firmware" "$R/var/lib/cloud/instances/expolab-gerard-1" "$R/var/lib/cloud/data" "$R/etc/netplan" \
             "$R/etc/ssh" "$R/var/lib/dbus" "$R/home/temp" "$R/home/autre/.cache" "$R/root" "$R/var/log/journal/abc" \
             "$R/var/lib/apt/lists" "$R/tmp" "$R/var/tmp" "$R/var/lib/systemd" "$R/var/lib/chrony" "$R/var/cache/apt/archives"
    printf '#cloud-config\n{"users": [{"name": "pi", "passwd": "$6$sel$HACHE-DU-MOT-DE-PASSE"}]}\n' > "$R/boot/firmware/user-data"
    printf '{"network": {"wifis": {"wlan0": {"access-points": {"meskouy": {"password": "MOT-DE-PASSE-WIFI-EN-CLAIR"}}}}}}\n' > "$R/boot/firmware/network-config"
    printf 'instance-id: expolab-gerard-1\nlocal-hostname: gerard\n' > "$R/boot/firmware/meta-data"
    echo "config.txt du firmware" > "$R/boot/firmware/config.txt"
    echo "instance" > "$R/var/lib/cloud/instances/expolab-gerard-1/obj.pkl"
    echo "x" > "$R/etc/netplan/50-cloud-init.yaml"; echo "garde" > "$R/etc/netplan/01-autre.yaml"
    echo "0123456789abcdef0123456789abcdef" > "$R/etc/machine-id"
    echo "0123456789abcdef0123456789abcdef" > "$R/var/lib/dbus/machine-id"
    echo "cle" > "$R/etc/ssh/ssh_host_ed25519_key"; echo "pub" > "$R/etc/ssh/ssh_host_ed25519_key.pub"; echo "cle" > "$R/etc/ssh/ssh_host_rsa_key"
    echo "Port 22" > "$R/etc/ssh/sshd_config"
    echo "commande secrete" > "$R/home/temp/.bash_history"; echo "x" > "$R/home/autre/.viminfo"; echo "x" > "$R/home/autre/.cache/c"
    echo "ls" > "$R/root/.bash_history"; echo "garde" > "$R/home/temp/.profile"
    echo "un journal" > "$R/var/log/syslog"; echo "ancien" > "$R/var/log/syslog.1"; echo "zip" > "$R/var/log/auth.log.2.gz"
    echo "binaire" > "$R/var/log/journal/abc/system.journal"
    echo "liste" > "$R/var/lib/apt/lists/archive.ubuntu.com_x"; echo "deb" > "$R/var/cache/apt/archives/x.deb"
    echo "tmp" > "$R/tmp/fichier"; echo "tmp" > "$R/var/tmp/fichier"
    echo "graine" > "$R/var/lib/systemd/random-seed"; echo "derive" > "$R/var/lib/chrony/chrony.drift"
}

snapshot() { (cd "$R" && find . -type f -exec sha1sum {} + 2>/dev/null | sort); }

# --- sans confirmation : rien n'est modifie -----------------------------------------------------

build_tree
before="$(snapshot)"
if echo "pas master" | bash "$SCRIPT" > "$WORK/out.log" 2>&1; then fail "une mauvaise reponse aurait du annuler"; fi
[ "$(snapshot)" = "$before" ] || fail "des fichiers ont ete modifies sans confirmation"
grep -q "Annule" "$WORK/out.log" || fail "message d'annulation absent"
if bash "$SCRIPT" < /dev/null > /dev/null 2>&1; then fail "sans reponse, le script aurait du s'arreter"; fi
[ "$(snapshot)" = "$before" ] || fail "des fichiers ont ete modifies sans reponse"

# --- avec la bonne confirmation -------------------------------------------------------------------

build_tree
echo "MASTER" | bash "$SCRIPT" > "$WORK/out.log" 2>&1 || fail "echec avec la confirmation : $(cat "$WORK/out.log")"

# La graine du flasheur : plus aucun secret, fichiers neutres, meta-data supprime.
! grep -rq "MOT-DE-PASSE-WIFI-EN-CLAIR\|HACHE-DU-MOT-DE-PASSE" "$R" || fail "un secret du flasheur est encore dans l'image"
[ "$(cat "$R/boot/firmware/user-data")" = "#cloud-config" ] || fail "user-data non neutre"
grep -q "dhcp4: true" "$R/boot/firmware/network-config" || fail "network-config neutre absent"
[ ! -e "$R/boot/firmware/meta-data" ] || fail "meta-data conserve (instance-id perime)"
[ -f "$R/boot/firmware/config.txt" ] || fail "fichier du firmware supprime"

# cloud-init remis a neuf.
[ ! -e "$R/var/lib/cloud/instances" ] && [ ! -e "$R/var/lib/cloud/data" ] || fail "etat cloud-init conserve"
[ ! -e "$R/etc/netplan/50-cloud-init.yaml" ] || fail "netplan genere par cloud-init conserve"
[ -f "$R/etc/netplan/01-autre.yaml" ] || fail "un netplan qui n'est pas de cloud-init a ete supprime"

# Identite : machine-id VIDE (present), cles d'hote supprimees, configuration SSH conservee.
[ -f "$R/etc/machine-id" ] && [ ! -s "$R/etc/machine-id" ] || fail "machine-id non vide ou absent"
[ ! -e "$R/var/lib/dbus/machine-id" ] || fail "machine-id dbus conserve"
ls "$R"/etc/ssh/ssh_host_* > /dev/null 2>&1 && fail "cles SSH d'hote conservees"
[ -f "$R/etc/ssh/sshd_config" ] || fail "sshd_config supprime"
[ ! -e "$R/var/lib/systemd/random-seed" ] && [ ! -e "$R/var/lib/chrony/chrony.drift" ] || fail "graine aleatoire / derive conservees"

# Menage.
[ ! -e "$R/home/temp/.bash_history" ] && [ ! -e "$R/root/.bash_history" ] && [ ! -e "$R/home/autre/.viminfo" ] || fail "historique conserve"
[ ! -e "$R/home/autre/.cache" ] || fail "cache utilisateur conserve"
[ -f "$R/home/temp/.profile" ] || fail "un fichier de configuration utilisateur a ete supprime"
[ ! -s "$R/var/log/syslog" ] || fail "journal non vide"
[ -f "$R/var/log/syslog" ] || fail "fichier de journal supprime (doit etre vide, pas absent)"
[ ! -e "$R/var/log/syslog.1" ] && [ ! -e "$R/var/log/auth.log.2.gz" ] || fail "journaux archives conserves"
[ -z "$(ls -A "$R/var/log/journal")" ] || fail "journal systemd conserve"
[ -z "$(ls -A "$R/var/lib/apt/lists")" ] && [ ! -e "$R/var/cache/apt/archives/x.deb" ] || fail "cache apt conserve"
[ -z "$(ls -A "$R/tmp")" ] && [ -z "$(ls -A "$R/var/tmp")" ] || fail "fichiers temporaires conserves"

# Idempotent : relancer ne casse rien.
echo "MASTER" | bash "$SCRIPT" > /dev/null 2>&1 || fail "second passage en echec"
[ ! -s "$R/etc/machine-id" ] || fail "machine-id non vide au second passage"

# --- options et garde-fous -------------------------------------------------------------------------

build_tree
bash "$SCRIPT" --yes --keep-user > /dev/null 2>&1 || fail "--yes --keep-user refuse"
[ ! -s "$R/etc/machine-id" ] || fail "--yes n'a pas execute le menage"
if bash "$SCRIPT" --nimportequoi > /dev/null 2>&1; then fail "une option inconnue aurait du echouer"; fi

# Un nom de compte inattendu n'est jamais passe a une commande de suppression.
build_tree
if SUDO_USER='x; rm -rf /' bash "$SCRIPT" --yes > /dev/null 2>&1; then fail "nom de compte dangereux accepte"; fi
[ -s "$R/etc/machine-id" ] || fail "des fichiers ont ete modifies malgre un nom de compte invalide"

# Hors simulation : root et cloud-init exiges (jamais lance par erreur sur autre chose).
if [ "$(id -u)" -ne 0 ]; then
    if env -u PREPARE_MASTER_DRY bash "$SCRIPT" --yes > /dev/null 2>&1; then fail "lance sans etre root"; fi
fi

echo "OK : image/prepare-master.sh"
