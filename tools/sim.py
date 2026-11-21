"""Small approximate simulator for duels on the real map. Not the server: use it to find bugs and big effects.

Models: ground/air acceleration, walls and roofs from the World grids, jump and jet with energy, torso slew,
gun bullets with spread and travel time, laser charge/hold/release (hitscan), rear armor, regen, vision
(field of view, near range, line of sight, 5 s memory), gun and laser sounds. Not modeled: lightning, mortar,
loot, crates, drones, cubes, zone shrink, canopy.

    python -I tools/sim.py --bots v1,v0 --hunters 1 --difficulty easy --matches 20
"""
import argparse
import importlib.util
import json
import gzip
import math
import os
import random
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
KIT = os.path.join(os.path.dirname(ROOT), "mechfight-kit")
sys.path.insert(0, ROOT)
import mechfight as mf  # noqa: E402
from world import World  # noqa: E402
from nav import NavGrid, Follower  # noqa: E402

TAU = 2 * math.pi
DT = 0.05


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def load_bot_module(spec):
    """'v1' = working tree bot.py; any other name = bot.py at that git revision (tag or hash)."""
    if spec == "v1" or spec == "work":
        path = os.path.join(ROOT, "bot.py")
    else:
        rev = {"v0": "333fb36"}.get(spec, spec)
        src = subprocess.run(["git", "-C", ROOT, "show", f"{rev}:bot.py"], capture_output=True, text=True, check=True).stdout
        d = tempfile.mkdtemp(prefix=f"bot_{spec}_")
        path = os.path.join(d, "bot.py")
        with open(path, "w") as f:
            f.write(src)
    name = f"bot_{spec}_{abs(hash(path)) % 10000}"
    s = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return mod


class Mech:
    def __init__(self, mid, pos, cfg, weapons):
        m = cfg["mech"]
        self.id = mid
        self.cfg = cfg
        self.pos = list(pos)
        self.vel = [0.0, 0.0, 0.0]
        self.aim = [0.0, 0.0]
        self.cmd_aim = [0.0, 0.0]
        self.hp = float(m["hp"])
        self.max_hp = float(m["hp"])
        self.energy = float(m["energy"])
        self.spend_t = -99.0
        self.grounded = True
        self.alive = True
        self.last_dmg = -99.0
        self.weapons = []
        for k in weapons:
            if k is None:
                self.weapons.append(None)
                continue
            w = {"kind": k, "cooldown": 0.0, "stats": dict(cfg["weapons"][k])}
            if k == "gun":
                w["ammo"] = w["stats"]["mag"]
                w["reload"] = 0.0
            if k == "laser":
                w["charge"] = 0.0
                w["_full_t"] = 0.0
            self.weapons.append(w)
        self.action = {}
        self.fire_prev = [False, False]
        self.events = []
        self.sounds = []
        self.seen = {}  # other id -> last time seen
        self.dmg_dealt = 0.0
        self.shots = 0
        self.hits = 0
        self.stuck_t = 0.0

    def stats(self):
        s = dict(self.cfg["mech"])
        s["eye_height"] = 8.0
        s["muzzle_height"] = 7.0
        return s

    def charging(self):
        return any(w and w["kind"] == "laser" and w["charge"] > 0 for w in self.weapons)


