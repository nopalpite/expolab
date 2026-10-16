# expolab

Infra d'exposition : un serveur (Raspberry Pi 5) et les vraies machines de
l'expo (Raspberry Pi), reliees au serveur par un switch.

- **Le reseau de l'expo** (`network/`) - un pont `expo-lan` sur une vraie
  carte reseau du serveur, reliee au switch des machines.
- **Le serveur d'expo** (`server/`, `vpn/`) - DHCP/DNS, reverse-proxy, et
  les services applicatifs (Bastion, ansible-web, plus tard Gitea) tournent
  en Docker **directement sur l'hote**. **Dockhand est le
  point d'entree de gestion de tout le stack** : chaque service (y
  compris le VPN WireGuard) est une stack Dockhand independante,
  creee/redeployee via son API plutot qu'un unique `docker-compose.yml`
  monolithique - voir `server/dockhand-api.sh`.

## Architecture reseau

Le serveur a **deux cartes reseau** :
- celle qui lui donne internet (ex: `eth0`, route par defaut) ;
- celle du **reseau de l'expo** (ex: `eth1` ou un adaptateur USB-Ethernet),
  reliee au switch ou sont branchees les machines.

`network/setup.sh <carte>` cree sur la seconde un **pont NetworkManager
`expo-lan`** (la carte en est le seul port) qui porte `10.42.0.1/24`, sans
passerelle ni IPv6. Un pont plutot que l'adresse posee sur la carte : le nom
`expo-lan` ne depend pas de la carte (dnsmasq, le VPN et les pages
d'administration n'ont rien a savoir), et un pont garde son adresse **meme
cable debranche** - dnsmasq demarre et le DNS des clients VPN reste joignable,
switch eteint. Le script refuse une carte Wi-Fi, la carte qui porte la route
par defaut (ce serait couper l'acces du serveur), et une carte deja utilisee
par une autre connexion (sauf `--force`).

Les machines de l'expo n'ont **pas acces a internet** : pas de NAT sur
`expo-lan`, seul le serveur sort (voir "Paquets sans internet").

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
- **Bastion** (https://github.com/nopalpite/bastion, dashboard +
  SSH/VNC web) en `network_mode: host` - image prete a l'emploi publiee
  sur GHCR (`ghcr.io/nopalpite/bastion`), avec les **identifiants
  d'administration choisis a l'installation** (les memes que partout, voir
  "Identifiants d'administration" plus bas, conserves dans `bastion.env`).
  Son resolveur DNS est **dnsmasq** (`server/stacks/bastion/resolv.conf`, monte
  sur `/etc/resolv.conf`) et non celui de l'hote : le scan reseau de Bastion
  nomme ainsi les machines par DNS inverse (nom annonce au DHCP), sans mDNS.
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
  `#dnsmasq`, point vert / rouge selon l'etat du service, bouton "ouvrir a part").
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
  (`frame-ancestors 'self' https://dashboard.<domaine>` : le portail, et le service
  lui-meme pour ses propres sous-cadres, comme la session SSH de Bastion).
- **dnsmasq-admin**, **caddy-admin**, **vpn-admin** - pages minimales
  (Flask) pour piloter dnsmasq/Caddy/WireGuard sans repasser par la ligne
  de commande : editer/lire un fichier de config existant et reutiliser les
  scripts/mecanismes deja en place, sans rien reimplementer.
  - `dnsmasq-admin` (https://dnsmasq.web.expolab.lan) : onglet **Topologie**,
    vue en direct du reseau de l'expo - une ligne par machine (bail, reservation,
    presence ARP sondee toutes les 10 s en une seule commande, `DNSMASQ_PRESENCE_INTERVAL`),
    alertes (sans reservation, IP de repli, nom en double), reservations sans bail
    en grise, detail et actions (reserver, liberer), journal des arrivees/departs.
    Poussee au navigateur par SSE (`/api/topology/stream`) a chaque changement : le
    fichier de baux, que dnsmasq reecrit a chaque bail, est surveille chaque seconde
    (aucun script branche sur dnsmasq). Onglet **Baux et reservations** : baux DHCP actifs
    (lecture seule) + **reservations DHCP** (MAC -> IP fixe), ecrites
    dans un fichier dedie (`server/stacks/dnsmasq/admin-config/`, non
    versionne) que dnsmasq relit **a chaud sur SIGHUP**
    (`--dhcp-hostsfile`, voir `dnsmasq.conf`) via
    `docker kill --signal=HUP expolab-dnsmasq` - pas de redemarrage du
    conteneur, pas de coupure DHCP/DNS pour le reste des machines. Pas
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

Le reverse-proxy n'expose PAS les machines de l'expo : il sert a exposer
les services applicatifs du serveur d'expo lui-meme, declares dans
`server/services.yaml`.

La patte WAN/VPN (`vpn/`) est desormais **conteneurisee** elle aussi (stack
Dockhand `wireguard`, `vpn/wireguard/`), pour la meme coherence que le
reste : `network_mode: host` (comme dnsmasq/caddy/bastion, necessaire pour
que ses regles PostUp/PostDown iptables visent les vraies interfaces de
l'hote) et route le trafic des clients VPN vers `10.42.0.0/24`. Cles et
config des pairs persistees dans `vpn/wireguard/config/` (non versionne).
L'hote lui-meme n'a plus besoin du paquet `wireguard-tools` : `vpn/add-peer.sh`/
`remove-peer.sh` passent par `docker exec wireguard wg ...`.

**Pourquoi le VPN, meme en local** : `dnsmasq` ne repond qu'a la resolution
DNS sur `expo-lan` (par design - pas question de repondre au DHCP/DNS du
reseau auquel le serveur est relie pour internet). Un poste sur le meme
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
Endpoint = <adresse>:51820   # adresse publique du VPN si elle est configuree (voir
                               # "Acces depuis l'exterieur"), sinon IP LAN de l'hote
AllowedIPs = 10.42.0.0/24, 10.66.66.0/24   # split tunnel : seul le trafic vers
                                             # expo-lan et le VPN lui-meme passe
                                             # par le tunnel, le reste (internet)
                                             # suit la route habituelle du client
PersistentKeepalive = 25   # garde la connexion ouverte a travers un NAT/firewall
```

### Acces depuis l'exterieur (VPN derriere un routeur)

Derriere un routeur qui fait de la redirection de port :
1. **Routeur** : rediriger le port **UDP 51820** vers le Pi (et lui reserver une
   adresse fixe). C'est la seule redirection necessaire : jamais le 443, le 3000
   ni le 5000, tout le reste passe par le tunnel.
2. **Adresse publique** : une IP publique, ou mieux un **nom DNS** (DDNS) qui
   pointe dessus (ex. `vpn.nopalpite.fr`), car l'IP d'une box change parfois.
   La renseigner **une fois** dans la page VPN du portail, section "Adresse
   publique du VPN" (ou `echo vpn.exemple.fr:51820 >
   vpn/wireguard/config/public-endpoint`) : `add-peer.sh` l'utilise ensuite pour
   tous les nouveaux pairs. La case "Mettre aussi a jour les pairs existants"
   reecrit la ligne `Endpoint` de leurs configs ; un poste qui les avait deja
   importees garde l'ancienne adresse, il faut lui redonner le `.conf` / QR.
   Sans adresse publique, un pair vise l'IP locale du Pi (utilisable seulement
   depuis le reseau local) ; une adresse locale est acceptee mais signalee.
3. **Limites** : derriere un CGNAT (pas d'IP publique propre) la redirection ne
   peut pas fonctionner ; chez soi, certaines box ne savent pas viser leur propre
   IP publique ("hairpin NAT") - garder un pair avec l'adresse locale pour la
   maison.

Deux domaines DNS distincts, resolus par `dnsmasq` :
- `<nom>.expolab.lan` — enregistrement DHCP automatique, pointe vers la
  **vraie IP** de chaque machine - acces direct (SSH)
- `<nom>.web.expolab.lan` — wildcard fixe vers l'hote (`10.42.0.1`, ou
  tourne Caddy), c'est le nom que doit utiliser un client HTTPS pour
  passer par le reverse-proxy

Ne pas les confondre : aucun vhost `<nom-de-machine>.web.expolab.lan` n'est
genere pour les machines (voir plus haut, le reverse-proxy ne les concerne pas).

## Prerequis

- Raspberry Pi 5 avec Raspberry Pi OS (ou Debian 64-bit) a jour, reseau gere
  par **NetworkManager** (defaut de Raspberry Pi OS)
- **Deux cartes reseau** : une pour internet (Wi-Fi ou Ethernet), une
  Ethernet pour le reseau de l'expo (`eth1`, adaptateur USB-Ethernet...),
  reliee au switch des machines. Pour **tester le deploiement sans cette
  seconde carte**, voir `--sans-carte` plus bas
- Une connexion internet sur l'hote pour que Docker telecharge ses images
  et que le cache apt serve les machines
- Le **fuseau horaire de l'hote** (`timedatectl set-timezone Europe/Paris`) : chaque
  conteneur monte `/etc/localtime` de l'hote, toutes les pages affichent donc la meme heure

## Mise en route (a executer directement sur le Pi 5)

Point d'entree unique - installe tout dans l'ordre :

```bash
sudo ./install.sh eth1    # eth1 = la carte reliee au switch des machines
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
sudo ./network/setup.sh eth1
# Carte deja utilisee par une autre connexion (ex: "Wired connection 2",
# creee par NetworkManager au branchement) : ajouter --force.

sudo ./server/deploy-server.sh
# 1ere fois : demande les identifiants d'administration, puis configure
# Dockhand (environnement, utilisateur, authentification) par son API.

sudo ./vpn/install.sh
# Cree aussi un pair "default" pret a distribuer (vpn/wireguard/config/peers/default.conf)
sudo ./vpn/add-peer.sh mon-laptop   # optionnel : un pair nommement identifie en plus
```

`network/setup.sh` est idempotent : relance sans argument, il garde la carte
deja choisie ; avec un autre nom, le pont change de carte.

**Tester sans seconde carte** : `sudo ./install.sh --sans-carte` (ou
`sudo ./network/setup.sh --sans-carte`) cree le pont `expo-lan` **seul**, sans le
rattacher a une carte - les autres etapes n'ont besoin que de ce pont et de son
adresse. Tous les services demarrent, mais **aucune machine ne peut s'y connecter**
(il n'y a pas de switch). Pour brancher le reseau de l'expo ensuite : `sudo
./network/setup.sh eth1` - le pont est conserve, la carte s'y ajoute. Le script
refuse `--sans-carte` si une carte est deja rattachee (il ne coupe jamais un reseau
d'expo en service : `network/teardown.sh` d'abord), et n'accepte pas les deux a la
fois. Il ne contourne pas la protection de la route par defaut : donner `eth0` a
l'expo couperait l'acces du serveur, ce qui reste refuse. `deploy-server.sh`
est idempotent aussi : relancez-le apres avoir modifie `server/services.yaml`
ou le code d'une application pour regenerer le Caddyfile et redeployer les
stacks concernees via l'API Dockhand.

## Interface d'exploitation (compte exploitant)

Pour l'exploitant de l'exposition : un **second compte**, cree par l'administrateur dans
le portail (**Compte > Identifiants**, section "Compte exploitant", mot de passe
administrateur exige ; fichier `server/stacks/dashboard/operator.env`, separe
d'`auth.env`). Il se connecte au meme portail et ne voit que **deux onglets** :

- **Plans des salles** (lecture seule, servi par le portail, `portal/plans.py`) : les
  plans de Bastion avec chaque machine a sa position, point vert / rouge clignotant / gris
  (en ligne / ne repond plus / inconnue) d'apres la presence de dnsmasq-admin (Topologie),
  rafraichi toutes les 5 s. **Meme technique de placement que Bastion** : le cadre du
  plan a exactement le rapport largeur/hauteur de l'image, les positions sont en % de ce
  cadre et centrees, la taille des points suit la largeur affichee - un point reste au
  meme endroit du plan sur telephone, tablette, avec zoom ou non. Donnees de Bastion lues
  en lecture seule (`config/machines.yaml`, `maps/`), en liste blanche : aucun identifiant
  n'en sort. Un clic sur une machine : etat, adresse, et "Preparer une carte" pour elle.
- **Cartes SD** (page `/exploitation` du flasheur, dans un cadre) : carte detectee, menu
  des machines (reservations par nom), un bouton, une confirmation, la progression, "Carte
  prete". L'image est celle choisie par l'administrateur (**"Image utilisee par
  l'exploitant"**, section Image master du flasheur ; aucune = rien a flasher) et tous les
  reglages sont ceux de l'administrateur : l'API d'exploitation ne prend du navigateur que
  la carte et le nom de la machine (reservee). Journal d'audit : `actor: exploitant`.

**Controle d'acces** : la session porte un role, signe avec le hash du mot de passe de ce
role (changer le mot de passe exploitant ne ferme que ses sessions ; un jeton exploitant ne
peut pas passer pour un administrateur). Pour l'exploitant, `/auth/verify` (Caddy) ne
laisse passer QUE `sd-flasher.<domaine>/exploitation`, `/api/exploitation/*` et
`/static/*` ; tout le reste repond 403 (ou renvoie a son accueil). Le portail lui refuse
aussi la liste des services. Le basic auth (curl, git) reste reserve a l'administrateur.

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
  `server/services.yaml` (dockhand, dnsmasq, caddy, vpn, git-mirror,
  ansible-web). Identifiants = ceux choisis a l'installation
  (`server/stacks/caddy/auth.env`, non versionne, hash bcrypt calcule avec
  le binaire `caddy`). Pas de basic auth sur
  `dashboard` (liens uniquement) ni `bastion` (a sa propre authentification) ;
  le chemin `/git/*` de git-mirror est exempte (`auth_except`) car utilise
  par des clients git. `render-caddyfile.py` refuse de generer un Caddyfile
  si un service demande l'auth sans identifiants disponibles (jamais de
  vhost silencieusement ouvert). Reglable par service depuis caddy-admin.
- **Les backends ne sont joignables que via Caddy** : leurs ports sont
  publies sur `127.0.0.1` (dashboard, dnsmasq-admin, git-mirror) ou
  leur serveur Flask ecoute sur `127.0.0.1` (caddy-admin, vpn-admin,
  ansible-web) - sans ca, le basic auth serait contournable en visant
  directement `<ip-du-pi>:<port>` depuis le LAN. **Exceptions assumees** :
  Dockhand (`:3000`, joignable avant que Caddy existe) et Bastion (`:5000`),
  tous deux proteges par leur propre authentification, avec les memes
  identifiants d'administration.
- **Acces aux machines** : ansible-web se connecte par la **cle
  d'automatisation** (injectee par le flasheur de cartes SD, sudo sans mot de
  passe), jamais par mot de passe. Compte utilise : `EXPOLAB_ANSIBLE_USER`
  au deploiement (defaut `pi`), le meme que celui cree par le flasheur.

## Verifier / se connecter

```bash
ip -br addr show expo-lan        # 10.42.0.1/24 sur le pont de l'expo
docker exec expolab-dnsmasq cat /var/lib/misc/dnsmasq.leases   # machines et leurs IP
ssh pi@<ip-d-une-machine>

# http://<ip-du-pi>:3000 - toutes les stacks (dnsmasq, caddy,
# bastion, wireguard, dashboard) y sont visibles/pilotables

# Reverse proxy HTTPS (TLS auto-signe, cert "not trusted" attendu sans
# importer le CA interne de Caddy) :
curl -k https://dashboard.web.expolab.lan  # point d'entree, liens vers tout le reste
# Les UIs d'administration ci-dessous demandent le basic auth Caddy
# (curl -k -u <identifiant>:<mot de passe choisi a l'installation>) :
curl -k https://dockhand.web.expolab.lan   # Dockhand
curl -k https://bastion.web.expolab.lan    # Bastion (memes identifiants)
curl -k https://dnsmasq.web.expolab.lan    # baux DHCP actifs
curl -k https://caddy.web.expolab.lan      # ajouter/retirer un service expose
curl -k https://vpn.web.expolab.lan        # pairs WireGuard (QR code, trafic)

# VPN : importer vpn/wireguard/config/peers/default.conf (ou mon-laptop.conf)
# sur le poste client (WireGuard app ou wg-quick), puis une fois connecte, les memes URLs
# https://*.web.expolab.lan et un acces SSH direct aux machines
# fonctionnent depuis ce poste.
```

## Remplacer une machine de l'expo (hors ligne)

Scenario : pendant l'exposition, un Raspberry Pi meurt. On le change, on
insere une carte SD flashee avec l'image type, on lance le playbook Ansible
- **sans internet**. La carte neuve reprend le **meme nom et la meme IP**
que la morte : son entree Bastion (identifiants, tags) reste valable.
L'installation du *serveur* est un autre sujet (connecte).

### Ce qui a ete verifie (experience dans le lab, sur des machines simulees)

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

**Liberation automatique des baux.** dnsmasq-admin verifie toutes les 10 s (et
aussitot qu'un bail change), pour chaque reservation par nom, que la machine qui
tient l'IP repond encore en ARP ; apres 3 verifications sans reponse ET au moins
60 s de silence (`DNSMASQ_STALE_AFTER`) son bail est libere. Une duree et non un
nombre de passages : le Wi-Fi rate parfois des requetes ARP, et liberer le bail d'une
machine vivante ferait disparaitre son nom du DNS jusqu'a son renouvellement. La carte neuve, qui demarre ensuite, prend directement l'IP. Une
machine simplement eteinte retrouve elle aussi sa place a son retour. Un detenteur
qui repond encore n'est **jamais** libere : deux machines vivantes pour un meme nom
donnent un conflit, signale dans `docker logs expolab-dnsmasq-admin` et sur
`/api/events`.

Si la carte neuve est branchee **avant** cette liberation, elle recoit d'abord une
IP de repli ; dnsmasq-admin libere alors les deux baux des qu'il le voit. Deux
durees de bail font le reste :
- les adresses de la **plage dynamique** (donc l'IP de repli) ont un bail de
  **2 min** (`dnsmasq.conf`). La carte la redemande au bout d'une minute et dnsmasq
  refuse (NAK) - mais `systemd-networkd` (Ubuntu) **ignore ce refus** pendant un
  renouvellement. C'est l'**expiration** du bail, 2 min au plus tard, qui la fait
  repartir de zero et prendre son adresse reservee. Verifie sur une vraie carte :
  retour seul en 1 min 20 s ;
- les **reservations** gardent un bail de **12 h**, ecrit sur chaque ligne de
  `reservations.conf` par dnsmasq-admin (`nom,ip,12h` ; les anciennes lignes sont
  completees a son demarrage) : une panne du serveur ne leur fait pas perdre leur
  adresse.

Constate avant ce reglage : une carte arrivee 1 s trop tot est restee 6 h (moitie
d'un bail de 12 h) sur l'IP de repli. A savoir : un appareil **non reserve** (portable
d'admin, point d'acces) perd son adresse apres 2 min de panne de dnsmasq - lui donner
une reservation s'il doit tenir. Le bouton **Liberer** reste disponible pour forcer a
la main, et `DNSMASQ_AUTO_RELEASE=0` coupe l'automatisme.

Dans Bastion, referencer les machines par **IP** (pas `*.expolab.lan` :
l'hote et le conteneur ansible-web n'utilisent pas dnsmasq comme resolveur).

### L'image type

Ubuntu Server pour Raspberry Pi + `image/machine-setup.sh` (execute une fois sur
la carte modele - preparation complete : `image/README-master.md`). Il pose :

- l'utilisateur d'automatisation, la cle publique
  `server/stacks/ansible-web/automation_key/automation_ed25519.pub` et un
  `sudo` sans mot de passe (`--stage access`) ;
- le proxy apt (`--stage network`) : `http://10.42.0.1:3142`, sans repli
  direct (la machine n'a pas internet) ;
- le serveur de temps `10.42.0.1` (stack `ntp`, voir plus bas ; aussi annonce par DHCP
  - option 42) : une carte neuve a l'horloge fausse, et apt refuse un
  `Release` « pas encore valide ».


### Flasher les cartes SD

**Interface "Cartes SD"** (portail > Infrastructure > Cartes SD, `sd-flasher/`).
Un lecteur de carte USB branche **sur le serveur** (un navigateur ne peut pas
ecrire sur un disque brut) : la page detecte la carte, propose les machines
connues (reservations DHCP **par nom**, avec leur adresse), et ecrit l'image
master commune, puis la personnalise.

1. Envoyer l'image master **depuis la page** (section "Image master" : choisir le
   fichier, SHA-256 attendu facultatif, barre de progression ; `.img`, `.img.gz`,
   `.img.xz`, `.img.zst`), ou la copier a la main dans
   `server/stacks/sd-flasher/images/`. La plus recente est utilisee par defaut ; un
   bouton supprime les anciennes. Sa preparation : `image/README-master.md`.
2. Brancher le lecteur, choisir la machine, **retaper son nom** pour confirmer.
3. Quatre etapes affichees : ecriture, verification (relecture du support et
   comparaison de l'empreinte), personnalisation, ejection. A la fin : « Carte
   prete, tu peux la retirer — module-02 / 10.42.0.148 ».

Le master est un **Ubuntu Server** : la personnalisation passe par **cloud-init**,
sans script maison dans le master. Ecrit **avant** le premier demarrage :
une graine cloud-init sur la partition `system-boot` (`user-data` : hostname,
**compte utilisateur** - nom, mot de passe hache, sudo sans mot de passe, cles SSH ;
`network-config` : DHCP et Wi-Fi ; `meta-data` : un `instance-id`
neuf a chaque flash, pour que cloud-init rejoue tout), puis sur la racine
`/etc/hostname` (le nom est annonce des la premiere requete DHCP, or les
reservations sont par nom), `/etc/machine-id` vide (sinon toutes les machines ont
le meme identifiant) et cles SSH d'hote supprimees (cloud-init en genere de
neuves). L'agrandissement du systeme de fichiers au premier demarrage est fait par
cloud-init (`growpart`/`resizefs`). Reglages dans la page : compte (a definir : le
`user-data` par defaut de l'image Ubuntu, qui creait le compte et son mot de passe,
est remplace - sans compte, personne ne peut se connecter ; la page avertit),
Wi-Fi, **serveur de temps du lab** (chrony, ajoute aux sources d'Ubuntu), **proxy apt** (prerempli :
`http://10.42.0.1:3142`, le cache du serveur ; a vider pour une carte destinee a un autre reseau), **clavier, langue et fuseau horaire** (azerty `fr`, `fr_FR.UTF-8` et `Europe/Paris`
par defaut, modules `keyboard`, `locale` et `timezone` de cloud-init ; l'image Ubuntu
est en qwerty et en UTC), **paquets a installer** (installes au demarrage de la machine par
le cache apt - proxy apt obligatoire - par un petit service `expolab-packages` qui reessaie
chaque minute et a chaque demarrage jusqu'a reussite, puis ne se relance plus ; pas le module
`packages` de cloud-init, qui ne tourne qu'au premier demarrage, quand une carte en Wi-Fi n'a
pas encore de reseau, et ne reessaie jamais) et cles (la cle d'automatisation Ansible est injectee par defaut) ; le mot de passe Wi-Fi reste sur
le serveur (`data/settings.json`, mode 600), jamais renvoye au navigateur.

**Garde-fous** (verifies a chaque flash, pas seulement a l'affichage) : seuls les
disques **USB** en ecriture, de 1 Go a 256 Go ; jamais un disque que le serveur
utilise (montages lus dans `/proc/1/mountinfo` de l'hote ; si illisibles, tout
est refuse) ; le peripherique doit figurer dans la liste actuelle des cartes
eligibles (jamais un chemin pris tel quel), et re-verifie (meme numero de serie, meme
taille) juste avant d'ecrire ; image brute plus grande que la carte refusee d'emblee ;
confirmation par retape du nom ; un seul flash a la fois ; journal d'audit (`data/audit.log`, affiche dans la page).
Le conteneur est **privilegie** (`pid: host`, `/dev`) : c'est le prix d'ecrire
sur un disque ; il ne publie qu'un port local, derriere le portail.

**Envoi d'images** : le fichier est ecrit en flux directement sur disque (jamais
en memoire) dans `<nom>.part`, puis renomme - une image a moitie envoyee n'apparait
jamais. Refuses : nom hors `[A-Za-z0-9._-]`, type inconnu, fichier dont le debut ne
correspond pas a son extension (verifie aussi dans le navigateur avant d'envoyer),
nom deja pris, espace insuffisant (1 Go est toujours laisse au serveur), envoi
interrompu, un envoi deja en cours, et un SHA-256 qui ne correspond pas a celui
fourni. Une image en cours d'ecriture ne peut pas etre supprimee.

**Depuis un lien** (section "Depuis un lien") : le serveur telecharge lui-meme l'image
(http/https uniquement, redirections comprises mais jamais vers `file://` ou `ftp://`),
en arriere-plan avec progression et annulation, et avec les memes controles qu'un envoi
(nom, extension, debut du fichier, place, SHA-256 facultatif, `.part` renomme a la fin).
**Telecharger** une image stockee : bouton sur chaque ligne de la liste
(`/api/images/<nom>/file`, reprise possible apres coupure).

**Capturer une carte** (section du meme nom, `sd-flasher/capture.py`) : copie une carte
Raspberry Pi OS / Ubuntu en image **reduite**, rangee avec les autres images. La carte
est seulement lue, jamais montee ni modifiee. Pour ne pas demander au serveur la taille
de la carte entiere : debut de la carte (table MBR, partition de demarrage) copie tel
quel, puis seuls les blocs utilises de la partition systeme (`e2image -ra`, fichier
creux), `e2fsck` + `resize2fs -M` sur cette copie, taille corrigee dans la table, et
compression (`.img.zst` par defaut, `.img.xz` plus petit mais lent, ou `.img`). Place
necessaire verifiee avant de commencer (~2 x les donnees utilisees). Disposition
acceptee : MBR, partition systeme ext2/3/4 primaire et derniere ; le reste est refuse.
Une capture et un flash ne tournent jamais en meme temps (meme lecteur). A savoir : une
image Ubuntu re-agrandit sa partition au premier demarrage (cloud-init), une image
Raspberry Pi OS deja demarree non.

**Preparer en image master** (case cochee par defaut) : apres la copie et avant la
reduction, la copie (jamais la carte) est montee en boucle et passee a
`image/prepare-master.sh --remove-accounts` - le meme script que sur une machine, ici hors
ligne : graine du flasheur neutre, cloud-init remis a neuf (dont le netplan qui contenait le
Wi-Fi en clair), identite, menage, et **tous les comptes ordinaires** supprimes avec leur
dossier personnel, leurs cles et leurs droits sudo, ainsi que les fichiers que le flasheur
ecrit (proxy apt, serveur de temps). L'image se comporte alors comme une Ubuntu neuve : le
flasheur cree le compte et applique **tous** ses reglages, sans reste de la machine
d'origine. Ce qui est installe hors de `/home` (`/opt`, `/usr/local`, paquets...) est
conserve. Decocher pour une copie brute exacte (comptes, cles et historique gardes). Le
script est monte dans le conteneur depuis le depot (`/app/prepare-master.sh`).

**Au flash**, un reglage **vide** (proxy apt, serveur de temps) supprime le fichier
correspondant sur la carte s'il existe (cas d'une image capturee brute), au lieu de laisser
l'ancienne valeur.

Aucun secret ne doit rester dans l'image, y compris dans l'espace LIBRE : un fichier efface
ou remplace garde son contenu dans ses anciens blocs. Cote systeme ext4, `e2image` ne copie
que les blocs utilises, donc rien n'en sort. La partition de demarrage, elle, est copiee
telle quelle : la capture y remplit de zeros les clusters FAT **libres** (sur la copie, la
carte reste intacte) - sans cela, le mot de passe Wi-Fi et le hache de la graine du flasheur,
remplacee par `prepare-master.sh`, se retrouvaient dans l'image (constate sur disque virtuel).
Un cluster n'est vide que si sa case de la table FAT vaut 0 ; si la partition n'est pas un
FAT16/32 reconnu, la capture continue mais le signale (`boot_wipe_note`). L'archive est ensuite
**relue et comparee** aux octets envoyes au compresseur avant d'apparaitre dans la liste.

`SD_FLASHER_DRY_RUN=1` : mode simulation (ecrit dans des fichiers, aucun disque
touche) - c'est comme ca que les tests tournent, sans materiel.

**Reste a valider sur materiel** : detection du lecteur depuis le conteneur
(partitions qui apparaissent apres l'ecriture), montage des partitions de la
carte, ejection, et le premier demarrage d'une vraie carte (cloud-init
applique la graine, agrandissement de la racine, cles d'hote differentes) - liste
de verification dans `image/README-master.md`.

### Heure : le serveur de temps

Une carte de remplacement demarre avec une horloge fausse (pas de pile RTC), et sans
internet rien ne la remet a l'heure : `apt` refuse alors un fichier `Release` « pas
encore valide », et les journaux deviennent incoherents. Le serveur sert l'heure au lab.

**Interface "Heure"** (portail > Reseau > Heure, `ntp-admin/`) :
- **Etat** : synchronise sur internet, ou « horloge locale » (aucun serveur amont : les
  machines recoivent l'heure du serveur, qui peut deriver) ; precision, derniere mise a
  l'heure, heure du serveur comparee a celle de ton navigateur ; bouton « Remettre a
  l'heure maintenant » (saut d'horloge, pour une heure tres fausse).
- **Reglages** : mode en ligne / hors ligne (hors ligne : aucun serveur amont contacte),
  serveurs amont, reseaux autorises a demander l'heure (par defaut `10.42.0.0/24`
  seulement, jamais le reseau de la maison ; un reseau plus large que /8 est refuse),
  repli sur l'horloge du serveur quand aucun amont n'est joignable (recommande).
- **Sources** et **machines** qui ont demande l'heure (avec leur nom).

Le service est **chrony dans un conteneur** (`server/stacks/ntp/`, image maison, `network_mode:
host`, capacite `SYS_TIME` : il regle aussi l'horloge de l'hote). Il remplace le chrony
installe sur l'hote par les versions precedentes : `deploy-server.sh` l'arrete et le
desactive (meme port UDP 123). Les reglages sont des fichiers dans
`server/stacks/ntp/config/` (non versionnes), relus au redemarrage du conteneur ; une ligne
invalide est ignoree avec un message plutot que d'empecher le demarrage. Les machines
l'utilisent par l'option DHCP NTP de dnsmasq (`dhcp-option=option:ntp-server`).

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
sudo ./network/lan-internet.sh status|on|off   # NAT des machines (depannage : on, puis off ; un redemarrage le coupe)
sudo ./server/apt-cache-mode.sh status|online|offline   # offline = SERVEUR sans internet
```

`deploy-server.sh` isole le reseau en fin d'installation
(`EXPOLAB_LAN_INTERNET=1` pour laisser le NAT tel quel).

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
ou un telechargement GitHub dans un role echoue sur une machine isolee
(`git-mirror` couvre les depots git).
Le cache relaie aussi de l'HTTP vers des hotes non declares pour les fichiers de
type paquet (constate : une requete vers un hote quelconque atteint l'amont) :
l'isolement est celui du NAT, pas celui du cache. Pas de gel de version (deux
jours differents peuvent donner des versions differentes). Le cache est ecrit
souvent (a deplacer sur SSD plutot que la carte SD si possible). Le serveur
reste un point unique de defaillance : **cloner sa carte SD** avant l'expo.

## Redemarrage de l'hote (coupure de courant)

Apres un redemarrage, tout doit revenir **sans relancer `install.sh`** : c'est ce
qui garde l'acces distant. Ce qui s'y passe :

1. **NetworkManager remonte le pont `expo-lan`** (`10.42.0.1`), meme cable
   debranche (connexions `expo-lan` et `expo-lan-port`, demarrage automatique).
2. **Docker relance les conteneurs** (`restart: unless-stopped`). dnsmasq
   **attend** que son interface porte son adresse (`server/stacks/dnsmasq/entrypoint.sh`)
   au lieu de s'arreter : si le reseau est plus lent que Docker, il demarre des
   que le pont apparait. L'interface et l'adresse sont lues dans `dnsmasq.conf` ;
   sans elle apres 10 min, le conteneur s'arrete en erreur plutot que de rester
   "en marche" sans servir.

Pour verifier apres un changement : `sudo reboot`, puis, une minute plus tard,
`ip -br addr show expo-lan` (adresse `10.42.0.1/24`) et `docker ps` (tout `Up`,
`expolab-dnsmasq` sans redemarrages).

## Rollback (retour a l'etat initial)

Deux niveaux, a combiner :

**1. Le plus fiable : image disque AVANT de commencer.** Les scripts
d'installation touchent au reseau systeme et installent des paquets ; le
seul rollback garanti a 100% reste une image complete de la carte SD/NVMe
prise avant `install.sh` (Raspberry Pi Imager -> "Sauvegarder", ou
`dd if=/dev/mmcblk0 of=backup.img bs=4M status=progress` depuis un autre
poste). Restaurer cette image = retour exact a l'etat d'avant, quoi qu'il
se soit passe entre-temps.

**2. Rollback scripte (rapide, pour un usage courant) :**

```bash
sudo ./rollback.sh          # demande confirmation avant chaque suppression
sudo ./rollback.sh --yes    # sans confirmation
```

Ce script defait dans l'ordre ce que `install.sh` a mis en place :

1. `vpn/uninstall.sh` — arrete les stacks Docker `wireguard` et
   `vpn-admin`, supprime tous les pairs et la config generee
2. `server/teardown-server.sh` — arrete Dockhand et les stacks Docker
   (dnsmasq, dnsmasq-admin, caddy, caddy-admin, bastion, dashboard,
   git-mirror, apt-cache, ansible-web...), desinstalle Docker
3. `network/teardown.sh` — supprime le pont `expo-lan` ; la carte redevient
   une carte ordinaire

Chaque etape est aussi utilisable seule. Limite connue : un `apt purge` ne
garantit jamais un retrait 100% parfait (fichiers de config residuels, etc.) —
d'ou la recommandation de l'image disque si un retour a l'etat initial
*exact* est requis.

## Tests et CI

`.github/workflows/ci.yml` (a chaque push/PR) : `ruff` (syntaxe et noms
indefinis), `bash -n` sur tous les scripts, validation YAML des
`docker-compose.yml`/`services.yaml`, `pytest tests` (rendu du Caddyfile :
modes TLS, basic auth, echec ferme ; `dockhand_stack.py` contre un faux Dockhand local) et `tests/test_deploy_libs.sh` (test de
fumee des modules `server/lib/*.sh` avec de faux `docker`/`ip`/`iptables`,
sans Docker reel), `tests/test_network.sh` (pont `expo-lan` avec un faux
`nmcli`) et `tests/test_machine_setup.sh` (script d'image
`image/machine-setup.sh` sur un faux systeme de fichiers). Tout se lance aussi
en local : `pytest tests && bash tests/test_deploy_libs.sh && bash tests/test_machine_setup.sh`.

## Structure

```
install.sh                    # point d'entree unique, enchaine tout ce qui suit
network/
  setup.sh                   # pont expo-lan (10.42.0.1/24, sans NAT) sur la carte reseau de l'expo
  lan-internet.sh            # NAT des machines : on / off / status (normalement off)
  teardown.sh                # defait setup.sh
image/
  machine-setup.sh            # preparation de l'image type (cle d'automatisation, sudo, proxy apt, NTP)
  prepare-master.sh           # remet une machine a neuf comme master (graine, identite, menage, compte temporaire) ; --remove-accounts : sur la copie, a la capture
  README-master.md            # preparer l'image master Ubuntu (cloud-init) - contrat avec sd-flasher
server/
  deploy-server.sh             # orchestration : installe Docker, demarre Dockhand, cree/redeploie les stacks via son API
  lib/                           # une fonction par responsabilite, sources par deploy-server.sh :
                                 #   preflight, tls, secrets (+ basic auth), credentials, automation-key, ansible-web, apt-cache, sd-flasher, ntp, dashboard, dnsmasq, stacks, summary
  apt-cache-mode.sh             # bascule le cache apt en ligne / hors ligne (ou le redemarre) en ligne de commande
  docker-cleanup.sh             # libere la place de Docker (cache de construction au-dela de 2 Go, images sans nom) ; appele en fin d'installation
  teardown-server.sh            # arrete Dockhand + les stacks (docker compose direct), desinstalle Docker
  docker-compose.yml            # bootstrap UNIQUEMENT : Dockhand (ne peut pas se creer via sa propre API)
  dockhand-api.sh                # helpers partages : attente sante, upsert d'une stack (appelle dockhand_stack.py)
  dockhand_stack.py              # implementation unique de "supprimer + recreer une stack Dockhand" (bash ET caddy-admin)
  dockhand_setup.py              # configuration initiale de Dockhand par son API : environnement local, utilisateur admin, authentification, jeton
  set-credentials.sh             # change le couple identifiant / mot de passe d'administration partout (Dockhand, portail, Bastion)
  admin_credentials.py           # meme logique en Python, utilisee par caddy-admin (formulaire "Identifiants")
  services.yaml                  # services applicatifs exposes au reverse-proxy (dashboard, dockhand, bastion, dnsmasq/caddy/vpn-admin, git-mirror)
  render-caddyfile.py             # genere le Caddyfile a partir de server/services.yaml
  stacks/
    dnsmasq/{docker-compose.yml, Dockerfile, dnsmasq.conf, data/, admin-config/}   # data/ et admin-config/ non versionnes (baux, reservations, enregistrements DNS)
    dnsmasq-admin/docker-compose.yml                                  # build context = ../../dnsmasq-admin
    caddy/{docker-compose.yml, Caddyfile}                            # Caddyfile genere, non versionne
    caddy-admin/docker-compose.yml                                    # build context = ../../caddy-admin
    bastion/{docker-compose.yml, bastion.env, config/, maps/}        # ces 3 derniers non versionnes
    dashboard/{docker-compose.yml, dashboard.env, portal.env, services.yaml}   # le portail (code : portal/) ; les 3 fichiers .env/.yaml sont generes et non versionnes
    vpn-admin/docker-compose.yml                                      # build context = ../../vpn-admin
    git-mirror/{docker-compose.yml, data/}                            # data/ non versionne (clones bare + mirrors.yaml)
    apt-cache/{docker-compose.yml, Dockerfile, entrypoint.sh, apt-cache.env, config/, logs/, data/}  # apt-cacher-ng ; apt-cache.env (secret admin), config/ (mode, miroirs HTTPS), logs/ et data/ non versionnes
    apt-cache-admin/docker-compose.yml                                # build context = ../../apt-cache-admin
    ntp/{docker-compose.yml, Dockerfile, entrypoint.sh, config/, data/, run/}  # serveur de temps (chrony) ; config/ (reglages), data/ (derive), run/ (socket) non versionnes
    ntp-admin/docker-compose.yml                                       # build context = ../../ntp-admin
    sd-flasher/{docker-compose.yml, images/, data/, keys/}            # conteneur privilegie ; images/ (master), data/ (reglages, audit), keys/ non versionnes
    ansible-web/{docker-compose.yml, ansible-web.env, automation_key/, config.yaml, repo/, runs/}  # tout sauf docker-compose.yml non versionne ; execute bastion-ansible (depot separe, clone depuis git-mirror)
dnsmasq-admin/                  # app Flask : app.py, Dockerfile, templates/, static/
apt-cache-admin/                 # idem - interface du cache apt : etat, efficacite, journal, mode, miroirs HTTPS
portal/                         # le portail (stack dashboard) : connexion unique, tiroir et onglets, verification de session pour Caddy
caddy-admin/                     # idem - edite server/services.yaml, recharge Caddy via son admin API
vpn-admin/                       # idem - appelle vpn/add-peer.sh et vpn/remove-peer.sh
git-mirror/                      # idem, mais fait lui-meme le travail git (clone/fetch), rien a piloter
ansible-web/                     # idem, mais fait lui-meme le travail ansible-playbook (clone/pull depuis git-mirror, execute)
ntp-admin/                       # idem - interface du serveur de temps : etat, reglages, sources, machines
sd-flasher/                      # flasheur de cartes SD : flasher.py (logique, garde-fous), capture.py (capture reduite), app.py (API), templates/, static/
vpn/
  install.sh                  # genere wg0.conf, cree/redeploie les stacks Dockhand "wireguard" et "vpn-admin"
  uninstall.sh                 # arrete la stack (docker compose direct), supprime la config generee
  add-peer.sh                  # ajoute un pair VPN (docker exec wireguard wg ...) + config client
  remove-peer.sh                # retire un pair VPN
  wireguard/{docker-compose.yml, Dockerfile, entrypoint.sh, config/}   # config/ non versionne (cles, pairs)
rollback.sh                   # orchestre les 3 teardown dans le bon ordre
```

## A venir

- Deploiement effectif de Gitea (conteneur Docker sur l'hote, gere depuis
  Dockhand), puis entree correspondante dans `server/services.yaml`
- Authentification Dockhand (desactivee par defaut, protegee par
  l'isolation reseau pour l'instant) - l'activer cree un premier
  compte admin dans son UI ; deposer ensuite un token dans
  `server/dockhand.env` (non versionne) pour que `deploy-server.sh` et
  `vpn/install.sh` continuent de fonctionner (voir `server/dockhand-api.sh`)
- Renouvellement/rotation des certs Caddy au-dela du lab (hors scope d'un
  environnement isole)
- Redirection de port sur le routeur du reseau reel pour un acces VPN
  depuis l'exterieur (specifique a la box de l'utilisateur, hors perimetre
  scriptable)
