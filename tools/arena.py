"""Full practice-format match simulator (approximate): our bot vs a 15-bot house squad on the real map.

What it models, from protocol.md and hello.config:
- 16 spawns 130 m apart, the real zone phases (random next circles), zone damage, regen, rear armor
- movement (accel, walls, roofs, jump, jet, energy), torso slew, vision (fov, near range, LOS, 5 s memory)
- gun, laser (charge/hold/release, cook-off), lightning (cone, chain, short), mortar (ballistic shells, splash, knock)
- 40 floor weapons, 36 crates (cube + 50% weapon), 12 drones (shoot back, 2 cubes), kill drops (cubes + 2, weapons)
- cubes (+4% hp and damage, +5% size), upgrade offers and their effects, 3 supply drops
The house squad is a guess tuned to match real practice scores (see PARAMS): it shares vision, never
fights itself, and sends hunters at us weakest first, more as we kill them, all of them late.

    python -I tools/arena.py --bots starter,v0,v1 --matches 48
"""
import argparse
import gzip
import importlib.util
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import time
from multiprocessing import Pool

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
KIT = os.path.join(os.path.dirname(ROOT), "mechfight-kit")
sys.path.insert(0, ROOT)
import mechfight as mf  # noqa: E402
from world import World  # noqa: E402
from nav import NavGrid, Follower  # noqa: E402
sys.path.insert(0, HERE)
import precompute_conceal as PC  # noqa: E402

TAU = 2 * math.pi
DT = 0.05
LEVELS = ["easy"] * 4 + ["medium"] * 4 + ["hard"] * 4 + ["expert"] * 3

# Squad model, calibrated 2026-11-20 against real practice scores. Weak scripted easy bots plus medium-and-up
# hunters that run our v2 combat logic with aim noise reproduce the "wall" all versions hit:
# real starter / v0 / v1 / v2 = 1.15 / 3.94 / 3.79 / 4.04; sim 0.65 / 4.12 / 3.40 / 4.94 (48 matches each).
PARAMS = {
    "skill": {  # aim noise (rad), reaction lag (s), strafe amount, laser release tolerance (rad)
        "easy": {"noise": 0.014, "lag": 0.40, "strafe": 0.25, "ltol": 0.014, "react": 0.8},
        "medium": {"noise": 0.010, "lag": 0.30, "strafe": 0.45, "ltol": 0.010, "react": 0.5},
        "hard": {"noise": 0.006, "lag": 0.18, "strafe": 0.65, "ltol": 0.007, "react": 0.3},
        "expert": {"noise": 0.004, "lag": 0.10, "strafe": 0.85, "ltol": 0.005, "react": 0.2},
    },
    "hunters_start": 1,      # hunters at t=0
    "hunters_per_kill": 0.5,  # extra hunters per house bot we kill
    "all_hunt_t": 300.0,     # everyone hunts after this match time
    "engage_range": 0.0,   # a non-hunter that sees us closer than this fights
    "retaliate_s": 0.0,      # a non-hunter we shot fights back for this long (0 = never)
    "skill_scale": 3.0,      # multiplies aim noise, laser tolerance and reaction lag of every house bot
    "omniscient": True,      # hunters always know where we are (False: shared vision + hearing, then search)
    "loadout": {"laser": 0.5, "lightning": 0.25, "mortar": 0.25},
    "fight_range": {"laser": 130.0, "gun": 90.0, "lightning": 28.0, "mortar": 120.0},
    "strong_levels": ["medium", "hard", "expert"],  # hunters that run a real bot's combat logic (StrongBrain)
    "strong_bot": "v2",      # which bot version they run
    "strong_noise": {"easy": 0.02, "medium": 0.006, "hard": 0.003, "expert": 0.0015},  # aim noise added (rad)
}


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def hyp(dx, dy):
    return math.sqrt(dx * dx + dy * dy)


# ---------------------------------------------------------------------------------------------- bots
def load_bot_module(spec):
    """'v1'/'work' = working tree; 'starter' = kit example; 'v0' or a git rev = bot.py at that revision."""
    if spec in ("v1", "work"):
        path = os.path.join(ROOT, "bot.py")
    elif spec == "starter":
        src = open(os.path.join(KIT, "python", "example_bot.py")).read().replace("mf.run(Bot)", "")
        d = tempfile.mkdtemp(prefix="bot_starter_")
        path = os.path.join(d, "bot.py")
        open(path, "w").write(src)
    else:
        rev = {"v0": "333fb36", "v1c": "9f4e5fc", "v2": "92a99a7"}.get(spec, spec)
        src = subprocess.run(["git", "-C", ROOT, "show", f"{rev}:bot.py"], capture_output=True, text=True, check=True).stdout
        d = tempfile.mkdtemp(prefix=f"bot_{spec}_")
        path = os.path.join(d, "bot.py")
        open(path, "w").write(src)
    s = importlib.util.spec_from_file_location(f"bot_{spec.replace('.', '_')}", path)
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------------------------- entities
class Mech:
    def __init__(self, mid, pos, cfg, house=False, level=None):
        self.id = mid
        self.cfg = cfg
        self.house = house
        self.level = level
        self.st = dict(cfg["mech"])
        self.wst = {k: dict(v) for k, v in cfg["weapons"].items()}
        self.pos = list(pos)
        self.vel = [0.0, 0.0, 0.0]
        self.aim = [0.0, 0.0]
        self.cmd_aim = [0.0, 0.0]
        self.base_hp = float(self.st["hp"])
        self.max_hp = self.base_hp
        self.hp = self.max_hp
        self.energy = float(self.st["energy"])
        self.spend_t = -99.0
        self.grounded = True
        self.alive = True
        self.death_t = None
        self.cause = None
        self.last_combat = -99.0
        self.slots = [self.new_weapon("gun"), None]
        self.cubes = 0
        self.upgrades = []
        self.offers = []
        self.offer_seq = 0
        self.thresholds_hit = 0
        self.action = {}
        self.events = []
        self.sounds = []
        self.seen = {}
        self.short_until = -1.0
        self.kills = 0
        self.dealt = 0.0
        self.taken_by = {}

    def new_weapon(self, kind):
        w = {"kind": kind, "cooldown": 0.0, "equip": 0.0}
        if kind == "gun":
            w["ammo"] = self.wst["gun"]["mag"]
            w["reload"] = 0.0
        if kind == "laser":
            w["charge"] = 0.0
            w["full_t"] = 0.0
        return w

    @property
    def size(self):
        return 1.0 + self.cfg["cubes"]["size_per_cube"] * self.cubes

    @property
    def radius(self):
        return self.st["radius"] * self.size

    @property
    def height(self):
        return self.st["height"] * self.size

    def held(self):
        return [w["kind"] for w in self.slots if w]

    def has(self, kind):
        return any(w and w["kind"] == kind for w in self.slots)

    def weapon(self, kind):
        for w in self.slots:
            if w and w["kind"] == kind:
                return w
        return None

    def charging(self):
        w = self.weapon("laser")
        return bool(w and w["charge"] > 0)

    def dmg_mult(self):
        return 1.0 + self.cfg["cubes"]["damage_per_cube"] * min(self.cubes, self.cfg["cubes"]["stat_cap"])

    def stats_obs(self):
        s = dict(self.st)
        s["radius"] = self.radius
        s["height"] = self.height
        s["eye_height"] = self.st["eye_height"] * self.size
        s["muzzle_height"] = self.st["muzzle_height"] * self.size
        s["hp"] = self.max_hp
        return s


class Thing:
    def __init__(self, tid, kind, pos, hp=0.0, radius=1.4, height=2.4):
        self.id, self.kind, self.pos, self.hp = tid, kind, list(pos), hp
        self.radius, self.height = radius, height
        self.alive = True
        self.home = list(pos)
        self.cool = 0.0
        self.aggro = None
        self.aggro_t = -99.0
        self.vel = [0.0, 0.0, 0.0]
        self.wander = list(pos)


