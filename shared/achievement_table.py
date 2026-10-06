"""Retail Steam achievements of Ace of Spades: Battle Builder (app 224540).

The 77 rows of the Steam schema, in schema order: the schema token, the API
name, the displayed name and description, and for progress achievements the
backing integer statistic and the value that unlocks it. Two tiers may share
one statistic (sniper_kill_count 25 and 50). ``stat`` and ``threshold`` are
``None`` for one-shot achievements.

The descriptions are the only surviving specification of the rules: the
retail server evaluated them and no client binary contains one. See
docs/ACHIEVEMENTS.md.
"""
from __future__ import annotations

from typing import NamedTuple


class Achievement(NamedTuple):
    token: str
    api_name: str
    display_name: str
    description: str
    stat: str | None = None
    threshold: int | None = None


ACHIEVEMENTS: tuple[Achievement, ...] = (
    Achievement(
        "NEW_ACHIEVEMENT_2_1", "zombie_survivor",
        "Apocalypse Later",
        "Survive to the end of a round of Zombie",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_2", "zombie_kills_humans",
        "Three of a Kind",
        "Take out 3 survivors in 1 round",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_3", "zombie_mvp",
        "Stone Cold Killer",
        "Start the round as a zombie, and wipe out the highest number of enemy players",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_4", "zombie_fall",
        "Dead Drop",
        "Playing as a zombie, make an enemy player fall to their death",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_5", "vip_mvp",
        "The Main Man",
        "Start the round as VIP and kill the highest number of enemy players",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_6", "vip_pacifist",
        "I am the passenger",
        "Start the round as VIP, kill no enemy players, and survive anyway",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_7", "diamond_hotfoot",
        "Diamond Geezer",
        "Pick up a diamond within 5 seconds of it spawning, and make it to a dropoff point without dropping it",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_8", "diamond_collector",
        "Girl's Best Friend",
        "Find and drop off 3 diamonds in one match",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_9", "diamond_thief",
        "Feeling Flush",
        "Steal and drop off 5 diamonds found by the enemy team before they pick them up",
        "diamond_thief_count", 5,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_10", "diamond_interceptor",
        "King of Diamonds",
        "Intercept 10 diamonds carried by the enemy team and make it to a dropoff point",
        "diamond_interceptor_count", 10,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_11", "hill_greedy",
        "One Man Army",
        "Be the only player to score points from a hill before it vanishes",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_12", "hill_strike_survivor",
        "Shock and Awe",
        "Trigger 5 airstrikes and survive",
        "hill_strike_survivor_count", 5,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_13", "hill_strike_stay_put",
        "Strikebreaker",
        "Trigger an airstrike and survive without leaving the hill",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_14", "hill_interceptor",
        "Cubic Heir",
        "Take control of 10 contested hills",
        "hill_interceptor_count", 10,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_15", "hill_defender",
        "Tor Defence",
        "Retain control of 10 contested hills",
        "hill_defender_count", 10,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_16", "hill_kill_shooting_out",
        "Mountain Casualties",
        "Take out 50 enemy players while you are in an active hill",
        "hill_kill_shooting_out_count", 50,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_17", "hill_kill_shooting_in",
        "Butte Hurt",
        "Take out 50 enemy players while they are in an active hill",
        "hill_kill_shooting_in_count", 50,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_18", "bomb_survivor",
        "Bombs Away!",
        "Drop a bomb in the enemy base and survive",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_19", "bomb_hotfoot",
        "Hotfoot",
        "Pick up a bomb from a spawn point and make it to the enemy base without dropping it",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_20", "bomb_interceptor",
        "Bomb Disposal Expert",
        "Intercept 5 bomb carriers and defend the bomb until it detonates early.",
        "bomb_interceptor_count", 5,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_21", "bomb_final_bomber",
        "Bomb the Base",
        "Be the final player to plant a bomb successfully, and then win the round.",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_22", "demolition_repair",
        "Fort Process",
        "Repair 100 blocks of damage to your base",
        "demolition_repair_count", 100,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_23", "demolition_damage_one_round",
        "Block Party",
        "Cause 100 blocks of damage to the enemy base in one round",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_24", "demolition_damage_many_rounds",
        "Attack the Blocks",
        "Cause 500 blocks of damage to the enemy base over multiple rounds",
        "demolition_damage_many_rounds_count", 500,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_25", "demolition_final_damage",
        "Put the Boot in",
        "Be the final player to cause damage to the enemy base and win",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_26", "minigun_demolish",
        "Riddle Me This",
        "Demolish a structure of 50 blocks or more using the minigun",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_27", "rocket_fall",
        "Splashback Cashback",
        "Make someone fall to their death using rocket launcher knockback",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_28", "pistol_zombie_kill",
        "Face Off",
        "Kill 5 zombies by shooting them in the face with the pistol",
        "pistol_zombie_kill_count", 5,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_29", "grenade_demolish",
        "Holey Hand Grenade",
        "Demolish 5 structures of 100 or more blocks using a grenade",
        "grenade_demolish_count", 5,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_30", "spade_kill",
        "Dig Deep",
        "Kill 10 enemies with the spade",
        "spade_kill_count", 10,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_2_31", "sniper_accuracy",
        "Hat Trick",
        "3 kills with sniper rifle without missing a shot",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_0", "sniper_kill",
        "Head Hunter",
        "25 headshots with the sniper rifle",
        "sniper_kill_count", 25,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_1", "landmine_hidden",
        "Landmine Craft",
        "Kill 5 players with landmines placed on the ground and re-buried",
        "landmine_hidden_count", 5,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_2", "pickaxe_kill",
        "Take Your Pick",
        "Kill 10 enemies with the pickaxe",
        "pickaxe_kill_count", 10,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_3", "jetpack_kill",
        "Rocket Man",
        "Kill 20 enemies while using the jetpack",
        "jetpack_kill_count", 20,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_5", "jetpack_killed_using",
        "Jet Fighter",
        "Kill an enemy while he uses a jetpack",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_6", "jetpack_smg_kill",
        "Jetpack Drive-by",
        "Kill 10 enemies with the SMG while using the jetpack",
        "jetpack_smg_kill_count", 10,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_7", "shotgun_headshots",
        "Heads You Win",
        "10 headshots with the shotgun",
        "shotgun_headshots_count", 10,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_9", "drillgun_demolition",
        "All Your Base",
        "Destroy 1000 blocks of enemy bases in demolition mode using the drill gun",
        "drillgun_demolition_count", 1000,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_10", "dynamite_below",
        "Floored Genius",
        "Kill 10 enemies by placing dynamite below them then blowing out the floor",
        "dynamite_below_count", 10,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_11", "turret_accuracy",
        "Turret Syndrome",
        "Kill 10 enemies with 1 turret",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_13", "turret_evasion",
        "Collateral Damage",
        "Destroy 100 blocks by evading an enemy turret",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_14", "ammo_drop_greedy",
        "Loaded",
        "Collect a full resupply of ammunition in one game",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_15", "health_drop_greedy",
        "Well Heeled",
        "Repair 150 health in one game using health drops",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_16", "map_greatwall_destroy",
        "The War on Terracotta",
        "Destroy the Terracotta Army",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_18", "map_moon_destroy",
        "Kubrick Equation",
        "Lunar Base: Destroy the monolith with the rocket launcher",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_19", "map_london_destroy",
        "Death Toll",
        "Destroy the large bell at the top of Big Ben",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_20", "map_maya_kill",
        "Personal Sacrifice",
        "Melee kill an enemy while standing near the temple altar",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_21", "map_concrete_destroy",
        "Pillar Assault",
        "In demolition mode, destroy all the pillars in the enemy building lobby",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_23", "map_egypt_destroy",
        "Nose Job",
        "Destroy the nose of the Sphinx",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_24", "map_colosseum_kill",
        "The Undertaker",
        "Kill 10 enemies while hiding in the tunnel under the arena",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_27", "map_zombieisland_zombie_kill",
        "Brain Drain",
        "Kill 5 zombies while hiding in the basement",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_28", "map_zombieisland_destroy",
        "Tower Offense",
        "Playing as a zombie, demolish the mansion's towers",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_29", "misc_half_marathon",
        "Half Marathon",
        "Run 21 kilometers",
        "distance_run", 21000,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_30", "misc_marathon",
        "Marathon Man",
        "Run 42 kilometers",
        "distance_run", 42000,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_3_31", "misc_five_in_a_row",
        "Five Alive",
        "Get five kills in a row without dying",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_0", "misc_triple_explosion",
        "The Big Bang",
        "Kill 3 enemies with 1 explosion",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_1", "map_isleofdoom_destroy",
        "Exit The Dragon",
        "Dragon Island: Destroy the stone dragon",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_2", "intel_defence_easy",
        "Stop, thief!",
        "Intercept 5 enemies stealing your intel and defend it until it returns to base.",
        "intel_defence_count", 5,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_3", "intel_defence_hard",
        "Undelivered Message Report",
        "Intercept 10 enemies stealing your intel and defend it until it returns to base.",
        "intel_defence_count", 10,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_4", "classic_rifle_headshots",
        "Open Your Mind",
        "Score 50 headshots with the classic rifle",
        "classic_rifle_headshot_count", 50,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_5", "airborne_rockets",
        "Sky rockets in flight",
        "Score 5 direct hits with rockets while airborne",
        "airborne_rocket_count", 5,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_6", "knife_zombies",
        "Bit Late for an Autopsy",
        "Knife 10 zombies",
        "knife_zombie_count", 10,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_7", "misc_ten_in_a_row",
        "Streaker",
        "Get ten kills in a row without dying",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_8", "misc_fifteen_in_a_row",
        "OMG Hax!",
        "Get fifteen kills in a row without dying",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_9", "sniper_kill_hard",
        "Hey, is that a sni-",
        "50 headshots with the sniper rifle",
        "sniper_kill_count", 50,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_10", "low_health_killing",
        "Riskbreaker",
        "Kill ten players when your health is below 10% over multiple rounds",
        "low_health_kills", 10,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_11", "lastman_kills_zombies_easy",
        "Groovy!",
        "As last man standing take out at least 5 zombies in one round",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_12", "lastman_kills_zombies_hard",
        "Come get some!",
        "As last man standing take out at least 10 zombies in one round",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_13", "zombie_kills_in_water_ach",
        "Water on the Brainnnns",
        "Kill 5 players as a zombie in the water",
        "zombie_kills_in_water", 5,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_14", "blocks_as_zombie",
        "Dead Can Dig",
        "Destroy 666 blocks as a zombie",
        "block_as_zombie_count", 666,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_15", "zombie_lms_one_round",
        "28 Seconds Later",
        "Survive for 28 seconds as last man standing in one zombie round",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_16", "zombie_lms_multi_round",
        "Survivalist",
        "Survive as last man for 10 minutes total over multiple zombie rounds",
        "zombie_seconds_as_lms_count", 600,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_19", "airborne_for_hour",
        "Time Flies",
        "Spend a total of an hour in the air",
        "airborne_seconds_count", 3600,
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_20", "sniper2_rapid_kill",
        "The Quick and the Dead",
        "Kill 3 enemies in 20 seconds with the semi-auto sniper rifle",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_21", "sniper_accuracy_hard",
        "Wisely Snipes",
        "6 kills with sniper rifle without missing a shot",
    ),
    Achievement(
        "NEW_ACHIEVEMENT_4_22", "classic_kills_with_intel",
        "Courier Killer",
        "Kill 5 players while carrying the intel in classic",
        "classic_kills_with_intel_count", 5,
    ),
)
