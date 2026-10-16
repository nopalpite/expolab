"""Tests du client Matter (server/matter_cmd.py) : lecture des attributs d'une prise
(marche/arret, puissance, energie) dans le format de python-matter-server. Sans le
controleur : seule la mise en forme est testee ; le dialogue reel se valide sur le Pi."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import matter_cmd  # noqa: E402


def node(node_id=5, **attrs):
    base = {"0/40/1": "Fabricant", "0/40/3": "Prise X", "0/40/10": "1.2.0", "0/40/5": ""}
    return {"node_id": node_id, "available": True, "attributes": {**base, **attrs}}


def test_summary_reads_onoff_power_and_energy_matter_1_3():
    (plug,) = matter_cmd.summary([node(**{"1/6/0": True, "2/144/8": 96500, "2/145/1": {"energy": 840000}})])
    assert plug == {"node_id": 5, "disponible": True, "vendor": "Fabricant", "product": "Prise X", "version": "1.2.0",
                    "label": "", "endpoint_onoff": 1, "allumee": True, "puissance_W": 96.5, "energie_Wh": 840.0}


def test_summary_falls_back_to_the_old_electrical_measurement_cluster():
    (plug,) = matter_cmd.summary([node(**{"1/6/0": False, "1/2820/1291": 12})])
    assert plug["allumee"] is False and plug["puissance_W"] == 12 and plug["energie_Wh"] is None


def test_summary_without_measurement():
    (plug,) = matter_cmd.summary([node(**{"1/6/0": True})])
    assert plug["puissance_W"] is None and plug["energie_Wh"] is None
    assert matter_cmd.summary([]) == []