class Sim:
    def __init__(self, world, cfg, rng):
        self.w = world
        self.cfg = cfg
        self.rng = rng
        self.t = 0.0
        self.tick = 0
        self.mechs = []
        self.bullets = []
        self.beams = []
        self.zone = {"center": [0.0, 0.0], "radius": 2000.0, "next_center": [0.0, 0.0], "next_radius": 1500.0,
                     "shrink_start": 1e6, "shrink_end": 1e6 + 1, "dps": 20}

    # ------------------------------------------------------------------ geometry
    def solid_at(self, x, y, z0, z1):
        w = self.w
        k = w.idx(x, y)
        if k < 0:
            return True
        return (w.top[k] > z0 and w.base[k] < z1) or (w.btop[k] > z0 and w.bbase[k] < z1)

    def body_blocked(self, x, y, z):
        su, h = self.cfg["mech"]["step_up"], self.cfg["mech"]["height"]
        for a in range(8):
            px, py = x + 2.4 * math.cos(a * TAU / 8), y + 2.4 * math.sin(a * TAU / 8)
            if self.solid_at(px, py, z + su, z + h):
                return True
            if self.w.ground_at(px, py) > z + su:
                return True
        return self.solid_at(x, y, z + su, z + h)

    def support(self, x, y, z):
        w = self.w
        g = w.ground_at(x, y)
        k = w.idx(x, y)
        best = g
        if k >= 0:
            for top in (w.top[k], w.btop[k]):
                if g < top <= z + self.cfg["mech"]["step_up"] + 0.05:
                    best = max(best, top)
        return best

    # ------------------------------------------------------------------ physics
    def step_mech(self, m):
        mc = self.cfg["mech"]
        a = m.action
        mv = a.get("move", [0.0, 0.0])
        ln = math.hypot(mv[0], mv[1])
        if ln > 1:
            mv = [mv[0] / ln, mv[1] / ln]
        boost = a.get("boost", False)
        speed = mc["walk_speed"]
        if boost and m.grounded and m.energy > 0 and ln > 0.05:
            speed = mc["boost_speed"]
            m.energy -= mc["boost_drain"] * DT
            m.spend_t = self.t
        if m.charging():
            speed *= mc["charge_move_mult"]
        acc = mc["ground_accel"] if m.grounded else mc["air_accel"]
        for i in (0, 1):
            want = mv[i] * speed
            dv = want - m.vel[i]
            lim = acc * DT
            m.vel[i] += max(-lim, min(lim, dv))
        if a.get("jump") and m.grounded:
            m.vel[2] = mc["jump_speed"]
            m.grounded = False
        if not m.grounded:
            m.vel[2] -= mc["gravity"] * DT
            if boost and m.energy > 0:
                m.vel[2] = min(mc["max_rise_speed"], m.vel[2] + mc["jet_accel"] * DT)
                m.energy -= mc["jet_drain"] * DT
                m.spend_t = self.t
        m.energy = max(0.0, m.energy)
        if self.t - m.spend_t > mc["energy_regen_delay"]:
            m.energy = min(mc["energy"], m.energy + mc["energy_regen"] * DT)
        x, y, z = m.pos
        nx, ny = x + m.vel[0] * DT, y + m.vel[1] * DT
        if not self.body_blocked(nx, ny, z):
            x, y = nx, ny
        elif not self.body_blocked(nx, y, z):
            x, m.vel[1] = nx, 0.0
        elif not self.body_blocked(x, ny, z):
            y, m.vel[0] = ny, 0.0
        else:
            m.vel[0] = m.vel[1] = 0.0
        nz = z + m.vel[2] * DT
        sup = self.support(x, y, max(z, nz))
        if nz <= sup:
            nz = sup
            m.vel[2] = 0.0
            m.grounded = True
        else:
            m.grounded = nz - sup < 0.05
        m.pos = [x, y, nz]
        # torso slew
        tr, pr = mc["turn_rate"] * DT, mc["pitch_rate"] * DT
        dy = wrap(m.cmd_aim[0] - m.aim[0])
        m.aim[0] = wrap(m.aim[0] + max(-tr, min(tr, dy)))
        dp = m.cmd_aim[1] - m.aim[1]
        m.aim[1] += max(-pr, min(pr, dp))
        # regen
        if self.t - m.last_dmg > mc["regen_delay"]:
            m.hp = min(m.max_hp, m.hp + mc["regen_per_s"] * DT)

    def muzzle(self, m):
        yaw, pitch = m.aim
        d = (math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch))
        o = (m.pos[0] + 3.0 * d[0], m.pos[1] + 3.0 * d[1], m.pos[2] + 7.0 + 3.0 * d[2])
        return o, d

    def step_weapons(self, m):
        fire = m.action.get("fire", [False, False])
        for i, w in enumerate(m.weapons):
            if w is None:
                continue
            w["cooldown"] = max(0.0, w["cooldown"] - DT)
            held = bool(fire[i]) if i < len(fire) else False
            s = w["stats"]
            if w["kind"] == "gun":
                if w["reload"] > 0:
                    w["reload"] = max(0.0, w["reload"] - DT)
                    if w["reload"] == 0:
                        w["ammo"] = s["mag"]
                if m.action.get("reload") and w["reload"] == 0 and w["ammo"] < s["mag"]:
                    w["reload"] = s["reload"]
                if held and w["cooldown"] <= 0 and w["ammo"] > 0 and w["reload"] == 0:
                    o, d = self.muzzle(m)
                    sp = math.radians(s["spread_deg"])
                    yaw = m.aim[0] + self.rng.gauss(0, sp)
                    pitch = m.aim[1] + self.rng.gauss(0, sp)
                    v = (s["speed"] * math.cos(pitch) * math.cos(yaw), s["speed"] * math.cos(pitch) * math.sin(yaw), s["speed"] * math.sin(pitch))
                    self.bullets.append({"by": m.id, "pos": list(o), "vel": v, "left": s["range"], "dmg": s["damage"]})
                    w["ammo"] -= 1
                    w["cooldown"] = s["cooldown"]
                    m.shots += 1
                    self.sound("gun", m)
                    if w["ammo"] == 0:
                        w["reload"] = s["reload"]
            elif w["kind"] == "laser":
                if w["cooldown"] > 0:
                    w["charge"] = 0.0
                    continue
                if held:
                    if w["charge"] == 0:
                        self.sound("laser_charge", m)
                    w["charge"] = min(s["charge"], w["charge"] + DT)
                    if w["charge"] >= s["charge"]:
                        w["_full_t"] += DT
                        if w["_full_t"] >= s["hold_s"]:
                            self.fire_laser(m, w)
                elif w["charge"] > 0:
                    if w["charge"] >= s["min_charge"]:
                        self.fire_laser(m, w)
                    else:
                        w["charge"] += DT  # fires once min_charge is in
                        if w["charge"] >= s["min_charge"]:
                            self.fire_laser(m, w)

    def fire_laser(self, m, w):
        s = w["stats"]
        frac = s["min_frac"] + (1 - s["min_frac"]) * min(1.0, w["charge"] / s["charge"])
        dmg = s["damage"] * frac
        o, d = self.muzzle(m)
        w["charge"] = 0.0
        w["_full_t"] = 0.0
        w["cooldown"] = s["cooldown"]
        m.shots += 1
        self.sound("laser", m)
        end = None
        for step in range(int(s["range"])):
            p = (o[0] + d[0] * step, o[1] + d[1] * step, o[2] + d[2] * step)
            for v in self.mechs:
                if v is m or not v.alive:
                    continue
                if math.hypot(p[0] - v.pos[0], p[1] - v.pos[1]) < 2.5 and v.pos[2] <= p[2] <= v.pos[2] + 9:
                    self.damage(v, dmg, "laser", m, o)
                    end = p
                    break
            if end:
                break
            if self.solid_at(p[0], p[1], p[2], p[2] + 0.01) or p[2] < self.w.ground_at(p[0], p[1]):
                end = p
                break
        end = end or (o[0] + d[0] * s["range"], o[1] + d[1] * s["range"], o[2] + d[2] * s["range"])
        self.beams.append({"by": m.id, "kind": "laser", "path": [list(o), list(end)], "age": 0.0})

    def step_bullets(self):
        keep = []
        for b in self.bullets:
            hit = False
            n = 5
            for _ in range(n):
                p = b["pos"]
                step = [v * DT / n for v in b["vel"]]
                p[0] += step[0]
                p[1] += step[1]
                p[2] += step[2]
                b["left"] -= math.sqrt(step[0] ** 2 + step[1] ** 2 + step[2] ** 2)
                if b["left"] <= 0 or self.solid_at(p[0], p[1], p[2], p[2] + 0.01) or p[2] < self.w.ground_at(p[0], p[1]):
                    hit = True
                    break
                for v in self.mechs:
                    if v.id == b["by"] or not v.alive:
                        continue
                    if math.hypot(p[0] - v.pos[0], p[1] - v.pos[1]) < 2.5 + 0.3 and v.pos[2] - 0.3 <= p[2] <= v.pos[2] + 9.3:
                        src = (p[0] - b["vel"][0], p[1] - b["vel"][1], p[2])
                        self.damage(v, b["dmg"], "gun", self.by_id(b["by"]), src)
                        hit = True
                        break
                if hit:
                    break
            if not hit:
                keep.append(b)
        self.bullets = keep

    def by_id(self, i):
        for m in self.mechs:
            if m.id == i:
                return m
        return None

    def damage(self, v, amount, weapon, attacker, src):
        bearing = math.atan2(src[1] - v.pos[1], src[0] - v.pos[0])
        rear = abs(wrap(bearing - v.aim[0])) > math.radians(self.cfg["mech"]["front_arc_deg"] / 2)
        if rear:
            amount *= self.cfg["mech"]["rear_mult"]
        v.hp -= amount
        v.last_dmg = self.t
        v.events.append({"type": "damaged", "amount": amount, "weapon": weapon, "bearing": bearing, "by": attacker.id if attacker else None, "rear": rear})
        if attacker:
            attacker.last_dmg = self.t
            attacker.dmg_dealt += amount
            attacker.hits += 1
            attacker.events.append({"type": "hit", "target": v.id, "amount": amount, "weapon": weapon})
        if v.hp <= 0 and v.alive:
            v.alive = False
            v.died_t = self.t
            if attacker:
                attacker.events.append({"type": "kill", "target": v.id})

    def sound(self, kind, src):
        rng = self.cfg["sound"]["range"].get(kind, 100)
        for m in self.mechs:
            if m is src or not m.alive:
                continue
            d = math.hypot(src.pos[0] - m.pos[0], src.pos[1] - m.pos[1])
            if d < rng:
                b = math.atan2(src.pos[1] - m.pos[1], src.pos[0] - m.pos[0]) + self.rng.gauss(0, math.radians(7))
                m.sounds.append({"kind": kind, "bearing": wrap(b), "dist": "near" if d < 60 else ("mid" if d < 160 else "far"), "age": 0.05})

    # ------------------------------------------------------------------ vision and observations
    def sees(self, a, b):
        dx, dy = b.pos[0] - a.pos[0], b.pos[1] - a.pos[1]
        d = math.hypot(dx, dy)
        vis = self.cfg["vision"]
        if d > vis["range"]:
            return False
        if d > vis["near_range"] and abs(wrap(math.atan2(dy, dx) - a.aim[0])) > math.radians(vis["fov_deg"] / 2):
            return False
        e = (a.pos[0], a.pos[1], a.pos[2] + 8)
        return self.w.los(*e, b.pos[0], b.pos[1], b.pos[2] + 4.5, skip_start=0.5) or self.w.los(*e, b.pos[0], b.pos[1], b.pos[2] + 8.5, skip_start=0.5)

    def obs_for(self, m):
        mechs = []
        for o in self.mechs:
            if o is m or not o.alive:
                continue
            if self.sees(m, o):
                m.seen[o.id] = self.t
            if self.t - m.seen.get(o.id, -99) <= self.cfg["vision"]["memory_s"]:
                mechs.append({"id": o.id, "kind": "mech", "pos": list(o.pos), "vel": list(o.vel), "aim": list(o.aim),
                              "hp": o.hp, "max_hp": o.max_hp, "cubes": 0,
                              "weapons": [w["kind"] for w in o.weapons if w], "charging": o.charging()})
        weapons = []
        for w in m.weapons:
            if w is None:
                weapons.append(None)
                continue
            ww = {k: v for k, v in w.items() if not k.startswith("_")}
            weapons.append(ww)
        projectiles = [{"id": i, "kind": "gun", "pos": list(b["pos"]), "vel": list(b["vel"])} for i, b in enumerate(self.bullets) if b["by"] != m.id]
        o = {
            "type": "obs", "tick": self.tick, "t": round(self.t, 2), "alive": sum(1 for x in self.mechs if x.alive),
            "self": {"id": m.id, "pos": list(m.pos), "vel": list(m.vel), "aim": list(m.aim), "hp": m.hp, "max_hp": m.max_hp,
                     "energy": m.energy, "max_energy": self.cfg["mech"]["energy"], "grounded": m.grounded, "cubes": 0,
                     "upgrades": [], "in_zone": True, "weapons": weapons, "stats": m.stats()},
            "mechs": mechs, "projectiles": projectiles, "crates": [], "cubes": [], "pickups": [],
            "sounds": m.sounds, "beams": [b for b in self.beams if b["by"] != m.id], "events": m.events,
            "drops": [], "zone": self.zone, "offer": None,
        }
        m.sounds, m.events = [], []
        return o

    def step(self):
        for m in self.mechs:
            if m.alive:
                self.step_weapons(m)
        for _ in range(2):
            for m in self.mechs:
                if m.alive:
                    self.step_mech(m)
            self.step_bullets()
            self.t += DT
            self.tick += 1
        self.beams = [b for b in self.beams if self.t - b.get("_t", self.t) < 0.2]
        for b in self.beams:
            b.setdefault("_t", self.t)


