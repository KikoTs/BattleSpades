"""Opt-in community rulesets. Wire IDs remain supported retail TDM IDs."""
from __future__ import annotations

from dataclasses import dataclass, replace
from .arena_designs import DESIGNS


@dataclass(frozen=True, slots=True)
class ModeSpec:
    code: str
    title: str
    description: str
    map_name: str = "FunYard"
    duration: int = 180
    preparation: int = 8
    solo: bool = False
    elimination: bool = False
    asymmetric: bool = False
    building: bool = True
    digging: bool = True


SPECS = (
    ModeSpec("block_hunt", "Block Hunt", "Hiders: E samples colour, fire disguises (unlimited); shovel digs but cannot hurt players. Hunters find everyone.", "SpookyMansion", preparation=30, elimination=True, asymmetric=True, building=False),
    ModeSpec("false_wall", "False Wall", "Hide your cache with /stash, build decoys, then carry the enemy cache back to YOUR base.", duration=300, preparation=180),
    ModeSpec("crumbling_run", "Crumbling Run", "Touch checkpoints in order. Platforms crumble behind you; /retry returns to your checkpoint.", "FunCourse", 240, solo=True, building=False, digging=False),
    ModeSpec("last_exit", "Last Exit", "Collect valuables and hold the announced exit to extract. Only extracted loot scores.", duration=240, elimination=True),
    ModeSpec("night_shift", "Night Shift", "Cooperate: repair the marked wall, deliver supplies, then hold evacuation against bot waves.", duration=360),
    ModeSpec("tunnel_rats", "Tunnel Rats", "Dig through the bunker and bring two underground caches back to your base.", "FunTunnels", 240),
    ModeSpec("bridge_chicken", "Bridge Chicken", "Build across the gap and deliver your relay to the far island. Falling drops your relay.", "FunBridge", 240),
    ModeSpec("towerfall", "Towerfall", "Build around your tower, then destroy enemy supports. A falling core loses the round.", "FunTowers", 180, 40),
    ModeSpec("floor_tax", "Floor Tax", "The floor beneath you disappears periodically. Keep moving; last survivor wins.", "FunFloors", 120, solo=True, elimination=True, building=False, digging=False),
    ModeSpec("hot_potato", "Hot Potato", "Melee-tag another player to pass the cursed intel. The fuse costs a life; three lives each.", duration=150, solo=True, building=False, digging=False),
    ModeSpec("lockdown", "Lockdown", "Defeated players respawn in jail. Hold the enemy jail's rescue marker to free your team.", duration=240),
    ModeSpec("paint_raid", "Paint Raid", "Use your block tool to paint the marked floor. Most team-coloured cells at the bell wins.", duration=150, building=False, digging=False),
    ModeSpec("smugglers_run", "Smuggler's Run", "Smugglers carry hidden packages to delivery points; guards intercept. Roles alternate.", duration=180, asymmetric=True),
    ModeSpec("one_in_the_spade", "One in the Spade", "One lethal shot. A kill earns another; when empty use your spade. First to 15 wins.", solo=True, building=False, digging=False),
    ModeSpec("borrowed_arsenal", "Borrowed Arsenal", "Take your victim's gun on a kill. Respawns rotate weapons. First to 20 kills wins.", solo=True),
    ModeSpec("reverse_siege", "Reverse Siege", "The surrounded team must carry supplies out of its fortress. Defenders intercept; roles alternate.", "FunFort", 180, 20, asymmetric=True),
    ModeSpec("kingmaker", "Kingmaker", "Only your rotating champion scores while holding the marked platforms. Protect the crown.", duration=180),
    ModeSpec("red_light_raid", "Red Light Raid", "Race through checkpoints. Freeze on RED; moving resets you. /retry returns to your checkpoint.", "FunCourse", 240, solo=True, building=False, digging=False),
    ModeSpec("scrapyard_engineers", "Scrapyard Engineers", "Collect central scrap to fund construction. Destroy the enemy generator while protecting yours.", duration=240),
    ModeSpec("spadeball", "Spadeball", "Carry or throw the intel into the opposing goal. Spades only; first to five goals wins.", duration=180, digging=False),
)
REVISIONS = {
    "crumbling_run": dict(duration=360),
    "block_hunt": dict(title="Prop Hunt"),
    "last_exit": dict(title="Extraction"),
    "towerfall": dict(title="Tower Wars"),
    "hot_potato": dict(duration=240),
    "night_shift": dict(title="Survival", duration=900, description="Clear six enemy waves, then reach evacuation. Repair the yellow barricade for a team resupply between waves."),
    "bridge_chicken": dict(title="Bridge Race"),
    "floor_tax": dict(title="Spleef", duration=180, digging=True, description="Hold shovel fire to cut the floor. Grenades knock players back. Fall through three floors and you are out; last survivor wins."),
    "one_in_the_spade": dict(title="One in the Chamber"),
    "borrowed_arsenal": dict(title="Gun Game", building=False, digging=False, description="Earn two kills with each weapon to advance. Finish the weapon ladder to win; death keeps your progress."),
    "kingmaker": dict(title="King of the Hill", description="Hold the marked hill uncontested to score for your team. First to 90 points wins."),
    "red_light_raid": dict(title="Red Light, Green Light", duration=480),
}
ALL_SPECS = tuple(replace(spec, map_name=DESIGNS[spec.code].name, **REVISIONS.get(spec.code, {})) for spec in SPECS)
# Old configuration codes remain loadable; retired prototypes are excluded
# from the GUI, generated presets and the recommended playlist.
RETIRED = frozenset({"false_wall", "tunnel_rats", "lockdown", "paint_raid", "smugglers_run", "reverse_siege", "scrapyard_engineers"})
SPECS = tuple(spec for spec in ALL_SPECS if spec.code not in RETIRED)
CATALOG = {spec.code: spec for spec in ALL_SPECS}
ALIASES = {"prop_hunt": "block_hunt", "prophunt": "block_hunt", "bhop": "crumbling_run", "parkour": "crumbling_run", "extraction": "last_exit", "survival": "night_shift", "ffa": "borrowed_arsenal"}
ALIASES.update(spleef="floor_tax", gun_game="borrowed_arsenal", koth="kingmaker", one_in_the_chamber="one_in_the_spade", bridge_race="bridge_chicken")


def canonical(code: str) -> str:
    normalized = str(code).strip().lower().replace("-", "_")
    return ALIASES.get(normalized, normalized)
