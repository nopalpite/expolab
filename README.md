# expolab

Lab de simulation d'infra d'exposition sur un Raspberry Pi 5 physique.

Deux couches bien separees :

- **La flotte de faux Raspberry Pi** (`incus/`, `fleet/`) - simulee via
  Incus, parce qu'en vraie vie ce sont de vrais boitiers separes qu'on ne
  peut evidemment pas dupliquer sur une seule machine de lab. C'est un
  artifice propre au lab.
- **Le serveur d'expo** (`server/`, `vpn/`, `webui/`) - DHCP/DNS,
  reverse-proxy, et les services applicatifs (webui, Bastion, plus tard
  Gitea) tournent en Docker **directement sur l'hote**, pas dans Incus.
  C'est exactement ce qui sera deploye sur le vrai serveur de
  l'exposition, qui n'aura pas besoin d'Incus du tout. **Dockhand est le
  point d'entree de gestion de tout le stack** : chaque service (y
  compris le VPN WireGuard) est une stack Dockhand independante,
  creee/redeployee via son API plutot qu'un unique `docker-compose.yml`
  monolithique - voir `server/dockhand-api.sh`.

## Architecture reseau

Le reseau `expo-lan` cree par `incus/network-setup.sh` est un **bridge
Incus purement virtuel**, independant de l'interface physique de l'hote
(fonctionne identiquement en Wi-Fi ou en Ethernet). Objectif : simuler un
reseau local pour la flotte, pas rendre les faux Pi visibles/joignables
depuis le reseau physique ou Internet. L'hote porte lui-meme l'adresse
`10.42.0.1` sur ce bridge (c'est Incus qui la lui donne a la creation du
reseau).

`server/deploy-server.sh` demarre **Dockhand seul** (`server/docker-compose.yml`
- il ne peut pas se creer lui-meme via sa propre API), puis cree/redeploie
chaque service comme **stack Dockhand independante** via son API REST
(`server/dockhand-api.sh`), plutot que de tout lancer d'un coup avec
`docker compose up`. Chaque stack vit dans `server/stacks/<nom>/` :
- **dnsmasq** (DHCP/DNS) en `network_mode: host`, lie a l'interface
  `expo-lan` - besoin d'un acces bas niveau (broadcast DHCP) qu'un reseau
  Docker isole ne permet pas. Bail DHCP persiste dans
  `server/stacks/dnsmasq/data/` (survit a une recreation du conteneur).
