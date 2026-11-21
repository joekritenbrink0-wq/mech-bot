"""Print a timeline of one arena match: our state, label, nearest house bots, damage."""
import json, math, os, sys, random, gzip, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import arena as AR
from arena import *  # noqa

spec = sys.argv[1] if len(sys.argv) > 1 else "v1"
seed = int(sys.argv[2]) if len(sys.argv) > 2 else 100000
params = json.loads(json.dumps(AR.PARAMS))
if len(sys.argv) > 3:
    for k, v in json.loads(sys.argv[3]).items():
        params[k] = v
params.setdefault("strong_levels", [])
AR._init()
W = AR._W
mod = load_bot_module(spec)
rng = random.Random(seed)
cfg = W["hello"]["config"]
A = Arena(W["world"], W["nav"], cfg, rng, params)
our_slot = rng.randrange(16)
A.populate(our_slot)
us = A.mechs[our_slot]
squad = Squad(A, us, params, rng)
if params.get("strong_levels"):
    smod = load_bot_module(params["strong_bot"])
    for m in squad.members:
        if m.level in params["strong_levels"]:
            m.strong = AR.StrongBrain(A, m, us, smod, W["hello"], params["strong_noise"][m.level], rng)
h = dict(W["hello"]); h["slot"] = our_slot; h["seed"] = seed
bot = mod.Bot(h)
A.zone = A.zone_now()
dealt_prev = 0
while A.t < 480 and us.alive:
    A.zone = A.zone_now()
    o = A.obs_for(us)
    evs = [e for e in us.events]
    us.events, us.sounds = [], []
    mf.draw.clear()
    act = bot.act(o) or {}
    lab = (mf.draw.take() or {}).get("label", "")
    us.action = act
    squad.update()
    for m in squad.members:
        if m.alive:
            sb = getattr(m, "strong", None)
            m.action = sb.decide(squad) if (sb is not None and m.brain.hunter) else m.brain.decide(squad)
    for m in A.mechs:
        if m.alive: A.apply_one_shots(m)
    for m in A.mechs:
        if m.alive: A.step_weapons(m)
    for _ in range(2):
        for m in A.mechs:
            if m.alive: A.step_mech(m)
        A.push_apart(); A.step_projectiles(); A.t += DT; A.tick += 1; A.zone = A.zone_now()
    A.step_npcs(); A.step_cubes(); A.step_drops()
    A.beams = [b for b in A.beams if A.t - b["t"] < 0.15]
    if A.tick % 20 == 0 or any(e["type"] in ("kill", "took") for e in evs):
        near = sorted([m for m in A.mechs if m.house and m.alive], key=lambda m: hyp(m.pos[0]-us.pos[0], m.pos[1]-us.pos[1]))[:2]
        ns = " ".join(f"[{m.level[:2]}{'H' if m.brain.hunter else ''} {hyp(m.pos[0]-us.pos[0], m.pos[1]-us.pos[1]):.0f}m hp{m.hp:.0f} {'/'.join(m.held())}]" for m in near)
        print(f"t={A.t:5.1f} hp={us.hp:5.0f} e={us.energy:3.0f} {lab:22s} w={'/'.join(us.held())} k={us.kills} dealt={us.dealt:5.0f} pos=({us.pos[0]:.0f},{us.pos[1]:.0f},{us.pos[2]:.0f}) fire={act.get('fire')} {ns}", [e['type'] for e in evs if e['type'] not in ('damaged','hit')])
print("died", us.cause, "at", round(A.t, 1), "taken", {k: round(v) for k, v in us.taken_by.items()})
