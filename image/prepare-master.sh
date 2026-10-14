#!/usr/bin/env bash
# Remet une machine "a neuf" juste avant de la CAPTURER comme image master, puis l'eteint.
# A lancer SUR la machine qui sert de master (Ubuntu Server + cloud-init), en root.
#
#   sudo image/prepare-master.sh [--yes] [--keep-user]
#
# Se connecter pour preparer le master avec un compte TEMPORAIRE (pas celui que le flasheur
# cree sur les cartes) : le script le supprime a la fin. Sinon cloud-init verrait un compte
# deja present sur chaque carte flashee, et rien ne garantit qu'il lui remette son mot de
# passe et ses cles.
#
# Ce que fait le script (rien n'est touche avant la confirmation) :
#   - graine du flasheur (system-boot) : remplacee par des fichiers NEUTRES. Elle contient le
#     mot de passe Wi-Fi en clair et le hache du mot de passe du compte : l'image ne doit pas les
#     embarquer. Le flasheur reecrit ces trois fichiers a chaque flash.
#   - cloud-init remis a neuf : la carte sera traitee comme une premiere installation.
#   - identite : /etc/machine-id vide, cles SSH d'hote supprimees (chaque carte aura les siennes).
#   - menage : cache et listes apt, journaux, historiques, fichiers temporaires.
#   - compte temporaire supprime, puis extinction. NE PAS redemarrer ensuite : un demarrage
#     refabriquerait l'identite que ce script vient d'effacer.
#
# Variables (surtout pour les tests) : PREPARE_MASTER_ROOT (prefixe toutes les ecritures),
# PREPARE_MASTER_BOOT (partition de demarrage, defaut <racine>/boot/firmware),
# PREPARE_MASTER_DRY=1 (aucune commande systeme : cloud-init, apt, journalctl, extinction...).
set -euo pipefail

ROOT="${PREPARE_MASTER_ROOT:-}"
BOOT="${PREPARE_MASTER_BOOT:-$ROOT/boot/firmware}"
DRY="${PREPARE_MASTER_DRY:-}"
ASSUME_YES=0
KEEP_USER=0

for arg in "$@"; do
    case "$arg" in
        --yes) ASSUME_YES=1 ;;
        --keep-user) KEEP_USER=1 ;;
        *) echo "Option inconnue : $arg" >&2; echo "Usage : sudo $0 [--yes] [--keep-user]" >&2; exit 2 ;;
    esac
done

say() { echo "[+] $*"; }

if [ -z "$DRY" ]; then
    if [ "$(id -u)" -ne 0 ]; then
        echo "Ce script doit etre lance en root (sudo)." >&2
        exit 1
    fi
    if ! command -v cloud-init >/dev/null 2>&1; then
        echo "cloud-init introuvable : ce script prepare un master Ubuntu Server (voir image/README-master.md)." >&2
        exit 1
    fi
fi