class Hunter:
    """House-bot stand-in: knows where its target is, closes to laser range, charges and tracks, fires the gun with lead."""

    PROFILES = {
        "easy": {"noise": 0.012, "lag": 0.35, "laser_err": 0.012, "strafe": 0.3},
        "medium": {"noise": 0.008, "lag": 0.25, "laser_err": 0.009, "strafe": 0.5},
        "hard": {"noise": 0.005, "lag": 0.15, "laser_err": 0.006, "strafe": 0.7},
        "expert": {"noise": 0.003, "lag": 0.08, "laser_err": 0.004, "strafe": 0.9},
    }

    def __init__(self, sim, me, target, level, rng, nav=None):
        self.sim, self.me, self.target, self.rng = sim, me, target, rng
        self.follow = Follower(nav) if nav is not None else None
        self.p = self.PROFILES[level]
        self.hist = []
        self.side = 1
        self.flip = 0.0

    def act(self):
        sim, me, tg = self.sim, self.me, self.target
        self.hist.append((sim.t, list(tg.pos), list(tg.vel)))
        lag = self.p["lag"]
        while len(self.hist) > 2 and self.hist[1][0] <= sim.t - lag:
            self.hist.pop(0)
        _, tp, tv = self.hist[0]
        x, y, z = me.pos
        d = math.hypot(tp[0] - x, tp[1] - y)
        to = math.atan2(tp[1] - y, tp[0] - x)
        e = (x, y, z + 7)
        vis = sim.w.los(*e, tp[0], tp[1], tp[2] + 4.5) or sim.w.los(*e, tp[0], tp[1], tp[2] + 8.5)
        a = {}
        if sim.t > self.flip:
            self.side = -self.side
            self.flip = sim.t + self.rng.uniform(1.0, 2.5)
        if not vis or d > 150:
            yaw = to
            if self.follow is not None:
                py = self.follow.direction(x, y, tp[0], tp[1], sim.t, 0.05)
                if py is not None:
                    to = py
            for off in (0, 0.5, -0.5, 1.0, -1.0, 1.5, -1.5, 2.2, -2.2):
                h = to + off * self.side
                if not sim.body_blocked(x + 4 * math.cos(h), y + 4 * math.sin(h), z):
                    yaw = h
                    break
            a["move"] = [math.cos(yaw), math.sin(yaw)]
        else:
            s = self.p["strafe"]
            a["move"] = [-math.sin(to) * self.side * s, math.cos(to) * self.side * s]
        las = me.weapons[1]
        gun = me.weapons[0]
        tcz = tp[2] + 4.5
        direct = (to + self.rng.gauss(0, self.p["laser_err"]), math.atan2(tcz - (z + 7), max(d, 1)))
        fire = [False, False]
        aim = direct
        if vis and d < 230:
            tt = d / gun["stats"]["speed"]
            lx, ly = tp[0] + tv[0] * tt, tp[1] + tv[1] * tt
            gaim = (math.atan2(ly - y, lx - x) + self.rng.gauss(0, self.p["noise"]), direct[1])
            if las["charge"] < las["stats"]["charge"] - 0.15:
                aim = gaim
            if abs(wrap(gaim[0] - me.aim[0])) < 0.02:
                fire[0] = True
        if las["cooldown"] <= 0 and vis and d < 260:
            fire[1] = True
            if las["charge"] >= las["stats"]["charge"] - 0.01 and abs(wrap(direct[0] - me.aim[0])) < 0.015:
                fire[1] = False
        elif las["charge"] > 0:
            fire[1] = True
        a["aim"] = list(aim)
        a["fire"] = fire
        return a


