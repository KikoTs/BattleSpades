"""Cross-case failure frequencies from runs/_detect.json and runs/_analysis.json."""

from collections import Counter, defaultdict
import json
import os

RUNS = os.environ.get("BOT_AUDIT_RUNS", "tmp/bot-audit")

d = json.load(open(RUNS + "/_detect.json"))
a = {c["file"]: c for c in json.load(open(RUNS + "/_analysis.json"))}
print("cases", len(d), "sim minutes", round(sum(c["t_end"] for c in d) / 60))
modes = Counter(c["mode"] for c in d)
print("by mode", dict(modes))
maps = sorted({c["map"] for c in d})
print("maps", len(maps))


def stat_seconds(c, kinds, min_seconds=0.0):
    an = a.get(c["file"])
    if an is None:
        return 0.0
    return sum(s["seconds"] for s in an["stationary"] if s["kind"] in kinds and s["seconds"] >= min_seconds
               and s["t1"] <= c["t_end"] + 1)


flags = defaultdict(list)
for c in d:
    key = (c["map"], c["mode"], c["seed"])
    mode = c["mode"]
    if c["t_end"] >= 200 and c["kills_per_min"] < 1.0 and mode not in ("dia",):
        flags["no_contact(<1 kill/min)"].append(key)
    if mode in ("ctf", "cctf"):
        if c["ctf"]["captures"] == 0:
            flags["ctf_zero_captures"].append(key)
        if c["ctf"]["pickups"] == 0:
            flags["ctf_zero_pickups"].append(key)
        if c["ctf"]["at_base_no_capture"]:
            flags["ctf_carrier_at_base_no_capture"].append(key)
    if mode == "cctf":
        idle = dict(c["team_roles"].get("2", [])).get("idle_no_goal", 0) + dict(c["team_roles"].get("3", [])).get("idle_no_goal", 0)
        total = sum(n for team in c["team_roles"].values() for _r, n in team) or 1
        if idle / total > 0.03:
            flags["cctf_idle_no_goal(>3% of top roles)"].append(key + (round(idle / total, 2),))
    ug = sum(u["t1"] - u["t0"] for u in c["under_goal"])
    if ug >= 60:
        flags["under_goal>=60s"].append(key + (round(ug),))
    if mode == "oc" and c["oc"]["detonations_inside"] == 0:
        flags["oc_never_planted"].append(key)
    if mode == "mh":
        sc = c["scores_at_end"] or {}
        vals = sorted(sc.values())
        if vals and vals[-1] >= 90 and vals[0] <= 20:
            flags["mh_one_sided(>=90 vs <=20)"].append(key + (c["t_end"],))
    if mode == "dem":
        if not c["dem"]["destroyed"]:
            flags["dem_no_block_destroyed"].append(key)
        if c["t_end"] < 200 and "on_mode_end" in c["events"]:
            flags["dem_over_in<200s"].append(key + (round(c["t_end"]),))
    if mode == "vip" and c["vip"]["vip_kills"] == 0:
        flags["vip_no_vip_kill_in_360s"].append(key)
    if mode == "dia" and c["dia"]["cashed"] <= 2:
        flags["dia_<=2_cashed"].append(key)
    if mode == "tc" and c["events"].get("_announce_capture", 0) == 0:
        flags["tc_no_capture"].append(key)
    falls = [s for s in c["self_or_world"] if s[3] == "fall"]
    if len(falls) >= 2:
        flags["falls>=2"].append(key + (len(falls),))
    fire = [s for s in c["self_or_world"] if s[3] in ("blockfire", "molotov")]
    if fire:
        flags["self_burn"].append(key + (len(fire),))
    own = [s for s in c["self_or_world"] if s[3] in ("grenade", "apgrenade", "sticky", "chem", "glauncher", "rocket", "rocket2", "mine", "landmine", "dynamite", "c4", "cgrenade")]
    if own:
        flags["own_explosive_death"].append(key + (len(own),))
    if c["subrole_share"].get("planning_wait", 0) >= 0.05:
        flags["planning_wait>=5%"].append(key + (c["subrole_share"]["planning_wait"],))
    if c["family_share"].get("climb_skill", 0) >= 0.10:
        flags["climb_skill>=10%"].append(key + (c["family_share"]["climb_skill"],))
    if c["family_share"].get("water", 0) >= 0.08:
        flags["water>=8%"].append(key + (c["family_share"]["water"],))
    s = stat_seconds(c, ("nav_wait", "stuck_moving", "idle", "other"), 30.0)
    if s >= 60:
        flags["nav_stall(>=30s each) total>=60s"].append(key + (round(s),))
    an = a.get(c["file"])
    if an and len([l for l in an["loops"] if l["t1"] <= c["t_end"]]) >= 15 and mode != "zom":
        flags["loops>=15"].append(key + (len(an["loops"]),))

for name, items in sorted(flags.items(), key=lambda kv: -len(kv[1])):
    eligible = len(d)
    for token, ms in (("ctf_", ("ctf", "cctf")), ("cctf_", ("cctf",)), ("oc_", ("oc",)), ("mh_", ("mh",)),
                      ("dem_", ("dem",)), ("vip_", ("vip",)), ("dia_", ("dia",)), ("tc_", ("tc",))):
        if name.startswith(token):
            eligible = sum(modes[m] for m in ms)
    if name == "self_burn":
        eligible = modes["vip"] + modes["tc"]
    by_map = Counter(i[0] for i in items)
    print(f"\n## {name}: {len(items)}/{eligible} cases; maps: {dict(by_map.most_common(12))}")
    print("   ", items[:10])

print("\n=== per map kills/min (mean over cases, excluding dia/zom) and objective notes")
by_map = defaultdict(list)
for c in d:
    if c["mode"] not in ("dia", "zom"):
        by_map[c["map"]].append(c["kills_per_min"])
for m in maps:
    v = by_map[m]
    pw = [c["subrole_share"].get("planning_wait", 0) for c in d if c["map"] == m]
    cl = [c["family_share"].get("climb_skill", 0) for c in d if c["map"] == m]
    wa = [c["family_share"].get("water", 0) for c in d if c["map"] == m]
    print(f"  {m:16s} cases={len([c for c in d if c['map']==m]):2d} kills/min={sum(v)/max(len(v),1):5.1f} "
          f"planning_wait={sum(pw)/len(pw):.3f} climb={sum(cl)/len(cl):.3f} water={sum(wa)/len(wa):.3f}")
