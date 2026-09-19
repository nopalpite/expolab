# expolab

Lab de simulation d'infra d'exposition : une flotte de "faux" Raspberry Pi
(conteneurs Incus avec MAC propre, DHCP, SSH) hebergee sur un Raspberry Pi 5
physique, avec un serveur dedie DHCP/DNS/reverse-proxy (TLS auto-signe) pour
ce reseau simule.

## Architecture reseau

Le reseau `expo-lan` cree par `network-setup.sh` est un **bridge Incus
purement virtuel**, independant de l'interface physique de l'hote (fonctionne
identiquement en Wi-Fi ou en Ethernet). Objectif : simuler un reseau local
pour la flotte, pas rendre les faux Pi visibles/joignables depuis le reseau
physique ou Internet.

Le conteneur `expo-gw` (voir `gateway/`) est le serveur dedie DHCP/DNS/
reverse-proxy de ce LAN simule : il prend le relais du DHCP integre d'Incus
des qu'il est deploye.

La patte WAN/VPN (voir `vpn/`) tourne sur l'**hote** directement, pas dans
un conteneur : donner une deuxieme interface reseau a `expo-gw` demanderait
du macvlan sur l'interface physique, qui ne fonctionne pas de facon fiable
en Wi-Fi (meme constat que pour `expo-lan`). L'hote a deja naturellement les
deux pattes - son interface physique (WAN reel) et le bridge `expo-lan`
(LAN simule) - c'est le point de frontiere naturel, WireGuard y route le
trafic des clients VPN vers `10.42.0.0/24`.

Deux domaines DNS distincts, resolus par le `dnsmasq` d'`expo-gw` :
- `<nom>.expolab.lan` — enregistrement DHCP automatique, pointe vers la
  **vraie IP** de chaque faux Pi (SSH, acces direct)
- `<nom>.web.expolab.lan` — wildcard fixe vers `expo-gw` (10.42.0.10), c'est
  le nom que doit utiliser un client HTTPS pour passer par le reverse-proxy
  Caddy

Ne pas les confondre : un client qui demande `https://pi-01.expolab.lan`
tape directement sur pi-01 (qui n'ecoute qu'en HTTP, port 80) et contourne
Caddy entierement.

## Prerequis

- Raspberry Pi 5 avec Raspberry Pi OS (ou Debian/Ubuntu Server 64-bit) a jour
- Une connexion reseau sur l'hote (Wi-Fi ou Ethernet, peu importe : `expo-lan`
  n'en depend pas) uniquement pour qu'Incus puisse telecharger l'image de
  base et que les conteneurs fassent leurs `apt-get install` pendant le
  provisioning

## Mise en route (a executer directement sur le Pi 5)

```bash
sudo ./incus/install.sh
# se deconnecter/reconnecter pour prendre en compte le groupe incus-admin

sudo ./incus/network-setup.sh

./fleet/deploy-fleet.sh

./gateway/deploy-gateway.sh

sudo ./vpn/install.sh
sudo ./vpn/add-peer.sh mon-laptop
```

`deploy-fleet.sh` est idempotent : relancez-le apres avoir modifie
`fleet/inventory.yaml` pour ajouter/retirer des faux Pi, seules les entrees
manquantes sont creees. Relancez `gateway/deploy-gateway.sh` apres coup pour
regenerer le Caddyfile avec les nouvelles entrees.

## Verifier / se connecter

```bash
incus list                       # etat + IP de chaque faux Pi (+ expo-gw)
incus exec pi-01 -- bash         # shell direct dans le conteneur
ssh pi@<ip-de-pi-01>             # mot de passe: raspberry (a changer si besoin)

# Reverse proxy HTTPS (TLS auto-signe, cert "not trusted" attendu sans
# importer le CA interne de Caddy) :
incus exec expo-gw -- curl -sk https://pi-01.web.expolab.lan

# VPN : recuperer /etc/wireguard/peers/mon-laptop.conf sur le poste client
# (WireGuard app ou wg-quick), puis une fois connecte :
ssh pi@10.42.0.181                       # acces direct a un faux Pi via le VPN
curl -k https://pi-01.web.expolab.lan     # (DNS = 10.42.0.10 pousse par le VPN)
```