# ---------------------------------------------------------------------------------------------- simulator
class Arena:
    def __init__(self, world, nav, cfg, rng, params):
        self.w, self.nav, self.cfg, self.rng, self.P = world, nav, cfg, rng, params
        self.t = 0.0
        self.tick = 0
        self.mechs = []
        self.bullets = []
        self.shells = []
        self.beams = []
        self.npcs = []
        self.crates = []
        self.cubes = []
        self.pickups = []
        self.drops = []
        self.next_id = {"crate": 1000, "npc": 100, "cube": 5000, "pickup": 8000, "drop": 20000}
        self.main_comp = self._main_comp()
        self.setup_zone()

    def _main_comp(self):
        counts = {}
        for c in self.nav.comp:
            if c:
                counts[c] = counts.get(c, 0) + 1
        return max(counts, key=counts.get)

    def nid(self, kind):
        self.next_id[kind] += 1
        return self.next_id[kind]

    # ---------------------------------------------------------------- zone
    def setup_zone(self):
        z = self.cfg["zone"]
        c = list(z["start_center"])
        r = z["start_radius"]
        self.zphases = []
        t = 0.0
        for ph in z["phases"]:
            nr = ph["radius"]
            for _ in range(50):
                ang = self.rng.uniform(-math.pi, math.pi)
                dist = self.rng.uniform(0, max(0.0, r - nr) * 0.9)
                nc = [c[0] + dist * math.cos(ang), c[1] + dist * math.sin(ang)]
                if self.nav is None:
                    break
                I, J = self.nav.cell_of(*nc)
                if self.nav.comp_at(I, J) == self.main_comp or nr > 150:
                    break
            hold_end = t + ph["hold_s"]
            shrink_end = hold_end + ph["shrink_s"]
            self.zphases.append({"c0": list(c), "r0": r, "c1": nc, "r1": nr, "s0": hold_end, "s1": shrink_end, "dps": ph["dps"]})
            c, r, t = nc, nr, shrink_end

    def zone_now(self):
        for ph in self.zphases:
            if self.t < ph["s1"]:
                f = 0.0 if self.t < ph["s0"] else (self.t - ph["s0"]) / (ph["s1"] - ph["s0"])
                c = [ph["c0"][0] + (ph["c1"][0] - ph["c0"][0]) * f, ph["c0"][1] + (ph["c1"][1] - ph["c0"][1]) * f]
                r = ph["r0"] + (ph["r1"] - ph["r0"]) * f
                return {"center": c, "radius": r, "next_center": list(ph["c1"]), "next_radius": ph["r1"],
                        "shrink_start": ph["s0"], "shrink_end": ph["s1"], "dps": ph["dps"]}
        ph = self.zphases[-1]
        return {"center": list(ph["c1"]), "radius": 0.0, "next_center": list(ph["c1"]), "next_radius": 0.0,
                "shrink_start": ph["s0"], "shrink_end": ph["s1"], "dps": ph["dps"]}

    # ---------------------------------------------------------------- setup
    def open_point(self, cx=None, cy=None, rad=None, tries=400):
        nav = self.nav
        for _ in range(tries):
            if cx is None:
                I, J = self.rng.randrange(nav.nx), self.rng.randrange(nav.ny)
                x, y = nav.center(I, J)
            else:
                a = self.rng.uniform(-math.pi, math.pi)
                d = rad * math.sqrt(self.rng.random())
                x, y = cx + d * math.cos(a), cy + d * math.sin(a)
                I, J = nav.cell_of(x, y)
            if nav.free(I, J) and nav.comp_at(I, J) == self.main_comp:
                x, y = nav.center(I, J)
                return x, y
        return None

    def populate(self, our_slot):
        z = self.cfg["zone"]
        cx, cy = z["start_center"]
        rad = z["start_radius"] * self.cfg["match"]["spawn_zone_fraction"]
        spawns = []
        sep = self.cfg["match"]["spawn_min_separation"]
        while len(spawns) < 16:
            p = self.open_point(cx, cy, rad)
            if p and all(hyp(p[0] - q[0], p[1] - q[1]) >= sep for q in spawns):
                spawns.append(p)
            elif p and len(spawns) < 16 and self.rng.random() < 0.002:
                sep *= 0.95
        levels = list(LEVELS) + ["expert"]  # 16th label only used in free-for-all mode
        for i in range(16):
            p = spawns[i]
            pos = (p[0], p[1], self.w.ground_at(*p))
            if i == our_slot:
                m = Mech(i, pos, self.cfg)
            else:
                m = Mech(i, pos, self.cfg, house=True, level=levels.pop(0))
            m.aim = [self.rng.uniform(-math.pi, math.pi), 0.0]
            m.cmd_aim = list(m.aim)
            self.mechs.append(m)
        loot = self.cfg["loot"]
        kinds = [k for k, v in loot["weights"].items() for _ in range(int(v * 10))]
        for _ in range(int(loot["floor_count"] * (1 - loot["floor_roof_fraction"]))):
            p = self.open_point(cx, cy, z["start_radius"])
            if p:
                self.pickups.append(Thing(self.nid("pickup"), self.rng.choice(kinds), (p[0], p[1], self.w.ground_at(*p))))
        cr = self.cfg["crates"]
        for _ in range(int(cr["count"] * (1 - cr["roof_fraction"]))):
            p = self.open_point(cx, cy, z["start_radius"])
            if p:
                self.crates.append(Thing(self.nid("crate"), "crate", (p[0], p[1], self.w.ground_at(*p)), cr["hp"], cr["radius"], cr["height"]))
        npc = self.cfg["npc"]
        for _ in range(npc["count"]):
            p = self.open_point(cx, cy, z["start_radius"])
            if p:
                self.npcs.append(Thing(self.nid("npc"), "npc", (p[0], p[1], self.w.ground_at(*p)), npc["hp"], npc["radius"], npc["height"]))
        self.drop_sched = list(self.cfg["drops"]["times"])

    # ---------------------------------------------------------------- geometry
    def solid(self, x, y, z0, z1):
        w = self.w
        k = w.idx(x, y)
        if k < 0:
            return True
        return (w.top[k] > z0 and w.base[k] < z1) or (w.btop[k] > z0 and w.bbase[k] < z1)

    def point_blocked(self, x, y, z):
        w = self.w
        k = w.idx(x, y)
        if k < 0:
            return True
        return w.base[k] <= z <= w.top[k] or w.bbase[k] <= z <= w.btop[k] or z < w.ground[k] - 0.3

    def body_blocked(self, m, x, y, z):
        su, h, r = m.st["step_up"], m.height, m.radius * 0.95
        if self.solid(x, y, z + su, z + h) or self.w.ground_at(x, y) > z + su:
            return True
        for a in range(6):
            px, py = x + r * math.cos(a * TAU / 6), y + r * math.sin(a * TAU / 6)
            if self.solid(px, py, z + su, z + h) or self.w.ground_at(px, py) > z + su:
                return True
        return False

    def support(self, x, y, z, su):
        w = self.w
        g = w.ground_at(x, y)
        k = w.idx(x, y)
        best = g
        if k >= 0:
            for top in (w.top[k], w.btop[k]):
                if g < top <= z + su + 0.05:
                    best = max(best, top)
        return best

    def los(self, a, b):
        return self.w.los(a[0], a[1], a[2], b[0], b[1], b[2], skip_start=0.5, skip_end=0.5)

    # ---------------------------------------------------------------- vision
    def sees(self, a, pos, top_h):
        dx, dy = pos[0] - a.pos[0], pos[1] - a.pos[1]
        d = hyp(dx, dy)
        v = self.cfg["vision"]
        near = v["near_range"] + a.st.get("near_range_bonus", 0)
        rng = v["range"] + a.st.get("vision_bonus", 0)
        if d > rng:
            return False
        if d > near and abs(wrap(math.atan2(dy, dx) - a.aim[0])) > math.radians(v["fov_deg"] / 2):
            return False
        e = (a.pos[0], a.pos[1], a.pos[2] + a.st["eye_height"] * a.size)
        cg = _W.get("canopy")
        if cg is not None:
            far = d > near  # proximity sensors ignore canopy
            return (PC.sees(self.w, cg[0], cg[1], e[0], e[1], e[2], pos[0], pos[1], pos[2] + top_h * 0.5, far)
                    or PC.sees(self.w, cg[0], cg[1], e[0], e[1], e[2], pos[0], pos[1], pos[2] + top_h * 0.95, far))
        return self.los(e, (pos[0], pos[1], pos[2] + top_h * 0.5)) or self.los(e, (pos[0], pos[1], pos[2] + top_h * 0.95))

    # ---------------------------------------------------------------- damage
    def damage(self, v, amount, weapon, attacker, src):
        if not v.alive:
            return
        if isinstance(v, Thing):
            v.hp -= amount
            if attacker is not None:
                attacker.events.append({"type": "hit", "target": v.id, "amount": amount, "weapon": weapon})
                attacker.last_combat = self.t
                if v.kind == "npc":
                    v.aggro, v.aggro_t = attacker, self.t
            if v.hp <= 0:
                v.alive = False
                if v.kind == "crate":
                    self.spawn_cubes(v.pos, self.cfg["crates"]["cubes"])
                    if self.rng.random() < self.cfg["loot"]["crate_weapon_chance"]:
                        kinds = [k for k, w in self.cfg["loot"]["weights"].items() if w > 0]
                        self.pickups.append(Thing(self.nid("pickup"), self.rng.choice(kinds), v.pos))
                    self.crates.remove(v)
                    self.sound("crate", v.pos, None)
                else:
                    self.spawn_cubes(v.pos, self.cfg["npc"]["cubes"])
                    self.npcs.remove(v)
            return
        bearing = math.atan2(src[1] - v.pos[1], src[0] - v.pos[0]) if src else None
        rear = False
        if weapon != "zone":
            rear = abs(wrap(bearing - v.aim[0])) > math.radians(v.st["front_arc_deg"] / 2)
            if rear:
                amount *= v.st["rear_mult"]
            amount *= max(0.0, 1.0 - v.st.get("armor", 0.0))
        v.hp -= amount
        if weapon != "zone":
            v.last_combat = self.t
        else:
            v.last_combat = max(v.last_combat, self.t)
        v.events.append({"type": "damaged", "amount": round(amount, 2), "weapon": weapon,
                         "bearing": bearing, "by": attacker.id if isinstance(attacker, Mech) else None, "rear": rear})
        key = weapon if not isinstance(attacker, Mech) else f"{attacker.level or 'us'}:{weapon}"
        v.taken_by[key] = v.taken_by.get(key, 0.0) + amount
        if isinstance(attacker, Mech):
            attacker.last_combat = self.t
            attacker.dealt += amount
            attacker.events.append({"type": "hit", "target": v.id, "amount": round(amount, 2), "weapon": weapon, "rear": rear})
        if v.hp <= 0:
            v.alive = False
            v.death_t = self.t
            v.cause = key
            self.sound("death", v.pos, v)
            self.spawn_cubes(v.pos, v.cubes + self.cfg["cubes"]["death_drop_extra"])
            for w in v.slots:
                if w and w["kind"] != "gun":
                    self.pickups.append(Thing(self.nid("pickup"), w["kind"], v.pos))
            if isinstance(attacker, Mech):
                attacker.kills += 1
                attacker.events.append({"type": "kill", "target": v.id})

    def spawn_cubes(self, pos, n):
        for i in range(int(n)):
            a = i * TAU / max(1, n)
            self.cubes.append(Thing(self.nid("cube"), "cube", (pos[0] + 1.5 * math.cos(a), pos[1] + 1.5 * math.sin(a), pos[2])))

    def sound(self, kind, pos, src):
        rng = self.cfg["sound"]["range"].get(kind, 100)
        sq = getattr(self, "squad", None)
        if sq is not None and src is not None and not src.house:
            for m in sq.members:
                if m.alive and hyp(pos[0] - m.pos[0], pos[1] - m.pos[1]) < rng:
                    err = 10.0 if kind in ("step", "boost", "reload") else 20.0
                    sq.known = [pos[0] + self.rng.gauss(0, err), pos[1] + self.rng.gauss(0, err), pos[2]]
                    sq.known_t = self.t
                    break
        for m in self.mechs:
            if m is src or not m.alive or m.house:
                continue
            d = hyp(pos[0] - m.pos[0], pos[1] - m.pos[1])
            if d < rng:
                b = math.atan2(pos[1] - m.pos[1], pos[0] - m.pos[0]) + self.rng.gauss(0, math.radians(7) * m.st.get("hearing", 1))
                m.sounds.append({"kind": kind, "bearing": wrap(b), "dist": "near" if d < 60 else ("mid" if d < 160 else "far"), "age": 0.05})

    # ---------------------------------------------------------------- targets for projectiles
    def bodies(self, shooter):
        """What a shot from `shooter` can hit. House bots never hurt each other."""
        out = []
        for m in self.mechs:
            if m.alive and m is not shooter and not (shooter is not None and shooter.house and m.house):
                out.append(m)
        if shooter is None or not shooter.house:
            out += [c for c in self.crates] + [n for n in self.npcs]
        return out

    def hit_body(self, p, bodies, pad=0.3):
        for b in bodies:
            if hyp(p[0] - b.pos[0], p[1] - b.pos[1]) < b.radius + pad and b.pos[2] - pad <= p[2] <= b.pos[2] + b.height + pad:
                return b
        return None

    # ---------------------------------------------------------------- weapons
    def muzzle(self, m, tube=False):
        yaw, pitch = m.aim
        d = (math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch))
        h = m.height + 0.5 if tube else m.st["muzzle_height"] * m.size
        off = m.radius + 0.5
        return (m.pos[0] + off * d[0], m.pos[1] + off * d[1], m.pos[2] + h + off * d[2]), d

    def step_weapons(self, m):
        fire = m.action.get("fire", [False, False]) or [False, False]
        for i, w in enumerate(m.slots):
            if w is None:
                continue
            w["cooldown"] = max(0.0, w["cooldown"] - DT)
            w["equip"] = max(0.0, w["equip"] - DT)
            held = bool(fire[i]) if i < len(fire) else False
            k = w["kind"]
            s = m.wst[k]
            if k == "gun":
                if w["reload"] > 0:
                    w["reload"] = max(0.0, w["reload"] - DT)
                    if w["reload"] == 0:
                        w["ammo"] = s["mag"]
                if m.action.get("reload") and w["reload"] == 0 and w["ammo"] < s["mag"]:
                    w["reload"] = s["reload"]
                    self.sound("reload", m.pos, m)
                if held and w["cooldown"] <= 0 and w["equip"] <= 0 and w["ammo"] > 0 and w["reload"] == 0:
                    o, d = self.muzzle(m)
                    sp = math.radians(s["spread_deg"])
                    yaw, pitch = m.aim[0] + self.rng.gauss(0, sp), m.aim[1] + self.rng.gauss(0, sp)
                    v = s["speed"]
                    self.bullets.append({"by": m, "pos": list(o), "vel": (v * math.cos(pitch) * math.cos(yaw), v * math.cos(pitch) * math.sin(yaw), v * math.sin(pitch)),
                                         "left": s["range"], "dmg": s["damage"] * m.dmg_mult(), "kind": "gun"})
                    w["ammo"] -= 1
                    w["cooldown"] = s["cooldown"]
                    self.sound("gun", m.pos, m)
                    if w["ammo"] <= 0:
                        w["reload"] = s["reload"]
            elif k == "laser":
                if w["cooldown"] > 0 or w["equip"] > 0 or self.t < m.short_until:
                    w["charge"] = 0.0
                    continue
                if held:
                    if w["charge"] == 0:
                        self.sound("laser_charge", m.pos, m)
                    w["charge"] = min(s["charge"], w["charge"] + DT)
                    if w["charge"] >= s["charge"]:
                        w["full_t"] += DT
                        if w["full_t"] >= s["hold_s"]:
                            self.fire_laser(m, w, s)
                elif w["charge"] > 0:
                    if w["charge"] >= s["min_charge"]:
                        self.fire_laser(m, w, s)
                    else:
                        w["charge"] += DT
                        if w["charge"] >= s["min_charge"]:
                            self.fire_laser(m, w, s)
            elif k == "lightning":
                if held and w["cooldown"] <= 0 and w["equip"] <= 0 and m.energy >= s["energy"]:
                    m.energy -= s["energy"]
                    m.spend_t = self.t
                    w["cooldown"] = s["cooldown"]
                    self.fire_lightning(m, s)
            elif k == "mortar":
                if held and w["cooldown"] <= 0 and w["equip"] <= 0:
                    o, d = self.muzzle(m, tube=True)
                    v = s["speed"]
                    self.shells.append({"by": m, "pos": list(o), "vel": [v * d[0], v * d[1], v * d[2]], "ttl": s["ttl"],
                                        "s": dict(s), "mult": m.dmg_mult()})
                    w["cooldown"] = s["cooldown"]
                    self.sound("mortar", m.pos, m)

    def fire_laser(self, m, w, s):
        frac = s["min_frac"] + (1 - s["min_frac"]) * min(1.0, w["charge"] / s["charge"])
        dmg = s["damage"] * frac * m.dmg_mult()
        o, d = self.muzzle(m)
        w["charge"] = 0.0
        w["full_t"] = 0.0
        w["cooldown"] = s["cooldown"]
        self.sound("laser", m.pos, m)
        bodies = self.bodies(m)
        end = None
        p = list(o)
        for _ in range(int(s["range"])):
            b = self.hit_body(p, bodies, 0.0)
            if b is not None:
                mult = 1.0 + s["cookoff"] if isinstance(b, Mech) and b.has("mortar") else 1.0
                self.damage(b, dmg * mult, "laser", m, o)
                if isinstance(b, Mech) and mult > 1:
                    b.events.append({"type": "effect", "effect": "cookoff", "by": m.id})
                end = list(p)
                break
            if self.point_blocked(*p):
                end = list(p)
                break
            p[0] += d[0]
            p[1] += d[1]
            p[2] += d[2]
        self.beams.append({"by": m.id, "kind": "laser", "path": [list(o), end or list(p)], "t": self.t})

    def fire_lightning(self, m, s):
        self.sound("lightning", m.pos, m)
        eye = (m.pos[0], m.pos[1], m.pos[2] + m.st["muzzle_height"] * m.size)
        cone = math.radians(s["cone_deg"])
        best = None
        for b in self.bodies(m):
            d = hyp(b.pos[0] - m.pos[0], b.pos[1] - m.pos[1])
            if d > s["range"] or abs(wrap(math.atan2(b.pos[1] - m.pos[1], b.pos[0] - m.pos[0]) - m.aim[0])) > cone:
                continue
            if not self.los(eye, (b.pos[0], b.pos[1], b.pos[2] + b.height * 0.5)):
                continue
            if best is None or d < best[0]:
                best = (d, b)
        if best is None:
            return
        dmg = s["damage"] * m.dmg_mult()
        hit = [best[1]]
        cur = best[1]
        self.zap(cur, dmg, m)
        for _ in range(int(s["chain"])):
            dmg *= s["chain_falloff"]
            nxt = None
            for b in self.bodies(m):
                if b in hit:
                    continue
                d = hyp(b.pos[0] - cur.pos[0], b.pos[1] - cur.pos[1])
                if d <= s["chain_range"] and (nxt is None or d < nxt[0]):
                    nxt = (d, b)
            if nxt is None:
                break
            cur = nxt[1]
            hit.append(cur)
            self.zap(cur, dmg, m)
        self.beams.append({"by": m.id, "kind": "lightning", "path": [list(eye)] + [[b.pos[0], b.pos[1], b.pos[2] + 2] for b in hit], "t": self.t})

    def zap(self, b, dmg, m):
        self.damage(b, dmg, "lightning", m, m.pos)
        if isinstance(b, Mech):
            lw = b.weapon("laser")
            if lw:
                lw["charge"] = 0.0
                lw["full_t"] = 0.0
                b.short_until = self.t + m.wst["lightning"]["short_s"]
                b.events.append({"type": "effect", "effect": "short", "by": m.id})

    def step_projectiles(self):
        keep = []
        for b in self.bullets:
            bodies = self.bodies(b["by"]) if isinstance(b["by"], Mech) else [m for m in self.mechs if m.alive and not m.house]
            done = False
            for _ in range(4):
                p = b["pos"]
                st = (b["vel"][0] * DT / 4, b["vel"][1] * DT / 4, b["vel"][2] * DT / 4)
                p[0] += st[0]
                p[1] += st[1]
                p[2] += st[2]
                b["left"] -= math.sqrt(st[0] ** 2 + st[1] ** 2 + st[2] ** 2)
                if b["left"] <= 0 or self.point_blocked(*p):
                    done = True
                    break
                hb = self.hit_body(p, bodies)
                if hb is not None:
                    src = (p[0] - b["vel"][0], p[1] - b["vel"][1], p[2])
                    self.damage(hb, b["dmg"], b["kind"], b["by"], src)
                    done = True
                    break
            if not done:
                keep.append(b)
        self.bullets = keep
        keep = []
        for sh in self.shells:
            sh["ttl"] -= DT
            p, v = sh["pos"], sh["vel"]
            g = sh["s"]["gravity"]
            bodies = self.bodies(sh["by"])
            boom = None
            direct = None
            for _ in range(4):
                v[2] -= g * DT / 4
                p[0] += v[0] * DT / 4
                p[1] += v[1] * DT / 4
                p[2] += v[2] * DT / 4
                hb = self.hit_body(p, bodies, 0.2)
                if hb is not None:
                    boom, direct = list(p), hb
                    break
                if self.point_blocked(*p):
                    boom = list(p)
                    break
            if boom is None and sh["ttl"] <= 0:
                boom = list(p)
            if boom is None:
                keep.append(sh)
                continue
            self.explode(sh, boom, direct)
        self.shells = keep

    def explode(self, sh, at, direct):
        s, m, mult = sh["s"], sh["by"], sh["mult"]
        self.sound("explosion", at, None)
        if direct is not None:
            self.damage(direct, s["damage"] * mult, "mortar", m, at)
        R = s["splash_radius"]
        for b in [x for x in self.mechs if x.alive] + self.crates + self.npcs:
            if b is direct:
                continue
            cz = b.pos[2] + b.height * 0.5
            d = math.sqrt((b.pos[0] - at[0]) ** 2 + (b.pos[1] - at[1]) ** 2 + (cz - at[2]) ** 2) - b.radius
            if d > R:
                continue
            f = 1.0 - (1.0 - s["splash_edge"]) * max(0.0, d) / R
            if b is m:
                self.damage(b, s["splash"] * mult * f * m.st["self_damage_mult"], "mortar", None, at)
            elif m.house and isinstance(b, Mech) and b.house:
                continue
            else:
                self.damage(b, s["splash"] * mult * f, "mortar", m, at)
            if isinstance(b, Mech):
                dx, dy = b.pos[0] - at[0], b.pos[1] - at[1]
                dd = max(0.5, hyp(dx, dy))
                b.vel[0] += s["knock"] * f * dx / dd
                b.vel[1] += s["knock"] * f * dy / dd
                b.vel[2] += s["knock_up"] * f
                b.grounded = False
                b.energy = max(0.0, b.energy - s["drain"] * f)
                if b is not m:
                    b.events.append({"type": "effect", "effect": "concuss", "by": m.id})

    # ---------------------------------------------------------------- physics
    def step_mech(self, m):
        st = m.st
        a = m.action
        mv = a.get("move", [0.0, 0.0]) or [0.0, 0.0]
        ln = hyp(mv[0], mv[1])
        if ln > 1:
            mv = [mv[0] / ln, mv[1] / ln]
        boost = bool(a.get("boost"))
        speed = st["walk_speed"]
        if boost and m.grounded and m.energy > 0 and ln > 0.05:
            speed = st["boost_speed"]
            m.energy -= st["boost_drain"] * DT
            m.spend_t = self.t
        if m.charging():
            speed *= st["charge_move_mult"]
        acc = st["ground_accel"] if m.grounded else st["air_accel"]
        for i in (0, 1):
            dv = mv[i] * speed - m.vel[i]
            lim = acc * DT
            m.vel[i] += max(-lim, min(lim, dv))
        if a.get("jump") and m.grounded:
            m.vel[2] = st["jump_speed"]
            m.grounded = False
        if not m.grounded:
            m.vel[2] -= st["gravity"] * DT
            if boost and m.energy > 0:
                m.vel[2] = min(st["max_rise_speed"], m.vel[2] + st["jet_accel"] * DT)
                m.energy -= st["jet_drain"] * DT
                m.spend_t = self.t
        m.energy = max(0.0, m.energy)
        if self.t - m.spend_t > st["energy_regen_delay"]:
            m.energy = min(st["energy"], m.energy + st["energy_regen"] * DT)
        x, y, z = m.pos
        nx, ny = x + m.vel[0] * DT, y + m.vel[1] * DT
        if (abs(m.vel[0]) > 1e-3 or abs(m.vel[1]) > 1e-3):
            if self.body_blocked(m, x, y, z):
                x, y = nx, ny  # already overlapping geometry (e.g. grew a size step): let it slide out
            elif not self.body_blocked(m, nx, ny, z):
                x, y = nx, ny
            elif not self.body_blocked(m, nx, y, z):
                x, m.vel[1] = nx, 0.0
            elif not self.body_blocked(m, x, ny, z):
                y, m.vel[0] = ny, 0.0
            else:
                m.vel[0] = m.vel[1] = 0.0
        nz = z + m.vel[2] * DT
        sup = self.support(x, y, max(z, nz), st["step_up"])
        if nz <= sup:
            nz = sup
            m.vel[2] = 0.0
            m.grounded = True
        else:
            m.grounded = nz - sup < 0.05
        m.pos = [x, y, nz]
        if m.grounded and hyp(m.vel[0], m.vel[1]) > self.cfg["sound"]["step_speed"]:
            if self.t - getattr(m, "step_t", -9) >= self.cfg["sound"]["step_every"]:
                m.step_t = self.t
                self.sound("boost" if boost else "step", m.pos, m)
        tr, pr = st["turn_rate"] * DT, st["pitch_rate"] * DT
        dy = wrap(m.cmd_aim[0] - m.aim[0])
        m.aim[0] = wrap(m.aim[0] + max(-tr, min(tr, dy)))
        p = max(st["min_pitch"], min(st["max_pitch"], m.cmd_aim[1]))
        m.aim[1] += max(-pr, min(pr, p - m.aim[1]))
        # zone and regen
        z = self.zone
        if hyp(x - z["center"][0], y - z["center"][1]) > z["radius"]:
            self.damage(m, z["dps"] * DT, "zone", None, None)
            m.in_zone = False
        else:
            m.in_zone = True
            if self.t - m.last_combat > st["regen_delay"]:
                m.hp = min(m.max_hp, m.hp + st["regen_per_s"] * DT)

    def push_apart(self):
        ms = [m for m in self.mechs if m.alive]
        for i in range(len(ms)):
            for j in range(i + 1, len(ms)):
                a, b = ms[i], ms[j]
                dx, dy = b.pos[0] - a.pos[0], b.pos[1] - a.pos[1]
                d = hyp(dx, dy)
                r = a.radius + b.radius
                if 1e-6 < d < r:
                    k = (r - d) / 2 / d
                    for m, sg in ((a, -1.0), (b, 1.0)):
                        nx, ny = m.pos[0] + sg * dx * k, m.pos[1] + sg * dy * k
                        if not self.body_blocked(m, nx, ny, m.pos[2]):  # never push anyone into a wall
                            m.pos[0], m.pos[1] = nx, ny

    # ---------------------------------------------------------------- npcs, cubes, drops
    def step_npcs(self):
        npc = self.cfg["npc"]
        for n in self.npcs:
            n.cool = max(0.0, n.cool - DT)
            tgt = None
            if n.aggro is not None and n.aggro.alive and hyp(n.aggro.pos[0] - n.pos[0], n.aggro.pos[1] - n.pos[1]) < npc["deaggro_range"] \
                    and hyp(n.pos[0] - n.home[0], n.pos[1] - n.home[1]) < npc["chase_leash"]:
                tgt = n.aggro
            else:
                n.aggro = None
                if hyp(n.pos[0] - n.home[0], n.pos[1] - n.home[1]) < npc["leash"]:
                    for m in self.mechs:
                        if m.alive and not m.house and hyp(m.pos[0] - n.pos[0], m.pos[1] - n.pos[1]) < npc["aggro_range"]:
                            if self.los((n.pos[0], n.pos[1], n.pos[2] + 3), (m.pos[0], m.pos[1], m.pos[2] + 4.5)):
                                tgt = m
            goal = tgt.pos if tgt is not None else (n.home if hyp(n.pos[0] - n.home[0], n.pos[1] - n.home[1]) > npc["leash"] else n.wander)
            if self.rng.random() < 0.01:
                n.wander = [n.home[0] + self.rng.uniform(-20, 20), n.home[1] + self.rng.uniform(-20, 20), n.home[2]]
            dx, dy = goal[0] - n.pos[0], goal[1] - n.pos[1]
            d = hyp(dx, dy)
            if d > (25 if tgt is not None else 2):
                nx, ny = n.pos[0] + npc["speed"] * DT * dx / d, n.pos[1] + npc["speed"] * DT * dy / d
                if not self.solid(nx, ny, self.w.ground_at(nx, ny) + 1, self.w.ground_at(nx, ny) + 3):
                    n.pos = [nx, ny, self.w.ground_at(nx, ny)]
            if tgt is not None and n.cool <= 0:
                o = (n.pos[0], n.pos[1], n.pos[2] + 2.5)
                tp = (tgt.pos[0], tgt.pos[1], tgt.pos[2] + 4.5)
                if self.los(o, tp):
                    dd = math.sqrt(sum((tp[i] - o[i]) ** 2 for i in range(3)))
                    sp = math.radians(npc["spread_deg"])
                    yaw = math.atan2(tp[1] - o[1], tp[0] - o[0]) + self.rng.gauss(0, sp)
                    pitch = math.asin((tp[2] - o[2]) / max(dd, 0.1)) + self.rng.gauss(0, sp)
                    v = npc["projectile_speed"]
                    self.bullets.append({"by": n, "pos": [o[0] + 2 * math.cos(yaw), o[1] + 2 * math.sin(yaw), o[2]],
                                         "vel": (v * math.cos(pitch) * math.cos(yaw), v * math.cos(pitch) * math.sin(yaw), v * math.sin(pitch)),
                                         "left": 120, "dmg": npc["damage"], "kind": "npc"})
                    n.cool = npc["cooldown"]
                    self.sound("npc_gun", n.pos, None)

    def step_cubes(self):
        cc = self.cfg["cubes"]
        keep = []
        for c in self.cubes:
            best = None
            for m in self.mechs:
                if not m.alive:
                    continue
                d = hyp(m.pos[0] - c.pos[0], m.pos[1] - c.pos[1])
                if d <= cc["pickup_radius"] and abs(m.pos[2] - c.pos[2]) <= 4 and (best is None or d < best[0]):
                    best = (d, m)
            if best is None or best[1].house:
                keep.append(c)
                continue
            self.give_cubes(best[1], 1)
            best[1].events.append({"type": "pickup", "cubes": best[1].cubes})
        self.cubes = keep

    def give_cubes(self, m, n):
        cc = self.cfg["cubes"]
        for _ in range(n):
            old = m.max_hp
            m.cubes += 1
            m.max_hp = m.base_hp * (1 + cc["hp_per_cube"] * min(m.cubes, cc["stat_cap"]))
            m.hp += m.max_hp - old
            if m.cubes in cc["upgrade_thresholds"]:
                m.offers.append(None)

    def make_offer(self, m):
        avail = []
        for u in self.cfg["upgrades"]:
            if m.upgrades.count(u["id"]) >= u["max"]:
                continue
            if u.get("requires") and not m.has(u["requires"]):
                continue
            avail.append(u)
        self.rng.shuffle(avail)
        m.offer_seq += 1
        return {"id": m.offer_seq, "expires": self.t + self.cfg["bot"]["offer_timeout_s"], "choices": avail[:3]}

    def apply_upgrade(self, m, u):
        m.upgrades.append(u["id"])
        for e in u["effects"]:
            path = e["path"].split(".")
            if path[0] == "mech":
                key = path[1]
                if key == "hp":
                    old = m.max_hp
                    m.base_hp *= e.get("mul", 1.0)
                    cc = self.cfg["cubes"]
                    m.max_hp = m.base_hp * (1 + cc["hp_per_cube"] * min(m.cubes, cc["stat_cap"]))
                    m.hp += m.max_hp - old
                    continue
                if "mul" in e:
                    m.st[key] = m.st.get(key, 0.0) * e["mul"]
                if "add" in e:
                    m.st[key] = m.st.get(key, 0.0) + e["add"]
            elif path[0] == "weapons":
                k, key = path[1], path[2]
                if "mul" in e:
                    m.wst[k][key] *= e["mul"]
                if "add" in e:
                    m.wst[k][key] += e["add"]
        m.events.append({"type": "upgrade", "id": u["id"]})

    def step_drops(self):
        dc = self.cfg["drops"]
        while self.drop_sched and self.t >= self.drop_sched[0]:
            self.drop_sched.pop(0)
            z = self.zone
            p = self.open_point(z["next_center"][0], z["next_center"][1], max(5.0, z["next_radius"] * dc.get("spread", 0.7)))
            if p:
                self.drops.append({"id": self.nid("drop"), "pos": [p[0], p[1], self.w.ground_at(*p)], "radius": dc["radius"],
                                   "land_t": self.t + dc["fall_s"], "capture_s": dc["capture_s"], "progress": 0.0, "holder": None})
        keep = []
        for d in self.drops:
            if self.t < d["land_t"]:
                keep.append(d)
                continue
            inside = [m for m in self.mechs if m.alive and hyp(m.pos[0] - d["pos"][0], m.pos[1] - d["pos"][1]) <= d["radius"]]
            if len(inside) == 1:
                if d["holder"] is not inside[0]:
                    d["holder"], d["progress"] = inside[0], 0.0
                d["progress"] += DT
                if d["progress"] >= d["capture_s"]:
                    m = inside[0]
                    self.give_cubes(m, dc["cubes"])
                    m.hp = m.max_hp
                    m.energy = m.st["energy"]
                    m.events.append({"type": "drop", "id": d["id"], "cubes": m.cubes})
                    continue
            elif not inside:
                d["holder"], d["progress"] = None, 0.0
            keep.append(d)
        self.drops = keep

    # ---------------------------------------------------------------- actions
    def apply_one_shots(self, m):
        a = m.action
        if "aim" in a and a["aim"]:
            m.cmd_aim = [float(a["aim"][0]), float(a["aim"][1])]
        if m.offers and a.get("pick") is not None and isinstance(m.offers[0], dict):
            i = int(a["pick"])
            ch = m.offers[0]["choices"]
            if 0 <= i < len(ch):
                self.apply_upgrade(m, ch[i])
                m.offers.pop(0)
        if a.get("take") is not None:
            pid = a["take"]
            pk = next((p for p in self.pickups if p.id == pid), None)
            reason = None
            if pk is None:
                reason = "gone"
            elif hyp(pk.pos[0] - m.pos[0], pk.pos[1] - m.pos[1]) > self.cfg["loot"]["pickup_radius"] or abs(pk.pos[2] - m.pos[2]) > 4:
                reason = "range"
            elif m.has(pk.kind):
                reason = "have"
            else:
                slot = a.get("drop")
                if slot is None:
                    slot = next((i for i, w in enumerate(m.slots) if w is None), None)
                if slot is None:
                    reason = "full"
                else:
                    slot = int(slot)
                    old = m.slots[slot]
                    if old is not None and old["kind"] == "laser" and old["charge"] > 0:
                        reason = "busy"
                    else:
                        if old is not None:
                            np_ = Thing(self.nid("pickup"), old["kind"], m.pos)
                            self.pickups.append(np_)
                            m.events.append({"type": "dropped", "id": np_.id, "kind": old["kind"], "slot": slot})
                        w = m.new_weapon(pk.kind)
                        w["equip"] = max(self.cfg["loot"]["equip_s"], m.wst[pk.kind].get("cooldown", 0))
                        m.slots[slot] = w
                        self.pickups.remove(pk)
                        m.events.append({"type": "took", "id": pk.id, "kind": pk.kind, "slot": slot})
            if reason:
                m.events.append({"type": "take_failed", "id": pid, "reason": reason})

    # ---------------------------------------------------------------- observation for our bot
    def obs_for(self, m):
        mechs = []
        mem = self.cfg["vision"]["memory_s"]
        for o in self.mechs:
            if o is m or not o.alive:
                continue
            if self.sees(m, o.pos, o.height):
                m.seen[o.id] = self.t
            if self.t - m.seen.get(o.id, -99) <= mem:
                mechs.append({"id": o.id, "kind": "mech", "pos": list(o.pos), "vel": list(o.vel), "aim": list(o.aim),
                              "hp": o.hp, "max_hp": o.max_hp, "cubes": o.cubes, "weapons": o.held(), "charging": o.charging()})
        for n in self.npcs:
            if self.sees(m, n.pos, n.height):
                m.seen[n.id] = self.t
            if self.t - m.seen.get(n.id, -99) <= mem:
                mechs.append({"id": n.id, "kind": "npc", "pos": list(n.pos), "vel": [0, 0, 0], "aim": [0, 0], "hp": n.hp, "max_hp": self.cfg["npc"]["hp"]})
        vis = lambda th: self.sees(m, th.pos, th.height)  # noqa: E731
        weapons = []
        for w in m.slots:
            if w is None:
                weapons.append(None)
                continue
            ww = {"kind": w["kind"], "cooldown": max(w["cooldown"], w["equip"]), "stats": dict(m.wst[w["kind"]])}
            if w["kind"] == "gun":
                ww["ammo"], ww["reload"] = w["ammo"], w["reload"]
            if w["kind"] == "laser":
                ww["charge"] = w["charge"]
            weapons.append(ww)
        if m.offers and m.offers[0] is None:
            m.offers[0] = self.make_offer(m)
        offer = None
        if m.offers:
            of = m.offers[0]
            if self.t > of["expires"]:
                if of["choices"]:
                    self.apply_upgrade(m, of["choices"][0])
                m.offers.pop(0)
            else:
                offer = {"id": of["id"], "expires": of["expires"], "choices": [{"id": c["id"], "name": c["name"], "desc": c["desc"]} for c in of["choices"]]}
        return {
            "type": "obs", "tick": self.tick, "t": round(self.t, 2), "alive": sum(1 for x in self.mechs if x.alive),
            "self": {"id": m.id, "pos": list(m.pos), "vel": list(m.vel), "aim": list(m.aim), "hp": m.hp, "max_hp": m.max_hp,
                     "energy": m.energy, "max_energy": m.st["energy"], "grounded": m.grounded, "cubes": m.cubes,
                     "upgrades": list(m.upgrades), "in_zone": getattr(m, "in_zone", True), "weapons": weapons, "stats": m.stats_obs()},
            "mechs": mechs,
            "projectiles": [{"id": i, "kind": b["kind"], "pos": list(b["pos"]), "vel": list(b["vel"])} for i, b in enumerate(self.bullets) if b["by"] is not m and hyp(b["pos"][0] - m.pos[0], b["pos"][1] - m.pos[1]) < 200]
            + [{"id": 900000 + i, "kind": "mortar", "pos": list(s["pos"]), "vel": list(s["vel"])} for i, s in enumerate(self.shells) if s["by"] is not m],
            "crates": [{"id": c.id, "pos": list(c.pos), "hp": c.hp} for c in self.crates if hyp(c.pos[0] - m.pos[0], c.pos[1] - m.pos[1]) < 210 and vis(c)],
            "cubes": [{"id": c.id, "pos": list(c.pos)} for c in self.cubes if hyp(c.pos[0] - m.pos[0], c.pos[1] - m.pos[1]) < 210 and vis(c)],
            "pickups": [{"id": p.id, "kind": p.kind, "pos": list(p.pos)} for p in self.pickups if hyp(p.pos[0] - m.pos[0], p.pos[1] - m.pos[1]) < 210 and vis(p)],
            "sounds": m.sounds, "beams": [{k: v for k, v in b.items() if k != "t"} for b in self.beams if b["by"] != m.id],
            "events": m.events,
            "drops": [{"id": d["id"], "pos": list(d["pos"]), "radius": d["radius"], "lands_in": max(0.0, d["land_t"] - self.t),
                       "capture_s": d["capture_s"], "progress": d["progress"], "holder": d["holder"].id if d["holder"] else None} for d in self.drops],
            "zone": dict(self.zone), "offer": offer,
        }


