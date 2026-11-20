# mech-bot

A bot for the Builders Mech Royale track (mechfight/1). Python 3.13, standard library only.

| File | What it does |
| --- | --- |
| `bot.py` | The bot: targeting, movement, aim and fire, loot, upgrades |
| `world.py` | Map model from `/arena`: terrain, building and bridge grids, line of sight, walkability |
| `mechfight.py` | The kit's protocol wrapper (unchanged) |
| `tools/run_stream.py` | Replays the kit's recorded stream in-process: timing and a sample of decisions |
| `tools/test_geometry.py` | Checks aim lead, laser charge and release, mortar ballistics, upgrade picks |

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
```

The scorer also runs the check inside Docker with the match sandbox flags (see the kit README).
