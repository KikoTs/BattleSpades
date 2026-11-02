"""Inventory an offline wiki and compare literal historical constants safely.

Archive text is data: no Python, HTML, wiki templates or commands are executed.
The coverage ledger records reads, not a claim of semantic gameplay parity.
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_ASSIGNMENT = re.compile(r"^\s*([A-Z][A-Z0-9_]+)\s*=\s*(-?\d+(?:\.\d+)?)(?:\s*#.*)?\s*$", re.M)
_INVALID_WINDOWS = re.compile(r'[<>:"\\|?*]')


def archive_name(title: str) -> str:
    """The supplied Mac archive encoded slashes and Windows replaced colons."""
    return _INVALID_WINDOWS.sub("_", title.replace("/", "%2F"))


def literal_claims(text: str):
    for match in _ASSIGNMENT.finditer(text):
        yield match.group(1), ast.literal_eval(match.group(2))


def gameplay_checks(contents: dict) -> list[dict]:
    """Compare dated page claims with the live catalog, preserving differences."""
    from shared import constants as C, constants_gamemode as CG
    from server import class_data, mode_data
    from server.game_constants import WEAPON_CATALOG, WEAPON_PROFILES
    from server.prefabs import PrefabRegistry

    checks = []

    def page(title):
        return contents.get(f"text/{archive_name(title)}.txt", ("",))[0]

    def field(title, label):
        match = re.search(r"^\s*" + re.escape(label) + r"\s*\n\s*([^\n]+)", page(title), re.M)
        return match.group(1).strip() if match else ""

    def add(title, field_name, expected, actual, *, note="", tolerance=0.0):
        equal = (math.isclose(expected, actual, abs_tol=tolerance, rel_tol=0)
                 if isinstance(expected, (float, int)) and isinstance(actual, (float, int))
                 else expected == actual)
        checks.append(dict(page=title, field=field_name, wiki=expected, server=actual,
                           status="match" if equal else "difference", note=note))

    classes = {
        "Commando": C.CLASS_SOLDIER, "Marksman": C.CLASS_SCOUT,
        "Engineer": C.CLASS_ENGINEER, "Miner": C.CLASS_MINER,
        "Deuce": C.CLASS_CLASSIC_SOLDIER, "Zombie_(class)": C.CLASS_ZOMBIE,
        "Gangster": C.CLASS_GANGSTER_1, "Specialist": C.CLASS_SPECIALIST,
        "Medic": C.CLASS_MEDIC,
    }
    for title, cid in classes.items():
        health = re.match(r"\d+(?:\.\d+)?", field(title, "Health"))
        if health:
            add(title, "effective_health", float(health.group()),
                100 / class_data.get_damage(cid).damage_multiplier,
                tolerance=.1, note="Normalized health divided by victim class damage multiplier; HUD rounding allowed.")
        wallet = re.match(r"(\d+)\s*/\s*(\d+)", field(title, "Block Wallet"))
        if wallet:
            add(title, "starting/max_blocks", list(map(int, wallet.groups())), list(C.CLASS_BLOCKS[cid]))

    guns = {"Bolt-Action Rifle": 18, "Semi-Auto Rifle": 19, "Pistol": 17,
            "Minigun": 8, "SMG": 7, "Pump-Action Shotgun": 9,
            "Double Barrel Shotgun": 10, "Snub Nosed Pistol": 36, "Tommy Gun": 35}
    for line in page("Weapon_Stats").splitlines():
        cells = line.strip().split("\t")
        if len(cells) < 14 or cells[0] not in guns:
            continue
        profile = WEAPON_PROFILES[guns[cells[0]]]
        for index, name, actual in (
            (1, "head_damage", profile.part_damage[1]),
            (2, "torso_damage", profile.part_damage[0]),
            (3, "limb_damage", profile.part_damage[2]),
            (4, "pellets", profile.pellet_count),
            (8, "range", profile.max_range),
            (11, "shot_interval", profile.fire_interval),
            (12, "reload_seconds", profile.reload_time),
        ):
            try:
                value = float(cells[index])
            except ValueError:
                continue  # minigun variable cadence is described in prose
            add("Weapon_Stats", cells[0] + "." + name, value, actual,
                note="Wiki explicitly labels this table v163, not the final Steam build.")

    for title, tool in (("RPG", C.RPG_TOOL), ("Triple_Barrel_RPG", C.RPG2_TOOL)):
        claim = field(title, "Player damage")
        if claim.isdigit():
            add(title, "blast_damage", float(claim), WEAPON_CATALOG[tool].base_damage,
                note="Individual page agrees with the final build, unlike the older named-constant list.")
    for title, tool in (("Pistol", C.PISTOL_TOOL), ("Semi-Auto_Rifle", C.SNIPER2_TOOL)):
        text = page(title)
        match = re.search(r"One shot every (\d+(?:\.\d+)?) seconds", text)
        if match:
            add(title, "shot_interval", float(match.group(1)), WEAPON_PROFILES[tool].fire_interval)
        match = re.search(r"Player Head\s+(\d+)", text)
        if match:
            add(title, "head_damage", float(match.group(1)), WEAPON_PROFILES[tool].part_damage[1])

    flare_cost = field("Flare_Block", "Block Usage")
    if flare_cost.isdigit():
        add("Flare_Block", "block_cost", int(flare_cost), C.FLAREBLOCK_COST)
    from shared.achievement_table import ACHIEVEMENTS
    add("Achievements", "catalog_entries", 77, len(ACHIEVEMENTS),
        note="Catalog coverage does not imply all unlock triggers are reconstructed; see docs/ACHIEVEMENTS.md.")

    prefab_names = {
        "Caltrop": "prefab_caltrop", "Fort_Wall": "prefab_fort_wall",
        "Platform": "prefab_platform", "Safety_Corridor": "prefab_safety_corridor",
        "Super_Barrier": "prefab_superbarrier", "Super_Bridge": "prefab_superbridge",
        "Super_Dome": "prefab_superdome", "Super_Mini-Bunker": "prefab_superminibunker",
        "Super_Pole": "prefab_superpole", "Super_Small_Wall": "prefab_supersmallwall",
        "Super_Tower": "prefab_supertower", "Ultra_Barrier": "prefab_ultrabarrier",
        "Zombie_Bone": "prefab_zombiebone", "Zombie_Hand": "prefab_zombiehand",
        "Zombie_Head": "prefab_zombiehead",
    }
    registry = PrefabRegistry()
    for title, name in prefab_names.items():
        claim = field(title, "Block Usage")
        if claim.isdigit():
            model = registry.get(name)
            add(title, "advertised_cost_vs_model_voxels", int(claim),
                len(model.get_points()) if model else None,
                note="Full KV6 voxel count; placement charges only new cells. This is not a measured default-ground placement cost.")

    metadata = json.loads((ROOT / "maps/retail_map_info.json").read_text(encoding="utf-8"))["maps"]
    maps = page("Maps").split("Custom Maps", 1)[0].splitlines()
    for line in maps:
        title = line.strip()
        if not title or title in {"Maps", "Map Name", "Map"} or " " in title and title.startswith("Players "):
            continue
        name = re.sub(r"[^A-Za-z0-9]", "", title)
        if title in {"The Colosseum", "City of Chicago"}:
            name = {"The Colosseum": "TheColosseum", "City of Chicago": "CityOfChicago"}[title]
        add("Maps", title + ".bundled_vxl_and_metadata", True,
            name in metadata and (ROOT / "maps" / (name + ".vxl")).is_file())

    for title, code, score in (("Team_Deathmatch", "tdm", 200), ("Capture_the_Flag", "ctf", 5),
                               ("Classic_CTF", "cctf", 5), ("Game_Modes", "dia", 15),
                               ("Occupation", "oc", 30), ("Zombie", "zom", 3), ("VIP", "vip", 3),
                               ("Territory_Control", "tc", 5)):
        add(title, code + ".default_target_or_rounds", score, mode_data.get(code).default_score_limit)
    for title, name, value in (
        ("Zombie", "ZOM_TIME_BEFORE_FIRST_INFECTION", 50),
        ("Zombie", "ZOM_NOOF_FIRST_INFECTION_ZOMBIES", 2),
        ("Zombie", "ZOM_GAMEMODE_TIMEOUT", 600),
        ("Multi-Hill", "MH_DEFAULT_BASE_AUTO_TIMEOUT", 300),
        ("Multi-Hill", "MH_TEAM_SCORE_TICK_RATE", 1),
        ("Demolition", "DEM_DEFAULT_BUILD_TIME", 30),
        ("Capture_the_Flag", "CTF_INTEL_RETURN_TIME", 60),
        ("Territory_Control", "TC_DEFAULT_BASE_COUNT_TO_USE", 5),
    ):
        add(title, name, value, getattr(CG, name),
            note="Multi-Hill says approximately five minutes; server retail constant is four minutes." if name == "MH_DEFAULT_BASE_AUTO_TIMEOUT" else "")
    from shared import packet
    packet_names = {
        "Server Details": "InitialInfo", "Ping Pong": "ClockSync",
        "World Update": "WorldUpdate", "Entity update": "EntityUpdates",
        "Set Player Loadout": "SetClassLoadout", "Existing Player": "ExistingPlayer",
        "Spawn Player": "CreatePlayer", "Block State List": "BlockManagerState",
        "Game State": "StateData", "Chat Message": "ChatMessage",
        "Mesh File": "SkyboxData", "Map Send Start": "MapDataStart",
        "Map Sync Start": "MapSyncStart", "Map Send Data": "MapDataChunk",
        "Map Sync Data": "MapSyncChunk", "Map Send End": "MapDataEnd",
        "Map Sync End": "MapSyncEnd", "Incoming Map Checksum": "MapDataValidation",
        "Steam Key (unencrypted)": "SteamSessionTicket", "Player Input": "ClientData",
        "Shoot": "ShootPacket", "Throw": "UseOrientedItem", "Block Colour": "SetColor",
        "Choose Loadout": "SetClassLoadout", "Announce Player": "NewPlayerConnection",
        "Send Chat Message": "ChatMessage", "Expected Map Checksum": "MapDataValidation",
        "Set Team": "ChangeTeam", "Set Class": "ChangeClass", "Menu open": "ClientInMenu",
    }
    found = set()
    for match in re.finditer(r"^0x([0-9A-Fa-f]+): ([^\n]+)", page("Protocol/Packets"), re.M):
        label = re.sub(r"\s*\[.*\]$", "", match.group(2)).strip()
        if label in found or label not in packet_names:
            continue
        found.add(label)
        add("Protocol/Packets", label + ".packet_id", int(match.group(1), 16),
            getattr(packet, packet_names[label]).id,
            note="ID comparison only, not schema equality. Wiki mixes v122/v163; current wire classes are verified separately.")
    return checks


def audit(archive: Path, output: Path) -> dict:
    from shared import constants, constants_gamemode

    archive = archive.resolve(strict=True)
    rows = list(csv.DictReader((archive / "pages.csv").open(encoding="utf-8-sig")))
    if not rows:
        raise ValueError("pages.csv contains no page records")
    # Read every supplied text and wikitext file, including ones absent from
    # pages.csv. File hashes make subsequent archive changes visible.
    contents = {}
    for folder in ("text", "wikitext"):
        for path in sorted((archive / folder).rglob("*")):
            if path.is_file():
                data = path.read_bytes()
                contents[path.relative_to(archive).as_posix()] = (
                    data.decode("utf-8-sig", errors="replace"),
                    hashlib.sha256(data).hexdigest(), len(data))
    pages, claims, claimed_files = [], {}, set()
    for row in rows:
        title = row["title"]
        found = []
        for kind, suffix in (("text", ".txt"), ("wikitext", ".wiki")):
            supplied = row.get(kind + "_file", "")
            candidates = [supplied, f"{kind}/{archive_name(title)}{suffix}"]
            key = next((c for c in candidates if c and c in contents), None)
            if key:
                claimed_files.add(key)
                text, digest, size = contents[key]
                found.append((kind, key, digest, size))
                for name, value in literal_claims(text):
                    entry = claims.setdefault((name, value), {"name": name, "wiki": value, "pages": set()})
                    entry["pages"].add(title)
        namespace = title.split(":", 1)[0] if ":" in title else "(main)"
        pages.append({
            "title": title, "namespace": namespace,
            "coverage": "indexed" if found else "missing",
            "text_path": next((v[1] for v in found if v[0] == "text"), ""),
            "wikitext_path": next((v[1] for v in found if v[0] == "wikitext"), ""),
            "sha256": ";".join(v[2] for v in found),
            "bytes": sum(v[3] for v in found),
            "reference": row.get("text_wayback_url") or row.get("wikitext_wayback_url", ""),
        })
    checks = []
    for entry in claims.values():
        name = entry["name"]
        module = next((m for m in (constants, constants_gamemode) if hasattr(m, name)), None)
        actual = getattr(module, name) if module else None
        checks.append({**entry, "pages": sorted(entry["pages"]), "server": actual,
                       "status": "unmapped" if module is None else
                       "match" if actual == entry["wiki"] else "difference"})
    checks.sort(key=lambda v: v["name"])
    gameplay = gameplay_checks(contents)
    ledger = "\n".join(f"{k}:{v[1]}" for k, v in sorted(contents.items()))
    source_hash = hashlib.sha256()
    for folder in ("server", "modes", "shared", "commands"):
        for source in sorted((ROOT / folder).rglob("*.py")):
            source_hash.update(source.relative_to(ROOT).as_posix().encode())
            source_hash.update(source.read_bytes())
    for source in sorted((ROOT / "prefabs").glob("*.kv6")):
        source_hash.update(source.name.encode())
        source_hash.update(source.read_bytes())
    source_hash.update((ROOT / "maps/retail_map_info.json").read_bytes())
    summary = {
        "archive": str(archive), "page_records": len(rows),
        "files_read": len(contents), "bytes_read": sum(v[2] for v in contents.values()),
        "corpus_sha256": hashlib.sha256(ledger.encode()).hexdigest(),
        "server_sources_and_prefabs_sha256": source_hash.hexdigest(),
        "namespace_counts": dict(Counter(p["namespace"] for p in pages)),
        "coverage": dict(Counter(p["coverage"] for p in pages)),
        "unmapped_files": sorted(set(contents) - claimed_files),
        "constant_checks": dict(Counter(c["status"] for c in checks)),
        "gameplay_checks": dict(Counter(c["status"] for c in gameplay)),
        "limits": "Indexed means the full local text was read and hashed; it does not mean every statement has been proven in gameplay. See the accompanying human review.",
    }
    output.mkdir(parents=True, exist_ok=True)
    with (output / "pages.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(pages[0]))
        writer.writeheader()
        writer.writerows(pages)
    for name, value in (("summary.json", summary), ("constants.json", checks), ("gameplay.json", gameplay)):
        (output / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.archive, args.output), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
