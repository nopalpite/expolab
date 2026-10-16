"""Client de l'API WebSocket de python-matter-server (stack matter, 127.0.0.1:5580). Une
connexion par appel : simple, et aucun etat a resynchroniser si le controleur redemarre."""
import asyncio
import os

URL = os.environ.get("PRISES_MATTER_URL", "http://127.0.0.1:5580/ws")


class MatterError(Exception):
    pass


async def _call(command: str | None, args: dict | None, timeout: float):
    import aiohttp   # present dans l'image ; importe ici pour tester le reste sans lui

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(URL) as ws:
            info = await asyncio.wait_for(ws.receive_json(), 10)
            if command is None:
                return info
            await ws.send_json({"message_id": "1", "command": command, "args": args or {}})
            while True:
                msg = await asyncio.wait_for(ws.receive(), timeout)
                if msg.type != aiohttp.WSMsgType.TEXT:
                    raise MatterError("Connexion fermee par le controleur Matter.")
                data = msg.json()
                if data.get("message_id") != "1":
                    continue   # evenements diffuses : ignores
                if "error_code" in data:
                    raise MatterError(str(data.get("details") or f"erreur {data['error_code']}"))
                return data.get("result")


def call(command: str | None, args: dict | None = None, timeout: float = 30):
    """Envoie une commande ; `command=None` : informations du serveur. Leve MatterError."""
    import aiohttp

    try:
        return asyncio.run(_call(command, args, timeout))
    except MatterError:
        raise
    except asyncio.TimeoutError as exc:
        raise MatterError("Le controleur Matter ne repond pas (delai depasse).") from exc
    except (aiohttp.ClientError, OSError) as exc:
        raise MatterError(f"Controleur Matter injoignable ({exc.__class__.__name__}).") from exc
