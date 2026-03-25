# uncompyle6 version 3.9.2 OKAY Decrypted
# Python bytecode version base 2.7 (62211)
# Decompiled from: Python 3.10.11 (tags/v3.10.11:7d4cc5a, Apr  5 2023, 00:38:17) [MSC v.1929 64 bit (AMD64)]
# Embedded file name: C:\TeamCity\buildAgent\work\dc8eb0b1d2cf198a\Main\client\standalone\build\pyi.win32\run_obfuscated\out00-PYZ.pyz\shared.constants_ugc_objectives
from shared.constants import *

UGC_OBJECTIVES_TYPES = {
    "UGC_OBJECTIVE_TEAM1_SPAWN_POINTS": {
        "min": 1,
        "max": 10,
        "priority": 1,
        "entity_ids": [UGC_ITEM_BLUE_SPAWN_ZONE_SMALL, UGC_ITEM_BLUE_SPAWN_ZONE_MEDIUM, UGC_ITEM_BLUE_SPAWN_ZONE_LARGE],
    },
    "UGC_OBJECTIVE_TEAM2_SPAWN_POINTS": {
        "min": 1,
        "max": 10,
        "priority": 1,
        "entity_ids": [UGC_ITEM_GREEN_SPAWN_ZONE_SMALL, UGC_ITEM_GREEN_SPAWN_ZONE_MEDIUM, UGC_ITEM_GREEN_SPAWN_ZONE_LARGE],
    },
    "UGC_OBJECTIVE_TEAM1_ZONE": {
        "min": 1,
        "max": 1,
        "priority": 2,
        "entity_ids": [UGC_ITEM_BLUE_BASE_ZONE_SMALL, UGC_ITEM_BLUE_BASE_ZONE_MEDIUM, UGC_ITEM_BLUE_BASE_ZONE_LARGE],
    },
    "UGC_OBJECTIVE_TEAM2_ZONE": {
        "min": 1,
        "max": 1,
        "priority": 2,
        "entity_ids": [UGC_ITEM_GREEN_BASE_ZONE_SMALL, UGC_ITEM_GREEN_BASE_ZONE_MEDIUM, UGC_ITEM_GREEN_BASE_ZONE_LARGE],
    },
    "UGC_OBJECTIVE_TC_NEUTRAL_ZONES": {
        "min": 2,
        "max": 10,
        "priority": 3,
        "entity_ids": [UGC_ITEM_NEUTRAL_BASE_ZONE_SMALL, UGC_ITEM_NEUTRAL_BASE_ZONE_MEDIUM, UGC_ITEM_NEUTRAL_BASE_ZONE_LARGE],
    },
    "UGC_OBJECTIVE_MH_NEUTRAL_ZONES": {
        "min": 2,
        "max": 10,
        "priority": 3,
        "entity_ids": [UGC_ITEM_NEUTRAL_BASE_ZONE_SMALL, UGC_ITEM_NEUTRAL_BASE_ZONE_MEDIUM, UGC_ITEM_NEUTRAL_BASE_ZONE_LARGE],
    },
    "UGC_OBJECTIVE_DIA_NEUTRAL_ZONES": {
        "min": 2,
        "max": 5,
        "priority": 3,
        "entity_ids": [UGC_ITEM_NEUTRAL_BASE_ZONE_SMALL, UGC_ITEM_NEUTRAL_BASE_ZONE_MEDIUM, UGC_ITEM_NEUTRAL_BASE_ZONE_LARGE],
    },
    "UGC_OBJECTIVE_AMMOCRATE_SPAWNS": {
        "min": 2,
        "max": 25,
        "priority": 4,
        "entity_ids": [UGC_ITEM_AMMO_DROP_POINT],
    },
    "UGC_OBJECTIVE_HEALTHCRATE_SPAWNS": {
        "min": 2,
        "max": 25,
        "priority": 4,
        "entity_ids": [UGC_ITEM_HEALTH_DROP_POINT],
    },
    "UGC_OBJECTIVE_BLOCKCRATE_SPAWNS": {
        "min": 2,
        "max": 25,
        "priority": 4,
        "entity_ids": [UGC_ITEM_BLOCK_DROP_POINT],
    },
    "UGC_OBJECTIVE_BLOCKCOUNT": {
        "min": 0,
        "max": 100000,
        "priority": 5,
        "entity_ids": [],
    },
    "UGC_OBJECTIVE_BOMB_SPAWNS": {
        "min": 1,
        "max": 5,
        "priority": 3,
        "entity_ids": [UGC_ITEM_OCC_BOMB_POINT],
    },
}

UGC_OBJECTIVES = {
    "COMMON": [
        "UGC_OBJECTIVE_AMMOCRATE_SPAWNS",
        "UGC_OBJECTIVE_HEALTHCRATE_SPAWNS",
        "UGC_OBJECTIVE_BLOCKCRATE_SPAWNS",
    ],
    "ctf": [
        "UGC_OBJECTIVE_TEAM1_SPAWN_POINTS",
        "UGC_OBJECTIVE_TEAM2_SPAWN_POINTS",
        "UGC_OBJECTIVE_TEAM1_ZONE",
        "UGC_OBJECTIVE_TEAM2_ZONE",
    ],
    "dem": [
        "UGC_OBJECTIVE_TEAM1_SPAWN_POINTS",
        "UGC_OBJECTIVE_TEAM2_SPAWN_POINTS",
        "UGC_OBJECTIVE_TEAM1_ZONE",
        "UGC_OBJECTIVE_TEAM2_ZONE",
    ],
    "mh": [
        "UGC_OBJECTIVE_TEAM1_SPAWN_POINTS",
        "UGC_OBJECTIVE_TEAM2_SPAWN_POINTS",
        "UGC_OBJECTIVE_MH_NEUTRAL_ZONES",
    ],
    "oc": [
        "UGC_OBJECTIVE_TEAM1_SPAWN_POINTS",
        "UGC_OBJECTIVE_TEAM2_SPAWN_POINTS",
        "UGC_OBJECTIVE_TEAM2_ZONE",
        "UGC_OBJECTIVE_BOMB_SPAWNS",
    ],
    "tdm": ["UGC_OBJECTIVE_TEAM1_SPAWN_POINTS", "UGC_OBJECTIVE_TEAM2_SPAWN_POINTS"],
    "tc": [
        "UGC_OBJECTIVE_TEAM1_SPAWN_POINTS",
        "UGC_OBJECTIVE_TEAM2_SPAWN_POINTS",
        "UGC_OBJECTIVE_TC_NEUTRAL_ZONES",
    ],
    "vip": ["UGC_OBJECTIVE_TEAM1_SPAWN_POINTS", "UGC_OBJECTIVE_TEAM2_SPAWN_POINTS"],
    "zom": ["UGC_OBJECTIVE_TEAM1_SPAWN_POINTS", "UGC_OBJECTIVE_TEAM2_SPAWN_POINTS"],
    "dia": [
        "UGC_OBJECTIVE_TEAM1_SPAWN_POINTS",
        "UGC_OBJECTIVE_TEAM2_SPAWN_POINTS",
        "UGC_OBJECTIVE_DIA_NEUTRAL_ZONES",
    ],
}
