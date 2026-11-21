# mech-bot

A bot for the Builders Mech Royale track (mechfight/1). Python 3.13, standard library only.

| File | What it does |
| --- | --- |
| `bot.py` | The bot: targeting, movement, aim and fire, loot, upgrades |
| `world.py` | Map model from `/arena`: terrain, building and bridge grids, line of sight, walkability |
| `nav.py` | 2 m walk grid, connected regions, time-sliced A*, path following |
| `mechfight.py` | The kit's protocol wrapper (unchanged) |
| `tools/run_stream.py` | Replays the kit's recorded stream in-process: timing and a sample of decisions |
| `tools/test_geometry.py` | Checks aim lead, laser charge and release, mortar ballistics, upgrade picks, drop/zone/final-point decisions |
| `tools/sim.py` | Approximate duel simulator on the real map against house-style hunters; compares bot versions |
| `tools/arena.py` | Full practice-format match simulator (15-bot squad, zone, loot, crates, drones, cubes, upgrades, drops) and a 16-bot free-for-all mode; parallel |
| `tools/trace_match.py` | Second-by-second timeline of one arena match |

## Practice simulator (tools/arena.py)

The squad model is a guess calibrated to the real practice scores we have:

| Bot | Real practice | Sim (calibrated) |
| --- | --- | --- |
| starter (kit example) | 1.15 | about 0.8 |
| v0 | 3.94 | about 4.2 to 4.6 |
| v1 (commit 9f4e5fc) | 3.79 | about 4.4 |

Calibrated settings: one hunter at the start plus one per two kills, everyone hunts after 300 s, non-hunters
never start or return fights, house aim noise 3x the base profiles. Every new real score should be added to
this table and the settings rechecked.

```sh
python -I tools/arena.py --bots "v1,v1+USE_DISENGAGE=False" --matches 160 --seed 6   # A/B with module switches
python -I tools/arena.py --ffa "v1:8,starter:8" --matches 21                            # free-for-all
python -I tools/trace_match.py v1 600001                                               # one match timeline
```

With 160 matches a score has about +-0.2 of noise; trust differences of 0.5 or more.

## v2 changes (on top of v1), chosen with the practice simulator

- Combat movement never leaves the zone (hard penalty) and stays inside the next circle (graded penalty).
- Keeps lightning holders beyond 58 m and boosts away when one is close.
- Weapons that upgrade the loadout and cubes pull combat movement toward them, so kills' drops get picked up.
- Disengage switched off (scored lower in two sim A/B runs); hold-fire-to-regen kept for when it is on.
- Sim (calibrated): v2 about 6.5 vs v0 about 4.6 and v1 (as submitted) about 4.4.

## v1 changes (on top of v0)

- Path planning around buildings for every long walk (zone, loot, drops, hunting an unseen enemy).
- Combat movement picks one of 12 headings (or standing) by exposure to every known threat, line of fire to
  the target, range band and zone. It breaks line of sight when a laser charges at it and peeks with a full laser charge.
- Disengages when losing (hp under 33%), retreats to cover and regenerates, comes back above 75%.
- Captures supply drops when no enemy is near them (full heal, 3 cubes).
- Fix: in the last zone phases it now stops on the final point instead of running circles around it.
- Per-tick time guard (skips optional work past 20 ms) and garbage collection between ticks.

Sim, gun only against a gun + laser hunter: v1 beats a medium hunter 23/24 (v0 5/24) and a hard one 20/32
over two seed sets (v0 0/32). With a laser both are about even 1v1; v1 survives two hunters more often.

## v0 behavior

- Leads gun shots and fires only when the real torso aim is on target.
- Laser: charges with a target in range, holds the charge, releases at full charge (or early to finish a target).
- Lightning when the target is in its cone and range; mortar for targets behind cover or while the gun reloads.
- Picks up weapons (laser, then lightning, then mortar), remembers pickups and cubes it has seen.
- Keeps a range band that suits its weapons, strafes, sidesteps a laser charging at it, retreats when losing.
- Shoots drones and crates for cubes when no enemy mech is around.
- Moves into the next zone circle before it shrinks; in the last phases stands on the final circle's center.
- Steers around buildings with the map and has an unstick routine (jump, jet, change side).
- Upgrades: plating, armor, nanites, laser upgrades, gyros, then the rest.

## Check it

```sh
python -I tools/test_geometry.py
python -I tools/run_stream.py
python ../mechfight-kit/check/check.py -- python -u bot.py
python -I tools/sim.py --bots v1,v0 --weapons gun,none --difficulty hard --matches 16
```

The scorer also runs the check inside Docker with the match sandbox flags (see the kit README).
