# Consignes pour Claude (expolab)

Mémoire du projet versionnée : ce qui doit suivre d'un ordinateur à l'autre. Le
fonctionnement du lab lui-même est décrit dans `README.md`. Dépôt **public** : rien
de secret ici (ni mot de passe, ni jeton, ni clé privée).

## Branches

- `vraie-vie` : configuration de production. Pas de flotte simulée, le réseau de
  l'expo (pont `expo-lan`) passe par une vraie carte du serveur (`network/setup.sh`).
- `main` : l'ancien lab, avec sa flotte de faux Pi sous Incus.

## Accès au serveur (rpi-expolab)

- **SSH avec une clé dédiée, propre à chaque ordinateur.** Elle se trouve dans
  `~/.claude/keys/claude_expolab`, un dossier stable plutôt que le dossier temporaire
  de la session. Il faut la passer explicitement :
  `ssh -i ~/.claude/keys/claude_expolab -o IdentitiesOnly=yes -o BatchMode=yes pi@<IP du Pi> '<cmd>'`.
  Un `ssh pi@...` simple échoue en « Permission denied » : la clé n'est pas
  manquante pour autant. Sur un nouvel ordinateur, génère la clé et donne à
  l'utilisateur une ligne qui ajoute la partie publique à `~/.ssh/authorized_keys`
  sur le Pi. Ne jamais utiliser de mot de passe ni fouiller `~/.ssh`.
- **Demander l'adresse du Pi à l'utilisateur** si elle n'est pas connue : elle
  change selon le réseau. Utilisateur `pi`.
- **sudo :** vérifier d'abord avec `sudo -n true`. S'il demande un mot de passe,
  ne jamais le passer par un tube (`sudo -S`) : demander à l'utilisateur.
- **Mise à jour du code sur le Pi :** `cd ~/expolab && git pull --ff-only`. Le
  dépôt est public, aucun identifiant n'est nécessaire. Si un fichier arrive de
  Windows autrement que par git, retirer les fins de ligne CRLF avec
  `sed -i 's/\r$//'`.
- **Redéployer une seule stack :**
  `sudo env REPO_ROOT=$PWD bash -c "source server/dockhand-api.sh && dockhand_require_env && dockhand_upsert_stack <stack> server/stacks/<stack>/docker-compose.yml"`.
  Tout redéployer : `sudo ./server/deploy-server.sh < /dev/null`, puis
  `sudo ./vpn/install.sh` pour `wireguard` et `vpn-admin`.
- **Diagnostiquer une machine de l'expo** depuis le Pi, avec la clé
  d'automatisation : `sudo ssh -i ~/expolab/server/stacks/ansible-web/automation_key/automation_ed25519 pi@10.42.0.x`.

## Règles de travail

- **Lecture seule par défaut.** Un diagnostic est libre. Toute action qui modifie
  le Pi ou une machine attend l'accord explicite de l'utilisateur : installation,
  redéploiement, changement de réglage. Même chose pour un commit ou un push.
- **« Donne-moi » ou « fais-moi » X**, sans verbe comme déployer, tester ou
  pousser, veut dire : produire le fichier, puis s'arrêter. « À la main » signifie
  que l'utilisateur se réserve cette étape.
- **Toujours demander avant** un `rollback.sh` ou un script de démontage : cela
  détruit l'environnement en service.
- **Infra minimale.** Pas de nouveau service pour couvrir chaque cas (un miroir
  pip a été refusé : « usine à gaz »). Étendre l'existant, ou annoncer la limite.
- **Une page d'administration reste dans son propre conteneur**, jamais dans
  celui du service qu'elle pilote : ses mises à jour ne doivent pas redémarrer le
  service.
- Après un déploiement, prévenir que le navigateur peut garder l'ancienne page en
  cache (Ctrl+F5).

## Tests

- CI (`.github/workflows/ci.yml`) : `ruff check --select E9,F .`, `bash -n`,
  `pytest tests`, puis les scripts `tests/test_*.sh`.
- **Sous Windows,** le Python du système (3.8) est trop ancien. Il faut un
  environnement 3.12, par exemple avec `uv` : `uv venv -p 3.12`, puis
  `uv pip install ruff==0.6.9 pytest==8.3.3 pyyaml==6.0.2 flask==3.0.3 bcrypt==4.2.0 ruamel.yaml==0.18.6`.
- **Tests sautés sous Windows,** comme la capture réduite, qui a besoin de
  e2fsprogs et sfdisk. Les lancer sur le Pi dans un conteneur jetable
  (`debian:trixie-slim` avec `python3-pytest python3-flask python3-yaml fdisk e2fsprogs zstd xz-utils`),
  avec la copie de travail montée en volume. Supprimer ensuite le dossier de test
  avec `sudo`.

## Pièges connus

- **GitLab et git-mirror :** un jeton d'accès au projet a le rôle *Guest* par
  défaut, ce qui donne une erreur 403 « not allowed to download code ». Il faut le
  rôle **Reporter** et la portée `read_repository`. Une erreur 401 signale plutôt
  un mauvais jeton.
- **Heure :** les conteneurs montent `/etc/localtime` de l'hôte. Le serveur de
  temps ne transmet que l'heure UTC : le fuseau est un réglage local à chaque
  machine.
- **chrony sur Ubuntu 26.04 :** il ignore un serveur sans authentification NTS
  (comme celui du lab) tant que des sources NTS existent. Il faut
  `authselectmode ignore` et `makestep 1 -1`, que le flasheur écrit déjà.
- **API Dockhand :** `/api/` sur le port 3000, avec l'en-tête
  `Authorization: Bearer dh_...` (jeton dans `server/dockhand.env`). Les opérations
  longues renvoient un `jobId` ; avec l'en-tête `Accept: application/json`, le
  résultat revient directement. Manuel : https://dockhand.pro/manual/#api-reference
