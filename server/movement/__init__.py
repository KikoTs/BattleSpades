"""server.movement — authoritative movement / reconciliation placeholder.

Today the server trusts client-reported positions almost completely (the
existing Player.update applies soft-correction but the world simulation
isn't truly authoritative). This is GOAL.md Phase 2.

The intended shape:

    server/movement/
        engine.py
            run_movement_tick(player, dt, input_flags, orientation)
            -> (new_xyz, velocity)
            Wraps aoslib.world.Player.move() and applies per-class
            multipliers (sprint/jump/crouch_sneak/water_friction) sourced
            from server.class_data.MOVEMENT.

        reconcile.py
            apply_position_data(player, packet)
            -> validates against server-simulated state; soft-corrects
               within drift tolerance, hard-snaps if exceeded.

        prediction.py
            world_update_snapshot(player)
            -> packs the wire format the client uses for prediction
               (position + velocity + orientation + input_flags).

The key invariant: client prediction and server simulation must produce
the same trajectory for the same input + orientation. They're tied
together by:
    - InitialInfo.movement_speed_multipliers (we now drive this from
      server.class_data — see builders/initial_info.py)
    - StateData.gravity / time_scale
    - per-class multipliers in shared.constants

If those agree, walking looks smooth. If they disagree, the client
visibly snaps every WorldUpdate. (See `spawn_walk` test scenario for
the integration check.)
"""