# ---------------------------------------------------------------------------------------------- house squad
class Squad:
    def __init__(self, arena, us, params, rng):
        self.A, self.us, self.P, self.rng = arena, us, params, rng
        self.members = [m for m in arena.mechs if m.house]
        self.order = sorted(self.members, key=lambda m: LEVELS.index(m.level) + rng.random() * 0.1)
        for m in self.members:
            sk = dict(params["skill"][m.level])
            for k in ("noise", "ltol", "lag"):
                sk[k] *= params.get("skill_scale", 1.0)
            m.brain = HouseBrain(arena, m, us, sk, params, rng)
            r = rng.random()
            acc = 0.0
            for k, p in params["loadout"].items():
                acc += p
                if r <= acc:
                    m.slots[1] = m.new_weapon(k)
                    break
        self.known = None
        self.known_t = -99.0
        self.hist = []

    def update(self):
        A, us = self.A, self.us
        self.hist.append((A.t, list(us.pos), list(us.vel)))
        if len(self.hist) > 40:
            self.hist.pop(0)
        for m in self.members:
            if m.alive and A.sees(m, us.pos, us.height):
                self.known, self.known_t = list(us.pos), A.t
                break
        alive = [m for m in self.order if m.alive]
        n = int(self.P["hunters_start"] + self.P["hunters_per_kill"] * us.kills)
        if A.t >= self.P["all_hunt_t"]:
            n = len(alive)
        hunters = set(id(m) for m in alive[:n])
        for m in alive:
            m.brain.hunter = id(m) in hunters

    def past(self, lag):
        for t, p, v in reversed(self.hist):
            if t <= self.A.t - lag:
                return p, v
        return self.hist[0][1], self.hist[0][2]


