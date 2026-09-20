# expolab

Lab de simulation d'infra d'exposition sur un Raspberry Pi 5 physique.

Deux couches bien separees :

- **La flotte de faux Raspberry Pi** (`incus/`, `fleet/`) - simulee via
  Incus, parce qu'en vraie vie ce sont de vrais boitiers separes qu'on ne
  peut evidemment pas dupliquer sur une seule machine de lab. C'est un
  artifice propre au lab.
- **Le serveur d'expo** (`server/`, `vpn/`, `webui/`) - DHCP/DNS,
  reverse-proxy, et les services applicatifs (Dockhand, webui, Bastion,
  plus tard Gitea) tournent en Docker **directement sur l'hote**, pas
  dans Incus. C'est exactement ce qui sera deploye sur le vrai serveur
  de l'exposition, qui n'aura pas besoin d'Incus du tout.

## Architecture reseau

Le reseau `expo-lan` cree par `incus/network-setup.sh` est un **bridge
Incus purement virtuel**, independant de l'interface physique de l'hote
(fonctionne identiquement en Wi-Fi ou en Ethernet). Objectif : simuler un
reseau local pour la flotte, pas rendre les faux Pi visibles/joignables
depuis le reseau physique ou Internet. L'hote porte lui-meme l'adresse
`10.42.0.1` sur ce bridge (c'est Incus qui la lui donne a la creation du
reseau).

`server/deploy-server.sh` deploie sur l'hote un stack Docker qui prend le
relais du DHCP integre d'Incus des qu'il est pret :
- **dnsmasq** (DHCP/DNS) en `network_mode: host`, lie a l'interface
  `expo-lan` - besoin d'un acces bas niveau (broadcast DHCP) qu'un reseau
  Docker isole ne permet pas
- **Caddy** (reverse-proxy TLS auto-signe), egalement en
  `network_mode: host`
- **Dockhand** (UI de gestion Docker) et la **webui** expolab
  (creation/suppression de faux Pi), sur le reseau Docker par defaut avec
  publication de port
- **Bastion** (https://github.com/nopalpite/bastion, dashboard +
  SSH/VNC web) en `network_mode: host` - image prete a l'emploi publiee
  sur GHCR (`ghcr.io/nopalpite/bastion`), identifiants par defaut
  `admin`/`raspberry`. Secrets (cle de session, cle de chiffrement des
  identifiants memorises) generes une seule fois au premier
  `deploy-server.sh` dans `server/bastion.env` (non versionne) ;
  inventaire de machines et donnees persistees dans `server/bastion/`
  (egalement non versionne, propre a chaque lab)

Le reverse-proxy n'expose PAS les faux Pi (ils n'ont pas vocation a etre
joignables en HTTPS individuellement) : il sert a exposer les services
applicatifs du serveur d'expo lui-meme, declares dans
`server/services.yaml`.

