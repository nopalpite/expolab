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
# --remove-accounts (copie HORS LIGNE uniquement, PREPARE_MASTER_ROOT obligatoire) : supprime
#   aussi tous les comptes ordinaires (UID 1000 a 59999) avec leur dossier personnel, leurs droits
#   sudo poses par cloud-init, les cles SSH de root, et les fichiers que le flasheur ecrit lui-meme
#   (proxy apt, serveur de temps). Utilise par le flasheur a la capture d'une carte : l'image se
#   comporte alors comme une Ubuntu neuve, le flasheur cree le compte et applique TOUS ses reglages.
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
REMOVE_ACCOUNTS=0
for arg in "$@"; do
    case "$arg" in
        --yes) ASSUME_YES=1 ;;
        --keep-user) KEEP_USER=1 ;;
        --remove-accounts) REMOVE_ACCOUNTS=1 ;;
        *) echo "Option inconnue : $arg" >&2; echo "Usage : sudo $0 [--yes] [--keep-user] [--remove-accounts]" >&2; exit 2 ;;
    esac
done
if [ "$REMOVE_ACCOUNTS" -eq 1 ] && [ -z "$ROOT" ]; then
    # Jamais sur la machine en marche : on y supprimerait le compte de la session en cours.
    echo "--remove-accounts ne s'utilise que sur une copie hors ligne (PREPARE_MASTER_ROOT)." >&2
    exit 2
fi

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

# --- comptes (copie hors ligne) -------------------------------------------------------------------
if [ "$REMOVE_ACCOUNTS" -eq 1 ]; then
    accounts=""
    if [ -f "$ROOT/etc/passwd" ]; then
        accounts="$(awk -F: '$3 >= 1000 && $3 <= 59999 {print $1}' "$ROOT/etc/passwd")"
    fi
    say "Comptes ordinaires supprimes : ${accounts:-aucun}"
    for name in $accounts; do
        home="$(awk -F: -v u="$name" '$1 == u {print $6}' "$ROOT/etc/passwd")"
        # Seulement un dossier /home/<nom> : jamais un chemin inattendu (/, /root, /srv...).
        if [[ "$home" =~ ^/home/[A-Za-z0-9._-]+$ ]]; then
            rm -rf "$ROOT$home"
        fi
        rm -f "$ROOT/var/mail/$name" "$ROOT/var/spool/mail/$name"
    done
    if [ -n "$accounts" ]; then
        names="$(printf '%s,' $accounts)"
        # Fichier par fichier, reecrit en place (memes droits) : lignes des comptes retires, groupes
        # personnels portant leur nom, et leur nom retire des listes de membres des autres groupes.
        clean() {   # clean <fichier> <programme awk>
            [ -f "$1" ] || return 0
            awk -F: -v OFS=: -v names="$names" "$2" "$1" > "$1.expolab-tmp" && cat "$1.expolab-tmp" > "$1" && rm -f "$1.expolab-tmp"
        }
        members='function strip(list,   n, i, a, out) { n = split(list, a, ","); out = ""; for (i = 1; i <= n; i++) if (a[i] != "" && !gone[a[i]]) out = out (out == "" ? "" : ",") a[i]; return out }
                 BEGIN { n = split(names, a, ","); for (i = 1; i <= n; i++) if (a[i] != "") gone[a[i]] = 1 }'
        clean "$ROOT/etc/passwd"  "$members"' !gone[$1]'
        clean "$ROOT/etc/shadow"  "$members"' !gone[$1]'
        clean "$ROOT/etc/group"   "$members"' !gone[$1] { $4 = strip($4); print }'
        clean "$ROOT/etc/gshadow" "$members"' !gone[$1] { $3 = strip($3); $4 = strip($4); print }'
        clean "$ROOT/etc/subuid"  "$members"' !gone[$1]'
        clean "$ROOT/etc/subgid"  "$members"' !gone[$1]'
    fi
    # Droits sudo et cles poses par cloud-init / le flasheur, cles de root.
    rm -f "$ROOT/etc/sudoers.d/90-cloud-init-users" "$ROOT/root/.ssh/authorized_keys"
    # Fichiers que le flasheur ecrit (sd-flasher/flasher.py) : il les reecrit a chaque flash ; laisses
    # dans l'image, ils garderaient l'ancienne valeur si le reglage est vide au flash suivant.
    rm -f "$ROOT/etc/apt/apt.conf.d/01expolab-proxy" \
          "$ROOT/etc/chrony/sources.d/expolab.sources" "$ROOT/etc/chrony/conf.d/expolab.conf"
fi

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
