# Préparer l'image master (Ubuntu Server)

L'image master est **commune à toutes les machines** : le flasheur (`sd-flasher`) l'écrit telle quelle, puis ne change que ce qui rend chaque carte unique. Ce que fait le déploiement Ansible ensuite (rôles selon le hostname) n'est pas du ressort de cette image.

Image utilisée : **Ubuntu Server 26.04 LTS pour Raspberry Pi** (`ubuntu-26.04.1-preinstalled-server-arm64+raspi.img.xz`, image arm64 préinstallée). Elle s'appuie sur **cloud-init** : c'est lui qui applique la personnalisation au premier démarrage, il n'y a aucun script maison à installer dans le master.

## Ce que le flasheur fait à chaque carte

| Où | Quoi | Pourquoi |
|---|---|---|
| partition `system-boot` (FAT) | `user-data`, `network-config`, `meta-data` | graine cloud-init (source NoCloud) : hostname, clés SSH autorisées, Wi-Fi, DHCP |
| `user-data` | compte utilisateur : nom, mot de passe **haché** (SHA-512, `openssl passwd -6`), `sudo` sans mot de passe, clés SSH | l'image Ubuntu livre un `user-data` par défaut (compte `ubuntu`, mot de passe `ubuntu`) que celui du flasheur remplace : sans compte défini dans l'interface, personne ne pourrait se connecter |
| `meta-data` | `instance-id` **nouveau à chaque flash** | sans ça, cloud-init croit avoir déjà configuré cette carte (copie du master) et ne rejoue rien |
| partition `writable` (ext4) | `/etc/hostname`, `/etc/hosts` | le nom doit être le bon dès la première requête DHCP : les réservations de dnsmasq sont **par nom**, et cloud-init n'applique le hostname qu'après le réseau |
| partition `writable` | `/etc/machine-id` vidé | sinon toutes les machines ont le même identifiant machine (journal, certains clients DHCP) |
| partition `writable` | `/etc/ssh/ssh_host_*` supprimés | sinon toutes les machines ont la même identité SSH ; cloud-init en génère de neuves (`ssh_deletekeys`) |

## Préparer le master

1. Installer Ubuntu Server sur une carte, la démarrer, créer l'environnement commun (paquets), et lancer `image/machine-setup.sh --user NOM` (proxy apt vers le cache du serveur, serveur de temps, clé d'automatisation et `sudo` sans mot de passe) avec **le même nom de compte** que celui défini dans l'interface du flasheur : cloud-init le crée au premier démarrage de chaque carte. Si ce compte existe déjà dans le master, à valider sur une vraie carte : que son mot de passe et ses clés soient bien réappliqués (sinon, laisser le compte être créé par cloud-init et ne pas le créer dans le master).
2. **Remettre la carte « à neuf » avant de la capturer**, puis l'éteindre **sans la redémarrer** :
   ```
   sudo cloud-init clean --logs --seed --machine-id --configs all   # options à confirmer avec `cloud-init clean --help`
   sudo truncate -s 0 /etc/machine-id
   sudo rm -f /etc/ssh/ssh_host_*
   sudo apt-get clean && history -c
   sudo poweroff
   ```
3. Capturer la carte en image (`dd`, Win32DiskImager…), puis la **réduire** (PiShrink avec `-s` : sans son propre agrandissement automatique, c'est cloud-init qui agrandit au premier démarrage), et la compresser (`.xz` ou `.zst`).
4. Déposer l'image dans `server/stacks/sd-flasher/images/` sur le serveur (la plus récente est utilisée par défaut).

**Agrandissement au premier démarrage** : les modules `growpart` et `resizefs` de cloud-init agrandissent la partition racine puis son système de fichiers jusqu'à la taille de la carte. Ils tournent à chaque démarrage, donc même sur une copie du master.

## À valider sur une vraie carte

- l'image réduite redémarre et la racine occupe bien toute la carte (`df -h /`) ;
- `cloud-init status --long` termine en `done`, sans erreur ;
- le hostname, la clé SSH et le Wi-Fi demandés sont appliqués ;
- `/etc/machine-id` et les clés SSH d'hôte diffèrent d'une carte à l'autre ;
- dnsmasq voit le nom dans la requête DHCP (réservation par nom → bonne adresse).

## Bon à savoir

- `user-data` et `network-config` **restent** sur la partition FAT après le démarrage, mot de passe Wi-Fi compris, en clair (comme avec Raspberry Pi Imager). Garder la carte avec la machine.
- Ne pas supprimer la configuration NoCloud d'Ubuntu (`/etc/cloud/cloud.cfg.d/`) : c'est elle qui fait lire `system-boot`.
- La clé d'automatisation est déjà dans le master si `machine-setup.sh` y est passé ; l'injecter aussi depuis le flasheur est inoffensif (la même clé n'est pas dupliquée).
