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

# --- --remove-accounts : copie hors ligne preparee par le flasheur a la capture ----------------------

build_tree
mkdir -p "$R/home/pi/.ssh" "$R/home/autre" "$R/etc/sudoers.d" "$R/root/.ssh" "$R/etc/apt/apt.conf.d" \
         "$R/etc/chrony/sources.d" "$R/etc/chrony/conf.d" "$R/var/mail" "$R/srv/expo"
cat > "$R/etc/passwd" <<'EOT'
root:x:0:0:root:/root:/bin/bash
_chrony:x:105:110::/var/lib/chrony:/usr/sbin/nologin
pi:x:1000:1000:pi:/home/pi:/bin/bash
autre:x:1001:1001::/home/autre:/bin/bash
bizarre:x:1002:1002::/srv/expo:/bin/bash
nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin
EOT
printf 'root:*:1:0:99999:7:::\npi:$6$sel$HACHE:1:0:99999:7:::\nautre:!:1:0:99999:7:::\nbizarre:!:1:0:99999:7:::\n' > "$R/etc/shadow"
cat > "$R/etc/group" <<'EOT'
root:x:0:
sudo:x:27:pi,autre
video:x:44:pi
pi:x:1000:
autre:x:1001:
bizarre:x:1002:
staff:x:50:
EOT
printf 'root:*::\nsudo:*::pi,autre\npi:!::\nautre:!::\nbizarre:!::\nstaff:*:pi:\n' > "$R/etc/gshadow"
printf 'pi:100000:65536\nautre:165536:65536\n' > "$R/etc/subuid"; cp "$R/etc/subuid" "$R/etc/subgid"
echo "ssh-ed25519 AAAA cle-personnelle" > "$R/home/pi/.ssh/authorized_keys"
echo "pi ALL=(ALL) NOPASSWD:ALL" > "$R/etc/sudoers.d/90-cloud-init-users"
echo "garde" > "$R/etc/sudoers.d/README"
echo "ssh-ed25519 AAAA cle-root" > "$R/root/.ssh/authorized_keys"
echo 'Acquire::http::Proxy "http://10.42.0.1:3142";' > "$R/etc/apt/apt.conf.d/01expolab-proxy"
echo "server 10.42.0.1 iburst prefer" > "$R/etc/chrony/sources.d/expolab.sources"
echo "authselectmode ignore" > "$R/etc/chrony/conf.d/expolab.conf"
echo "courrier" > "$R/var/mail/pi"; echo "application" > "$R/srv/expo/app"

bash "$SCRIPT" --yes --keep-user --remove-accounts > "$WORK/out.log" 2>&1 || fail "--remove-accounts en echec : $(cat "$WORK/out.log")"
grep -q "Comptes ordinaires supprimes : pi" "$WORK/out.log" || fail "comptes supprimes non annonces"
[ "$(cut -d: -f1 "$R/etc/passwd" | tr '\n' ' ')" = "root _chrony nobody " ] || fail "passwd : $(cut -d: -f1 "$R/etc/passwd" | tr '\n' ' ')"
[ "$(cut -d: -f1 "$R/etc/shadow" | tr '\n' ' ')" = "root " ] || fail "shadow non nettoye"
grep -qx "sudo:x:27:" "$R/etc/group" && grep -qx "video:x:44:" "$R/etc/group" || fail "membres des groupes non retires : $(cat "$R/etc/group")"
! grep -q "^pi:\|^autre:\|^bizarre:" "$R/etc/group" || fail "groupes personnels conserves"
grep -qx "staff:x:50:" "$R/etc/group" && grep -qx "root:x:0:" "$R/etc/group" || fail "un groupe systeme a ete touche"
grep -qx "sudo:\*::" "$R/etc/gshadow" && grep -qx "staff:\*::" "$R/etc/gshadow" || fail "gshadow non nettoye : $(cat "$R/etc/gshadow")"
[ ! -s "$R/etc/subuid" ] && [ ! -s "$R/etc/subgid" ] || fail "subuid/subgid non nettoyes"
[ ! -e "$R/home/pi" ] && [ ! -e "$R/home/autre" ] || fail "dossiers personnels conserves"
[ -f "$R/srv/expo/app" ] || fail "un dossier personnel hors /home a ete supprime (chemin inattendu : jamais touche)"
[ ! -e "$R/var/mail/pi" ] || fail "courrier du compte conserve"
[ ! -e "$R/etc/sudoers.d/90-cloud-init-users" ] && [ -f "$R/etc/sudoers.d/README" ] || fail "sudoers mal nettoye"
[ ! -e "$R/root/.ssh/authorized_keys" ] || fail "cles SSH de root conservees"
[ ! -e "$R/etc/apt/apt.conf.d/01expolab-proxy" ] && [ ! -e "$R/etc/chrony/sources.d/expolab.sources" ] && [ ! -e "$R/etc/chrony/conf.d/expolab.conf" ] \
    || fail "fichiers du flasheur conserves"
! grep -rq "HACHE\|cle-personnelle\|cle-root" "$R" || fail "un secret de compte est encore dans la copie"
bash "$SCRIPT" --yes --keep-user --remove-accounts > /dev/null 2>&1 || fail "--remove-accounts : second passage en echec"

# Jamais sur la machine en marche (sans PREPARE_MASTER_ROOT) : refuse avant de rien toucher.
if env -u PREPARE_MASTER_ROOT bash "$SCRIPT" --yes --remove-accounts > "$WORK/out.log" 2>&1; then fail "--remove-accounts accepte sans copie hors ligne"; fi
grep -q "hors ligne" "$WORK/out.log" || fail "refus sans explication"

echo "OK : image/prepare-master.sh"
