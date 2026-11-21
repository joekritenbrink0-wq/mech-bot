"""mech-bot v2 (v1 + zone-strict combat movement, lightning kiting, looting mid-fight, disengage off): v0 (aimed fire, laser charge/release, loot memory, zone/endgame, steering, unstick) plus
path planning, cover-aware combat movement with laser peeking, disengage-and-regen, supply drops,
and a practice-vs-FFA mode flag. See PLAN.md (one folder up)."""
import gc
import hashlib
import math
import os
import struct
import random
import time
import traceback

import mechfight as mf
from world import World
from nav import NavGrid, Follower

TAU = 2 * math.pi

UPGRADE_OFFENSE = [
    "laser_damage", "laser_charge", "plating", "gun_damage", "gun_rate", "armor", "lightning_efficiency", "nanites", "gyros",
    "lightning_range", "sensors", "servos", "reactor", "capacitor", "jets", "gun_mag", "gun_velocity",
    "mortar_reload", "mortar_velocity", "lightning_chain", "laser_pierce", "mortar_splash",
]
UPGRADE_SET = "defense"  # "defense" (UPGRADE_PRIORITY) or "offense" (UPGRADE_OFFENSE)
UPGRADE_PRIORITY = [
    "plating", "armor", "nanites", "laser_damage", "laser_charge", "gyros", "lightning_efficiency",
    "gun_damage", "gun_rate", "lightning_range", "sensors", "servos", "reactor", "capacitor", "jets",
    "gun_mag", "gun_velocity", "mortar_reload", "mortar_velocity", "lightning_chain", "laser_pierce", "mortar_splash",
]
WEAPON_VALUE = {"laser": 3.0, "lightning": 2.0, "mortar": 1.0, "gun": 0.5}
STEER_ANGLES = [0.0, 0.35, 0.7, 1.05, 1.4, 1.75, 2.1, 2.5]
FIGHT_DIRS = [i * TAU / 12 for i in range(12)]

# Feature switches (for A/B runs). All on in v1.
USE_NAV = True
USE_COVER = True
USE_DROPS = True
USE_DISENGAGE = False  # sim A/B (2 seed sets, 280 matches each side): off scored +0.2 and +0.5
COVER_WHILE_CHARGING = False  # hide during our own laser charge when an enemy has a laser
ZONE_STRICT = True  # combat movement: hard penalty for leaving the zone, graded one for leaving the safe circle
HOLD_FIRE_TO_REGEN = True  # while disengaging and not under fire, stop shooting so regen can start
KITE_LIGHTNING = True  # keep lightning holders beyond their 45 m reach
DISENGAGE_HP = 0.33  # start disengaging below this HP fraction (when USE_DISENGAGE)
DISENGAGE_EXIT = 0.75  # stop disengaging above this HP fraction
TWO_SPECIALS = False  # with a laser, swap the gun for lightning (and the reverse): laser + lightning loadout
LOOT_IN_FIGHT = True  # weapons that upgrade our loadout and cubes pull combat movement toward them
STEALTH = False  # when nobody has found us: sit in well-hidden spots, walk silently, don't start fights
STEALTH_MAG = 0.38  # move command length while sneaking: 0.38 * 11.7 m/s = 4.4 m/s, under the 5 m/s step-sound speed
STEALTH_ENGAGE_S = 6.0  # stay in fight mode this long after the last sign we were found
FARM = True  # between fights, go after remembered drones (2 cubes) and crates (1 cube, 50% weapon) until 10 cubes
FARM_RANGE = 220.0  # how far we'll walk for a farm target
SEED_SALT = 1  # mixed into our RNG seed: same strength, different match paths (variant submissions)
PATH_BUDGET_S = 0.008


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def hyp(dx, dy):
    return math.sqrt(dx * dx + dy * dy)


def weapon_dps(kind, d):
    if kind == "gun":
        return 100.0 if d < 230 else 0.0
    if kind == "laser":
        return 131.0 if d < 280 else 0.0
    if kind == "lightning":
        return 240.0 if d < 47 else 0.0
    if kind == "mortar":
        return 60.0 if 20 < d < 235 else 0.0
    return 0.0