class HouseBrain:
    def __init__(self, arena, me, us, skill, params, rng):
        self.A, self.me, self.us, self.sk, self.P, self.rng = arena, me, us, skill, params, rng
        self.follow = Follower(arena.nav)
        self.hunter = False
        self.side = 1 if rng.random() < 0.5 else -1
        self.flip = 0.0
        self.wander = None
        self.wander_t = -99.0
        self.engaged_t = -99.0
        self.seen_t = -99.0

    def decide(self, squad):
        A, me, us = self.A, self.me, self.us
        a = {"fire": [False, False]}
        z = A.zone
        x, y, zz = me.pos
        if A.sees(me, us.pos, us.height):
            self.seen_t = A.t
        if any(e["type"] == "damaged" and e.get("by") == us.id for e in me.events):
            self.engaged_t = A.t
        me.events = []
        me.sounds = []
        d_us = hyp(us.pos[0] - x, us.pos[1] - y) if us.alive else 1e9
        fight = us.alive and (self.hunter or (A.t - self.seen_t < 1.0 and d_us < self.P["engage_range"]) or A.t - self.engaged_t < self.P["retaliate_s"])
        # where to stand: inside the circle we will need
        safe_c, safe_r = (z["next_center"], z["next_radius"]) if A.t > z["shrink_start"] - 20 else (z["center"], z["radius"])
        dz = hyp(x - safe_c[0], y - safe_c[1])
        zone_urgent = dz > safe_r * 0.9 - 5 or not getattr(me, "in_zone", True)
        tp, tv = squad.past(self.sk["lag"])
        know = self.P["omniscient"] or A.t - squad.known_t < 10
        if not self.P["omniscient"] and squad.known is not None:
            tp = squad.known if A.t - squad.known_t > self.sk["lag"] else tp
        d = hyp(tp[0] - x, tp[1] - y)
        to = math.atan2(tp[1] - y, tp[0] - x)
        eye = (x, y, zz + 7 * me.size)
        vis = A.los(eye, (tp[0], tp[1], tp[2] + 4.5)) or A.los(eye, (tp[0], tp[1], tp[2] + 8.5))
        vis = vis and d < 200
        second = me.slots[1]["kind"] if me.slots[1] else "gun"
        want = self.P["fight_range"][second]
        if A.t > self.flip:
            self.side = -self.side
            self.flip = A.t + self.rng.uniform(0.8, 2.4)
        move_to = None
        if zone_urgent:
            move_to = (safe_c[0], safe_c[1])
        elif fight and know:
            if not vis or d > want * 1.25:
                move_to = (tp[0], tp[1])
        else:
            if self.wander is None or A.t - self.wander_t > 25 or hyp(self.wander[0] - safe_c[0], self.wander[1] - safe_c[1]) > safe_r * 0.8:
                p = A.open_point(safe_c[0], safe_c[1], max(10.0, safe_r * 0.6), tries=40)
                self.wander = p or safe_c
                self.wander_t = A.t
            if hyp(self.wander[0] - x, self.wander[1] - y) > 6:
                move_to = self.wander
        if move_to is not None:
            yaw = self.follow.direction(x, y, move_to[0], move_to[1], A.t, 0.004)
            if yaw is None:
                yaw = math.atan2(move_to[1] - y, move_to[0] - x)
            spd = 1.0 if (fight or zone_urgent) else 0.6
            a["move"] = [math.cos(yaw) * spd, math.sin(yaw) * spd]
            a["boost"] = zone_urgent and me.energy > 40
        elif fight and vis:
            s = self.sk["strafe"]
            rad = 0.0 if abs(d - want) < 25 else (0.6 if d > want else -0.6)
            a["move"] = [math.cos(to) * rad - math.sin(to) * self.side * s, math.sin(to) * rad + math.cos(to) * self.side * s]
        else:
            a["move"] = [0.0, 0.0]
        # aim and fire
        if fight and know and d < 260:
            tcz = tp[2] + 4.5 * us.size
            mz = zz + 7 * me.size
            direct = (to + self.rng.gauss(0, self.sk["noise"]), math.atan2(tcz - mz, max(d, 1)))
            aim = direct
            gun = me.slots[0]
            if vis and d < 225:
                tt = d / me.wst["gun"]["speed"]
                lx, ly = tp[0] + tv[0] * tt, tp[1] + tv[1] * tt
                gaim = (math.atan2(ly - y, lx - x) + self.rng.gauss(0, self.sk["noise"]), direct[1])
                aim = gaim
                if abs(wrap(gaim[0] - me.aim[0])) < 0.02 and A.t - self.seen_t < 3:
                    a["fire"][0] = True
            w = me.slots[1]
            if w is not None:
                k = w["kind"]
                if k == "laser":
                    full = me.wst["laser"]["charge"]
                    if w["charge"] > 0 and w["charge"] >= full - 0.15:
                        aim = direct
                    if w["cooldown"] <= 0 and vis:
                        a["fire"][1] = True
                        if w["charge"] >= full - 0.01 and abs(wrap(direct[0] - me.aim[0])) < self.sk["ltol"]:
                            a["fire"][1] = False
                    elif w["charge"] > 0:
                        a["fire"][1] = True
                elif k == "lightning":
                    if d < me.wst["lightning"]["range"] * 0.95 and vis and me.energy > 8:
                        aim = (to, aim[1])
                        if abs(wrap(to - me.aim[0])) < 0.3:
                            a["fire"][1] = True
                elif k == "mortar":
                    ms = me.wst["mortar"]
                    if 25 < d < 225 and w["cooldown"] <= 0.2:
                        v, g = ms["speed"], ms["gravity"]
                        R = max(1.0, d - 3)
                        dh = tp[2] + 1 - (zz + me.height + 0.5)
                        disc = v ** 4 - g * (g * R * R + 2 * dh * v * v)
                        if disc > 0:
                            th = math.atan((v * v + (0 if vis else 1) * math.sqrt(disc) - (1 if vis else 0) * math.sqrt(disc)) / (g * R))
                            tof = R / (v * math.cos(th))
                            lx, ly = tp[0] + tv[0] * tof, tp[1] + tv[1] * tof
                            maim = (math.atan2(ly - y, lx - x) + self.rng.gauss(0, self.sk["noise"] * 2), th + self.rng.gauss(0, self.sk["noise"] * 2))
                            if gun.get("reload", 0) > 0 or not vis or d > 160:
                                aim = maim
                                if abs(wrap(maim[0] - me.aim[0])) < 0.02 and abs(maim[1] - me.aim[1]) < 0.03:
                                    a["fire"][1] = True
            a["aim"] = list(aim)
        else:
            a["aim"] = [me.aim[0] + 0.6, 0.0] if self.rng.random() < 0.3 else list(me.cmd_aim)
        return a