def random_open_point(nav, rng, comp):
    while True:
        I, J = rng.randrange(nav.nx), rng.randrange(nav.ny)
        if nav.free(I, J) and nav.comp_at(I, J) == comp:
            return nav.center(I, J)


def play(mod, world, nav, cfg, hello, seed, n_hunters, level, max_t=120.0, our_weapons=("gun", "laser")):
    rng = random.Random(seed)
    sim = Sim(world, cfg, rng)
    main_comp = max(set(nav.comp) - {0}, key=nav.comp.count) if not hasattr(play, "_comp") else play._comp
    play._comp = main_comp
    p0 = random_open_point(nav, rng, main_comp)
    me = Mech(0, (p0[0], p0[1], world.ground_at(*p0)), cfg, list(our_weapons))
    sim.mechs.append(me)
    hunters = []
    for i in range(n_hunters):
        for _ in range(200):
            p = random_open_point(nav, rng, main_comp)
            if 140 < math.hypot(p[0] - p0[0], p[1] - p0[1]) < 260:
                break
        h = Mech(1 + i, (p[0], p[1], world.ground_at(*p)), cfg, ["gun", "laser"])
        sim.mechs.append(h)
        hunters.append(Hunter(sim, h, me, level, rng, nav))
    hello = dict(hello)
    hello["slot"] = 0
    hello["seed"] = seed
    bot = mod.Bot(hello)
    times = []
    while sim.t < max_t and me.alive and any(h.me.alive for h in hunters):
        o = sim.obs_for(me)
        mf.draw.clear()
        t0 = time.perf_counter()
        try:
            a = bot.act(o)
        except Exception:
            import traceback
            traceback.print_exc()
            a = {}
        times.append((time.perf_counter() - t0) * 1000)
        me.action = a
        if "aim" in a:
            me.cmd_aim = list(a["aim"])
        for h in hunters:
            if h.me.alive:
                ha = h.act()
                h.me.action = ha
                h.me.cmd_aim = ha["aim"]
                sim.obs_for(h.me)  # keeps their memory and clears events
        sim.step()
    kills = sum(1 for h in hunters if not h.me.alive)
    return {"won": me.alive and kills == n_hunters, "alive": me.alive, "kills": kills, "t": round(sim.t, 1),
            "hp": round(max(0, me.hp)), "dealt": round(me.dmg_dealt), "taken": round(sum(h.me.dmg_dealt for h in hunters)),
            "acc": round(me.hits / max(1, me.shots), 2), "p99_ms": round(sorted(times)[int(len(times) * 0.99)] if times else 0, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bots", default="v1,v0")
    ap.add_argument("--hunters", type=int, default=1)
    ap.add_argument("--difficulty", default="easy")
    ap.add_argument("--matches", type=int, default=10)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--weapons", default="gun,laser")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--set", action="append", default=[], help="NAME=VALUE module constant override, e.g. COVER_WHILE_CHARGING=0")
    args = ap.parse_args()
    hello = json.loads(gzip.open(os.path.join(KIT, "check", "stream.jsonl.gz"), "rt").readline())
    hello["map"]["dir"] = os.path.join(KIT, "arena")
    cfg = hello["config"]
    world = World(hello["map"]["dir"], cfg)
    nav = NavGrid(world)
    weapons = tuple(w if w != "none" else None for w in args.weapons.split(","))
    for spec in args.bots.split(","):
        name, _, sets = spec.partition("+")
        mod = load_bot_module(name)
        for kv in [x for x in sets.split("+") if x] + (args.set if name == "v1" else []):
            k, _, v = kv.partition("=")
            setattr(mod, k, type(getattr(mod, k))(float(v)) if not isinstance(getattr(mod, k), bool) else v not in ("0", "False"))
        res = [play(mod, world, nav, cfg, hello, args.seed * 1000 + i, args.hunters, args.difficulty, our_weapons=weapons)
               for i in range(args.matches)]
        if args.verbose:
            for r in res:
                print(spec, r)
        n = len(res)
        print(f"{spec:6s} {args.difficulty:7s} x{args.hunters}: won {sum(r['won'] for r in res)}/{n}  "
              f"kills {sum(r['kills'] for r in res) / n:.2f}  survived {sum(r['alive'] for r in res)}/{n}  "
              f"dealt {sum(r['dealt'] for r in res) / n:.0f}  taken {sum(r['taken'] for r in res) / n:.0f}  "
              f"acc {sum(r['acc'] for r in res) / n:.2f}  mean t {sum(r['t'] for r in res) / n:.0f}s  "
              f"p99 {max(r['p99_ms'] for r in res):.1f} ms", flush=True)


if __name__ == "__main__":
    main()
