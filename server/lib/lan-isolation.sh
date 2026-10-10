# Isolement des machines de l'expo - source par deploy-server.sh.
#
# Les machines n'ont pas acces a internet : leurs paquets passent par le
# cache apt du serveur (stack apt-cache), qui lui a acces a internet. Appele
# en fin de deploiement, une fois le cache en place. EXPOLAB_LAN_INTERNET=1
# laisse le NAT actif (lab qui a encore besoin d'internet cote machines).
# Reactiver temporairement : incus/lan-internet.sh on

isolate_lan() {
    if [ "${EXPOLAB_LAN_INTERNET:-0}" = "1" ]; then
        echo "[=] EXPOLAB_LAN_INTERNET=1 : le NAT d'expo-lan reste actif (machines avec acces a internet)."
        return 0
    fi
    if [ "$(incus network get expo-lan ipv4.nat 2>/dev/null)" = "true" ]; then
        echo "[+] Isolement des machines : NAT d'expo-lan desactive (leurs paquets passent par le cache apt du serveur)."
        incus network set expo-lan ipv4.nat=false
    else
        echo "[=] Machines deja isolees (pas de NAT sur expo-lan)."
    fi
}
