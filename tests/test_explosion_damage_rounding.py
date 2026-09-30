"""Explosion fractions survive until the final authoritative HP conversion."""

import math

import pytest

import shared.constants as C
from tests.test_weapons_retail import _BlastServer, _duel


@pytest.mark.parametrize(
    "raw_damage,multiplier,expected_loss",
    [(1.1, 0.5, 1), (2.8, 0.5, 1), (0.49, 3.0, 1)],
)
def test_blast_rounds_after_mode_damage_modifier(raw_damage, multiplier, expected_loss):
    server, attacker, target, _combat = _duel(C.RIFLE_TOOL)
    target.end_spawn_protection()
    modified = []

    class Mode:
        def modify_incoming_damage(self, victim, amount, source, kill_type):
            assert victim is target
            modified.append(amount)
            return amount * multiplier

    server.mode = Mode()
    # The stock grenade curve is D * (1 - d^2 / R^2). Put its centre at
    # the victim's body height so this distance produces the desired fraction.
    distance = math.sqrt(16.0 * (1.0 - raw_damage / 230.0))
    blast = _BlastServer([target])
    blast._apply_blast(
        target.x - distance, target.y, target.z + 0.75,
        230.0, 0.0, int(C.KILL.GRENADE_KILL), attacker,
    )

    assert modified == pytest.approx([raw_damage])
    assert target.health == 100 - expected_loss


@pytest.mark.parametrize("damage", [0.0, -1.0])
def test_nonpositive_blast_does_not_enter_hp_damage_policy(damage):
    server, attacker, target, _combat = _duel(C.RIFLE_TOOL)
    target.end_spawn_protection()
    modified = []

    class Mode:
        def modify_incoming_damage(self, victim, amount, source, kill_type):
            modified.append(amount)
            return amount

    server.mode = Mode()
    blast = _BlastServer([target])
    blast._apply_blast(
        target.x - 1.0, target.y, target.z + 0.75,
        damage, 0.0, int(C.KILL.GRENADE_KILL), attacker,
        blast_radius=4.0,
    )

    assert modified == []
    assert target.health == 100
