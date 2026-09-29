"""Weapon-catalog data-parity test.

Asserts the Phase-1 weapon data layer (server/game_constants.WEAPON_CATALOG)
matches the ground-truth values extracted from the original client in
docs/CONTENT_TABLES.md. If someone edits a number, this test catches drift
from the client's real stats.
"""
import shared.constants as C
from server import game_constants as G


def test_catalog_covers_all_selectable_tools():
    # ids 0..NUMBER_OF_WEAPONS-2 are selectable; the last id is the
    # NOOF_SELECTABLE_TOOLS sentinel and is intentionally excluded.
    selectable = range(int(C.NUMBER_OF_WEAPONS) - 1)
    missing = [i for i in selectable if i not in G.WEAPON_CATALOG]
    assert missing == [], f"catalog missing tool ids: {missing}"
    assert len(G.WEAPON_CATALOG) == int(C.NUMBER_OF_WEAPONS) - 1


def test_damage_and_kill_enums():
    assert len(G.DAMAGE_TYPES) == 44
    assert G.DAMAGE_TYPES["WEAPON_DAMAGE"] == 6
    assert G.DAMAGE_TYPES["GRENADE_DAMAGE"] == 7
    assert len(G.KILL_TYPES) == 37
    assert G.KILL_TYPES["WEAPON_KILL"] == 0
    assert G.KILL_TYPES["HEADSHOT_KILL"] == 1


# (tool_id, torso, head, fire_interval, clip, max reserve, reload, pellets,
# block_dmg) -- the stock Steam classes (docs/WEAPONS_RETAIL.md).
_GUN_EXPECT = [
    (6,  70, 150, 0.5,   10, 50,  2.5,  1, 2),
    (7,  10, 15,  0.1,   25, 100, 1.25, 1, 1),
    (8,  15, 30,  0.3,   100, 300, 2.0, 1, 2.5),
    (9,  20, 30,  1.0,   5, 20,  0.5, 10, 1),
    (10, 40, 50,  1.0,   2, 14,  1.0, 10, 2.5),
    (15, 30, 20,  0.5,   100, 400, 4.0, 1, 2),
    # The STOCK (Steam) pistolWeapon reads A1124..A1133: head 45, 0.4 s,
    # 0.6 s reload, range 550. Only the rebuilt nonsteam .pyc reads the
    # modded named constants (head 50, 0.3 s, 0.5 s, 800).
    (17, 20, 45,  0.4,   6, 30,  0.6,  1, 3),
    (18, 50, 175, 1.0,   1, 7,   2.0,  1, 5),
    (19, 34, 85,  1.1,   5, 15,  3.0,  1, 3),
    (35, 30, 35,  0.12,  30, 120, 2.0, 1, 1),
    (36, 40, 70,  0.5,   6, 30, 0.75, 1, 1),
    (37, 20, 30,  1.0,   5, 45, 0.5, 12, 1),
    (38, 20, 20,  0.1,   25, 100, 1.25, 1, 2),
    (60, 20, 40,  0.5,   15, 60,  0.9, 1, 2.5),
    (61, 20, 37,  0.15,  50, 250, 2.0, 1, 2.5),
    (62, 20, 25,  0.35,  8, 40,  2.5, 10, 2),
]


def test_gun_stats_match_client():
    for tid, torso, head, fire, clip, reserve, reload, pellets, block in _GUN_EXPECT:
        p = G.WEAPON_CATALOG[tid]
        assert p.base_damage == torso, f"tool {tid} torso"
        assert p.head_damage == head, f"tool {tid} head"
        assert p.fire_interval == fire, f"tool {tid} fire_interval"
        assert p.clip_size == clip, f"tool {tid} clip"
        assert p.reserve_ammo == reserve, f"tool {tid} reserve"
        assert p.reload_time == reload, f"tool {tid} reload"
        assert p.pellet_count == pellets, f"tool {tid} pellets"
        assert p.block_damage == block, f"tool {tid} block_dmg"
        # guns route through the hit-scan profile table
        assert tid in G.WEAPON_PROFILES


def test_pistol_matches_the_stock_steam_class_not_the_nonsteam_rebuild():
    pistol = G.WEAPON_CATALOG[int(C.PISTOL_TOOL)]

    assert pistol.base_damage == 20
    assert pistol.head_damage == 45
    assert pistol.fire_interval == 0.4
    assert pistol.reload_time == 0.6
    assert pistol.max_range == 550
    assert (pistol.clip_size, pistol.reserve_ammo) == (6, 30)


def test_melee_player_and_block_damage():
    # (tool_id, player_dmg, block_dmg): stock <TOOL>_HITPLAYER_DAMAGE_AMOUNT
    # (pickaxe A1105 = 40) and the stock DiggingTool ``damage``.
    for tid, player, block in [(0, 40, 7), (2, 35, 5), (3, 50, 7.5), (24, 70, 2), (34, 80, 5)]:
        p = G.WEAPON_CATALOG[tid]
        assert p.is_melee
        assert p.base_damage == player, f"melee {tid} player dmg"
        assert p.block_damage == block, f"melee {tid} block dmg"
        assert tid not in G.WEAPON_PROFILES  # melee not routed as hit-scan


def test_projectile_blast_and_radius():
    # (tool_id, blast, radius, fuse): radius = the player-damage radius the
    # stock ExplosionDamageManager wrapper passes (classic grenade 9, APG 6,
    # dynamite 8), not the block-crater EXPLOSION_RADIUS constants.
    for tid, blast, radius, fuse in [(11, 230, 4, 2.5), (31, 130, 9, 3.0),
                                     (32, 500, 6, 2.5), (21, 300, 8, 7.0), (25, 500, 7, 10.0)]:
        p = G.WEAPON_CATALOG[tid]
        assert p.is_projectile
        assert p.base_damage == blast, f"proj {tid} blast"
        assert p.blast_radius == radius, f"proj {tid} radius"
        assert p.fuse_time == fuse, f"proj {tid} fuse"


def test_named_profiles_resolve():
    assert G.RIFLE_PROFILE.tool_id == int(C.RIFLE_TOOL)
    assert G.SMG_PROFILE.tool_id == int(C.SMG_TOOL)
    assert G.SHOTGUN_PROFILE.tool_id == int(C.SHOTGUN_TOOL)
    assert G.SPADE_PROFILE.tool_id == int(C.SPADE_TOOL)
    # spade stays the generic melee routing profile
    assert G.SPADE_PROFILE.kill_type == G.KILL_MELEE
