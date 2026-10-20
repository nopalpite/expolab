#!/usr/bin/env bash
# Test de la stack ntp (serveur de temps) :
#  - server/stacks/ntp/entrypoint.sh : configuration de chrony generee a partir du dossier
#    de reglages (mode, serveurs amont, reseaux autorises, repli sur l'horloge locale) ;
#  - server/lib/ntp.sh : fichiers par defaut jamais ecrases, chrony de l'hote desactive.
# Sans lancer chrony, sans Docker. Lance en CI et a la main.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENTRYPOINT="$REPO/server/stacks/ntp/entrypoint.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail() { echo "ECHEC : $*" >&2; exit 1; }

render() {  # render : ecrit $WORK/chrony.conf a partir de $WORK/cfg/
    rm -f "$WORK/chrony.conf"
    NTP_CONFIG_DIR="$WORK/cfg" NTP_OUT="$WORK/chrony.conf" NTP_RENDER_ONLY=1 \
        sh "$ENTRYPOINT" > "$WORK/out.log" 2> "$WORK/err.log"
}
conf="$WORK/chrony.conf"
has() { grep -qxF -- "$1" "$conf" || fail "ligne absente de la config : '$1'"; }
hasnt() { ! grep -qE -- "$1" "$conf" || fail "ligne inattendue dans la config : /$1/"; }

# --- entrypoint -----------------------------------------------------------------------

# Dossier de reglages absent : expo-lan seulement, repli local, pas de serveur amont.
mkdir -p "$WORK/cfg"
render
has "allow 10.42.0.0/24"
has "local stratum 10"
has "makestep 1 3"
has "cmdport 0"
hasnt '^server '

# Mode en ligne : serveurs amont, avec ou sans espaces / commentaires / lignes vides.
printf 'online\n' > "$WORK/cfg/mode"
printf '# serveurs\n0.pool.ntp.org\n\n  ntp.ubuntu.com  \n192.168.1.1\n' > "$WORK/cfg/upstream.conf"
printf '10.42.0.0/24\n10.66.66.0/24\n' > "$WORK/cfg/allow.conf"
render
has "server 0.pool.ntp.org iburst"
has "server ntp.ubuntu.com iburst"
has "server 192.168.1.1 iburst"
has "allow 10.42.0.0/24"
has "allow 10.66.66.0/24"
has "local stratum 10"

# Mode hors ligne : aucun serveur amont contacte, mais le serveur sert son horloge.
printf 'offline\n' > "$WORK/cfg/mode"
printf 'off\n' > "$WORK/cfg/local-fallback"   # ignore : sans repli, un serveur hors ligne ne servirait rien d'utile
render
hasnt '^server '
has "local stratum 10"

# Repli desactive en mode en ligne : pas de "local".
printf 'online\n' > "$WORK/cfg/mode"
render
has "server 0.pool.ntp.org iburst"
hasnt '^local '

# Lignes invalides : ignorees avec un message, le reste est conserve, jamais d'injection.
printf '0.pool.ntp.org\nmauvais nom\nevil;rm -rf\nok.example.org extra\n$(touch x)\nbon-serveur.fr\n' > "$WORK/cfg/upstream.conf"
printf '10.42.0.0/24\nnimporte quoi\n10.0.0.0/8 extra\n300.1.1.1x\n300.1.1.1\n10.0.0.0/33\n10.0.0\n1.2.3.4.5\n10.0.0.0/\n10.0.0.0/a\n192.168.1.5\n' > "$WORK/cfg/allow.conf"
render
has "server 0.pool.ntp.org iburst"
has "server bon-serveur.fr iburst"
[ "$(grep -c '^server ' "$conf")" = "2" ] || fail "des serveurs invalides ont ete retenus"
has "allow 10.42.0.0/24"
has "allow 192.168.1.5"   # une adresse seule est valide
[ "$(grep -c '^allow ' "$conf")" = "2" ] || fail "des reseaux invalides ont ete retenus : $(grep '^allow ' "$conf" | tr '\n' ' ')"
grep -q "ligne invalide ignoree" "$WORK/err.log" || fail "aucun message pour les lignes invalides"
! grep -qE 'rm -rf|touch' "$conf" || fail "contenu injecte dans la config"