class StrongBrain:
    """A hunter that runs a real bot's act() against us only (always told where we are), plus aim noise."""

    def __init__(self, arena, me, us, mod, hello, noise, rng):
        self.A, self.me, self.us, self.noise, self.rng = arena, me, us, noise, rng
        h = dict(hello)
        h["slot"], h["seed"] = me.id, rng.randrange(1 << 30)
        import io
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            self.bot = mod.Bot(h)

    def decide(self, squad):
        A, me, us = self.A, self.me, self.us
        if self.A.P["omniscient"] or A.t - squad.known_t < 10:
            me.seen[us.id] = A.t  # shared squad knowledge
        o = A.obs_for(me)
        o["mechs"] = [m for m in o["mechs"] if m["id"] == us.id or m["kind"] == "npc"]
        o["alive"] = 16
        me.events, me.sounds = [], []
        mf.draw.clear()
        try:
            a = self.bot.act(o) or {}
        except Exception:
            a = {}
        if "aim" in a:
            a["aim"] = [a["aim"][0] + self.rng.gauss(0, self.noise), a["aim"][1] + self.rng.gauss(0, self.noise)]
        a.pop("pick", None)
        return a


# ---------------------------------------------------------------------------------------------- match
def play_match(mod, world, nav, hello, seed, params, max_t=480.0, strong_mod=None):
    rng = random.Random(seed)
    cfg = hello["config"]
    A = Arena(world, nav, cfg, rng, params)
    our_slot = rng.randrange(16)
    A.populate(our_slot)
    us = A.mechs[our_slot]
    squad = Squad(A, us, params, rng)
    A.squad = squad
    if strong_mod is not None:
        for m in squad.members:
            if m.level in params["strong_levels"]:
                m.strong = StrongBrain(A, m, us, strong_mod, hello, params["strong_noise"][m.level], rng)
    h = dict(hello)
    h["slot"] = our_slot
    h["seed"] = seed
    bot = mod.Bot(h)
    times = []
    trail = []
    place = None
    A.zone = A.zone_now()
    while A.t < max_t:
        A.zone = A.zone_now()
        alive_before = sum(1 for m in A.mechs if m.alive)
        o = A.obs_for(us)
        us.events, us.sounds = [], []
        mf.draw.clear()
        t0 = time.perf_counter()
        try:
            act = bot.act(o) or {}
        except Exception:
            import traceback
            traceback.print_exc()
            act = {}
        times.append((time.perf_counter() - t0) * 1000)
        lab = (mf.draw.take() or {}).get("label", "")
        if A.tick % 20 == 0:
            z = A.zone
            trail.append((round(A.t), lab[:14], round(us.pos[0]), round(us.pos[1]), round(us.pos[2]), round(us.hp),
                          round(hyp(us.pos[0] - z["center"][0], us.pos[1] - z["center"][1]) - z["radius"]),
                          round(hyp(us.pos[0] - z["next_center"][0], us.pos[1] - z["next_center"][1]) - z["next_radius"])))
            trail = trail[-50:]
        us.action = act
        squad.update()
        for m in squad.members:
            if m.alive:
                sb = getattr(m, "strong", None)
                knows = params["omniscient"] or A.t - squad.known_t < 10
                m.action = sb.decide(squad) if (sb is not None and m.brain.hunter and knows) else m.brain.decide(squad)
        for m in A.mechs:
            if m.alive:
                A.apply_one_shots(m)
        for m in A.mechs:
            if m.alive:
                A.step_weapons(m)
        for _ in range(2):
            for m in A.mechs:
                if m.alive:
                    A.step_mech(m)
            A.push_apart()
            A.step_projectiles()
            A.t += DT
            A.tick += 1
            A.zone = A.zone_now()
        A.step_npcs()
        A.step_cubes()
        A.step_drops()
        A.beams = [b for b in A.beams if A.t - b["t"] < 0.15]
        if not us.alive:
            place = alive_before
            break
        if sum(1 for m in A.mechs if m.alive) == 1:
            place = 1
            break
    if place is None:  # time out: rank survivors by HP
        alive = [m for m in A.mechs if m.alive]
        place = 1 + sum(1 for m in alive if m is not us and m.hp > us.hp)
    times.sort()
    return {
        "score": 16 - place, "place": place, "kills": us.kills, "t": round(A.t, 1), "cause": us.cause,
        "cubes": us.cubes, "weapons": us.held(), "upgrades": us.upgrades,
        "hunters_at_end": sum(1 for m in squad.members if m.alive and m.brain.hunter),
        "taken": {k: round(v) for k, v in us.taken_by.items()},
        "trail": trail if us.cause == "zone" else None,
        "p99_ms": round(times[int(len(times) * 0.99)], 2) if times else 0.0, "max_ms": round(times[-1], 2) if times else 0.0,
    }


