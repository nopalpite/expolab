"""Salles de Bastion pour la console audio (audio-admin/bastion.py) : lecture seule, rapprochement par nom court,
aucune salle par defaut, fichier illisible = aucune machine affichee."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "audio-admin"))
import bastion as B  # noqa: E402

YAML = """rooms:
- {id: salle-a, name: Grande salle, map_image: a.png}
- {id: salle-b, name: Atelier}
machines:
- {id: 1, name: rpi-test.expolab.lan, host: 10.42.0.169, room: salle-a, credentials: {password: SECRET}}
- {id: 2, name: Projo, host: 10.42.0.151, room: salle-b}
- {id: 3, name: borne, host: 10.42.0.200}
- {id: 4, name: orpheline, room: salle-inconnue}
"""


def test_short_names_match_the_hostname_form():
    assert B.short_name("rpi-test.expolab.lan") == "rpi-test" and B.short_name("Projo") == "projo" and B.short_name(None) == ""


def test_parse_keeps_only_machines_placed_in_a_known_room():
    rmap = B.parse(__import__("yaml").safe_load(YAML))
    assert rmap.rooms == {"salle-a": "Grande salle", "salle-b": "Atelier"}
    assert rmap.room_of("rpi-test") == "salle-a" and rmap.room_of("RPI-TEST.expolab.lan") == "salle-a"
    assert rmap.room_of("projo") == "salle-b"
    assert rmap.room_of("borne") is None                 # creee dans Bastion sans salle : absente de la console
    assert rmap.room_of("orpheline") is None             # salle inconnue : idem
    assert rmap.room_of("inconnue") is None
    assert "SECRET" not in repr(vars(rmap))              # aucun identifiant de Bastion n'est repris


def test_parse_tolerates_garbage():
    for data in (None, [], "texte", {"rooms": "x", "machines": [1, None, {"name": "a"}]}):
        rmap = B.parse(data)
        assert rmap.rooms == {} and rmap.room_of("a") is None


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def test_the_file_is_read_again_only_when_it_changes(tmp_path):
    path, clock = tmp_path / "machines.yaml", Clock()
    path.write_text(YAML)
    src = B.BastionRooms(path, clock=clock)
    assert src().room_of("rpi-test") == "salle-a"
    path.write_text(YAML.replace("room: salle-a, credentials", "room: salle-b, credentials"))
    assert src().room_of("rpi-test") == "salle-a"        # relu au plus toutes les 2 s
    clock.t += 3
    import os
    os.utime(path, (clock.t + 1000, clock.t + 1000))
    assert src().room_of("rpi-test") == "salle-b"


def test_an_unreadable_file_hides_every_machine(tmp_path):
    clock = Clock()
    src = B.BastionRooms(tmp_path / "absent.yaml", clock=clock)
    assert src().rooms == {} and src().room_of("a") is None
    bad = tmp_path / "bad.yaml"
    bad.write_text("rooms: [unclosed")
    assert B.BastionRooms(bad, clock=clock)().rooms == {}
