# Isolement des machines de l'expo - source par deploy-server.sh.
#
# Les machines n'ont pas acces a internet : leurs paquets passent par le
# cache apt du serveur (stack apt-cache), qui lui a acces a internet. Appele
# en fin de deploiement, une fois le cache en place. EXPOLAB_LAN_INTERNET=1
# laisse le NAT tel quel. Reactiver temporairement : network/lan-internet.sh on

isolate_lan() {
    if [ "${EXPOLAB_LAN_INTERNET:-0}" = "1" ]; then
        echo "[=] EXPOLAB_LAN_INTERNET=1 : le NAT d'expo-lan reste tel quel."
        return 0
    fi
    bash "$REPO_ROOT/network/lan-internet.sh" off
}