def play_ffa(mods, world, nav, hello, seed, params, max_t=480.0):
    """16-bot free-for-all: mods[i] pilots slot i. Returns 16 - place for every slot."""
    rng = random.Random(seed)
    cfg = hello["config"]
    A = Arena(world, nav, cfg, rng, params)
    A.populate(-1)
    bots = []
    for i, m in enumerate(A.mechs):
        m.house, m.level = False, None
        h = dict(hello)
        h["slot"], h["seed"] = i, seed * 31 + i
        bots.append(mods[i].Bot(h))
    place = [None] * 16
    A.zone = A.zone_now()
    while A.t < max_t:
        A.zone = A.zone_now()
        alive_before = sum(1 for m in A.mechs if m.alive)
        for i, m in enumerate(A.mechs):
            if not m.alive:
                continue
            o = A.obs_for(m)
            m.events, m.sounds = [], []
            mf.draw.clear()
            try:
                m.action = bots[i].act(o) or {}
            except Exception:
                m.action = {}
        for m in A.mechs:
            if m.alive:
                A.apply_one_shots(m)
        for m in A.mechs:
            if m.alive:
                A.step_weapons(m)
        for _ in range(2):
            for m in A.mechs:
                if m.alive:
                    A.step_mech(m)
            A.push_apart()
            A.step_projectiles()
            A.t += DT
            A.tick += 1
            A.zone = A.zone_now()
        A.step_npcs()
        A.step_cubes()
        A.step_drops()
        A.beams = [b for b in A.beams if A.t - b["t"] < 0.15]
        for i, m in enumerate(A.mechs):
            if not m.alive and place[i] is None:
                place[i] = alive_before
        if sum(1 for m in A.mechs if m.alive) <= 1:
            break
    alive = [m for m in A.mechs if m.alive]
    for i, m in enumerate(A.mechs):
        if place[i] is None:
            place[i] = 1 + sum(1 for o in alive if o is not m and o.hp > m.hp)
    return {"scores": [16 - p for p in place], "kills": [m.kills for m in A.mechs], "t": round(A.t, 1)}


