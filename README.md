# expolab

Lab de simulation d'infra d'exposition : une flotte de "faux" Raspberry Pi
(conteneurs Incus avec MAC propre, DHCP, SSH) hebergee sur un Raspberry Pi 5
physique.

Le GPIO physique par faux Pi est **hors scope pour cette phase** (voir plus
bas). On se concentre sur la creation de la flotte reseau.

## Architecture reseau

Le reseau `expo-lan` cree par `network-setup.sh` est un **bridge Incus
purement virtuel**, independant de l'interface physique de l'hote (fonctionne
identiquement en Wi-Fi ou en Ethernet). Objectif : simuler un reseau local
pour la flotte, pas rendre les faux Pi visibles/joignables depuis le reseau
physique ou Internet.

Dans l'architecture cible, le futur serveur dedie DHCP/DNS/reverse-proxy aura
deux pattes reseau : une sur `expo-lan` (le "LAN" de la flotte) et une sur le
WAN pour un acces VPN a distance. En attendant ce serveur, le DHCP integre
d'Incus sur `expo-lan` fait office de DHCP temporaire, juste pour valider que
chaque faux Pi recoit bien une IP via sa propre MAC.

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
```

`deploy-fleet.sh` est idempotent : relancez-le apres avoir modifie
`fleet/inventory.yaml` pour ajouter/retirer des faux Pi, seules les entrees
manquantes sont creees.

## Verifier / se connecter

```bash
incus list                       # etat + IP de chaque faux Pi
incus exec pi-01 -- bash         # shell direct dans le conteneur
ssh pi@<ip-de-pi-01>             # mot de passe: raspberry (a changer si besoin)
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
`network-setup.sh` / `deploy-fleet.sh` ont mis en place :

1. `fleet/teardown-fleet.sh` — supprime les instances de la flotte
2. `incus/network-teardown.sh` — supprime le profil `fake-pi` et le reseau
   `expo-lan`
3. `incus/uninstall.sh` — desinstalle Incus, retire le depot Zabbly et
   `/var/lib/incus`

Chaque etape est aussi utilisable seule (ex: `./fleet/teardown-fleet.sh`
pour ne reinitialiser que la flotte sans desinstaller Incus). Limite connue :
un `apt purge` ne garantit jamais un retrait 100% parfait (fichiers de
config residuels, etc.) — d'ou la recommandation de l'image disque si un
retour a l'etat initial *exact* est requis.

## Structure

```
incus/
  install.sh              # installe Incus sur l'hote
  uninstall.sh             # desinstalle Incus (symetrique de install.sh)
  network-setup.sh         # cree le reseau expo-lan (bridge isole) + profil fake-pi
  network-teardown.sh      # defait network-setup.sh
  profiles/fake-pi.yaml    # profil Incus (reseau + limites CPU/RAM)
fleet/
  inventory.yaml           # liste declarative des faux Pi (nom, MAC, role)
  deploy-fleet.sh           # cree/provisionne les faux Pi manquants
  teardown-fleet.sh         # supprime les faux Pi de l'inventaire
  provision-fakepi.sh       # script de premier boot execute dans chaque conteneur
rollback.sh                 # orchestre les 3 teardown dans le bon ordre
```

## A venir (hors scope de cette phase)

- Serveur dedie DHCP (dnsmasq) + DNS + reverse proxy HTTPS auto-signe (Caddy)
- Acces GPIO physique (mock logiciel par defaut, passthrough reel sur 1-2
  noeuds si besoin de valider du vrai materiel)
