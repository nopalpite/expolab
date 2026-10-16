#!/usr/bin/env bash
# Prises connectees Matter (stack matter) en ligne de commande - pour essayer une prise avant
# la page d'administration. Parle a python-matter-server par server/matter_cmd.py, lance dans
# son conteneur.
#
#   sudo ./matter.sh info                 version du controleur, Bluetooth, etat
#   sudo ./matter.sh wifi                 transmet le Wi-Fi de l'expo (reglages du flasheur) au controleur
#   sudo ./matter.sh ajouter <code>       approvisionne une prise (code a 11 chiffres ou "MT:...")
#   sudo ./matter.sh prises               liste lisible : etat, puissance, energie
#   sudo ./matter.sh brut                 tous les attributs (JSON) - pour voir ce qu'une prise expose
#   sudo ./matter.sh allumer <node_id>    | eteindre <node_id>
#   sudo ./matter.sh identifier <node_id> fait clignoter la prise
#   sudo ./matter.sh retirer <node_id>    desappaire la prise
#
# Ajouter une prise : `wifi` une fois, puis `ajouter` avec la prise neuve branchee pres du serveur.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER=expolab-matter
SETTINGS="$SCRIPT_DIR/stacks/sd-flasher/data/settings.json"

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi
if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ]; then
    echo "Le controleur Matter ($CONTAINER) ne tourne pas : deployer la stack matter (deploy-server.sh)." >&2
    exit 1
fi

cmd() {   # cmd <commande> [arguments JSON | -]
    docker exec -i "$CONTAINER" python3 /expolab/matter_cmd.py "$@"
}

node_arg() {
    [[ "${1:-}" =~ ^[0-9]+$ ]] || { echo "Numero de prise attendu (node_id, voir : $0 prises)." >&2; exit 2; }
}

case "${1:-}" in
    info) cmd info ;;
    wifi)
        # SSID et mot de passe pris dans les reglages du flasheur, passes sur l'entree standard :
        # jamais sur une ligne de commande (visible dans la liste des processus).
        python3 - "$SETTINGS" <<'PY' | cmd set_wifi_credentials -
import json, sys
s = json.load(open(sys.argv[1]))
if not s.get("wifi_ssid") or not s.get("wifi_psk"):
    sys.exit("Aucun Wi-Fi dans les reglages du flasheur (Cartes SD > Reglages).")
print(json.dumps({"ssid": s["wifi_ssid"], "credentials": s["wifi_psk"]}))
PY
        ;;
    ajouter)
        code="${2:-}"
        [[ "$code" =~ ^(MT:[A-Z0-9.:-]+|[0-9 -]{11,21})$ ]] || { echo "Code d'appairage attendu (11 chiffres ou MT:...)." >&2; exit 2; }
        code="${code//[ -]/}"
        echo "[+] Approvisionnement (Bluetooth puis Wi-Fi) : jusqu'a quelques minutes..."
        cmd commission_with_code "{\"code\": \"$code\", \"network_only\": false}"
        ;;
    prises) cmd resume ;;
    brut) cmd get_nodes ;;
    allumer|eteindre)
        node_arg "${2:-}"
        name=On; [ "$1" = eteindre ] && name=Off
        cmd device_command "{\"node_id\": $2, \"endpoint_id\": 1, \"cluster_id\": 6, \"command_name\": \"$name\", \"payload\": {}}"
        ;;
    identifier)
        node_arg "${2:-}"
        cmd device_command "{\"node_id\": $2, \"endpoint_id\": 1, \"cluster_id\": 3, \"command_name\": \"Identify\", \"payload\": {\"identifyTime\": 10}}"
        ;;
    retirer)
        node_arg "${2:-}"
        cmd remove_node "{\"node_id\": $2}"
        ;;
    *) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
