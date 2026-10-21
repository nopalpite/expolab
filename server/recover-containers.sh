#!/usr/bin/env bash
# Relance les conteneurs que Docker n'a pas su relancer apres un arret brutal.
#
# Apres une coupure de courant, Docker marque les conteneurs qui tournaient comme arretes
# avec le code 255, puis les relance selon leur politique de redemarrage. Il arrive qu'il
# n'y parvienne pas (vecu : dnsmasq, « error unmounting container ... layer not mounted ») :
# le conteneur reste arrete, sans nouvelle tentative, et l'expo perd son DNS et son DHCP.
#
# Ne relance QUE ce qui a ete coupe (code 255) ET doit tourner (always / unless-stopped) :
# un conteneur arrete volontairement (docker stop : code 0, 137 ou 143) n'est jamais touche.
# Lance par expolab-recover.timer (server/lib/resilience.sh) peu apres chaque demarrage,
# puis toutes les 2 minutes. Sans effet quand tout tourne.
set -uo pipefail

restarted=0
while read -r name; do
    [ -n "$name" ] || continue
    read -r policy code < <(docker inspect -f '{{.HostConfig.RestartPolicy.Name}} {{.State.ExitCode}}' "$name" 2>/dev/null) || continue
    case "$policy" in always|unless-stopped) ;; *) continue ;; esac
    [ "$code" = "255" ] || continue
    if docker start "$name" > /dev/null 2>&1; then
        echo "[+] $name relance (arrete par un arret brutal, code 255)."
        restarted=$((restarted + 1))
    else
        echo "[!] $name : echec de la relance (docker start)." >&2
    fi
done < <(docker ps -a --filter status=exited --format '{{.Names}}')

[ "$restarted" -eq 0 ] || echo "[=] $restarted conteneur(s) relance(s)."
exit 0
