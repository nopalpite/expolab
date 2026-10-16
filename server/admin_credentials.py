"""Changement du couple identifiant / mot de passe d'administration (un seul
couple pour toutes les interfaces), depuis l'interface de caddy-admin.

Meme logique que server/set-credentials.sh (voir son en-tete), en Python pour
tourner dans caddy-admin : Dockhand d'abord (s'il refuse, rien d'autre n'a
change), puis auth.env (portail), puis bastion.env et la recreation de sa stack.

Les appels reseau (Dockhand, recreation de la stack) sont injectes : le module
ne depend ni de Dockhand ni de Docker et se teste sans eux.
"""
import hmac
import re
from pathlib import Path
from typing import Callable

USER_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
PASSWORD_MIN = 8
SAFE_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]*$")
BCRYPT_ROUNDS = 12


class CredentialsError(Exception):
    """Echec d'une etape. `done` : les etapes deja appliquees (vide = rien n'a change)."""

    def __init__(self, message: str, done: list[str] | None = None):
        super().__init__(message)
        self.done = done or []


def check_user(user: str) -> str | None:
    if USER_RE.match(user or ""):
        return None
    return "Identifiant invalide : 3 a 32 caracteres parmi lettres, chiffres, '.', '_' et '-'."


def check_password(password: str) -> str | None:
    if len(password or "") < PASSWORD_MIN:
        return f"Mot de passe trop court ({PASSWORD_MIN} caracteres minimum)."
    if "'" in password or "\n" in password or "\r" in password:
        return "Mot de passe invalide : ni apostrophe (') ni retour a la ligne."
    return None


def env_value(path: Path, key: str) -> str:
    """Valeur apres le premier "=" (pas de `source` : les hash bcrypt contiennent des $)."""
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.partition("=")[2]
    return ""


def verify_current(server_dir: Path, password: str) -> bool:
    expected = env_value(server_dir / "stacks" / "caddy" / "auth.env", "BASIC_AUTH_PASSWORD")
    return bool(expected) and hmac.compare_digest(password.encode(), expected.encode())


def hash_password(password: str, rounds: int = BCRYPT_ROUNDS) -> str:
    import bcrypt  # paquet python3-bcrypt de l'image caddy-admin

    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=rounds)).decode()


def env_quote(value: str) -> str:
    """Valeur telle quelle si elle est "sure", entre apostrophes sinon (Docker
    Compose interpole $ et traite # dans un env_file non quote)."""
    return value if SAFE_ENV_VALUE_RE.match(value) else f"'{value}'"


def write_auth_env(path: Path, user: str, password: str, pw_hash: str) -> None:
    # Ecriture SUR PLACE (meme fichier, pas de renommage) : le portail monte
    # auth.env comme un fichier, un remplacement le laisserait perime.
    path.write_text(
        f"BASIC_AUTH_USER={user}\nBASIC_AUTH_PASSWORD={password}\nBASIC_AUTH_HASH={pw_hash}\n", encoding="utf-8"
    )


def update_bastion_env(path: Path, user: str, password: str) -> bool:
    """Remplace le couple dans bastion.env (le reste est intact). False si Bastion n'est pas installe."""
    if not path.exists():
        return False
    kept = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith(("BASTION_ADMIN_USER=", "BASTION_ADMIN_PASSWORD="))
    ]
    kept += [f"BASTION_ADMIN_USER={env_quote(user)}", f"BASTION_ADMIN_PASSWORD={env_quote(password)}"]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return True


def apply_credentials(
    server_dir: Path,
    new_user: str,
    new_password: str,
    *,
    dockhand_update: Callable[[str, str, str], None],
    redeploy_bastion: Callable[[], None],
    rounds: int = BCRYPT_ROUNDS,
) -> list[str]:
    """Applique le nouveau couple partout ; retourne les etapes faites. Leve
    CredentialsError (avec les etapes deja faites) au premier echec."""
    auth_env = server_dir / "stacks" / "caddy" / "auth.env"
    bastion_env = server_dir / "stacks" / "bastion" / "bastion.env"
    old_user = env_value(auth_env, "BASIC_AUTH_USER")
    done: list[str] = []

    try:
        dockhand_update(old_user, new_user, new_password)
    except Exception as exc:  # noqa: BLE001 - message remonte tel quel a l'utilisateur
        raise CredentialsError(f"Dockhand : {exc} (rien n'a ete modifie)", done) from exc
    done.append("Dockhand")

    try:
        write_auth_env(auth_env, new_user, new_password, hash_password(new_password, rounds))
    except Exception as exc:  # noqa: BLE001
        raise CredentialsError(f"Portail et interfaces d'administration : {exc}", done) from exc
    done.append("Portail et interfaces d'administration")

    try:
        if update_bastion_env(bastion_env, new_user, new_password):
            redeploy_bastion()
            done.append("Bastion")
    except Exception as exc:  # noqa: BLE001
        raise CredentialsError(f"Bastion : {exc}", done) from exc
    return done


# --- compte exploitant ------------------------------------------------------------------------
# Un second compte, limite par le portail a l'interface d'exploitation (plans des salles, cartes
# SD). Fichier SEPARE d'auth.env (reecrit en entier a chaque changement du couple d'administration)
# et monte dans le portail : stacks/dashboard/operator.env, toujours present (vide = pas de compte),
# ecrit SUR PLACE pour la meme raison qu'auth.env.

def operator_env_path(server_dir: Path) -> Path:
    return server_dir / "stacks" / "dashboard" / "operator.env"


def operator_user(server_dir: Path) -> str:
    return env_value(operator_env_path(server_dir), "OPERATOR_USER")


def set_operator(server_dir: Path, user: str, password: str, rounds: int = BCRYPT_ROUNDS) -> None:
    """Cree ou change le compte exploitant. Leve CredentialsError si invalide."""
    error = check_user(user) or check_password(password)
    if not error and user == env_value(server_dir / "stacks" / "caddy" / "auth.env", "BASIC_AUTH_USER"):
        error = "L'identifiant de l'exploitant doit etre different de celui de l'administrateur."
    if error:
        raise CredentialsError(error)
    path = operator_env_path(server_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:   # sur place (meme inode) : le portail voit le changement
        f.write(f"OPERATOR_USER={user}\nOPERATOR_HASH={hash_password(password, rounds)}\n")


def clear_operator(server_dir: Path) -> None:
    """Supprime le compte exploitant (le fichier reste, vide)."""
    path = operator_env_path(server_dir)
    if path.exists():
        with open(path, "w", encoding="utf-8"):
            pass
