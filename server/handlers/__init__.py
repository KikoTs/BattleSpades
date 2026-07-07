"""server.handlers — packet-handler placeholder package.

Today the active handlers all live in `protocol/packet_handler.py` (one big
file with @register_handler decorators). As we add support for the rest of
the ~127 protocol packets, we'll factor groups out into modules here:

    server/handlers/
        movement.py    # ClientData(4), PositionData(116)
        combat.py      # ShootPacket(6), Damage(37), KillAction(46)
        block.py       # BlockBuild(32), BlockBuildColored(33), BlockOccupy(34),
                       # BlockLiberate(35), BlockLine(40), PaintBlock(7)
        chat.py        # ChatMessage(49), LocalisedMessage(50), VoiceData(103)
        world.py       # UseOrientedItem(10) (grenades), Disguise(95)
        team.py        # ChangeTeam(77), ChangeClass(78), ForceTeamJoin(115)
        place.py       # PlaceMG(87), PlaceRocketTurret(88), PlaceLandmine(89),
                       # PlaceMedPack(90), PlaceRadarStation(91), PlaceC4(92),
                       # DetonateC4(93), PlaceFlareBlock(104), PlaceUGC(97)
        admin.py       # GenericVoteMessage(47), InstantiateKickMessage(48)
        ugc.py         # SetUGCEditMode(12), RequestUGCEntities(99),
                       # UGCMessage(100), InitialUGCBatch(98)
        auth.py        # SteamSessionTicket(105), Password(111),
                       # PasswordProvided(113)

Each module would expose its own @register_handler decorators. The current
all-in-one `protocol/packet_handler.py` is fine for ~14 handlers; once we
get past ~30 it becomes worth splitting.

This package exists now so the structure is visible; modules will be
filled out as part of GOAL.md Phase 1.
"""
