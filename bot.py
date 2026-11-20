"""mech-bot v0: aimed fire with lead, laser charge/release, weapon pickups, zone and endgame positioning,
obstacle steering and unstick. See PLAN.md (one folder up) for where this is going."""
import gc
import math
import random
import time
import traceback

import mechfight as mf
from world import World

TAU = 2 * math.pi

UPGRADE_PRIORITY = [
    "plating", "armor", "nanites", "laser_damage", "laser_charge", "gyros", "lightning_efficiency",
    "gun_damage", "gun_rate", "lightning_range", "sensors", "servos", "reactor", "capacitor", "jets",
    "gun_mag", "gun_velocity", "mortar_reload", "mortar_velocity", "lightning_chain", "laser_pierce", "mortar_splash",
]
WEAPON_VALUE = {"laser": 3.0, "lightning": 2.0, "mortar": 1.0, "gun": 0.5}
STEER_ANGLES = [0.0, 0.35, 0.7, 1.05, 1.4, 1.75, 2.1, 2.5]


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def hyp(dx, dy):
    return math.sqrt(dx * dx + dy * dy)


class Bot:
    name = "mech-bot v0"

    def __init__(self, hello):
        cfg = hello["config"]
        self.cfg = cfg
        self.rng = random.Random(hello.get("seed", 0))
        self.world = None
        try:
            self.world = World(hello["map"]["dir"], cfg)
        except Exception:
            mf.log("map load failed, running without line of sight:\n" + traceback.format_exc())
        self.reach = float(cfg["loot"]["pickup_radius"])
        self.cube_reach = float(cfg["cubes"]["pickup_radius"])
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
        gc.collect()
        gc.freeze()

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

    def walk_blocked(self, x, y, z, yaw):
        w = self.world
        if w is None:
            return False
        c, s = math.cos(yaw), math.sin(yaw)
        for d in (3.0, 5.5, 8.0):
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

    def zone_safe_radius(self, obs):
        """Circle we should be inside now: the next circle when its shrink is close, else the current one."""
        zn = obs["zone"]
        t = obs["t"]
        if t >= zn["shrink_start"] - 25:
            return zn["next_center"], zn["next_radius"]
        return zn["center"], zn["radius"]

    def pick_upgrade(self, offer):
        ids = [c["id"] for c in offer["choices"]]
        for u in UPGRADE_PRIORITY:
            if u in ids:
                return ids.index(u)
        return 0

    # ================================================================== main
    def act(self, obs):
        t0 = time.perf_counter()
        me = obs["self"]
        t = obs["t"]
        x, y, z = me["pos"]
        st = me["stats"]
        act = {"tick": obs["tick"]}
        hp_frac = me["hp"] / max(1.0, me["max_hp"])

        for e in obs["events"]:
            if e["type"] == "damaged" and e.get("bearing") is not None:
                self.threat_bearing, self.threat_t, self.dmg_t = e["bearing"], t, t
        for s in obs["sounds"]:
            if s["kind"] in ("gun", "laser", "laser_charge", "lightning", "mortar") and s["dist"] != "far" and t - self.threat_t > 1.5:
                self.threat_bearing, self.threat_t = s["bearing"], t

        if obs.get("offer"):
            act["pick"] = self.pick_upgrade(obs["offer"])
        self.remember_items(obs, me)

        slots = me["weapons"]
        held = {w["kind"]: i for i, w in enumerate(slots) if w}

        # ------------------------------------------------------------ targets
        mechs = []
        for m in obs["mechs"]:
            d = hyp(m["pos"][0] - x, m["pos"][1] - y)
            vis = self.mech_los(me, m)
            mechs.append((m, d, vis))
        enemies = [e for e in mechs if e[0]["kind"] == "mech"]
        npcs = [e for e in mechs if e[0]["kind"] == "npc"]

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
                if vis and d < 75 and (hp_frac > 0.5 or m["id"] in self._attackers(obs)):
                    side_target = (m, d, vis)
                    break
            if side_target is None:
                for c in sorted(obs["crates"], key=lambda c: hyp(c["pos"][0] - x, c["pos"][1] - y)):
                    d = hyp(c["pos"][0] - x, c["pos"][1] - y)
                    if d > 70:
                        break
                    cp = c["pos"]
                    if self.los((x, y, z + st.get("muzzle_height", 7)), (cp[0], cp[1], cp[2] + 1.2)):
                        side_target = ({"id": c["id"], "kind": "crate", "pos": cp, "vel": [0, 0, 0], "hp": c["hp"]}, d, True)
                        break

        # ------------------------------------------------------------ movement
        move_yaw, move_mag, boost, jump, label = self.choose_move(obs, me, held, target, side_target, hp_frac)

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
        if move_mag > 0.05:
            act["move"] = [math.cos(move_yaw) * move_mag, math.sin(move_yaw) * move_mag]
        else:
            act["move"] = [0.0, 0.0]
        act["boost"] = bool(boost)
        if jump:
            act["jump"] = True

        # ------------------------------------------------------------ aim and fire
        aim, fire = self.choose_fire(obs, me, held, target or side_target, move_yaw, act)
        act["aim"] = aim
        act["fire"] = fire

        # ------------------------------------------------------------ loot
        self.loot(obs, me, held, act)

        dt = (time.perf_counter() - t0) * 1000
        if dt > 30:
            self.slow_ticks += 1
            if self.slow_ticks < 20:
                mf.log(f"slow tick {obs['tick']}: {dt:.1f} ms")
        mf.draw.label(f"{label} hp{me['hp']:.0f}")
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

        # 1. outside the zone: run home
        if not me["in_zone"]:
            cx, cy = zn["center"]
            return math.atan2(cy - y, cx - x), 1.0, energy > 20, False, "zone!"

        # 2. a mech to fight (seen or remembered)
        if target is not None:
            m, d, vis = target
            tx, ty = m["pos"][0], m["pos"][1]
            to = math.atan2(ty - y, tx - x)
            if "laser" in held:
                lo, hi = 75.0, 165.0
            elif "lightning" in held:
                lo, hi = 12.0, 32.0
            else:
                lo, hi = 40.0, 105.0
            if "lightning" in held and d < 50:
                lo, hi = 10.0, 30.0
            if "lightning" in m.get("weapons", []) and "lightning" not in held:
                lo = max(lo, 55.0)
            radial = 0.0
            if d > hi:
                radial = 1.0
            elif d < lo:
                radial = -1.0
            retreat = hp_frac < 0.3 and m.get("hp", 0) / max(1, m.get("max_hp", 1400)) > hp_frac + 0.1
            if retreat:
                radial = -1.0
            elif not vis and hp_frac < 0.5 and radial > 0:
                radial = 0.0  # hurt and it's out of sight: don't walk into it, let regen work
            if t >= self.strafe_until:
                self.strafe = -self.strafe
                self.strafe_until = t + self.rng.uniform(0.7, 1.9)
            # laser charging and pointed at us: sidestep hard
            dodge = False
            if m.get("charging"):
                back = math.atan2(y - ty, x - tx)
                if abs(wrap(m["aim"][0] - back)) < 0.2:
                    dodge = True
            tang = 1.0 if dodge else 0.8
            vx = radial * math.cos(to) + tang * self.strafe * -math.sin(to)
            vy = radial * math.sin(to) + tang * self.strafe * math.cos(to)
            # stay inside the safe circle while fighting
            if dz > srad * 0.85 and srad > 5:
                k = 1.5 * (dz / max(srad, 1) - 0.85) / 0.15
                vx += k * (scx - x) / max(dz, 1)
                vy += k * (scy - y) / max(dz, 1)
            boost = (dodge or retreat or (t - self.dmg_t < 2.0) or (radial > 0 and d > hi + 60)) and energy > 30
            jump = dodge and self.rng.random() < 0.3
            return math.atan2(vy, vx), 1.0, boost, jump, "fight" if vis else "hunt"

        # 3. not safe for the coming shrink: move in
        margin = max(8.0, min(30.0, srad * 0.15))
        if dz > srad - margin:
            late = t >= zn["shrink_start"] - 8
            return math.atan2(scy - y, scx - x), 1.0, late and energy > 40, False, "zone"

        # 4. loot: better weapon, cubes
        goal = self.loot_goal(obs, me, held, scx, scy, srad)
        if goal is not None:
            gx, gy, why = goal
            d = hyp(gx - x, gy - y)
            return math.atan2(gy - y, gx - x), 1.0 if d > 6 else 0.5, False, False, why

        # 5. npc / crate: hold a shooting range
        if side_target is not None:
            m, d, vis = side_target
            to = math.atan2(m["pos"][1] - y, m["pos"][0] - x)
            want = 25.0 if m["kind"] == "crate" else 35.0
            if d > want + 8:
                return to, 1.0, False, False, "farm"
            if d < want - 10:
                return to + math.pi, 0.6, False, False, "farm"
            return to + math.pi / 2 * self.strafe, 0.4, False, False, "farm"

        # 6. position for the zone; endgame: stand on the final point
        ncx, ncy = zn["next_center"]
        dn = hyp(x - ncx, y - ncy)
        if zn["next_radius"] <= 30:
            if dn > 1.0:
                return math.atan2(ncy - y, ncx - x), min(1.0, dn / 6 + 0.2), dn > 30 and energy > 40, False, "final"
            return 0.0, 0.0, False, False, "final"
        want = max(10.0, zn["next_radius"] * 0.45)
        if dn > want:
            return math.atan2(ncy - y, ncx - x), 1.0, False, False, "drift"
        return 0.0, 0.0, False, False, "hold"

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
            gain = WEAPON_VALUE[k] - (0 if empty else cur)
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
            # keep a laser charge ready if one is cooling down? no: charge only with a target
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
            elif L["cooldown"] <= 0 and in_range and (vis or d < 120):
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