# ---------------------------------------------------------------------------------------------- parallel runner
_W = {}


def _init():
    hello = json.loads(gzip.open(os.path.join(KIT, "check", "stream.jsonl.gz"), "rt").readline())
    hello["map"]["dir"] = os.path.join(KIT, "arena")
    _W["hello"] = hello
    _W["world"] = World(hello["map"]["dir"], hello["config"])
    _W["nav"] = NavGrid(_W["world"])
    _W["mods"] = {}
    _W["canopy"] = PC.canopy_grid(_W["world"], json.load(open(os.path.join(hello["map"]["dir"], "v2", "world.json"))))


def _mod(spec, sets):
    key = spec + "|" + json.dumps(sets, sort_keys=True)
    if key not in _W["mods"]:
        mod = load_bot_module(spec)
        for k, v in sets.items():
            setattr(mod, k, v)
        _W["mods"][key] = mod
    return _W["mods"][key]


def _ffa_job(args):
    lineup, seed, params = args  # lineup: 16 (spec, sets)
    rng = random.Random(seed)
    order = list(range(16))
    rng.shuffle(order)
    mods = [_mod(*lineup[order[i]]) for i in range(16)]
    import io
    import contextlib
    with contextlib.redirect_stderr(io.StringIO()):
        r = play_ffa(mods, _W["world"], _W["nav"], _W["hello"], seed, params)
    return {"by_entry": [(order[i], r["scores"][i], r["kills"][i]) for i in range(16)], "t": r["t"]}


