from types import SimpleNamespace

import pytest

from server.explosions import explosion_falloff, explosion_impulse
from server.main import BattleSpadesServer


def test_squared_falloff_and_impulse_match_client_oracle():
    factor = explosion_falloff((0, 0, 0), (2, 0, 0), 4.0)
    assert factor == pytest.approx(0.71484375)
    impulse = explosion_impulse((0, 0, 0), (2, 0, 0), 4.0, 0.5, 1.0)
    assert impulse == pytest.approx((0.857421875, 0.0, 0.0))


def test_crouching_uses_lower_body_center_and_radius_is_strict():
    standing = explosion_falloff((0, 0, 0), (2, 0, 0), 4.0)
    crouched = explosion_falloff(
        (0, 0, 0), (2, 0, 0), 4.0, crouched=True
    )
    assert crouched < standing
    assert explosion_impulse((0, 0, 0), (4, 0, 0), 4.0, 0.5, 1.0) is None


class BlastPlayer:
    def __init__(self, player_id, team, x=2.0, y=0.0, z=0.0):
        self.id = player_id
        self.team = team
        self.x, self.y, self.z = x, y, z
        self.alive = True
        self.spawned = True
        self.input = SimpleNamespace(crouch=False)
        self._velocity = (0.0, 0.0, 0.0)
        self.damage_calls = []

    @property
    def position(self):
        return (self.x, self.y, self.z)

    @property
    def velocity(self):
        return self._velocity

    @velocity.setter
    def velocity(self, value):
        self._velocity = tuple(value)

    def damage(self, amount, source=None, kill_type=0):
        self.damage_calls.append((amount, source, kill_type))


class EmptyRegistry:
    def all(self):
        return []


class BlastServer:
    _apply_blast = BattleSpadesServer._apply_blast

    def __init__(self, players, *, blocked=False, friendly_fire=False):
        self.players = {player.id: player for player in players}
        self.config = SimpleNamespace(
            build_damage=False,
            friendly_fire=friendly_fire,
        )
        self.entity_registry = EmptyRegistry()
        self.blocked = blocked

    def _blocked_los(self, *args):
        return self.blocked

    def _build_entity_ctx(self):
        return None


def test_wall_blocks_damage_and_knockback_together():
    thrower = BlastPlayer(1, 0, x=0.0)
    target = BlastPlayer(2, 1)
    server = BlastServer([thrower, target], blocked=True)
    server._apply_blast(
        0, 0, 0, 100, 0, 3, thrower,
        blast_radius=4.0, knockback_min=0.5, knockback_max=1.0,
    )
    assert target.velocity == (0.0, 0.0, 0.0)
    assert target.damage_calls == []


def test_friendly_fire_policy_keeps_physics_but_suppresses_hp_damage():
    thrower = BlastPlayer(1, 0, x=0.0)
    teammate = BlastPlayer(2, 0)
    server = BlastServer([thrower, teammate], friendly_fire=False)
    server._apply_blast(
        0, 0, 0, 100, 0, 3, thrower,
        blast_radius=4.0, knockback_min=0.5, knockback_max=1.0,
    )
    assert teammate.velocity[0] == pytest.approx(0.857421875)
    assert teammate.damage_calls == []


def test_rocket2_self_knockback_override_is_applied():
    thrower = BlastPlayer(1, 0, x=2.0)
    server = BlastServer([thrower])
    server._apply_blast(
        0, 0, 0, 50, 0, 5, thrower,
        blast_radius=6.0, knockback_min=0.0, knockback_max=0.25,
        self_knockback_min=1.0, self_knockback_max=1.5,
    )
    factor = explosion_falloff((0, 0, 0), thrower.position, 6.0)
    assert thrower.velocity[0] == pytest.approx(1.0 + factor * 0.5)
    assert len(thrower.damage_calls) == 1