- **Caddy** (reverse-proxy), egalement en `network_mode: host`. Deux
  modes TLS, config dans `server/stacks/caddy/tls.env` (non versionne) -
  editable a la main OU depuis **caddy-admin** (section "Certificat
  TLS", voir plus bas). Au tout premier `deploy-server.sh` (ce fichier
  n'existe pas encore), le script propose interactivement de le
  configurer tout de suite (domaine + identifiants OVH) plutot que de
  se contenter du defaut auto-signe - reste modifiable plus tard de
  toute facon, jamais redemande sur les runs suivants :
  - `internal` (defaut) : certificat auto-signe (CA interne de Caddy) sur
    `*.web.expolab.lan` - fonctionne hors-ligne, mais chaque
    appareil/navigateur doit accepter l'avertissement une fois.
  - `signed` : vrai certificat Let's Encrypt via challenge **DNS-01 chez
    OVH** (image buildee localement avec le plugin `caddy-dns/ovh`, voir
    `server/stacks/caddy/Dockerfile`) sur un vrai domaine public (ex:
    `expolab.tondomaine.fr`). Aucun port a ouvrir (contrairement a un
    challenge HTTP-01) : seule l'API DNS d'OVH est contactee, en sortant -
    `dnsmasq` fait pointer ce domaine vers l'hote en local
    (`server/stacks/dnsmasq/admin-config/public-domain.conf`, genere par
    `deploy-server.sh` a partir de `tls.env`), exactement comme pour
    `*.web.expolab.lan`, donc resolu automatiquement par le meme pair VPN
    (voir plus bas). Necessite un token API OVH (droits GET/PUT/POST/DELETE
    sur `/domain/zone/*`, cree sur
    https://www.ovh.com/auth/api/createToken/) - saisi au premier
    `deploy-server.sh` (voir ci-dessus) ou plus tard dans caddy-admin, qui
    redeploie alors les stacks `caddy`/`dashboard`/`dnsmasq` via l'API
    Dockhand apres sauvegarde (necessaire pour qu'elles relisent leurs
    variables d'environnement - OVH_\* pour Caddy, PORTAL_DOMAIN
    pour dashboard - un simple rechargement a chaud ne suffit jamais pour
    ca). `deploy-server.sh`
    relit ce meme fichier a chaque deploiement, donc un choix fait depuis
    l'UI survit a un redeploiement.
- **webui** expolab (creation/suppression de faux Pi), publication de port
- **Bastion** (https://github.com/nopalpite/bastion, dashboard +
  SSH/VNC web) en `network_mode: host` - image prete a l'emploi publiee
  sur GHCR (`ghcr.io/nopalpite/bastion`), avec les **identifiants
  d'administration choisis a l'installation** (les memes que partout, voir
  "Identifiants d'administration" plus bas, conserves dans `bastion.env`).
  Secrets (cle de session, cle de chiffrement des
  identifiants memorises) generes une seule fois au premier
  `deploy-server.sh` dans `server/stacks/bastion/bastion.env` (non
  versionne) ; inventaire de machines et donnees persistees dans
  `server/stacks/bastion/{config,maps}/` (egalement non versionne, propre
  a chaque lab)
- **ansible-web** (https://ansible-web.web.expolab.lan, source dans
  `ansible-web/`) : declenche des runs pour
  [bastion-ansible](https://github.com/nopalpite/bastion-ansible) (Ansible,
  mode push, execute contre le parc **reference dans Bastion** via son
  `GET /api/machines` - jamais un second inventaire maintenu a la main).
  Ne contient aucun contenu Ansible lui-meme : clone/pull un mirroir
  `git-mirror` **en lecture seule** avant chaque action (nom du mirroir
  configurable depuis sa propre UI, defaut `ansible`) - jamais
  directement GitHub, et aucune edition/scaffold de role possible depuis
  cette UI (son clone ne peut rien pousser nulle part ; editer un role
  reste une operation locale sur le vrai depot GitHub, voir son README).
  Le mecanisme tag -> role (`inventory/bastion_inventory.py`,
  `order.yaml` + `scripts/render_site_yml.py` pour l'empilement
  multi-tags, `scripts/scaffold_roles.py`) vit entierement dans ce depot
  externe, inchange. `network_mode: host` pour joindre `git-mirror` et
  Bastion en direct (`127.0.0.1:5054`/`5000`, pas de resolution DNS
  `*.web.expolab.lan` requise) et atteindre `expo-lan` en SSH.
  `deploy-server.sh` genere automatiquement une cle SSH "automatisation"
  dediee au lab (`server/stacks/ansible-web/automation_key/`, distincte
  de toute cle de vraie production - authentification jamais mediee par
  Bastion, meme choix que la vraie infra), monte directement dans le
  conteneur (`ANSIBLE_PRIVATE_KEY_FILE`) avec `BASTION_URL`/
  `BASTION_API_TOKEN` - pas de configuration manuelle via une UI tierce
  a faire ici, contrairement a Semaphore (retire, l'experience d'usage
  ne convenait pas). **Etape manuelle unique** : creer le mirroir dans
  `git-mirror` (URL = le depot GitHub) s'il n'existe pas deja - aucun
  endpoint ne le fait de facon fiable a notre place, message d'erreur
  explicite dans l'UI ansible-web si absent.
- **dashboard** = le **portail** (https://dashboard.web.expolab.lan, `portal/`) :
  le point d'entree unique du lab. Une page de connexion (le couple
  identifiant / mot de passe choisi a l'installation), puis une interface a
  **tiroir et onglets** qui affiche chaque service dans un cadre, sans ouvrir de
  nouvelle fenetre (onglets conserves en arriere-plan, liens directs
  `#fleet`, point vert / rouge selon l'etat du service, bouton "ouvrir a part").
  Il remplace Homepage et le basic auth Caddy par le **cookie de session
  partage** `*.<domaine>` : Caddy demande au portail (`forward_auth`,
  `/auth/verify`) si la requete est autorisee, et le portail accepte aussi un
  basic auth pour `curl -u` et les clients git. Ferme par defaut : portail
  arrete => acces refuse. Liste, titres, groupes et icones des services :
  `server/services.yaml` (champs `title`, `group`, `icon`, `description`,
  `framed`, `portal`). Dockhand et Bastion gardent leur propre connexion (memes
  identifiants), saisie une fois dans leur cadre.
  **Prerequis navigateur** : un cadre ne peut pas afficher l'avertissement
  "certificat non fiable". En TLS auto-signe, installer **une fois** le
  certificat racine de Caddy (telechargeable sur la page de connexion, dans le
  tiroir "Aide" ou `/ca.crt`) ; en certificat signe (Let's Encrypt), rien a faire.
  Caddy n'autorise l'affichage en cadre que depuis le portail
  (`frame-ancestors https://dashboard.<domaine>`).
- **dnsmasq-admin**, **caddy-admin**, **vpn-admin** - pages minimales
  (Flask, meme style que la webui) pour piloter dnsmasq/Caddy/WireGuard
  sans repasser par la ligne de commande, sur le meme principe que la
  webui : editer/lire un fichier de config existant et reutiliser les
  scripts/mecanismes deja en place, sans rien reimplementer.
  - `dnsmasq-admin` (https://dnsmasq.web.expolab.lan) : baux DHCP actifs
    (lecture seule) + **reservations DHCP** (MAC -> IP fixe), ecrites
    dans un fichier dedie (`server/stacks/dnsmasq/admin-config/`, non
    versionne) que dnsmasq relit **a chaud sur SIGHUP**
    (`--dhcp-hostsfile`, voir `dnsmasq.conf`) via
    `docker kill --signal=HUP expolab-dnsmasq` - pas de redemarrage du
    conteneur, pas de coupure DHCP/DNS pour le reste de la flotte. Pas
    d'enregistrements DNS statiques independants : tous les services
    vivent sous *.web.expolab.lan, un unique wildcard dans dnsmasq.conf
    (le tri par service se fait cote Caddy via le Host: HTTP, pas via
    DNS) - une liste par nom aurait ete redondante.
  - `caddy-admin` (https://caddy.web.expolab.lan) : ajoute/retire/modifie
    une entree dans `server/services.yaml` (port backend, `extra_routes`
    incluses - ex: le pont VNC de Bastion), refuse tout port deja
    utilise par un autre service (validation + avertissement en direct),
    et regenere le Caddyfile puis le pousse a **l'admin API de Caddy
    lui-meme** (`http://127.0.0.1:2019/load`, atteignable en
    `network_mode: host` comme Caddy) pour un rechargement a chaud - pas
    de redeploiement de stack, pas de coupure de service. Detecte aussi
    les conteneurs Docker a port publie pas encore exposes (socket
    Docker monte, meme precedent que `dnsmasq-admin`/`vpn-admin`) et
    propose de les ajouter - toujours en pre-remplissant le formulaire,
    jamais automatiquement ; les conteneurs en `network_mode: host` n'ont
    pas de port "publie" au sens Docker, indetectables par ce biais.
    Section "Certificat TLS" (comme le gestionnaire de certificats d'un
    outil type Nginx Proxy Manager) : bascule `internal`/`signed` et
    identifiants OVH, ecrits dans `server/stacks/caddy/tls.env` (les
    identifiants deja enregistres ne sont jamais renvoyes au navigateur -
    un champ laisse vide au prochain enregistrement les garde
    inchanges), puis redemarre le conteneur `caddy` (necessaire pour
    qu'il relise ses variables d'environnement).
  - `vpn-admin` (https://vpn.web.expolab.lan) : liste les pairs (avec
    etat de connexion et trafic via `wg show wg0 dump`), QR code pour
    import mobile, et appelle **directement** `vpn/add-peer.sh`/
    `remove-peer.sh` (montes en volume) pour creer/retirer un pair -
    aucune logique WireGuard reimplementee.

- **git-mirror** (https://git-mirror.web.expolab.lan) : mirroir git
  minimaliste, pas un forge - Gitea ecarte volontairement pour ce besoin
  (trop de fonctionnalites : navigation de fichiers, PR, issues...).
  Garde des clones **bare** (`git clone --mirror`) de depots distants
  (GitLab, GitHub...) sous `server/stacks/git-mirror/data/repos/` (non
  versionne), avec sync manuel (bouton) ou programme (thread de fond,
  intervalle par mirroir - pas de cron/celery separe pour si peu).
  Contrairement a `dnsmasq-admin`/`caddy-admin`/`vpn-admin`, ce conteneur
  n'administre pas un service voisin : il fait lui-meme le travail git, en
  plus de servir l'UI - pas de `network_mode: host` ni de socket Docker
  necessaires, il ne parle qu'en sortant vers les remotes. Authentification
  (depot prive) via token integre a l'URL distante
  (`https://oauth2:TOKEN@...`), stocke en clair dans
  `server/stacks/git-mirror/data/mirrors.yaml` (non versionne) - plus
  simple que le chiffrement Fernet de Bastion, juge suffisant ici (enjeu
  moindre qu'un identifiant SSH/VNC vers une vraie machine). Chaque
  mirroir est directement clonable (`git clone
  https://git-mirror.web.expolab.lan/git/<nom>.git`, adresse affichee et
  copiable en un clic dans l'UI) via le protocole HTTP intelligent de git
  (`git http-backend` invoque en CGI depuis `app.py`, meme binaire
  qu'utiliserait Apache/nginx) - lecture seule par defaut (le push HTTP
  reste desactive tant que `http.receivepack` n'est pas active sur un
  repo, jamais fait ici).

**Pourquoi des pages maison plutot qu'un projet existant** : recherche
faite sur les GUIs disponibles pour WireGuard/Caddy/dnsmasq - la seule
option mature est [wg-easy](https://github.com/wg-easy/wg-easy), qui
s'est averee **cassee sur ce materiel** : son iptables embarque (legacy)
ne fonctionne pas sur le noyau Raspberry Pi 5 6.18+, qui a retire le
module `ip_tables` au profit exclusif de nftables (plusieurs issues
GitHub ouvertes sur exactement ce cas, non resolues en amont). Cote
Caddy/dnsmasq, aucune option n'atteint un niveau de maturite suffisant.
Vu que le vrai besoin est simple (CRUD sur des fichiers de config deja
en place), des pages minimales par-dessus notre propre logique deja
prouvee fonctionnelle evitent cette classe de probleme entierement.

Les binds relatifs (`./x`) d'une stack creee via l'API Dockhand se
resolvent dans le repertoire de donnees propre a Dockhand, pas dans ce
depot git - chaque `docker-compose.yml` de stack utilise donc des chemins
**absolus** via `${REPO_ROOT}`, substitues par `dockhand-api.sh` (envsubst)
avant l'envoi, pour que nos fichiers de config restent les notres
(versionnes/regenerables) plutot qu'une copie geree par Dockhand.

**Configuration de Dockhand entierement automatique** (`server/dockhand_setup.py`,
via son API REST - plus rien a faire dans son interface) : au premier
`deploy-server.sh`, le script cree l'environnement local (socket Docker),
cree le premier utilisateur avec les identifiants d'administration, active
l'authentification de Dockhand et depose un jeton d'API dans
`server/dockhand.env` (non versionne) pour les scripts et caddy-admin.
Idempotent : un jeton valide => rien n'est refait. Si la creation de
l'environnement echouait (version de Dockhand differente), le script
retombe sur l'ancienne etape manuelle et patiente.

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

La patte WAN/VPN (`vpn/`) est desormais **conteneurisee** elle aussi (stack
Dockhand `wireguard`, `vpn/wireguard/`), pour la meme coherence que le
reste : `network_mode: host` (comme dnsmasq/caddy/bastion, necessaire pour
que ses regles PostUp/PostDown iptables visent les vraies interfaces de
l'hote) et route le trafic des clients VPN vers `10.42.0.0/24`. Cles et
config des pairs persistees dans `vpn/wireguard/config/` (non versionne).
L'hote lui-meme n'a plus besoin du paquet `wireguard-tools` : `vpn/add-peer.sh`/
`remove-peer.sh` passent par `docker exec wireguard wg ...`.

**Pourquoi le VPN, meme en local** : `dnsmasq` ne repond qu'a la resolution
DNS sur `expo-lan` (par design - reseau simule, pas question de repondre
au DHCP/DNS de la vraie infra reseau de l'hote). Un poste sur le meme
Wi-Fi physique que le Pi ne peut donc pas resoudre `*.web.expolab.lan`
sans configuration DNS locale manuelle. Plutot que de demander a
l'utilisateur final (visiteur/exposant) de toucher son fichier hosts,
`vpn/install.sh` cree automatiquement un pair VPN `default` pret a
l'emploi : son fichier de config client pousse `DNS = 10.42.0.1`, donc la
resolution `*.web.expolab.lan` fonctionne des l'import du fichier dans un
client WireGuard, sans autre manip. Superflu si non utilise -
`sudo ./remove-peer.sh default` le retire proprement.

Contenu du fichier client genere (`vpn/wireguard/config/peers/<nom>.conf`,
pret a importer tel quel, rien a completer a la main) :

```ini
[Interface]
PrivateKey = ...      # clef privee generee pour ce pair, unique, jamais reutilisee
Address = 10.66.66.X/32   # IP du client DANS le tunnel VPN
DNS = 10.42.0.1            # pousse dnsmasq comme resolveur -> *.web.expolab.lan
                             # se resout automatiquement une fois connecte

[Peer]
PublicKey = ...             # clef publique du serveur WireGuard
PresharedKey = ...           # couche de chiffrement symetrique additionnelle
Endpoint = <ip-lan-du-pi>:51820   # detecte automatiquement (IP LAN de l'hote) -
                                    # a remplacer par une IP publique + redirection
                                    # de port sur la box pour un acces exterieur
AllowedIPs = 10.42.0.0/24, 10.66.66.0/24   # split tunnel : seul le trafic vers
                                             # expo-lan et le VPN lui-meme passe
                                             # par le tunnel, le reste (internet)
                                             # suit la route habituelle du client
PersistentKeepalive = 25   # garde la connexion ouverte a travers un NAT/firewall
```

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

Point d'entree unique - installe tout dans l'ordre :

```bash
sudo ./install.sh
# Demande, a la premiere installation, l'identifiant et le mot de passe
# d'administration (UN couple pour toutes les interfaces, voir plus bas) ;
# Dockhand est ensuite configure tout seul. Sans terminal : definir
# EXPOLAB_ADMIN_USER et EXPOLAB_ADMIN_PASSWORD (8 caracteres minimum).
```

Pas de `chmod +x` a faire a la main : les scripts sont versionnes
executables dans git (mode 100755), un `git clone`/`git archive` les
recupere deja prets a l'emploi.

Detail de chaque etape (`install.sh` n'est qu'un enchainement de
celles-ci - utile pour n'en relancer qu'une seule) :

```bash
sudo ./incus/install.sh
# se deconnecter/reconnecter pour prendre en compte le groupe incus-admin
# (install.sh contourne ca avec `sg incus-admin`, pas besoin en isole)

sudo ./incus/network-setup.sh

./fleet/deploy-fleet.sh

sudo ./server/deploy-server.sh
# 1ere fois : demande les identifiants d'administration, puis configure
# Dockhand (environnement, utilisateur, authentification) par son API.

sudo ./vpn/install.sh
# Cree aussi un pair "default" pret a distribuer (vpn/wireguard/config/peers/default.conf)
sudo ./vpn/add-peer.sh mon-laptop   # optionnel : un pair nommement identifie en plus
```

`deploy-fleet.sh` est idempotent : relancez-le apres avoir modifie
`fleet/inventory.yaml` pour ajouter/retirer des faux Pi, seules les
entrees manquantes sont creees. `deploy-server.sh` est idempotent aussi :
relancez-le apres avoir modifie `server/services.yaml` ou le code de
`webui/` pour regenerer le Caddyfile et redeployer les stacks concernees
via l'API Dockhand, sans repeter la bascule DHCP.

## Securite des UIs d'administration

- **Identifiants d'administration : un seul couple, choisi par toi.** A la
  premiere installation, `deploy-server.sh` demande un identifiant et un
  mot de passe (saisie masquee, confirmation, 8 caracteres minimum), puis
  les applique **partout** : Dockhand (utilisateur admin + authentification
  activee), Bastion (`BASTION_ADMIN_USER`/`BASTION_ADMIN_PASSWORD` dans
  `bastion.env`) et le basic auth Caddy des UIs d'administration. Ils sont
  conserves dans `server/stacks/caddy/auth.env` (non versionne) et jamais
  redemandes ni affiches : une installation existante garde les siens.
  Installation sans terminal : `EXPOLAB_ADMIN_USER` / `EXPOLAB_ADMIN_PASSWORD`
  (sinon le script s'arrete plutot que de laisser un mot de passe que personne
  n'a choisi). Un `bastion.env` anterieur garde son ancien mot de passe genere
  (rappele a la fin du script).
  **Changer le couple plus tard** - **depuis l'interface** : tiroir du portail
  > **Compte > Identifiants** (section de caddy-admin ; mot de passe actuel exige,
  application partout d'un coup, puis reconnexion) ; **ou en ligne de commande**
  (aussi pour unifier une installation ancienne) :
  `sudo ./server/set-credentials.sh` demande le nouveau couple (ou
  `EXPOLAB_ADMIN_USER` / `EXPOLAB_ADMIN_PASSWORD`) et l'applique partout d'un
  coup : compte Dockhand (par son API, le jeton reste valable), `auth.env`
  (le portail le relit seul, sessions ouvertes fermees) et Bastion
  (`bastion.env` + recreation de sa stack). Dockhand est modifie en premier :
  s'il refuse, rien d'autre n'a bouge. Pour retrouver le couple actuel :
  `sudo grep -E '^BASIC_AUTH_(USER|PASSWORD)=' server/stacks/caddy/auth.env`.
- **Basic auth Caddy** devant les vhosts marques `auth: true` dans
  `server/services.yaml` (dockhand, fleet, dnsmasq, caddy, vpn, git-mirror,
  ansible-web). Identifiants = ceux choisis a l'installation
  (`server/stacks/caddy/auth.env`, non versionne, hash bcrypt calcule avec
  le binaire `caddy`). Pas de basic auth sur
  `dashboard` (liens uniquement) ni `bastion` (a sa propre authentification) ;
  le chemin `/git/*` de git-mirror est exempte (`auth_except`) car utilise
  par des clients git. `render-caddyfile.py` refuse de generer un Caddyfile
  si un service demande l'auth sans identifiants disponibles (jamais de
  vhost silencieusement ouvert). Reglable par service depuis caddy-admin.
- **Les backends ne sont joignables que via Caddy** : leurs ports sont
  publies sur `127.0.0.1` (dashboard, dnsmasq-admin, git-mirror, webui) ou
  leur serveur Flask ecoute sur `127.0.0.1` (caddy-admin, vpn-admin,
  ansible-web) - sans ca, le basic auth serait contournable en visant
  directement `<ip-du-pi>:<port>` depuis le LAN. **Exceptions assumees** :
  Dockhand (`:3000`, joignable avant que Caddy existe) et Bastion (`:5000`),
  tous deux proteges par leur propre authentification, avec les memes
  identifiants d'administration.
- Les mots de passe par defaut des **faux Pi** (`pi`/`raspberry`,
  `fleet/provision-fakepi.sh`) restent une constante de lab connue,
  reutilisee par ansible-web (`LAB_FAKEPI_PASSWORD` dans
  `server/lib/secrets.sh`) - a changer pour toute vraie machine.

## Verifier / se connecter

```bash
incus list                       # etat + IP de chaque faux Pi
incus exec pi-01 -- bash         # shell direct dans le conteneur
ssh pi@<ip-de-pi-01>             # mot de passe: raspberry (a changer si besoin)

# http://<ip-du-pi>:3000 - toutes les stacks (dnsmasq, caddy, webui,
# bastion, wireguard, dashboard) y sont visibles/pilotables

# Reverse proxy HTTPS (TLS auto-signe, cert "not trusted" attendu sans
# importer le CA interne de Caddy) :
curl -k https://dashboard.web.expolab.lan  # point d'entree, liens vers tout le reste
# Les UIs d'administration ci-dessous demandent le basic auth Caddy
# (curl -k -u <identifiant>:<mot de passe choisi a l'installation>) :
curl -k https://fleet.web.expolab.lan      # webui flotte
curl -k https://dockhand.web.expolab.lan   # Dockhand
curl -k https://bastion.web.expolab.lan    # Bastion (memes identifiants)
curl -k https://dnsmasq.web.expolab.lan    # baux DHCP actifs
curl -k https://caddy.web.expolab.lan      # ajouter/retirer un service expose
curl -k https://vpn.web.expolab.lan        # pairs WireGuard (QR code, trafic)

# VPN : importer vpn/wireguard/config/peers/default.conf (ou mon-laptop.conf)
# sur le poste client (WireGuard app ou wg-quick), puis une fois connecte, les memes URLs
# https://*.web.expolab.lan et un acces SSH direct aux faux Pi
# fonctionnent depuis ce poste.
```

## Remplacer une machine de l'expo (hors ligne)

Scenario : pendant l'exposition, un Raspberry Pi meurt. On le change, on
insere une carte SD flashee avec l'image type, on lance le playbook Ansible
- **sans internet**. La carte neuve reprend le **meme nom et la meme IP**
que la morte : son entree Bastion (identifiants, tags) reste valable.
L'installation du *serveur* est un autre sujet (connecte) ; `fleet/` ne
sert qu'a simuler de vraies machines.

### Ce qui a ete verifie (experience dans le lab)

| Cas | Resultat |
|---|---|
| Reservation par MAC (ancien fonctionnement) | la carte neuve (autre MAC) obtient une autre IP ; le nom DNS reste colle au bail de la morte |
| Reservation **par nom** (`nom,ip`, sans MAC) | la carte neuve, qui annonce le meme hostname, obtient l'IP reservee et le nom DNS bascule sur son bail |
| Bail de la morte encore actif (12 h) | bloque la reutilisation de l'IP, tant qu'il n'est pas libere |

Decision : reservations **par nom** (MAC optionnelle) dans dnsmasq-admin, et
liberation du bail de la machine morte (`dhcp_release`, sans redemarrer dnsmasq
ni toucher aux autres baux), automatique ou par le bouton **Liberer**.

Attention : une reservation par **MAC** obsolete (ancienne carte) l'emporte sur
la reservation par nom si la meme MAC revient, sans avertissement - supprimer
les anciennes lignes MAC d'une machine qu'on gere par nom.

### Procedure de remplacement

1. dnsmasq-admin : verifier que la machine a une reservation **par nom**
   (nom + IP, MAC vide). C'est tout : **le bail de la morte est libere
   automatiquement** (voir ci-dessous).
2. Flasher la carte avec l'image type (voir plus bas) en saisissant le
   **hostname de la machine remplacee** dans Raspberry Pi Imager.
3. Brancher, laisser demarrer : meme IP, meme nom.
4. ansible-web : lancer le playbook en ciblant la **machine** (le select
   "Cible" propose les tags et les machines de Bastion).

**Liberation automatique des baux.** dnsmasq-admin verifie toutes les 20 s,
pour chaque reservation par nom, que la machine qui tient l'IP repond encore
en ARP ; apres 3 verifications sans reponse (~1 min) son bail est libere. La
carte neuve, qui demarre ensuite, prend directement l'IP. Une machine
simplement eteinte retrouve elle aussi sa place a son retour. Un detenteur
qui repond encore n'est **jamais** libere : deux machines vivantes pour un
meme nom donnent un conflit, signale dans `docker logs expolab-dnsmasq-admin`
et sur `/api/events`.

Si la carte neuve est branchee **avant** (moins d'une minute apres la mort),
elle recoit d'abord une IP de repli ; dnsmasq-admin libere alors les deux
baux et dnsmasq refuse (NAK) tout renouvellement de l'IP de repli. Le
**redemarrage de la carte** la remet sur l'IP reservee (verifie dans le lab ;
le client `systemd-networkd` du lab ne repart pas seul apres le NAK, le
comportement de NetworkManager sur Raspberry Pi OS reste a verifier sur
materiel). Le bouton **Liberer** reste disponible pour forcer a la main, et
`DNSMASQ_AUTO_RELEASE=0` coupe l'automatisme.

Dans Bastion, referencer les machines par **IP** (pas `*.expolab.lan` :
l'hote et le conteneur ansible-web n'utilisent pas dnsmasq comme resolveur).

### L'image type

Raspberry Pi OS standard + `image/machine-setup.sh` (execute une fois, par
exemple depuis le `firstrun.sh` de l'Imager). Il pose :

- l'utilisateur d'automatisation, la cle publique
  `server/stacks/ansible-web/automation_key/automation_ed25519.pub` et un
  `sudo` sans mot de passe (`--stage access`) ;
- le proxy apt (`--stage network`) : `http://10.42.0.1:3142`, sans repli
  direct (la machine n'a pas internet) ;
- le serveur de temps `10.42.0.1` (chrony sur l'hote, aussi annonce par DHCP
  - option 42) : une carte neuve a l'horloge fausse, et apt refuse un
  `Release` « pas encore valide ».

`fleet/` utilise le **meme script** pour ses faux Pi
(`fleet/apply-machine-setup.sh` pour mettre a niveau des instances
existantes) : le lab reste fidele a l'image reelle. Le secours par mot de
passe d'`ansible-web` (`ansible_ssh_pass`) pourra etre retire une fois la
cle partout.

### Paquets sans internet : le cache apt

**Interface "Cache apt"** (portail > Reseau > Cache apt, `apt-cache-admin/`) :
etat (mode, service, disque, nombre de paquets), efficacite (part des paquets
servis par le cache, volume de transfert evite), activite par machine (avec leur
nom, d'apres les baux dnsmasq), journal des requetes filtrable, bascule en
ligne / hors ligne, edition des miroirs HTTPS. Le mode et les miroirs sont des
fichiers de `server/stacks/apt-cache/config/` relus au demarrage du cache : il
suffit de le redemarrer (`docker restart`), l'interface le fait. Le journal est
conserve dans `server/stacks/apt-cache/logs/` ; la page d'administration
integree d'apt-cacher-ng, ouverte a toutes les machines par defaut, est verrouillee
par un secret genere (`apt-cache.env`).

Les machines de l'expo **n'ont pas acces a internet** (`expo-lan` sans NAT) ;
seul le serveur sort. Tout `apt update/install/upgrade` passe **obligatoirement**
par la stack `apt-cache` (apt-cacher-ng, `10.42.0.1:3142`, sans repli en
connexion directe) : si le paquet est dans le cache il est servi, sinon le
cache le telecharge sur internet, le garde, puis le sert.

```bash
incus/lan-internet.sh status|on|off     # NAT des machines (depannage : on, puis off)
sudo ./server/apt-cache-mode.sh status|online|offline   # offline = SERVEUR sans internet
```

`deploy-server.sh` isole le reseau en fin d'installation
(`EXPOLAB_LAN_INTERNET=1` pour laisser le NAT) ; `install.sh` deploie le
serveur AVANT la flotte, pour que les faux Pi se provisionnent par le cache.

**Depots en HTTPS (Docker, un editeur...).** Les depots Debian et Raspberry Pi OS
sont en HTTP et passent tels quels. Pour un depot tiers en HTTPS, on le declare
sur le serveur, comme on declare un depot dans `git-mirror` : une URL `https://`
par ligne - depuis l'interface **Cache apt** du portail (section "Miroirs
HTTPS", bouton "Enregistrer et appliquer"), ou a la main dans
`server/stacks/apt-cache/config/https-mirrors.conf` (cree au deploiement, jamais
ecrase) puis `sudo ./server/apt-cache-mode.sh restart`.
Le serveur telecharge alors le depot en HTTPS et le garde ; seuls les depots
listes sont joignables ainsi (les tunnels HTTPS quelconques sont refuses).
Cote machine (role Ansible), le depot s'ecrit en **http://** avec la meme adresse,
par le proxy habituel :

```
deb [signed-by=/etc/apt/keyrings/docker.asc] http://download.docker.com/linux/debian bookworm stable
```

La cle GPG se recupere par le meme chemin (`http://download.docker.com/linux/debian/gpg`,
via le proxy) ou, mieux, se versionne dans le role ; `apt` verifie toujours la
signature lui-meme. Verifie dans le lab sur Docker : cle, `apt update` signe et
telechargement de `docker-compose-plugin` depuis une machine sans internet.

Limites assumees : cela ne couvre que les depots `apt` - un `pip`, un `curl`
ou un telechargement GitHub dans un role echoue sur une machine isolee (on le
decouvre avec la repetition ci-dessous ; `git-mirror` couvre les depots git).
Le cache relaie aussi de l'HTTP vers des hotes non declares pour les fichiers de
type paquet (constate : une requete vers un hote quelconque atteint l'amont) :
l'isolement est celui du NAT, pas celui du cache. Pas de gel de version (deux
jours differents peuvent donner des versions differentes). Le cache est ecrit
souvent (a deplacer sur SSD plutot que la carte SD si possible). Le serveur
reste un point unique de defaillance : **cloner sa carte SD** avant l'expo.

### Repeter le scenario avant l'expo

```bash
./fleet/offline-rehearsal.sh
```

Le script coupe le NAT d'`expo-lan` (s'il etait actif), cree de zero un faux Pi
(nom et MAC inedits), verifie qu'il n'a aucun acces a internet, applique **tous
les roles** depuis le conteneur ansible-web **par cle SSH et sudo sans mot de
passe**, rapporte le resultat, puis restaure le NAT et supprime la machine de
test (meme en cas d'echec). Aucun amorcage prealable : le cache telecharge a la
demande. Hors CI GitHub (necessite Incus).

Non couvert par la repetition : le materiel reel (firmware, GPIO, Raspberry
Pi OS Trixie/cloud-init a verifier sur une vraie carte), et l'heure (les
conteneurs partagent l'horloge de l'hote - NTP est non verifiable dans le lab).

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

1. `vpn/uninstall.sh` — arrete les stacks Docker `wireguard` et
   `vpn-admin`, supprime tous les pairs et la config generee
2. `server/teardown-server.sh` — arrete Dockhand et les stacks Docker
   (dnsmasq, dnsmasq-admin, caddy, caddy-admin, webui, bastion,
   dashboard, git-mirror, apt-cache, ansible-web), reactive le DHCP integre
   d'Incus sur `expo-lan` (secours), desinstalle Docker
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

## Tests et CI

`.github/workflows/ci.yml` (a chaque push/PR) : `ruff` (syntaxe et noms
indefinis), `bash -n` sur tous les scripts, validation YAML des
`docker-compose.yml`/`services.yaml`, `pytest tests` (rendu du Caddyfile :
modes TLS, basic auth, echec ferme ; `dockhand_stack.py` contre un faux Dockhand local) et `tests/test_deploy_libs.sh` (test de
fumee des modules `server/lib/*.sh` avec de faux `docker`/`ip`, sans Docker
ni Incus reels) et `tests/test_machine_setup.sh` (script d'image
`image/machine-setup.sh` sur un faux systeme de fichiers). Tout se lance aussi
en local : `pytest tests && bash tests/test_deploy_libs.sh && bash tests/test_machine_setup.sh`.

## Structure

```
install.sh                    # point d'entree unique, enchaine tout ce qui suit
incus/
  install.sh                # installe Incus sur l'hote
  uninstall.sh               # desinstalle Incus (symetrique de install.sh)
  network-setup.sh           # cree le reseau expo-lan (bridge isole, sans NAT) + profil fake-pi
  lan-internet.sh            # NAT des machines : on / off / status (normalement off)
  network-teardown.sh        # defait network-setup.sh
  profiles/fake-pi.yaml      # profil Incus flotte (reseau + limites CPU/RAM + pool disque)
fleet/
  inventory.yaml             # liste declarative des faux Pi (nom, MAC, role, identifiants)
  deploy-fleet.sh             # cree/provisionne les faux Pi manquants
  teardown-fleet.sh           # supprime les faux Pi de l'inventaire
  provision-fakepi.sh         # script de premier boot execute dans chaque conteneur
  apply-machine-setup.sh      # remet a niveau des faux Pi existants (cle, sudo, proxy apt, NTP)
  offline-rehearsal.sh        # repetition du remplacement d'une machine isolee d'internet (voir plus haut)
image/
  machine-setup.sh            # preparation de l'image type (cle d'automatisation, sudo, proxy apt, NTP)
server/
  deploy-server.sh             # orchestration : installe Docker, demarre Dockhand, cree/redeploie les stacks via son API
  lib/                           # une fonction par responsabilite, sources par deploy-server.sh :
                                 #   preflight, tls, secrets (+ basic auth), credentials, automation-key, ansible-web, apt-cache, ntp, dashboard, dnsmasq, stacks, summary
  apt-cache-mode.sh             # bascule le cache apt en ligne / hors ligne (ou le redemarre) en ligne de commande
  teardown-server.sh            # arrete Dockhand + les stacks (docker compose direct), reactive le DHCP integre d'Incus, desinstalle Docker
  docker-compose.yml            # bootstrap UNIQUEMENT : Dockhand (ne peut pas se creer via sa propre API)
  dockhand-api.sh                # helpers partages : attente sante, upsert d'une stack (appelle dockhand_stack.py)
  dockhand_stack.py              # implementation unique de "supprimer + recreer une stack Dockhand" (bash ET caddy-admin)
  dockhand_setup.py              # configuration initiale de Dockhand par son API : environnement local, utilisateur admin, authentification, jeton
  set-credentials.sh             # change le couple identifiant / mot de passe d'administration partout (Dockhand, portail, Bastion)
  admin_credentials.py           # meme logique en Python, utilisee par caddy-admin (formulaire "Identifiants")
  services.yaml                  # services applicatifs exposes au reverse-proxy (dashboard, dockhand, fleet, bastion, dnsmasq/caddy/vpn-admin, git-mirror)
  render-caddyfile.py             # genere le Caddyfile a partir de server/services.yaml
  stacks/
    dnsmasq/{docker-compose.yml, Dockerfile, dnsmasq.conf, data/, admin-config/}   # data/ et admin-config/ non versionnes (baux, reservations, enregistrements DNS)
    dnsmasq-admin/docker-compose.yml                                  # build context = ../../dnsmasq-admin
    caddy/{docker-compose.yml, Caddyfile}                            # Caddyfile genere, non versionne
    caddy-admin/docker-compose.yml                                    # build context = ../../caddy-admin
    webui/docker-compose.yml                                          # build context = ../../webui
    bastion/{docker-compose.yml, bastion.env, config/, maps/}        # ces 3 derniers non versionnes
    dashboard/{docker-compose.yml, dashboard.env, portal.env, services.yaml}   # le portail (code : portal/) ; les 3 fichiers .env/.yaml sont generes et non versionnes
    vpn-admin/docker-compose.yml                                      # build context = ../../vpn-admin
    git-mirror/{docker-compose.yml, data/}                            # data/ non versionne (clones bare + mirrors.yaml)
    apt-cache/{docker-compose.yml, Dockerfile, entrypoint.sh, apt-cache.env, config/, logs/, data/}  # apt-cacher-ng ; apt-cache.env (secret admin), config/ (mode, miroirs HTTPS), logs/ et data/ non versionnes
    apt-cache-admin/docker-compose.yml                                # build context = ../../apt-cache-admin
    ansible-web/{docker-compose.yml, ansible-web.env, automation_key/, config.yaml, repo/, runs/}  # tout sauf docker-compose.yml non versionne ; execute bastion-ansible (depot separe, clone depuis git-mirror)
webui/
  app.py                        # backend Flask : edite inventory.yaml, pilote deploy-fleet.sh
  Dockerfile                     # image (Flask + client Incus)
  templates/, static/            # page unique HTML/JS/CSS
dnsmasq-admin/                  # meme forme que webui/ : app.py, Dockerfile, templates/, static/
apt-cache-admin/                 # idem - interface du cache apt : etat, efficacite, journal, mode, miroirs HTTPS
portal/                         # le portail (stack dashboard) : connexion unique, tiroir et onglets, verification de session pour Caddy
caddy-admin/                     # idem - edite server/services.yaml, recharge Caddy via son admin API
vpn-admin/                       # idem - appelle vpn/add-peer.sh et vpn/remove-peer.sh
git-mirror/                      # idem, mais fait lui-meme le travail git (clone/fetch), rien a piloter
ansible-web/                     # idem, mais fait lui-meme le travail ansible-playbook (clone/pull depuis git-mirror, execute)
vpn/
  install.sh                  # genere wg0.conf, cree/redeploie les stacks Dockhand "wireguard" et "vpn-admin"
  uninstall.sh                 # arrete la stack (docker compose direct), supprime la config generee
  add-peer.sh                  # ajoute un pair VPN (docker exec wireguard wg ...) + config client
  remove-peer.sh                # retire un pair VPN
  wireguard/{docker-compose.yml, Dockerfile, entrypoint.sh, config/}   # config/ non versionne (cles, pairs)
rollback.sh                   # orchestre les 5 teardown dans le bon ordre
```

## A venir

- Deploiement effectif de Gitea (conteneur Docker sur l'hote, gere depuis
  Dockhand), puis entree correspondante dans `server/services.yaml`
- Authentification sur la webui (aucune pour l'instant - protegee
  uniquement par l'isolation reseau d'`expo-lan`)
- Authentification Dockhand (desactivee par defaut, protegee comme la
  webui par l'isolation reseau pour l'instant) - l'activer cree un premier
  compte admin dans son UI ; deposer ensuite un token dans
  `server/dockhand.env` (non versionne) pour que `deploy-server.sh` et
  `vpn/install.sh` continuent de fonctionner (voir `server/dockhand-api.sh`)
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