# Compte temporaire = celui qui a lance sudo (jamais root, jamais un compte systeme).
TEMP_USER=""
if [ "$KEEP_USER" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    if ! [[ "$SUDO_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
        echo "Nom de compte inattendu : $SUDO_USER (suppression impossible, utiliser --keep-user)." >&2
        exit 1
    fi
    TEMP_USER="$SUDO_USER"
fi

cat <<EOF
Ce script va remettre cette machine a neuf pour en faire une image master :
  - remplacer la graine du flasheur (${BOOT}) par des fichiers neutres
  - remettre cloud-init a neuf, vider machine-id, supprimer les cles SSH d'hote
  - vider les caches apt, les journaux, les historiques
  - ${TEMP_USER:+supprimer le compte temporaire '$TEMP_USER' (et son dossier personnel), puis }eteindre la machine
Elle ne demarrera plus normalement tant qu'elle n'aura pas ete flashee a nouveau.
EOF
if [ "$ASSUME_YES" -ne 1 ]; then
    read -r -p "Tape MASTER pour continuer : " answer || answer=""
    if [ "$answer" != "MASTER" ]; then
        echo "Annule : rien n'a ete modifie."
        exit 1
    fi
fi

# --- graine du flasheur ---------------------------------------------------------------------

if [ -d "$BOOT" ]; then
    say "Graine cloud-init (system-boot) : remplacee par des fichiers neutres"
    printf '#cloud-config\n' > "$BOOT/user-data"
    cat > "$BOOT/network-config" <<'EOT'
network:
  version: 2
  ethernets:
    all-eth:
      match:
        name: "e*"
      dhcp4: true
      optional: true
EOT
    rm -f "$BOOT/meta-data"
else
    echo "Partition de demarrage introuvable ($BOOT) : graine non nettoyee." >&2
fi

# --- cloud-init -----------------------------------------------------------------------------

say "cloud-init remis a neuf"
if [ -z "$DRY" ]; then
    # Les options ont varie selon les versions : on essaie du plus complet au plus simple, puis
    # le menage ci-dessous garantit le resultat quoi qu'il arrive.
    cloud-init clean --logs --seed --machine-id --configs all >/dev/null 2>&1 \
        || cloud-init clean --logs --seed >/dev/null 2>&1 \
        || cloud-init clean >/dev/null 2>&1 \
        || true
fi
rm -rf "$ROOT/var/lib/cloud/instance" "$ROOT/var/lib/cloud/instances" "$ROOT/var/lib/cloud/data" \
       "$ROOT/var/lib/cloud/sem" "$ROOT/var/lib/cloud/seed" "$ROOT/var/lib/cloud/handlers" "$ROOT/var/lib/cloud/scripts"
rm -f "$ROOT/etc/netplan/50-cloud-init.yaml" "$ROOT/var/log/cloud-init.log" "$ROOT/var/log/cloud-init-output.log"

# --- identite ----------------------------------------------------------------------------------

say "Identite : machine-id vide, cles SSH d'hote et graine aleatoire supprimees"
install -d "$ROOT/etc"
: > "$ROOT/etc/machine-id"
if [ -f "$ROOT/var/lib/dbus/machine-id" ] && [ ! -L "$ROOT/var/lib/dbus/machine-id" ]; then
    rm -f "$ROOT/var/lib/dbus/machine-id"
fi
rm -f "$ROOT"/etc/ssh/ssh_host_*
rm -f "$ROOT/var/lib/systemd/random-seed" "$ROOT/var/lib/chrony/chrony.drift"

# --- menage ---------------------------------------------------------------------------------------

say "Menage : apt, journaux, historiques, fichiers temporaires"
if [ -z "$DRY" ]; then
    apt-get clean >/dev/null 2>&1 || true
    journalctl --rotate >/dev/null 2>&1 || true
    journalctl --vacuum-time=1s >/dev/null 2>&1 || true
fi
rm -rf "$ROOT"/var/lib/apt/lists/* "$ROOT"/var/cache/apt/archives/*.deb
rm -rf "$ROOT"/var/log/journal/*
if [ -d "$ROOT/var/log" ]; then
    find "$ROOT/var/log" -type f \( -name '*.gz' -o -name '*.xz' -o -name '*.[0-9]' -o -name '*.old' \) -delete
    find "$ROOT/var/log" -type f -exec truncate -s 0 {} +
fi
rm -rf "$ROOT"/tmp/* "$ROOT"/var/tmp/*
for home in "$ROOT"/home/* "$ROOT/root"; do
    [ -d "$home" ] || continue
    rm -f "$home"/.bash_history "$home"/.viminfo "$home"/.lesshst "$home"/.python_history
    rm -rf "$home"/.cache
done

# --- fin : compte temporaire et extinction ---------------------------------------------------------

if [ -n "$DRY" ]; then
    say "Simulation : ${TEMP_USER:+suppression du compte '$TEMP_USER' et }extinction non executees."
    exit 0
fi

sync
if [ -n "$TEMP_USER" ]; then
    say "Compte temporaire '$TEMP_USER' : suppression puis extinction dans quelques secondes (ta session va se fermer)."
    # Un service independant de la session : l'utilisateur ne peut pas etre supprime tant qu'il
    # a des processus (dont ce script et le shell qui l'a lance).
    systemd-run --no-block --collect --unit=expolab-master-finish /bin/bash -c \
        "sleep 3; pkill -KILL -u '$TEMP_USER' || true; sleep 1; deluser --remove-home '$TEMP_USER' || true; sync; poweroff"
else
    say "Extinction dans quelques secondes."
    systemd-run --no-block --collect --unit=expolab-master-finish /bin/bash -c "sleep 3; sync; poweroff"
fi
echo "Quand la machine est eteinte, retire la carte et capture-la. NE LA REDEMARRE PAS avant."
