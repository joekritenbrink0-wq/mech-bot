"""Sanity checks for aim conventions and ballistics, using a synthetic observation (no map).

    python -I tools/test_geometry.py
"""
import gzip
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import bot  # noqa: E402

KIT = os.path.join(os.path.dirname(os.path.dirname(HERE)), "mechfight-kit")
hello = json.loads(gzip.open(os.path.join(KIT, "check", "stream.jsonl.gz"), "rt").readline())
hello["map"]["dir"] = "/nonexistent"  # no map: line of sight always clear
cfg = hello["config"]
b = bot.Bot(hello)
assert b.world is None

W = cfg["weapons"]


def weapon(kind, **kw):
    w = {"kind": kind, "cooldown": 0.0, "stats": dict(W[kind])}
    w.update(kw)
    return w


def obs(mechs, weapons, aim=(0.0, 0.0), t=10.0):
    return {
        "type": "obs", "tick": int(t * 20), "t": t, "alive": 16,
        "self": {"id": 0, "pos": [0.0, 0.0, 0.0], "vel": [0, 0, 0], "aim": list(aim), "hp": 1400, "max_hp": 1400,
                 "energy": 100, "max_energy": 100, "grounded": True, "cubes": 0, "upgrades": [], "in_zone": True,
                 "weapons": weapons, "stats": dict(cfg["mech"])},
        "mechs": mechs, "projectiles": [], "crates": [], "cubes": [], "pickups": [], "sounds": [], "beams": [],
        "events": [], "drops": [],
        "zone": {"center": [0, 0], "radius": 560, "next_center": [0, 0], "next_radius": 380,
                 "shrink_start": 60, "shrink_end": 110, "dps": 20},
        "offer": None,
    }


def enemy(pos, vel=(0, 0, 0), **kw):
    m = {"id": 5, "kind": "mech", "pos": list(pos), "vel": list(vel), "aim": [math.pi, 0], "hp": 1400, "max_hp": 1400,
         "cubes": 0, "weapons": ["gun"], "charging": False}
    m.update(kw)
    return m


# 1. gun leads a target moving north: aim yaw must be north of the bearing (positive), and fire once aimed
gun = weapon("gun", ammo=12, reload=0)
a = b.act(obs([enemy((100, 0, 0), (0, 10, 0))], [gun, None]))
yaw = a["aim"][0]
expect = math.atan2(10 * 97 / 252, 100)
assert 0.5 * expect < yaw < 1.5 * expect, (yaw, expect)
a = b.act(obs([enemy((100, 0, 0), (0, 10, 0))], [gun, None], aim=a["aim"]))
assert a["fire"][0], a
print("gun lead ok: yaw", round(yaw, 4), "expected about", round(expect, 4))

# 2. target to the west: yaw near +-pi
a = b.act(obs([enemy((-80, 0, 0))], [gun, None]))
assert abs(abs(a["aim"][0]) - math.pi) < 0.05, a["aim"]
print("west bearing ok")

# 3. laser: start charging, hold, release at full charge when aimed
las = weapon("laser", charge=0.0)
a = b.act(obs([enemy((150, 0, 0))], [gun, las]))
assert a["fire"][1], "should start charging"
las_c = weapon("laser", charge=0.4)
a = b.act(obs([enemy((150, 0, 0))], [gun, las_c], aim=(0.0, -0.01)))
assert a["fire"][1], "should keep holding at partial charge"
las_f = weapon("laser", charge=W["laser"]["charge"])
aim_direct = (0.0, math.atan2(4.5 - 7.0, 150))
a = b.act(obs([enemy((150, 0, 0))], [gun, las_f], aim=aim_direct))
assert not a["fire"][1], "should release at full charge"
print("laser charge/release ok")

# 4. mortar: simulate the shell from the solution and check where it lands
ms = W["mortar"]
me = obs([], [gun, None])["self"]
for R, vis in ((120, True), (200, True), (120, False)):
    sol = b.mortar_solution(me, R, 0.0, 0.0, 0.0, 0.0, ms, vis)
    yaw, th = sol
    v, g = ms["speed"], ms["gravity"]
    x, z = 3.0 * math.cos(th), me["stats"]["height"] + 0.5 + 3.0 * math.sin(th)
    vx, vz = v * math.cos(th), v * math.sin(th)
    dt = 0.001
    while z > 1.0 or vz > 0:
        x += vx * dt
        vz -= g * dt
        z += vz * dt
    assert abs(x - R) < 4.0, (R, vis, x)
    print(f"mortar R={R} {'low' if vis else 'high'} arc pitch={th:.3f} lands at {x:.1f}")

# 5. upgrade picks prefer plating
offer = {"id": 1, "expires": 15, "choices": [{"id": "sensors"}, {"id": "plating"}, {"id": "gun_rate"}]}
assert b.pick_upgrade(offer) == 1
print("all geometry checks passed")
