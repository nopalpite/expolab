# Préparer l'image master (Ubuntu Server)

L'image master est **commune à toutes les machines** : le flasheur (`sd-flasher`) l'écrit telle quelle, puis ne change que ce qui rend chaque carte unique. Ce que fait le déploiement Ansible ensuite (rôles selon le hostname) n'est pas du ressort de cette image.

Image utilisée : **Ubuntu Server 26.04 LTS pour Raspberry Pi** (`ubuntu-26.04.1-preinstalled-server-arm64+raspi.img.xz`, image arm64 préinstallée). Elle s'appuie sur **cloud-init** : c'est lui qui applique la personnalisation au premier démarrage, il n'y a aucun script maison à installer dans le master.

## Faut-il capturer un master ?

**Pas forcément** : l'image officielle, telle quelle, fonctionne déjà. Tout ce qui est propre à l'expo vit dans le flasheur (donc dans le dépôt, versionné, modifiable sans recapturer) :

| Écrit par le flasheur sur chaque carte | Où |
|---|---|
| hostname, compte (mot de passe **haché**, sudo), clés SSH, Wi-Fi | `user-data`, `network-config` |
| **serveur de temps du lab** (`10.42.0.1`) | `/etc/chrony/sources.d/expolab.sources` — Ubuntu 26.04 synchronise avec **chrony** (pas timesyncd) sur des serveurs d'internet ; le serveur du lab est *ajouté*, préféré dès qu'il répond |
| **proxy apt** (cache du serveur), si renseigné | `/etc/apt/apt.conf.d/01expolab-proxy` — vide par défaut : injoignable hors de l'expo, il casserait `apt` ailleurs |
| mises à jour automatiques **désactivées** | `/etc/apt/apt.conf.d/99-expolab-no-auto-upgrade` — sinon `unattended-upgrades` garde le verrou `dpkg` au premier démarrage et bloque `apt` et Ansible |
| `instance-id` **nouveau à chaque flash** | `meta-data` — sans ça, cloud-init croit avoir déjà configuré la carte et ne rejoue rien |
| `/etc/hostname`, `/etc/hosts` | partition racine : le nom doit être le bon dès la première requête DHCP (les réservations de dnsmasq sont **par nom**) |
| `/etc/machine-id` vidé, clés SSH d'hôte supprimées | partition racine : sinon toutes les machines partagent la même identité (cloud-init en génère de neuves) |

**Capturer un master n'est utile que pour** :
- **embarquer les mises à jour** (130 paquets au premier `apt upgrade`) : indispensable si les machines n'ont pas internet et que le cache apt du serveur n'est pas amorcé ;
- **`avahi-daemon`** (facultatif) : Bastion trouve le nom d'une machine par mDNS, puis par DNS inverse. Sur le réseau de l'expo, le DNS inverse suffit : Bastion interroge dnsmasq, qui connaît le nom annoncé par chaque machine au DHCP. `avahi-daemon` ne sert que pour un scan depuis un autre réseau.

Tout le reste (logiciels propres à un rôle : GPIO, affichage, applications) relève d'Ansible : versionné, rejouable, et ça ne fige rien dans une image.

## Préparer et capturer un master

1. **Flasher une carte de travail** avec le flasheur, mais avec un **compte temporaire** (par exemple `master`), pas celui des machines de l'expo. Le Wi-Fi de la maison donne internet ; laisser le **proxy apt vide**.
2. Se connecter, puis :
   ```
   sudo apt update && sudo apt full-upgrade -y
   sudo apt install -y avahi-daemon
   sudo reboot
   ```
   Après le redémarrage, vérifier que tout va bien : `systemctl status avahi-daemon`, `chronyc sources`, `cloud-init status --long`.
