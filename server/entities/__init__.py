"""server.entities — placed/world entity placeholder package.

The original protocol carries server-tracked entities through:
    CreateEntity(21), DestroyEntity(19), HitEntity(20), ChangeEntity(16),
    DisableEntity(96), and the per-type Place* packets:
        PlaceMG(87), PlaceRocketTurret(88), PlaceLandmine(89),
        PlaceMedPack(90), PlaceRadarStation(91), PlaceC4(92),
        DetonateC4(93), PlaceFlareBlock(104), PlaceUGC(97),
        PlaceDynamite(1).

GOAL.md Phase 3 (combat completeness) is when these get filled in. Each
entity type will get its own module here, e.g.:

    server/entities/
        base.py            # base Entity class with id, position, owner, hp
        registry.py        # entity-id allocator, server-wide lookup
        mg.py              # MG turret (server-driven AI fire)
        rocket_turret.py   # rocket turret + RPG-derived shots
        landmine.py        # invisible/visible-to-team trigger entity
        medpack.py         # heal-on-proximity, charges
        radar_station.py   # team-only minimap reveal
        c4.py              # placed + detonated charges
        dynamite.py        # timer + AOE
        flare_block.py     # area illumination
        grenade.py         # already partly implemented in aoslib.world.Grenade

All would feed into WorldUpdate.updated_entities and the dedicated lifecycle
packets above.
"""