def _job(args):
    spec, sets, seed, params = args
    import io
    import contextlib
    with contextlib.redirect_stderr(io.StringIO()):
        strong = _mod(params["strong_bot"], {}) if params.get("strong_levels") else None
        r = play_match(_mod(spec, sets), _W["world"], _W["nav"], _W["hello"], seed, params, strong_mod=strong)
    r["seed"] = seed
    return r


def parse_spec(s):
    """'v1+NAME=VAL+NAME2=VAL2' -> ('v1', {NAME: VAL, ...}) with VAL as bool/int/float/str."""
    name, *kvs = s.split("+")
    sets = {}
    for kv in kvs:
        k, _, v = kv.partition("=")
        if v in ("True", "False"):
            sets[k] = v == "True"
        else:
            try:
                sets[k] = int(v)
            except ValueError:
                try:
                    sets[k] = float(v)
                except ValueError:
                    sets[k] = v
    return name, sets


def summarize(label, res):
    n = len(res)
    sc = [r["score"] for r in res]
    mean = sum(sc) / n
    sd = math.sqrt(sum((s - mean) ** 2 for s in sc) / max(1, n - 1))
    causes = {}
    for r in res:
        causes[r["cause"] or "alive"] = causes.get(r["cause"] or "alive", 0) + 1
    print(f"{label:34s} score {mean:5.2f} +-{sd / math.sqrt(n):.2f}  kills {sum(r['kills'] for r in res) / n:4.1f}  "
          f"death t {sum(r['t'] for r in res) / n:4.0f}s  cubes {sum(r['cubes'] for r in res) / n:4.1f}  "
          f"killed by {dict(sorted(causes.items(), key=lambda kv: -kv[1]))}  max tick {max(r['max_ms'] for r in res):.0f} ms", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bots", default="starter,v0,v1")
    ap.add_argument("--matches", type=int, default=48)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--params", default="{}", help="JSON merged into PARAMS")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--dump", default=None, help="write all per-match results to this JSON file")
    ap.add_argument("--ffa", default=None, help="free-for-all lineup, e.g. 'v1:4,v0:4,starter:8' (16 total)")
    args = ap.parse_args()
    params = json.loads(json.dumps(PARAMS))
    for k, v in json.loads(args.params).items():
        if isinstance(v, dict) and isinstance(params.get(k), dict):
            for k2, v2 in v.items():
                if isinstance(v2, dict):
                    params[k][k2].update(v2)
                else:
                    params[k][k2] = v2
        else:
            params[k] = v
    if args.ffa:
        lineup = []
        for part in args.ffa.split(","):
            sp, _, n = part.rpartition(":")
            lineup += [parse_spec(sp)] * int(n)
        assert len(lineup) == 16, len(lineup)
        t0 = time.time()
        with Pool(args.workers, initializer=_init) as pool:
            res = pool.map(_ffa_job, [(lineup, args.seed * 100000 + i, params) for i in range(args.matches)], chunksize=1)
        agg = {}
        for r in res:
            for idx, sc, k in r["by_entry"]:
                name = args.ffa.split(",")[0] if False else None
                label = lineup[idx][0] + "".join(f"+{a}={b}" for a, b in lineup[idx][1].items())
                agg.setdefault(label, []).append((sc, k))
        for label, v in agg.items():
            n = len(v)
            mean = sum(x[0] for x in v) / n
            sd = math.sqrt(sum((x[0] - mean) ** 2 for x in v) / max(1, n - 1))
            print(f"FFA {label:34s} mean score {mean:5.2f} +-{sd / math.sqrt(n):.2f}  kills {sum(x[1] for x in v) / n:.2f}  (n={n})")
        print(f"({len(res)} matches in {time.time() - t0:.0f} s)")
        return
    specs = [parse_spec(s) for s in args.bots.split(",")]
    jobs = [(name, sets, args.seed * 100000 + i, params) for name, sets in specs for i in range(args.matches)]
    t0 = time.time()
    with Pool(args.workers, initializer=_init) as pool:
        res = pool.map(_job, jobs, chunksize=1)
    out = {}
    for (name, sets), i in zip([(n, s) for n, s in specs for _ in range(args.matches)], range(len(res))):
        out.setdefault(name + "".join(f"+{k}={v}" for k, v in sets.items()), []).append(res[i])
    for label, rs in out.items():
        summarize(label, rs)
    print(f"({len(res)} matches in {time.time() - t0:.0f} s)")
    if args.dump:
        with open(args.dump, "w") as f:
            json.dump(out, f)


if __name__ == "__main__":
    main()