## Rollback (retour a l'etat initial)

Deux niveaux, a combiner :

**1. Le plus fiable : image disque AVANT de commencer.** Les scripts
d'installation touchent au reseau systeme et installent des paquets ; le
seul rollback garanti a 100% reste une image complete de la carte SD/NVMe
prise avant `install.sh` (Raspberry Pi Imager -> "Sauvegarder", ou
`dd if=/dev/mmcblk0 of=backup.img bs=4M status=progress` depuis un autre
poste). Restaurer cette image = retour exact a l'etat d'avant, quoi qu'il
se soit passe entre-temps.

**2. Rollback scripte (rapide, pour un usage courant sur le lab) :**

```bash
sudo ./rollback.sh          # demande confirmation avant chaque suppression
sudo ./rollback.sh --yes    # sans confirmation
```

Ce script defait dans l'ordre exactement ce que `install.sh` /
`network-setup.sh` / `deploy-fleet.sh` / `deploy-gateway.sh` /
`vpn/install.sh` ont mis en place :

1. `vpn/uninstall.sh` — arrete WireGuard, supprime tous les pairs et
   desinstalle wireguard-tools
2. `gateway/teardown-gateway.sh` — supprime `expo-gw` et reactive le DHCP
   integre d'Incus sur `expo-lan` (secours)
3. `fleet/teardown-fleet.sh` — supprime les instances de la flotte
4. `incus/network-teardown.sh` — supprime les profils `fake-pi`/`expo-gw` et
   le reseau `expo-lan`
5. `incus/uninstall.sh` — desinstalle Incus, retire le depot Zabbly et
   `/var/lib/incus`

Chaque etape est aussi utilisable seule (ex: `./fleet/teardown-fleet.sh`
pour ne reinitialiser que la flotte sans desinstaller Incus). Limite connue :
un `apt purge` ne garantit jamais un retrait 100% parfait (fichiers de
config residuels, etc.) — d'ou la recommandation de l'image disque si un
retour a l'etat initial *exact* est requis.

## Structure

```
incus/
  install.sh                # installe Incus sur l'hote
  uninstall.sh               # desinstalle Incus (symetrique de install.sh)
  network-setup.sh           # cree le reseau expo-lan (bridge isole) + profil fake-pi
  network-teardown.sh        # defait network-setup.sh
  profiles/fake-pi.yaml      # profil Incus flotte (reseau + limites CPU/RAM)
  profiles/expo-gw.yaml      # profil Incus gateway (reseau + limites CPU/RAM)
fleet/
  inventory.yaml             # liste declarative des faux Pi (nom, MAC, role)
  deploy-fleet.sh             # cree/provisionne les faux Pi manquants
  teardown-fleet.sh           # supprime les faux Pi de l'inventaire
  provision-fakepi.sh         # script de premier boot execute dans chaque conteneur
gateway/
  deploy-gateway.sh           # cree/provisionne expo-gw, bascule le DHCP de expo-lan
  teardown-gateway.sh         # supprime expo-gw, reactive le DHCP integre d'Incus
  provision-gateway.sh        # script de premier boot execute dans expo-gw
  dnsmasq.conf                # config DHCP+DNS (plage, domaine expolab.lan)
  resolv.dnsmasq.upstream     # DNS amont utilise par dnsmasq (evite la boucle)
  render-caddyfile.py         # genere le Caddyfile a partir de fleet/inventory.yaml
vpn/
  install.sh                  # installe WireGuard sur l'hote, cree wg0
  uninstall.sh                 # desinstalle WireGuard (symetrique de install.sh)
  add-peer.sh                  # ajoute un pair VPN + genere sa config client
  remove-peer.sh                # retire un pair VPN
rollback.sh                   # orchestre les 5 teardown dans le bon ordre
```

## A venir

- Renouvellement/rotation des certs Caddy au-dela du lab (hors scope d'un
  environnement isole)
- Redirection de port sur le routeur du reseau reel pour un acces VPN
  depuis l'exterieur (specifique a la box de l'utilisateur, hors perimetre
  scriptable)