La **webui** (`webui/`, https://fleet.web.expolab.lan) est une petite app
Flask pour creer/supprimer des faux Pi depuis un navigateur (hostname,
role, identifiants SSH pre-remplis `pi`/`raspberry`) sans avoir a toucher
`fleet/inventory.yaml` a la main. Elle pilote Incus directement (le socket
Incus de l'hote est monte dans son conteneur Docker) et reutilise
`fleet/deploy-fleet.sh` comme unique source de verite (elle edite
l'inventaire puis declenche le meme script que la ligne de commande) -
`fleet/` est monte en direct (pas copie) pour que les deux facons de gerer
la flotte restent sur le meme fichier. **A savoir** : ca donne au conteneur
Docker de la webui un controle total sur Incus, pas seulement sur la
flotte. Compromis assume pour ce lab.

La patte WAN/VPN (`vpn/`) tourne aussi directement sur l'**hote** : WireGuard
route le trafic des clients VPN vers `10.42.0.0/24` (la meme logique que le
stack Docker - l'hote est le seul point qui porte naturellement les deux
pattes, WAN reel et LAN simule).

Deux domaines DNS distincts, resolus par `dnsmasq` :
- `<nom>.expolab.lan` — enregistrement DHCP automatique, pointe vers la
  **vraie IP** de chaque faux Pi - acces direct (SSH)
- `<nom>.web.expolab.lan` — wildcard fixe vers l'hote (`10.42.0.1`, ou
  tourne Caddy), c'est le nom que doit utiliser un client HTTPS pour
  passer par le reverse-proxy

Ne pas les confondre : les faux Pi n'ecoutent qu'en SSH, jamais en HTTPS -
il n'y a d'ailleurs aucun vhost `<nom-de-pi>.web.expolab.lan` genere pour
eux (voir plus haut, le reverse-proxy ne les concerne pas).

## Prerequis

- Raspberry Pi 5 avec Raspberry Pi OS (ou Debian/Ubuntu Server 64-bit) a
  jour
- Une connexion reseau sur l'hote (Wi-Fi ou Ethernet, peu importe :
  `expo-lan` n'en depend pas) pour qu'Incus/Docker puissent telecharger
  leurs images et que les conteneurs fassent leurs `apt-get install`
  pendant le provisioning
- Budget RAM a garder en tete sur un Pi 5 8 Go : 512 Mo par faux Pi
  (2,5 Go pour 5 faux Pi, ajustable dans `incus/profiles/fake-pi.yaml`) +
  le stack Docker (leger : dnsmasq/Caddy/webui sont de petites images,
  Dockhand un peu plus)
- Budget disque : la flotte vit sur un pool Incus dedie en
  copie-sur-ecriture (`fake-pi-pool`, driver btrfs, cree par
  `network-setup.sh` sur un fichier loop de 9 Gio - pas de
  repartitionnement de la carte SD), ~250-300 Mio par faux Pi supplementaire
  au lieu de dupliquer l'image de base a chaque fois. Incident reel sur ce
  lab : le pool "default" (driver "dir", cree par `install.sh` pour
  l'initialisation d'Incus) ne partage rien entre instances et sature une
  carte SD de 15 Gio des 7-8 faux Pi, cassant silencieusement le
  provisioning en cours - d'ou ce pool separe pour la flotte

## Mise en route (a executer directement sur le Pi 5)

```bash
sudo ./incus/install.sh
# se deconnecter/reconnecter pour prendre en compte le groupe incus-admin

sudo ./incus/network-setup.sh

./fleet/deploy-fleet.sh

sudo ./server/deploy-server.sh

sudo ./vpn/install.sh
sudo ./vpn/add-peer.sh mon-laptop
```

`deploy-fleet.sh` est idempotent : relancez-le apres avoir modifie
`fleet/inventory.yaml` pour ajouter/retirer des faux Pi, seules les
entrees manquantes sont creees. `deploy-server.sh` est idempotent aussi :
relancez-le apres avoir modifie `server/services.yaml` ou le code de
`webui/` pour regenerer le Caddyfile et reconstruire le stack sans
repeter la bascule DHCP.

## Verifier / se connecter

```bash
incus list                       # etat + IP de chaque faux Pi
incus exec pi-01 -- bash         # shell direct dans le conteneur
ssh pi@<ip-de-pi-01>             # mot de passe: raspberry (a changer si besoin)

docker compose -f server/docker-compose.yml ps   # etat du stack serveur

# Reverse proxy HTTPS (TLS auto-signe, cert "not trusted" attendu sans
# importer le CA interne de Caddy) :
curl -k https://fleet.web.expolab.lan      # webui flotte
curl -k https://dockhand.web.expolab.lan   # Dockhand
curl -k https://bastion.web.expolab.lan    # Bastion (admin/raspberry)

# VPN : importer /etc/wireguard/peers/mon-laptop.conf sur le poste client
# (WireGuard app ou wg-quick), puis une fois connecte, les memes URLs
# https://*.web.expolab.lan et un acces SSH direct aux faux Pi
# fonctionnent depuis ce poste.
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
`network-setup.sh` / `deploy-fleet.sh` / `deploy-server.sh` /
`vpn/install.sh` ont mis en place :

1. `vpn/uninstall.sh` — arrete WireGuard, supprime tous les pairs et
   desinstalle wireguard-tools
2. `server/teardown-server.sh` — arrete le stack Docker (dnsmasq, caddy,
   dockhand, webui), reactive le DHCP integre d'Incus sur `expo-lan`
   (secours), desinstalle Docker
3. `fleet/teardown-fleet.sh` — supprime les instances de la flotte
4. `incus/network-teardown.sh` — supprime le profil `fake-pi` et le
   reseau `expo-lan`
5. `incus/uninstall.sh` — desinstalle Incus, retire le depot Zabbly et
   `/var/lib/incus`

Chaque etape est aussi utilisable seule (ex: `./fleet/teardown-fleet.sh`
pour ne reinitialiser que la flotte sans desinstaller Incus). Limite
connue : un `apt purge` ne garantit jamais un retrait 100% parfait
(fichiers de config residuels, etc.) — d'ou la recommandation de l'image
disque si un retour a l'etat initial *exact* est requis.

## Structure

```
incus/
  install.sh                # installe Incus sur l'hote
  uninstall.sh               # desinstalle Incus (symetrique de install.sh)
  network-setup.sh           # cree le reseau expo-lan (bridge isole) + profil fake-pi
  network-teardown.sh        # defait network-setup.sh
  profiles/fake-pi.yaml      # profil Incus flotte (reseau + limites CPU/RAM + pool disque)
fleet/
  inventory.yaml             # liste declarative des faux Pi (nom, MAC, role, identifiants)
  deploy-fleet.sh             # cree/provisionne les faux Pi manquants
  teardown-fleet.sh           # supprime les faux Pi de l'inventaire
  provision-fakepi.sh         # script de premier boot execute dans chaque conteneur
server/
  deploy-server.sh             # installe Docker sur l'hote, deploie/met a jour le stack
  teardown-server.sh            # arrete le stack, reactive le DHCP integre d'Incus, desinstalle Docker
  docker-compose.yml            # stack : dnsmasq, caddy, dockhand, webui, bastion
  dnsmasq/Dockerfile             # image dnsmasq minimale
  dnsmasq.conf                    # config DHCP+DNS (plage, domaine expolab.lan)
  services.yaml                    # services applicatifs exposes (dockhand, fleet, bastion)
  render-caddyfile.py               # genere le Caddyfile a partir de server/services.yaml
  bastion.env                        # secrets Bastion generes au 1er deploiement (non versionne)
  bastion/                            # inventaire + donnees persistees de Bastion (non versionne)
webui/
  app.py                        # backend Flask : edite inventory.yaml, pilote deploy-fleet.sh
  Dockerfile                     # image (Flask + client Incus)
  templates/, static/            # page unique HTML/JS/CSS
vpn/
  install.sh                  # installe WireGuard sur l'hote, cree wg0
  uninstall.sh                 # desinstalle WireGuard (symetrique de install.sh)
  add-peer.sh                  # ajoute un pair VPN + genere sa config client
  remove-peer.sh                # retire un pair VPN
rollback.sh                   # orchestre les 5 teardown dans le bon ordre
```

## A venir

- Deploiement effectif de Gitea (conteneur Docker sur l'hote, gere depuis
  Dockhand), puis entree correspondante dans `server/services.yaml`
- Authentification sur la webui (aucune pour l'instant - protegee
  uniquement par l'isolation reseau d'`expo-lan`)
- Renouvellement/rotation des certs Caddy au-dela du lab (hors scope d'un
  environnement isole)
- Redirection de port sur le routeur du reseau reel pour un acces VPN
  depuis l'exterieur (specifique a la box de l'utilisateur, hors perimetre
  scriptable)
- Le role `avec-ecran`/`sans-ecran` (`fleet/inventory.yaml`) reste pour
  l'instant une simple etiquette (ecrite dans `/etc/expolab-role` et le
  motd par `provision-fakepi.sh`, affichee dans la webui) - aucun paquet
  ni service ne differe encore selon sa valeur. Volontaire : dans la
  vraie vie, ce type de configuration (desktop, VNC...) sera gere par un
  playbook Ansible, pas par ce script de provisioning du lab. Setup
  manuel deja valide en attendant sur un faux Pi (sway en mode headless +
  wayvnc, teste via Bastion) : voir l'historique du projet, pas encore
  scripte.