# Mode inconnu : refus net plutot qu'un comportement devine.
printf 'bizarre\n' > "$WORK/cfg/mode"
if NTP_CONFIG_DIR="$WORK/cfg" NTP_OUT="$WORK/chrony.conf" NTP_RENDER_ONLY=1 sh "$ENTRYPOINT" > /dev/null 2> "$WORK/err.log"; then
    fail "un mode invalide aurait du faire echouer"
fi
grep -q "mode invalide" "$WORK/err.log" || fail "message 'mode invalide' absent"

# --- server/lib/ntp.sh -----------------------------------------------------------------

mkdir -p "$WORK/bin" "$WORK/server/lib"
cp "$REPO/server/lib/ntp.sh" "$WORK/server/lib/"
cat > "$WORK/bin/systemctl" <<'EOT'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_SYSTEMCTL_LOG"
# seul `is-active --quiet chrony` depend de FAKE_CHRONY_ACTIVE
[ "$1" = "is-active" ] && { [ -n "${FAKE_CHRONY_ACTIVE:-}" ]; exit; }
exit 0
EOT
chmod +x "$WORK/bin/systemctl"
export PATH="$WORK/bin:$PATH" FAKE_SYSTEMCTL_LOG="$WORK/systemctl.log"
export SCRIPT_DIR="$WORK/server"
# shellcheck disable=SC1091
source "$WORK/server/lib/ntp.sh"

STACK="$WORK/server/stacks/ntp"
: > "$FAKE_SYSTEMCTL_LOG"
setup_ntp
[ "$(cat "$STACK/config/mode")" = "online" ] || fail "mode par defaut incorrect"
[ "$(cat "$STACK/config/allow.conf")" = "10.42.0.0/24" ] || fail "reseau autorise par defaut incorrect"
[ "$(cat "$STACK/config/local-fallback")" = "on" ] || fail "repli local par defaut incorrect"
grep -q "pool.ntp.org" "$STACK/config/upstream.conf" || fail "serveurs amont par defaut absents"
[ -d "$STACK/data" ] && [ -d "$STACK/run" ] || fail "dossiers data/run absents"
# chrony desactive sa socket de commande si ce dossier est plus ouvert que 750 (invisible sous Windows)
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) ;;
    *) [ "$(stat -c %a "$STACK/run")" = "750" ] || fail "dossier run en mode $(stat -c %a "$STACK/run") au lieu de 750" ;;
esac
! grep -q "disable" "$FAKE_SYSTEMCTL_LOG" || fail "chrony de l'hote desactive alors qu'il n'est pas actif"

# Les choix de l'operateur ne sont jamais ecrases.
printf 'offline\n' > "$STACK/config/mode"
printf '192.168.50.0/24\n' > "$STACK/config/allow.conf"
printf 'off\n' > "$STACK/config/local-fallback"
printf 'ntp.interne\n' > "$STACK/config/upstream.conf"
setup_ntp
[ "$(cat "$STACK/config/mode")" = "offline" ] || fail "mode ecrase"
[ "$(cat "$STACK/config/allow.conf")" = "192.168.50.0/24" ] || fail "reseaux autorises ecrases"
[ "$(cat "$STACK/config/local-fallback")" = "off" ] || fail "repli local ecrase"
[ "$(cat "$STACK/config/upstream.conf")" = "ntp.interne" ] || fail "serveurs amont ecrases"

# Migration : le chrony de l'hote est arrete et desactive (il tient le port 123).
: > "$FAKE_SYSTEMCTL_LOG"
FAKE_CHRONY_ACTIVE=1 setup_ntp > "$WORK/out.log"
grep -q "^disable --now chrony$" "$FAKE_SYSTEMCTL_LOG" || fail "chrony de l'hote non desactive"
grep -q "chrony de l'hote" "$WORK/out.log" || fail "pas de message de migration"

# Coupure de courant : le fichier PID du demarrage precedent (dossier de l'hote) est retire
# AVANT de lancer chronyd, sinon il se croit deja lance et redemarre en boucle.
pid_line="$(grep -n 'rm -f /run/chrony/chronyd.pid' "$ENTRYPOINT" | cut -d: -f1)"
exec_line="$(grep -n '^exec chronyd' "$ENTRYPOINT" | cut -d: -f1)"
[ -n "$pid_line" ] && [ "$pid_line" -lt "$exec_line" ] || fail "PID perime de chronyd non retire avant son lancement"

echo "OK : stack ntp (entrypoint et fichiers)"
