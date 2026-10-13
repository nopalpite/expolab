# Préparer l'image master

L'image master est **commune à toutes les machines** : le flasheur (`sd-flasher`) l'écrit telle quelle, puis ne change que ce qui rend chaque carte unique. Ce que fait le déploiement Ansible ensuite (rôles selon le hostname) n'est pas du ressort de cette image.

## Ce que le flasheur fait à chaque carte

Avant le premier démarrage, sur la carte :

| Où | Quoi | Pourquoi |
|---|---|---|
| partition racine (`ext4`) | `/etc/hostname`, `/etc/hosts` | le nom doit être le bon dès la première requête DHCP : les réservations de dnsmasq sont **par nom** |
| partition racine | `/etc/machine-id` vidé | sinon toutes les machines ont le même identifiant client DHCP et se disputent la même adresse |
| partition racine | `/etc/ssh/ssh_host_*` supprimés | sinon toutes les machines ont la même identité SSH |
| partition de démarrage (`FAT`) | `expolab.conf` | Wi-Fi, clés SSH autorisées ; lu et supprimé par `expolab-firstboot` |

## Ce que le master doit contenir

1. **`expolab-firstboot`** : copier `image/expolab-firstboot.sh` dans `/usr/local/sbin/` (mode 755) et `image/expolab-firstboot.service` dans `/etc/systemd/system/`, puis `systemctl enable expolab-firstboot.service`. Il régénère les clés SSH d'hôte, installe les clés autorisées et le profil Wi-Fi.
2. **Un seul utilisateur normal** (uid 1000) : c'est lui qui reçoit les clés SSH autorisées (ou l'utilisateur nommé par `SSH_USER` dans `expolab.conf`).
3. **DHCP activé** et **le nom de la machine annoncé** dans la requête DHCP (`send host-name` pour `dhcpcd`, comportement par défaut de NetworkManager et systemd-networkd).
4. **L'agrandissement du système de fichiers au premier démarrage** : l'image est *réduite* (PiShrink ou équivalent), donc la carte entière n'est utilisée qu'après agrandissement de la partition 2 et de son système de fichiers. À valider sur votre système :
   - Raspberry Pi OS **Bookworm** : le mécanisme d'agrandissement d'origine (`init_resize.sh` via `init=` dans `cmdline.txt`) est conservé ou rétabli par l'outil de réduction ;
   - **Trixie** : à vérifier (cloud-init / `rpi-resize` selon l'image).
5. Ne **pas** laisser de fichier `expolab.conf` dans l'image.

## Format de `expolab.conf`

Lignes `CLE=VALEUR`, lues sans interprétation (jamais exécutées) :

```
HOSTNAME=module-02
WIFI_SSID=mon-reseau
WIFI_PSK=********
WIFI_COUNTRY=FR
AUTHORIZED_KEYS_B64=<clés publiques, une par ligne, encodées en base64>
SSH_USER=pi            # optionnel
```

## Où mettre l'image

Sur le serveur, dans `server/stacks/sd-flasher/images/` : `.img`, `.img.gz`, `.img.xz` ou `.img.zst`. La plus récente est utilisée par défaut.
