"""Petit client de l'API WebSocket de python-matter-server (stack matter). Lance DANS le
conteneur (aiohttp y est deja) par server/matter.sh :

    python3 /expolab/matter_cmd.py <commande> [arguments JSON | -]

`-` : arguments JSON lus sur l'entree standard (pour ne jamais mettre le mot de passe Wi-Fi
sur une ligne de commande). Commande `resume` : vue lisible des prises (etat, puissance,
energie) au lieu du JSON brut de get_nodes.
"""
import asyncio
import json
import sys

URL = "http://127.0.0.1:5580/ws"
TIMEOUT = 600   # un approvisionnement par Bluetooth peut prendre plusieurs minutes

# Attributs Matter utiles (cle "endpoint/cluster/attribut" de python-matter-server).
BASIC = {"vendor": "0/40/1", "product": "0/40/3", "version": "0/40/10", "label": "0/40/5"}


def find(attributes: dict, cluster: int, attribute: int):
    """Valeur de cluster/attribut sur le premier endpoint qui l'expose."""
    for key, value in attributes.items():
        parts = key.split("/")
        if len(parts) == 3 and parts[1] == str(cluster) and parts[2] == str(attribute):
            return int(parts[0]), value
    return None, None


def summary(nodes: list) -> list:
    out = []
    for node in nodes:
        attrs = node.get("attributes") or {}
        ep, on = find(attrs, 6, 0)                          # OnOff
        _ep, power_mw = find(attrs, 0x0090, 8)              # Electrical Power Measurement : ActivePower (mW)
        _ep, energy = find(attrs, 0x0091, 1)                # Electrical Energy Measurement : CumulativeEnergyImported
        _ep, legacy_w = find(attrs, 0x0B04, 0x050B)         # ancien Electrical Measurement : ActivePower (W)
        if isinstance(energy, dict):
            energy = energy.get("energy", energy.get("0"))
        out.append({
            "node_id": node.get("node_id"), "disponible": node.get("available"),
            **{k: attrs.get(v) for k, v in BASIC.items()},
            "endpoint_onoff": ep, "allumee": on,
            "puissance_W": round(power_mw / 1000, 1) if isinstance(power_mw, (int, float)) else legacy_w,
            "energie_Wh": round(energy / 1000, 1) if isinstance(energy, (int, float)) else None,
        })
    return out


async def main() -> int:
    import aiohttp   # present dans le conteneur ; import ici pour tester summary() sans lui

    command = sys.argv[1] if len(sys.argv) > 1 else "info"
    raw = sys.argv[2] if len(sys.argv) > 2 else "{}"
    args = json.loads(sys.stdin.read() if raw == "-" else raw)
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(URL) as ws:
            info = await ws.receive_json()
            if command == "info":
                print(json.dumps(info, indent=2, ensure_ascii=False))
                return 0
            real = "get_nodes" if command == "resume" else command
            await ws.send_json({"message_id": "1", "command": real, "args": args})
            while True:
                msg = await asyncio.wait_for(ws.receive(), TIMEOUT)
                if msg.type != aiohttp.WSMsgType.TEXT:
                    print("Connexion fermee par matter-server.", file=sys.stderr)
                    return 1
                data = json.loads(msg.data)
                if data.get("message_id") != "1":
                    continue   # evenements diffuses (noeud mis a jour...) : ignores ici
                if "error_code" in data:
                    print(f"Erreur {data.get('error_code')} : {data.get('details')}", file=sys.stderr)
                    return 1
                result = data.get("result")
                if command == "resume":
                    result = summary(result or [])
                print(json.dumps(result, indent=2, ensure_ascii=False))
                return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
