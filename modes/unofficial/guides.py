"""Short, actionable retail chat instructions for every community ruleset."""

GUIDES = {
    "block_hunt": ("HIDERS: select disguise, E samples colour, fire hides you. Uses are unlimited.", "Moving or digging reveals you. Your shovel digs but cannot hurt players.", "HUNTERS: wait for the hiding timer, then find everyone before time runs out."),
    "false_wall": ("1. PREP: stand within 14 blocks of YOUR base and type /stash to hide your cache.", "2. BUILD: surround it with cover and make extra empty rooms as decoys.", "3. RAID: touch the ENEMY cache, then carry it back within 6 blocks of YOUR base.", "First delivery wins. Protect your own cache! /funrules repeats these steps."),
    "crumbling_run": ("Touch the marked checkpoints IN ORDER, then reach the finish.", "Keep moving: tiles crumble behind you. Space jumps; /retry costs 2 seconds.", "The fastest completed run wins. Falling returns you to your last checkpoint."),
    "last_exit": ("Touch a loot marker to pick up valuables. Carry them toward the exit.", "The marked exit opens after the first minute. Hold it uncontested for 4 seconds.", "Only extracted loot scores. Dying drops your loot; extraction keeps you safe."),
    "night_shift": ("SURVIVE six waves. Kill every enemy; the next wave starts after 8 seconds.", "B marks the yellow barricade. Select BLOCKS and fill its exposed top-row gaps for a team ammo refill. Repair is optional.", "Ammo and fallen squadmates return between waves. After wave 6, hold EVAC for 6 seconds."),
    "tunnel_rats": ("Dig to the three marked underground caches; touch one to pick it up.", "Carry it back to YOUR base. First team to deliver TWO caches wins."),
    "bridge_chicken": ("Build a bridge across the chasm. Pick up YOUR team's relay at your base.", "Carry the relay to the FAR island. First delivery wins; falling drops it home."),
    "towerfall": ("PREP: reinforce the four supports under your team's tower.", "GO: destroy enemy supports or their marked core. A lost core loses the round."),
    "floor_tax": ("Aim your SHOVEL at the floor and HOLD FIRE: cuts 2x2 holes ten times per second.", "Throw GRENADES to knock opponents off. Grenades refill every 12 seconds; no health damage.", "Fall through all three floors and you are out. Last survivor wins. After 90 seconds standing tiles crumble."),
    "hot_potato": ("The marked player holds the curse. Spade-hit someone to pass it.", "The fuse NEVER resets on a pass. Each explosion costs its holder one life.", "You have three lives. Keep away from the carrier; last player remaining wins."),
    "lockdown": ("Defeated opponents are jailed. Imprison the entire enemy team to win.", "Free your teammates by holding the rescue marker at the ENEMY base for 3 seconds."),
    "paint_raid": ("Use the block tool's paint action on the marked central floor.", "Painting enemy tiles steals them. Team with the most tiles at the bell wins."),
    "smugglers_run": ("SMUGGLERS: pick up packages at home; hold the far delivery point for 2 seconds.", "GUARDS: intercept the smugglers. Carried packages have no visible intel marker.", "Roles swap each round. Most deliveries across both sides wins the match."),
    "one_in_the_spade": ("Everyone is an enemy, even matching uniforms. You start with ONE lethal shot.", "A kill earns another bullet. If you miss, use your spade. First to 15 kills wins."),
    "borrowed_arsenal": ("Everyone is an enemy, even matching uniforms. TWO kills advance your weapon.", "Complete all five weapons to win. Death keeps your weapon progress."),
    "reverse_siege": ("RAIDERS: take supplies from inside the fort to the marked outside exit.", "Hold delivery for 2 seconds. GUARDS: intercept! Gates close; dig or find another route.", "Roles alternate. Most delivered supplies across both sides wins."),
    "kingmaker": ("Stand within 8 blocks of A to score one point per second for your team.", "Enemies inside contest the hill and stop scoring. First to 90 points wins."),
    "red_light_raid": ("Touch checkpoints in order. Move on GREEN; stop completely on RED.", "Moving on red resets you. Space jumps; /retry returns to your last checkpoint.", "Fastest completed run, including penalties, wins."),
    "scrapyard_engineers": ("Protect your marked generator and destroy the enemy generator.", "Building blocks are limited. Visit the central salvage marker to refill every 8 seconds."),
    "spadeball": ("Touch the ball to carry it into the OPPOSING goal. First to five goals wins.", "Use the native drop-intel key to pass. Fight with spades and build cover."),
}


def instructions(mode, player) -> tuple[str, ...]:
    """Include this player's role and the live phase, not only generic rules."""
    left = max(0, int(mode.deadline - mode.clock()))
    state = f"{mode.spec.title} | Round {mode.round_number}/{mode.rounds} | {mode.phase.upper()} {left}s"
    lines = [state, *GUIDES[mode.mode_code]]
    if mode.spec.asymmetric:
        role = "HIDER" if mode.mode_code == "block_hunt" else "GUARD / DEFENDER"
        if player.team == mode.attackers:
            role = "HUNTER" if mode.mode_code == "block_hunt" else "RAIDER / CARRIER"
        lines.append(f"Your role this round: {role}.")
    if mode.phase == "live" and player not in mode.participants:
        lines.append("You enter next round. The countdown shows the current round's remaining time.")
    else:
        lines.append("/funrules repeats the objective. /funscore shows the standings.")
    return tuple(lines)