class Bot:
    name = "mech-bot v2-farm s1"

    def __init__(self, hello):
        cfg = hello["config"]
        self.cfg = cfg
        self.rng = random.Random(hello.get("seed", 0) * 1000003 + SEED_SALT)
        self.world = None
        self.nav = None
        self.follow = None
        try:
            self.world = World(hello["map"]["dir"], cfg)
        except Exception:
            mf.log("map load failed, running without line of sight:\n" + traceback.format_exc())
        if self.world is not None and USE_NAV:
            try:
                self.nav = NavGrid(self.world)
                self.follow = Follower(self.nav)
            except Exception:
                mf.log("nav grid failed:\n" + traceback.format_exc())
        self.reach = float(cfg["loot"]["pickup_radius"])
        self.stat_cap = int(cfg["cubes"].get("stat_cap", 10))
        self.strafe = 1
        self.strafe_until = 0.0
        self.side = 1
        self.hist = []
        self.unstick_until = -1.0
        self.unstick_dir = None
        self.stuck_count = 0
        self.threat_bearing = None
        self.threat_t = -99.0
        self.dmg_t = -99.0
        self.target_id = None
        self.slow_ticks = 0
        self.items = {}  # remembered pickups and cubes: id -> {"kind", "pos", "seen"}
        self.kills = 0
        self.ffa = False
        self.attacked_by = {}  # mech id -> last time it damaged us
        self.disengaging = False
        self.last_fight_yaw = None
        self.threats = []
        self.conceal = None
        self.hide = None
        self.hide_t = -99.0
        self.engaged_until = -99.0
        try:
            self.load_conceal(hello["map"]["dir"])
        except Exception:
            mf.log("conceal map not loaded: " + traceback.format_exc())
        self.t_start = time.perf_counter()
        gc.collect()
        gc.freeze()
        gc.disable()  # collected between ticks instead (see act)

    # ================================================================== helpers
    def los(self, a, b):
        if self.world is None:
            return True
        return self.world.los(a[0], a[1], a[2], b[0], b[1], b[2])

    def mech_los(self, me, m):
        """Line of fire from my muzzle to the target's center or top."""
        x, y, z = me["pos"]
        mz = z + me["stats"].get("muzzle_height", 7.0)
        tx, ty, tz = m["pos"]
        h = 3.5 if m["kind"] == "npc" else 9.0 * (1 + 0.05 * m.get("cubes", 0))
        return self.los((x, y, mz), (tx, ty, tz + h * 0.5)) or self.los((x, y, mz), (tx, ty, tz + h * 0.9))

    def walk_blocked(self, x, y, z, yaw, dists=(3.0, 5.5, 8.0)):
        w = self.world
        if w is None:
            return False
        c, s = math.cos(yaw), math.sin(yaw)
        for d in dists:
            for off in (-2.2, 0.0, 2.2):
                px = x + c * d - s * off
                py = y + s * d + c * off
                if w.blocked(px, py, z, d):
                    return True
        return False

    def steer(self, x, y, z, yaw):
        """First clear heading near `yaw`, trying our preferred side first."""
        if self.world is None:
            return yaw, True
        for a in STEER_ANGLES:
            for sgn in ((self.side, -self.side) if a else (1,)):
                h = yaw + sgn * a
                if not self.walk_blocked(x, y, z, h):
                    if a >= 0.7:
                        self.side = sgn
                    return h, True
        return yaw, False

    def load_conceal(self, map_dir):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conceal.bin")
        with open(path, "rb") as f:
            raw = f.read()
        if raw[:4] != b"CNC1":
            return
        nx, ny, ox, oy, step = struct.unpack("<HHfff", raw[4:20])
        sha = raw[20:40]
        with open(os.path.join(map_dir, "v2", "world.json"), "rb") as f:
            if hashlib.sha1(f.read()).digest() != sha:
                mf.log("conceal map is for a different world.json: stealth spots off")
                return
        self.conceal = (nx, ny, ox, oy, step, raw[40:40 + nx * ny])

    def conceal_at(self, x, y):
        if self.conceal is None:
            return 255
        nx, ny, ox, oy, step, data = self.conceal
        I, J = int((x - ox) / step + 0.5), int((y - oy) / step + 0.5)
        if 0 <= I < nx and 0 <= J < ny:
            return data[J * nx + I]
        return 255

    def pick_hide(self, me, scx, scy, srad):
        """Best concealed walkable spot near us inside the circle we must stay in."""
        if self.conceal is None:
            return None
        nx, ny, ox, oy, step, data = self.conceal
        x, y, z = me["pos"]
        R = 150.0
        lim = max(8.0, srad * 0.72 - 6.0)
        best, spot = -1e9, None
        I0, I1 = max(0, int((x - R - ox) / step)), min(nx - 1, int((x + R - ox) / step) + 1)
        J0, J1 = max(0, int((y - R - oy) / step)), min(ny - 1, int((y + R - oy) / step) + 1)
        for J in range(J0, J1 + 1, 2 if R > 100 else 1):
            cy = oy + J * step
            row = J * nx
            for I in range(I0, I1 + 1):
                v = data[row + I]
                if v == 255:
                    continue
                cx = ox + I * step
                if hyp(cx - scx, cy - scy) > lim:
                    continue
                d = hyp(cx - x, cy - y)
                if d > R:
                    continue
                sc = v - 0.3 * d
                for th in self.threats:
                    tm = th["m"]
                    if hyp(tm["pos"][0] - cx, tm["pos"][1] - cy) < 70:
                        sc -= 60
                if sc > best:
                    best, spot = sc, (cx, cy)
        return spot

    def found(self, obs, me, enemies):
        """Signs that someone knows where we are."""
        t = obs["t"]
        x, y, z = me["pos"]
        if t - self.dmg_t < STEALTH_ENGAGE_S:
            return True
        for m, d, vis in enemies:
            if not vis:
                continue
            if d < 45.0:
                return True
            back = math.atan2(y - m["pos"][1], x - m["pos"][0])
            if d < 200 and abs(wrap(m["aim"][0] - back)) < 0.35:
                return True
        return False

    def elapsed_ms(self):
        return (time.perf_counter() - self.t_start) * 1000.0

    def head_to(self, me, gx, gy, t):
        """Heading toward a far point: along a planned path when the straight line is blocked."""
        x, y, z = me["pos"]
        if self.follow is not None and self.world is not None:
            on_ground = z - self.world.ground_at(x, y) < 2.0
            if on_ground:
                try:
                    budget = min(PATH_BUDGET_S, max(0.0, 0.020 - self.elapsed_ms() / 1000.0))
                    yaw = self.follow.direction(x, y, gx, gy, t, budget)
                    if yaw is not None:
                        return yaw
                except Exception:
                    mf.log("path follow failed:\n" + traceback.format_exc())
                    self.follow = None
        return math.atan2(gy - y, gx - x)

    def zone_safe_radius(self, obs):
        """Circle we should be inside now: the next circle when its shrink is close, else the current one."""
        zn = obs["zone"]
        t = obs["t"]
        if t >= zn["shrink_start"] - 25:
            return zn["next_center"], zn["next_radius"]
        return zn["center"], zn["radius"]

    def pick_upgrade(self, offer):
        ids = [c["id"] for c in offer["choices"]]
        for u in (UPGRADE_OFFENSE if UPGRADE_SET == "offense" else UPGRADE_PRIORITY):
            if u in ids:
                return ids.index(u)
        return 0

    # ================================================================== main
    def act(self, obs):
        t0 = self.t_start = time.perf_counter()
        me = obs["self"]
        t = obs["t"]
        x, y, z = me["pos"]
        st = me["stats"]
        act = {"tick": obs["tick"]}
        hp_frac = me["hp"] / max(1.0, me["max_hp"])

        for e in obs["events"]:
            if e["type"] == "damaged" and e.get("bearing") is not None:
                self.threat_bearing, self.threat_t, self.dmg_t = e["bearing"], t, t
                if e.get("by") is not None:
                    self.attacked_by[e["by"]] = t
            elif e["type"] == "kill":
                self.kills += 1
        for s in obs["sounds"]:
            if s["kind"] in ("gun", "laser", "laser_charge", "lightning", "mortar") and s["dist"] != "far" and t - self.threat_t > 1.5:
                self.threat_bearing, self.threat_t = s["bearing"], t
        # Other mechs dying without our help means a real free-for-all (practice house bots never fight each other).
        if not self.ffa and (16 - obs["alive"]) - self.kills >= 2:
            self.ffa = True

        if obs.get("offer"):
            act["pick"] = self.pick_upgrade(obs["offer"])
        self.remember_items(obs, me)
        if FARM:
            self.remember_farm(obs, me)

        slots = me["weapons"]
        held = {w["kind"]: i for i, w in enumerate(slots) if w}

        # ------------------------------------------------------------ targets and threats
        mechs = []
        for m in obs["mechs"]:
            d = hyp(m["pos"][0] - x, m["pos"][1] - y)
            vis = self.mech_los(me, m)
            mechs.append((m, d, vis))
        enemies = [e for e in mechs if e[0]["kind"] == "mech"]
        npcs = [e for e in mechs if e[0]["kind"] == "npc"]

        threats = []
        for m, d, vis in enemies:
            dps = sum(weapon_dps(k, d) for k in m.get("weapons", ["gun"]))
            if dps <= 0:
                continue
            back = math.atan2(y - m["pos"][1], x - m["pos"][0])
            aimed = abs(wrap(m["aim"][0] - back)) < 0.2
            threats.append({"m": m, "d": d, "dps": dps, "charging_at_us": bool(m.get("charging")) and aimed})
        threats.sort(key=lambda th: -th["dps"] * (3 if th["charging_at_us"] else 1) / (1 + th["d"] / 150))
        self.threats = threats[:3]

        target = None
        best = 1e18
        for m, d, vis in enemies:
            score = d + 80 * (m.get("hp", 1400) / max(1, m.get("max_hp", 1400))) + (0 if vis else 250)
            if m["id"] == self.target_id:
                score -= 30
            if score < best:
                best, target = score, (m, d, vis)
        self.target_id = target[0]["id"] if target else None

        # opportunistic targets when no mech is around
        side_target = None
        if target is None:
            for m, d, vis in sorted(npcs, key=lambda e: e[1]):
                if vis and d < (110 if FARM and "laser" in held else 75) and (hp_frac > 0.5 or m["id"] in self._attackers(obs)):
                    side_target = (m, d, vis)
                    break
            if side_target is None:
                for c in sorted(obs["crates"], key=lambda c: hyp(c["pos"][0] - x, c["pos"][1] - y)):
                    d = hyp(c["pos"][0] - x, c["pos"][1] - y)
                    if d > (110 if FARM and "laser" in held else 70):
                        break
                    cp = c["pos"]
                    if self.los((x, y, z + st.get("muzzle_height", 7)), (cp[0], cp[1], cp[2] + 1.2)):
                        side_target = ({"id": c["id"], "kind": "crate", "pos": cp, "vel": [0, 0, 0], "hp": c["hp"]}, d, True)
                        break

        # ------------------------------------------------------------ movement
        sneaking = False
        if STEALTH:
            if self.found(obs, me, enemies):
                self.engaged_until = t + STEALTH_ENGAGE_S
            sneaking = t >= self.engaged_until and obs["zone"]["next_radius"] > 30
        if sneaking:
            move_yaw, move_mag, boost, jump, label = self.sneak_move(obs, me, held, hp_frac)
            if any(w and w["kind"] == "laser" and w.get("charge", 0) > 0 for w in slots):
                pass  # an existing charge is held below
            target_for_fire = None
        else:
            move_yaw, move_mag, boost, jump, label = self.choose_move(obs, me, held, target, side_target, hp_frac)
            target_for_fire = target or side_target

        # obstacle steering + unstick
        if move_mag > 0.05:
            if t < self.unstick_until and self.unstick_dir is not None:
                move_yaw = self.unstick_dir
                jump = True
                boost = not me["grounded"] and me["energy"] > 8
            else:
                move_yaw, clear = self.steer(x, y, z, move_yaw)
                if not clear and me["grounded"]:
                    jump = True
        self.hist.append((t, x, y, move_mag))
        if len(self.hist) > 12:
            self.hist.pop(0)
        if len(self.hist) >= 8 and t >= self.unstick_until:
            t_old, xo, yo, _ = self.hist[-8]
            if all(h[3] > 0.5 for h in self.hist[-8:]) and hyp(x - xo, y - yo) < 2.0:
                self.stuck_count += 1
                self.side = -self.side
                self.unstick_dir = move_yaw + self.side * (1.2 + 0.4 * self.rng.random()) if self.stuck_count % 3 else self.rng.uniform(-math.pi, math.pi)
                self.unstick_until = t + 1.2
                self.hist.clear()
                if self.follow is not None:
                    self.follow.path = None
        if move_mag > 0.05:
            act["move"] = [math.cos(move_yaw) * move_mag, math.sin(move_yaw) * move_mag]
        else:
            act["move"] = [0.0, 0.0]
        act["boost"] = bool(boost)
        if jump:
            act["jump"] = True

        # ------------------------------------------------------------ aim and fire
        aim, fire = self.choose_fire(obs, me, held, target_for_fire, move_yaw, act)
        act["aim"] = aim
        act["fire"] = fire

        # ------------------------------------------------------------ loot
        self.loot(obs, me, held, act)

        dt = (time.perf_counter() - t0) * 1000
        if dt < 12 and obs["tick"] % 40 == 0:
            gc.collect(1)
        elif dt < 6 and obs["tick"] % 1200 == 0:
            gc.collect()
        if dt > 30:
            self.slow_ticks += 1
            if self.slow_ticks < 20:
                mf.log(f"slow tick {obs['tick']}: {dt:.1f} ms")
        mf.draw.label(f"{label} hp{me['hp']:.0f}{' ffa' if self.ffa else ''}")
        return act

    def remember_items(self, obs, me):
        """Pickups and cubes are listed only while seen; keep them until we get close and they are gone."""
        t = obs["t"]
        x, y, z = me["pos"]
        now = set()
        for p in obs["pickups"]:
            self.items[p["id"]] = {"kind": p["kind"], "pos": p["pos"], "seen": t}
            now.add(p["id"])
        for c in obs["cubes"]:
            self.items[c["id"]] = {"kind": "cube", "pos": c["pos"], "seen": t}
            now.add(c["id"])
        for e in obs["events"]:
            if e["type"] in ("took", "take_failed"):
                self.items.pop(e["id"], None)
        eye = z + me["stats"].get("eye_height", 8.0)
        for i in list(self.items):
            if i in now:
                continue
            it = self.items[i]
            px, py, pz = it["pos"]
            ttl = 45.0 if it["kind"] == "cube" else 120.0
            if t - it["seen"] > ttl or (hyp(px - x, py - y) < 30 and self.los((x, y, eye), (px, py, pz + 0.5))):
                del self.items[i]

    def remember_farm(self, obs, me):
        """Drones and crates we have seen (drones stay within 30 m of home; crates never move)."""
        t = obs["t"]
        x, y, z = me["pos"]
        if not hasattr(self, "farm"):
            self.farm = {}
        now = set()
        for m in obs["mechs"]:
            if m["kind"] == "npc":
                f = self.farm.get(m["id"])
                self.farm[m["id"]] = {"kind": "npc", "pos": m["pos"], "seen": t, "hp": m.get("hp", 350)}
                now.add(m["id"])
        for c in obs["crates"]:
            self.farm[c["id"]] = {"kind": "crate", "pos": c["pos"], "seen": t, "hp": c.get("hp", 300)}
            now.add(c["id"])
        eye = z + me["stats"].get("eye_height", 8.0)
        for i in list(self.farm):
            if i in now:
                continue
            f = self.farm[i]
            px, py, pz = f["pos"]
            ttl = 90.0 if f["kind"] == "npc" else 240.0
            if t - f["seen"] > ttl or (hyp(px - x, py - y) < 40 and self.los((x, y, eye), (px, py, pz + 1.5))):
                del self.farm[i]

    def farm_goal(self, me, scx, scy, srad, hp_frac):
        if me["cubes"] >= self.stat_cap or hp_frac < 0.45:
            return None
        x, y, z = me["pos"]
        best, goal = 0.0, None
        for f in getattr(self, "farm", {}).values():
            px, py, pz = f["pos"]
            if pz - z > 6 or hyp(px - scx, py - scy) > srad - 8:
                continue
            d = hyp(px - x, py - y)
            if d > FARM_RANGE or d < 30:
                continue
            v = (2.0 if f["kind"] == "npc" else 1.3) * 60.0 / (d + 30.0)
            if v > best:
                best, goal = v, (px, py, "farm " + f["kind"])
        return goal

    def _attackers(self, obs):
        return {e.get("by") for e in obs["events"] if e["type"] == "damaged"}

    # ================================================================== movement choice
    def choose_move(self, obs, me, held, target, side_target, hp_frac):
        x, y, z = me["pos"]
        t = obs["t"]
        zn = obs["zone"]
        energy = me["energy"]
        (scx, scy), srad = self.zone_safe_radius(obs)
        dz = hyp(x - scx, y - scy)

        # disengage hysteresis: get out of a losing fight, come back after regen
        if USE_DISENGAGE:
            if not self.disengaging and hp_frac < DISENGAGE_HP and self.threats:
                th = self.threats[0]["m"]
                if th.get("hp", 0) / max(1, th.get("max_hp", 1400)) > hp_frac + 0.1 or len(self.threats) >= 2:
                    self.disengaging = True
            elif self.disengaging and (hp_frac > DISENGAGE_EXIT or (not self.threats and t - self.dmg_t > 12)):
                self.disengaging = False

        # 1. outside the zone: run home
        if not me["in_zone"]:
            cx, cy = zn["center"]
            return self.head_to(me, cx, cy, t), 1.0, energy > 20, False, "zone!"

        # 2. a mech to fight (seen or remembered)
        if target is not None:
            m, d, vis = target
            recently_hit = t - self.attacked_by.get(m["id"], -99) < 5
            patient = not vis and not recently_hit and hp_frac < 0.5  # self.ffa is logged only until it's verified
            if not patient:
                return self.fight_move(obs, me, held, target, hp_frac, scx, scy, srad)

        # 3. supply drop: full heal and 3 cubes if we get it alone
        if USE_DROPS:
            g = self.drop_goal(obs, me)
            if g is not None:
                gx, gy, rad = g
                dd = hyp(gx - x, gy - y)
                if dd < rad - 3:
                    return 0.0, 0.0, False, False, "capture"
                return self.head_to(me, gx, gy, t), 1.0, dd > 60 and energy > 50, False, "drop"

        # 4. not safe for the coming shrink: move in (in the last circles, go stand on the final point)
        if srad <= 30:
            return self.final_point(me, zn, t)
        margin = max(8.0, min(30.0, srad * 0.15))
        if dz > srad - margin:
            late = t >= zn["shrink_start"] - 8
            return self.head_to(me, scx, scy, t), 1.0, late and energy > 40, False, "zone"

        # 5. loot: better weapon, cubes
        goal = self.loot_goal(obs, me, held, scx, scy, srad)
        if goal is not None:
            gx, gy, why = goal
            d = hyp(gx - x, gy - y)
            return self.head_to(me, gx, gy, t), 1.0 if d > 6 else 0.5, False, False, why

        # 5b. farm: walk to remembered drones and crates for cubes
        if FARM and side_target is None and not self.disengaging:
            g = self.farm_goal(me, scx, scy, srad, hp_frac)
            if g is not None:
                return self.head_to(me, g[0], g[1], t), 1.0, False, False, g[2]

        # 6. npc / crate: hold a shooting range
        if side_target is not None and not self.disengaging:
            m, d, vis = side_target
            to = math.atan2(m["pos"][1] - y, m["pos"][0] - x)
            want = 25.0 if m["kind"] == "crate" else 35.0
            if d > want + 8:
                return to, 1.0, False, False, "farm"
            if d < want - 10:
                return to + math.pi, 0.6, False, False, "farm"
            return to + math.pi / 2 * self.strafe, 0.4, False, False, "farm"

        # 7. position for the zone; endgame: stand on the final point
        ncx, ncy = zn["next_center"]
        dn = hyp(x - ncx, y - ncy)
        if zn["next_radius"] <= 30:
            return self.final_point(me, zn, t)
        want = max(10.0, zn["next_radius"] * 0.45)
        if dn > want:
            return self.head_to(me, ncx, ncy, t), 1.0, False, False, "drift"
        return 0.0, 0.0, False, False, "hold"

    def sneak_move(self, obs, me, held, hp_frac):
        """Unfound: go to a well-hidden spot quietly; only nearby loot or the zone make us move faster."""
        x, y, z = me["pos"]
        t = obs["t"]
        zn = obs["zone"]
        (scx, scy), srad = self.zone_safe_radius(obs)
        if not me["in_zone"]:
            cx, cy = zn["center"]
            return self.head_to(me, cx, cy, t), 1.0, me["energy"] > 20, False, "zone!"
        dz = hyp(x - scx, y - scy)
        if dz > srad - max(8.0, min(30.0, srad * 0.15)):
            late = t >= zn["shrink_start"] - 8
            return self.head_to(me, scx, scy, t), 1.0 if late else STEALTH_MAG * 2, late and me["energy"] > 40, False, "zone"
        goal = self.loot_goal(obs, me, held, scx, scy, srad)
        if goal is not None and hyp(goal[0] - x, goal[1] - y) < 60:
            return self.head_to(me, goal[0], goal[1], t), STEALTH_MAG, False, False, "sneak " + goal[2]
        if self.hide is None or t - self.hide_t > 10 or hyp(self.hide[0] - scx, self.hide[1] - scy) > srad * 0.8:
            self.hide = self.pick_hide(me, scx, scy, srad) or (scx, scy)
            self.hide_t = t
        hx, hy = self.hide
        d = hyp(hx - x, hy - y)
        if d > 2.5:
            return self.head_to(me, hx, hy, t), STEALTH_MAG, False, False, f"hide {self.conceal_at(hx, hy)}"
        return 0.0, 0.0, False, False, f"hidden {self.conceal_at(x, y)}"

    def final_point(self, me, zn, t):
        """Stand on the next circle's center: in the last phase it stays inside the zone until the very end."""
        x, y, z = me["pos"]
        ncx, ncy = zn["next_center"]
        dn = hyp(x - ncx, y - ncy)
        if dn > 1.0:
            return self.head_to(me, ncx, ncy, t), min(1.0, dn / 6 + 0.2), dn > 30 and me["energy"] > 40, False, "final"
        return 0.0, 0.0, False, False, "final"

    def drop_goal(self, obs, me):
        x, y, z = me["pos"]
        best = None
        for dr in obs.get("drops") or []:
            px, py = dr["pos"][0], dr["pos"][1]
            d = hyp(px - x, py - y)
            if d > 320:
                continue
            eta = d / 11.7
            if dr["lands_in"] > eta + 12:
                continue
            if dr.get("holder") is not None and dr.get("holder") != me["id"] and dr.get("progress", 0) > 2.0:
                continue  # someone is almost done: not worth the walk
            contested = False
            for th in self.threats:
                m = th["m"]
                if hyp(m["pos"][0] - px, m["pos"][1] - py) < 70:
                    contested = True
            if contested:
                continue
            if best is None or d < best[0]:
                best = (d, px, py, dr.get("radius", 8.0))
        if best is None:
            return None
        return best[1], best[2], best[3]

    def fight_move(self, obs, me, held, target, hp_frac, scx, scy, srad):
        """Pick one of 12 headings (or standing still) by exposure to threats, line of fire, range and zone."""
        x, y, z = me["pos"]
        t = obs["t"]
        st = me["stats"]
        energy = me["energy"]
        m, d, vis = target
        tx, ty, tz = m["pos"]
        to = math.atan2(ty - y, tx - x)
        slots = me["weapons"]

        if "laser" in held:
            lo, hi = 75.0, 165.0
        elif "lightning" in held:
            lo, hi = 12.0, 32.0
        else:
            lo, hi = 40.0, 105.0
        if "lightning" in held and d < 50:
            lo, hi = 10.0, 30.0
        if "lightning" in m.get("weapons", []) and ("lightning" not in held or KITE_LIGHTNING):
            lo, hi = max(lo, 60.0), max(hi, 100.0)

        if t >= self.strafe_until:
            self.strafe = -self.strafe
            self.strafe_until = t + self.rng.uniform(0.7, 1.9)

        las = slots[held["laser"]] if "laser" in held else None
        charge = las.get("charge", 0.0) if las else 0.0
        full = las["stats"]["charge"] if las else 1.0
        peek = las is not None and charge >= full - 0.12
        enemy_laser = any("laser" in th["m"].get("weapons", []) and th["d"] < 290 for th in self.threats)
        dodge = any(th["charging_at_us"] for th in self.threats)
        want_cover = USE_COVER and (self.disengaging or (COVER_WHILE_CHARGING and enemy_laser and las is not None and not peek))

        # hunting something we can't see and won't see from here: walk to it (paths around buildings)
        if not vis and not self.disengaging and d > hi * 0.6:
            boost = d > hi + 60 and energy > 50
            return self.head_to(me, tx, ty, t), 1.0, boost, False, "hunt"

        if not USE_COVER:
            return self._v0_fight(me, m, d, vis, lo, hi, hp_frac, dodge, scx, scy, srad, energy, t)

        (zcx, zcy), zr = obs["zone"]["center"], obs["zone"]["radius"]
        lures = self.fight_lures(me, held) if LOOT_IN_FIGHT else []
        speed = st.get("walk_speed", 11.7) * (0.5 if charge > 0 else 1.0)
        horizon = 0.7
        mz = st.get("muzzle_height", 7.0)
        tcz = tz + 4.5 * (1 + 0.05 * m.get("cubes", 0))
        best = None
        for a in [None] + FIGHT_DIRS:
            if best is not None and self.elapsed_ms() > 22.0:
                break  # out of time: keep the best heading so far
            if a is None:
                px, py = x, y
            else:
                if self.walk_blocked(x, y, z, a, (3.0, 6.0)):
                    continue
                px, py = x + math.cos(a) * speed * horizon, y + math.sin(a) * speed * horizon
            exp = 0.0
            exp_charger = 0.0
            for th in self.threats:
                tm = th["m"]
                ex, ey = tm["pos"][0], tm["pos"][1]
                ez = tm["pos"][2] + 8.0 * (1 + 0.05 * tm.get("cubes", 0))
                if self.los((ex, ey, ez), (px, py, z + 4.5)) or self.los((ex, ey, ez), (px, py, z + 8.5)):
                    exp += th["dps"]
                    if th["charging_at_us"]:
                        exp_charger += 1
            can_hit = self.los((px, py, z + mz), (tx, ty, tcz))
            dp = hyp(tx - px, ty - py)
            s = 0.0
            if peek:
                s += 4.0 if can_hit else 0.0
            elif not want_cover:
                s += 1.5 if can_hit else 0.0
            s -= (3.0 if want_cover else 0.8) * exp / 150.0
            s -= 8.0 * exp_charger
            if self.disengaging:
                s += 0.01 * min(dp, 200.0)
            else:
                s -= 0.03 * max(0.0, lo - dp) + 0.02 * max(0.0, dp - hi)
            for lx, ly, val, rad in lures:
                dl = hyp(lx - px, ly - py)
                if dl < rad:
                    s += val * (1.0 - dl / rad)
            if KITE_LIGHTNING:
                for th in self.threats:
                    tm = th["m"]
                    if "lightning" in tm.get("weapons", []):
                        dl = hyp(tm["pos"][0] - px, tm["pos"][1] - py)
                        if dl < 58.0:
                            s -= 0.25 * (58.0 - dl)
            if ZONE_STRICT:
                out_cur = hyp(px - zcx, py - zcy) - (zr - 4.0)
                if out_cur > 0:
                    s -= 25.0 + 1.0 * out_cur
                out_safe = hyp(px - scx, py - scy) - srad * 0.9
                if out_safe > 0 and srad > 5:
                    s -= 1.0 + 0.1 * out_safe
            elif hyp(px - scx, py - scy) > srad * 0.92 and srad > 5:
                s -= 3.0
            if a is None:
                if exp > 0 and not peek:
                    s -= 0.5  # a standing target is easy to hit
            else:
                if exp > 0 and abs(abs(wrap(a - to)) - math.pi / 2) < 0.6:
                    s += 0.3  # strafing across their line
                if self.last_fight_yaw is not None and abs(wrap(a - self.last_fight_yaw)) < 0.6:
                    s += 0.4
            s += self.rng.uniform(0.0, 0.15)
            if best is None or s > best[0]:
                best = (s, a, exp, exp_charger)
        if best is None:
            return to + math.pi / 2 * self.strafe, 1.0, False, True, "boxed"
        _, a, exp, exp_charger = best
        self.last_fight_yaw = a
        zap_near = KITE_LIGHTNING and any("lightning" in th["m"].get("weapons", []) and th["d"] < 62 for th in self.threats)
        boost = energy > 30 and (dodge or self.disengaging or (want_cover and exp > 0) or zap_near)
        jump = dodge and exp_charger > 0 and self.rng.random() < 0.3
        label = "evade" if self.disengaging else ("peek" if peek else ("cover" if want_cover else ("fight" if vis else "hunt")))
        if a is None:
            return to, 0.0, False, False, label
        return a, 1.0, boost, jump, label

    def pickup_gain(self, k, me, held):
        """How much better our loadout gets by taking a weapon of kind k (<= 0: not worth it)."""
        if k == "gun" or k in held:
            return 0.0
        empty = any(w is None for w in me["weapons"])
        if empty:
            return WEAPON_VALUE[k]
        specials = [x for x in held if x != "gun"]
        if TWO_SPECIALS and "gun" in held and specials and {k, specials[0]} == {"laser", "lightning"}:
            return 1.5  # replaces the gun
        cur = min((WEAPON_VALUE[x] for x in specials), default=0.0)
        return WEAPON_VALUE[k] - cur

    def fight_lures(self, me, held):
        """(x, y, value, radius) of nearby loot worth stepping toward mid-fight."""
        x, y, z = me["pos"]
        out = []
        cur = min((WEAPON_VALUE[k] for k in held if k != "gun"), default=0.0)
        empty = any(w is None for w in me["weapons"])
        for it in self.items.values():
            px, py, pz = it["pos"]
            if pz - z > 3.5:
                continue
            d = hyp(px - x, py - y)
            k = it["kind"]
            if k == "cube":
                if me["cubes"] < self.stat_cap and d < 45:
                    out.append((px, py, 0.6, 45.0))
            elif k != "gun" and k not in held:
                gain = self.pickup_gain(k, me, held)
                if gain > 0 and d < 70:
                    out.append((px, py, 1.2 * gain, 70.0))
        out.sort(key=lambda o: -o[2])
        return out[:4]

    def _v0_fight(self, me, m, d, vis, lo, hi, hp_frac, dodge, scx, scy, srad, energy, t):
        x, y, z = me["pos"]
        tx, ty = m["pos"][0], m["pos"][1]
        to = math.atan2(ty - y, tx - x)
        radial = 0.0
        if d > hi:
            radial = 1.0
        elif d < lo:
            radial = -1.0
        retreat = hp_frac < 0.3 and m.get("hp", 0) / max(1, m.get("max_hp", 1400)) > hp_frac + 0.1
        if retreat:
            radial = -1.0
        tang = 1.0 if dodge else 0.8
        vx = radial * math.cos(to) + tang * self.strafe * -math.sin(to)
        vy = radial * math.sin(to) + tang * self.strafe * math.cos(to)
        dz = hyp(x - scx, y - scy)
        if dz > srad * 0.85 and srad > 5:
            k = 1.5 * (dz / max(srad, 1) - 0.85) / 0.15
            vx += k * (scx - x) / max(dz, 1)
            vy += k * (scy - y) / max(dz, 1)
        boost = (dodge or retreat or (t - self.dmg_t < 2.0) or (radial > 0 and d > hi + 60)) and energy > 30
        return math.atan2(vy, vx), 1.0, boost, dodge and self.rng.random() < 0.3, "fight" if vis else "hunt"

    def loot_goal(self, obs, me, held, scx, scy, srad):
        x, y, z = me["pos"]
        best, goal = 0.0, None
        cur = min((WEAPON_VALUE[k] for k in held if k != "gun"), default=0.0)
        empty = any(w is None for w in me["weapons"])
        for it in self.items.values():
            k = it["kind"]
            if k == "cube" or k in held or k == "gun":
                continue
            px, py, pz = it["pos"]
            if pz - z > 3.5 or hyp(px - scx, py - scy) > srad - 5:
                continue
            gain = self.pickup_gain(k, me, held)
            if gain <= 0:
                continue
            d = hyp(px - x, py - y)
            if d > 170:
                continue
            v = gain * 40 / (d + 20)
            if v > best:
                best, goal = v, (px, py, "get " + k)
        if me["cubes"] < self.stat_cap:
            for c in self.items.values():
                if c["kind"] != "cube":
                    continue
                cx, cy, cz = c["pos"]
                if cz - z > 3.5 or hyp(cx - scx, cy - scy) > srad - 5:
                    continue
                d = hyp(cx - x, cy - y)
                if d > 110:
                    continue
                v = 1.2 * 40 / (d + 20)
                if v > best:
                    best, goal = v, (cx, cy, "cube")
        return goal

    # ================================================================== aim and fire
    def choose_fire(self, obs, me, held, target, move_yaw, act):
        x, y, z = me["pos"]
        st = me["stats"]
        t = obs["t"]
        cur_yaw, cur_pitch = me["aim"]
        slots = me["weapons"]
        fire = [False, False]
        mz = z + st.get("muzzle_height", 7.0)

        if target is None:
            # scan: face the last threat, else sweep around the walking direction
            if self.threat_bearing is not None and t - self.threat_t < 3.0:
                yaw = self.threat_bearing
            else:
                base = move_yaw if act["move"] != [0.0, 0.0] else cur_yaw
                yaw = base + 1.5 * math.sin(t * 1.3)
                if act["move"] == [0.0, 0.0]:
                    yaw = cur_yaw + 0.9  # turning in place: a full look-around every ~0.7 s
            for i, w in enumerate(slots):
                if w and w["kind"] == "laser" and w.get("charge", 0) > 0:
                    fire[i] = True  # keep holding a charge we already have
            gun = slots[held["gun"]] if "gun" in held else None
            if gun and gun.get("reload", 0) == 0 and gun.get("ammo", 99) < gun["stats"]["mag"] * 0.6:
                act["reload"] = True
            return [wrap(yaw), 0.0], fire

        m, d, vis = target
        tx, ty, tz = m["pos"]
        vx, vy, vz = m.get("vel", [0, 0, 0])
        kind = m["kind"]
        if kind == "mech":
            h = 9.0 * (1 + 0.05 * m.get("cubes", 0))
            rad = 2.5 * (1 + 0.05 * m.get("cubes", 0))
        elif kind == "npc":
            h, rad = 3.5, 1.8
        else:
            h, rad = 2.4, 1.4
        cz = tz + h * 0.5
        ang_tol = math.atan2(rad * 0.75, max(d, 1.0))

        def direct():
            return math.atan2(ty - y, tx - x), math.atan2(cz - mz, max(d, 0.1))

        def lead(speed):
            px, py, pz = tx, ty, cz
            for _ in range(3):
                dd = math.sqrt((px - x) ** 2 + (py - y) ** 2 + (pz - mz) ** 2)
                tt = max(0.0, dd - 3.0) / speed
                px, py, pz = tx + vx * tt, ty + vy * tt, cz + (vz * tt if abs(vz) > 1 else 0)
            return math.atan2(py - y, px - x), math.atan2(pz - mz, max(hyp(px - x, py - y), 0.1))

        def err(a):
            return max(abs(wrap(a[0] - cur_yaw)), abs(a[1] - cur_pitch))

        gun_i = held.get("gun")
        las_i = held.get("laser")
        lig_i = held.get("lightning")
        mor_i = held.get("mortar")

        gun_aim = None
        if gun_i is not None:
            g = slots[gun_i]
            gs = g["stats"]
            if vis and d < gs["range"] * 0.95:
                gun_aim = lead(gs["speed"])
                if g.get("reload", 0) == 0 and g.get("ammo", 1) > 0 and g["cooldown"] <= 0.1 and err(gun_aim) < ang_tol + 0.004:
                    fire[gun_i] = True
            if g.get("ammo", 1) == 0 and g.get("reload", 0) == 0:
                act["reload"] = True

        aim = gun_aim or direct()

        if las_i is not None:
            L = slots[las_i]
            ls = L["stats"]
            charge = L.get("charge", 0.0)
            full = ls["charge"]
            in_range = d < ls["range"] * 0.97
            if charge > 0:
                # holding a charge: switch to the direct line near full charge, release when on target
                if vis and in_range:
                    dmg = ls["damage"] * (ls["min_frac"] + (1 - ls["min_frac"]) * min(1.0, charge / full))
                    near_full = charge >= full - 0.2
                    finish = kind != "crate" and m.get("hp", 1e9) <= dmg * 0.95 and charge >= ls["min_charge"]
                    if near_full or finish or kind == "crate":
                        aim = direct()
                    if (charge >= full - 0.01 or finish or kind == "crate") and err(direct()) < ang_tol:
                        fire[las_i] = False
                    else:
                        fire[las_i] = True
                else:
                    fire[las_i] = True  # keep it charged for when the target reappears
            elif L["cooldown"] <= 0 and in_range and (vis or d < 140):
                fire[las_i] = True  # start charging

        if lig_i is not None:
            Z = slots[lig_i]
            zs = Z["stats"]
            if vis and d < zs["range"] * 0.95 and me["energy"] > zs["energy"] + 6:
                if gun_aim is None or d < 30:
                    aim = (math.atan2(ty - y, tx - x), aim[1])
                if abs(wrap(math.atan2(ty - y, tx - x) - cur_yaw)) < math.radians(zs["cone_deg"]) * 0.8 and Z["cooldown"] <= 0.1:
                    fire[lig_i] = True

        if mor_i is not None:
            M = slots[mor_i]
            ms = M["stats"]
            use_mortar = d > 28 and (not vis or gun_aim is None or slots[gun_i].get("reload", 0) > 0 or slots[gun_i].get("ammo", 1) == 0)
            if use_mortar and M["cooldown"] <= 0.3 and (las_i is None or slots[las_i].get("charge", 0) == 0):
                sol = self.mortar_solution(me, tx, ty, tz, vx, vy, ms, vis)
                if sol is not None:
                    aim = sol
                    if err(sol) < 0.02 and M["cooldown"] <= 0:
                        fire[mor_i] = True

        # Regen needs 5 s without dealing damage too: while evading and not being shot, stop shooting
        # unless the shot would finish the target.
        if HOLD_FIRE_TO_REGEN and self.disengaging and kind == "mech" and t - self.dmg_t > 1.5:
            if m.get("hp", 1e9) / max(1.0, m.get("max_hp", 1400)) > 0.25:
                for i, w in enumerate(slots):
                    if w is None:
                        continue
                    if w["kind"] == "laser" and w.get("charge", 0) > 0:
                        continue  # can't cancel a charge; let it play out
                    fire[i] = False

        pmin = self.cfg["mech"].get("min_pitch", -0.7)
        pmax = self.cfg["mech"].get("max_pitch", 1.3)
        return [wrap(aim[0]), max(pmin, min(pmax, aim[1]))], fire

    def mortar_solution(self, me, tx, ty, tz, vx, vy, ms, vis):
        x, y, z = me["pos"]
        st = me["stats"]
        oz = z + st.get("height", 9.0) + 0.5
        v, g = ms["speed"], ms["gravity"]
        px, py = tx, ty
        sol = None
        for _ in range(2):
            R = max(1.0, hyp(px - x, py - y) - 3.0)
            dh = tz + 1.0 - oz
            disc = v ** 4 - g * (g * R * R + 2 * dh * v * v)
            if disc < 0:
                return None
            root = math.sqrt(disc)
            th = math.atan((v * v - root) / (g * R))
            if not vis:
                th = math.atan((v * v + root) / (g * R))
            tof = R / (v * math.cos(th))
            px, py = tx + vx * tof, ty + vy * tof
            sol = (math.atan2(py - y, px - x), th)
        return sol

    # ================================================================== loot actions
    def loot(self, obs, me, held, act):
        x, y, z = me["pos"]
        slots = me["weapons"]
        empty = [i for i, w in enumerate(slots) if w is None]
        for p in obs["pickups"]:
            k = p["kind"]
            if k in held or k == "gun":
                continue
            px, py, pz = p["pos"]
            if hyp(px - x, py - y) > self.reach - 0.3 or abs(pz - z) > 3.8:
                continue
            if empty:
                act["take"] = p["id"]
                return
            if self.pickup_gain(k, me, held) <= 0:
                continue
            specials = [x for x in held if x != "gun"]
            if TWO_SPECIALS and "gun" in held and specials and {k, specials[0]} == {"laser", "lightning"}:
                act["take"] = p["id"]
                act["drop"] = held["gun"]
                act["fire"][held["gun"]] = False
                return
            # swap out the weakest non-gun weapon if this one is better
            worst_i, worst_v = None, 1e9
            for i, w in enumerate(slots):
                if w and w["kind"] != "gun" and WEAPON_VALUE[w["kind"]] < worst_v:
                    if w["kind"] == "laser" and w.get("charge", 0) > 0:
                        continue
                    worst_i, worst_v = i, WEAPON_VALUE[w["kind"]]
            if worst_i is not None and WEAPON_VALUE[k] > worst_v:
                act["take"] = p["id"]
                act["drop"] = worst_i
                act["fire"][worst_i] = False
                return


if __name__ == "__main__":
    mf.run(Bot)