3. **Remettre la carte à neuf.** Le plus simple : **rien à faire sur la machine**. Éteins-la (`sudo poweroff`) et passe directement à l'étape 4 en laissant cochée « Préparer en image master » : le flasheur applique ce même script à la copie, et supprime en plus tous les comptes (donc le compte temporaire). À la main, si tu préfères, depuis ce compte temporaire : copier le script depuis le serveur (`scp pi@<serveur>:~/expolab/image/prepare-master.sh .`), puis :
   ```
   sudo bash prepare-master.sh
   ```
   Il demande de taper `MASTER`, puis : remplace la graine du flasheur par des fichiers **neutres**, remet cloud-init à neuf, vide `machine-id`, supprime les clés SSH d'hôte, vide les caches, journaux et historiques, **supprime le compte temporaire** et éteint la machine. Ne pas la redémarrer ensuite.
   - **La graine** contient le mot de passe Wi-Fi en clair et le hache du mot de passe : sans ce nettoyage, l'image les embarquerait. Le flasheur réécrit les trois fichiers à chaque flash.
   - **Le compte temporaire** doit disparaître : si un compte du même nom que celui de l'expo restait dans l'image, cloud-init verrait un compte déjà présent sur chaque carte, et je ne garantis pas qu'il lui remette son mot de passe et ses clés.
4. **Capturer** la carte depuis le flasheur (section « Capturer une carte », la carte dans le lecteur du serveur) : elle n'est que lue, l'image est réduite, compressée, vérifiée et rangée avec les autres. Une capture manuelle (`dd`, PiShrink…) est déconseillée : elle ne vide pas l'espace libre de la partition de démarrage, où restent le mot de passe Wi-Fi et le hache de l'ancienne graine, invisibles dans l'arborescence mais présents dans l'image.
5. L'image apparaît dans la liste « Image master » : prête à flasher.

**Agrandissement au premier démarrage** : les modules `growpart` et `resizefs` de cloud-init agrandissent la partition racine puis son système de fichiers jusqu'à la taille de la carte. Ils tournent à chaque démarrage, donc même sur une copie du master.

## Premier démarrage et Wi-Fi

Le Wi-Fi décrit dans `network-config` est écrit par cloud-init mais **pas activé au premier démarrage** : c'est un comportement connu (cloud-init n'applique pas la configuration Wi-Fi à ce moment-là), constaté sur Ubuntu 26.04 sur une vraie carte, où la machine n'avait pas de réseau avant d'être débranchée puis rebranchée. Le flasheur ajoute donc `power_state: {mode: reboot}` à `user-data` quand un Wi-Fi est configuré : cloud-init redémarre la machine **une seule fois** à la fin de sa première configuration (une fois par instance : un redémarrage par flash, aucun aux démarrages suivants). Sans Wi-Fi (Ethernet seul), aucun redémarrage.

Conséquence : au premier démarrage, l'écran s'arrête sur les clés SSH, puis la machine redémarre toute seule après environ une minute. Ne pas la débrancher.

## À valider sur une vraie carte

- l'image réduite redémarre et la racine occupe bien toute la carte (`df -h /`) ;
- `cloud-init status --long` termine en `done`, sans erreur ;
- le hostname, la clé SSH et le Wi-Fi demandés sont appliqués, sans verrou `apt` (`sudo apt upgrade` ne doit pas attendre) ;
- `/etc/machine-id` et les clés SSH d'hôte diffèrent d'une carte à l'autre ;
- `chronyc sources` liste `10.42.0.1` (préféré sur le réseau de l'expo) et la machine apparaît dans l'interface « Heure » du portail ;
- dnsmasq voit le nom dans la requête DHCP (réservation par nom → bonne adresse) ;
- le nom apparaît dans un scan Bastion (avec `avahi-daemon`).

## Bon à savoir

- `user-data` et `network-config` **restent** sur la partition FAT après le démarrage, mot de passe Wi-Fi compris, en clair (comme avec Raspberry Pi Imager). Garder la carte avec la machine, et ne jamais partager une image capturée sans avoir lancé `prepare-master.sh`.
- Ne pas supprimer la configuration NoCloud d'Ubuntu (`/etc/cloud/cloud.cfg.d/`) : c'est elle qui fait lire `system-boot`.
- La clé d'automatisation est injectée par le flasheur (case cochée par défaut dans les réglages) : le master n'a pas besoin de `image/machine-setup.sh`, qui reste disponible pour une machine installée autrement.
