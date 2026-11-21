"""Precompute how hidden each 4 m spot on the walkable ground is, for stealth mode. Writes conceal.bin.

For every walkable 4 m cell, observers are placed on a ring of 24 directions at 30, 55, 90, 130 and 175 m
(standing on the ground, or on a roof if that spot is inside a building), eye 8 m up. A sample counts as
"sees us" if the line from its eye to our center or top is clear of buildings, bridges and terrain and, for
observers beyond near range (45 m), passes through less than CANOPY_BLOCK_M of tree crown.
conceal = 255 * (1 - fraction of samples that see us). 255 also marks cells we can't stand on.

File format: b"CNC1", uint16 nx, uint16 ny, float32 minx, float32 miny, float32 step, 20-byte sha1 of
world.json, then nx*ny bytes (row-major, row = y).

    python -I tools/precompute_conceal.py [map_dir] [out_path]
"""
import hashlib
import math
import os
import struct
import sys
import time
from array import array
from multiprocessing import Pool

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from world import World  # noqa: E402
from nav import NavGrid  # noqa: E402

CANOPY_BLOCK_M = 4.0
NEAR = 45.0
DIRS = 24
DISTS = (30.0, 55.0, 90.0, 130.0, 175.0)
STEP = 4.0

_G = {}


def canopy_grid(world, w):
    t = w["trees"]
    f = t["fields"]
    n = len(f)
    d = t["data"]
    ix = {k: f.index(k) for k in f}
    N = world.nx * world.ny
    clo = array("f", [1e9]) * N
    chi = array("f", [-1e9]) * N
    for r in range(0, len(d), n):
        row = d[r:r + n]
        cx = row[ix["crownX"]] if "crownX" in ix else row[ix["x"]]
        cy = row[ix["crownY"]] if "crownY" in ix else row[ix["y"]]
        z0 = row[ix["z"]]
        lo, hi = z0 + row[ix["crownBase"]], z0 + row[ix["height"]]
        R = row[ix["crownR"]]
        for j in range(int(cy - R - world.miny), int(cy + R - world.miny) + 2):
            for i in range(int(cx - R - world.minx), int(cx + R - world.minx) + 2):
                if 0 <= i < world.nx and 0 <= j < world.ny:
                    x, y = world.minx + i, world.miny + j
                    if (x - cx) ** 2 + (y - cy) ** 2 <= R * R:
                        k = j * world.nx + i
                        if lo < clo[k]:
                            clo[k] = lo
                        if hi > chi[k]:
                            chi[k] = hi
    return clo, chi


def sees(world, clo, chi, ax, ay, az, bx, by, bz, use_canopy):
    dx, dy, dz = bx - ax, by - ay, bz - az
    d = math.sqrt(dx * dx + dy * dy + dz * dz)
    n = max(1, int(d))
    sx, sy, sz = dx / n, dy / n, dz / n
    seg = d / n
    minx, miny, nx, ny = world.minx - 0.5, world.miny - 0.5, world.nx, world.ny
    base, top, bbase, btop, ground = world.base, world.top, world.bbase, world.btop, world.ground
    x, y, z = ax + sx, ay + sy, az + sz
    leaf = 0.0
    for _ in range(1, n - 1):
        i = int(x - minx)
        j = int(y - miny)
        if 0 <= i < nx and 0 <= j < ny:
            k = j * nx + i
            if base[k] <= z <= top[k] or bbase[k] <= z <= btop[k] or z < ground[k] - 0.5:
                return False
            if use_canopy and clo[k] <= z <= chi[k]:
                leaf += seg
                if leaf >= CANOPY_BLOCK_M:
                    return False
        x += sx
        y += sy
        z += sz
    return True


def _init(map_dir):
    import json
    w = json.load(open(os.path.join(map_dir, "v2", "world.json")))
    cfg = {"mech": {"step_up": 1.2, "height": 9.0, "max_slope_deg": 50}}
    world = World(map_dir, cfg)
    _G["world"] = world
    _G["nav"] = NavGrid(world)
    _G["clo"], _G["chi"] = canopy_grid(world, w)
    counts = {}
    for c in _G["nav"].comp:
        if c:
            counts[c] = counts.get(c, 0) + 1
    _G["main"] = max(counts, key=counts.get)


def _row(J4):
    world, nav, clo, chi = _G["world"], _G["nav"], _G["clo"], _G["chi"]
    nx4 = nav.nx // 2
    out = bytearray([255]) * nx4
    for I4 in range(nx4):
        I, J = 2 * I4, 2 * J4
        if not nav.free(I, J) or nav.comp_at(I, J) != _G["main"]:
            continue
        px, py = nav.center(I, J)
        g = world.ground_at(px, py)
        seen = 0
        total = 0
        for a in range(DIRS):
            c, s = math.cos(a * 2 * math.pi / DIRS), math.sin(a * 2 * math.pi / DIRS)
            for d in DISTS:
                ox, oy = px + c * d, py + s * d
                if world.idx(ox, oy) < 0:
                    continue
                oz = world.surface_at(ox, oy) + 8.0
                total += 1
                far = d > NEAR
                if sees(world, clo, chi, ox, oy, oz, px, py, g + 4.5, far) or sees(world, clo, chi, ox, oy, oz, px, py, g + 8.5, far):
                    seen += 1
        out[I4] = int(round(254 * (1 - seen / max(1, total))))
    return J4, bytes(out)


def main():
    kit = os.path.join(os.path.dirname(ROOT), "mechfight-kit")
    map_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(kit, "arena")
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "conceal.bin")
    _init(map_dir)
    nav = _G["nav"]
    nx4, ny4 = nav.nx // 2, nav.ny // 2
    t0 = time.time()
    with Pool(max(1, (os.cpu_count() or 2) - 1), initializer=_init, initargs=(map_dir,)) as pool:
        rows = dict(pool.map(_row, range(ny4), chunksize=4))
    sha = hashlib.sha1(open(os.path.join(map_dir, "v2", "world.json"), "rb").read()).digest()
    with open(out, "wb") as f:
        f.write(b"CNC1" + struct.pack("<HHfff", nx4, ny4, nav.ox, nav.oy, 4.0) + sha)
        for J4 in range(ny4):
            f.write(rows[J4])
    vals = [v for r in rows.values() for v in r if v != 255]
    vals.sort()
    print(f"wrote {out}: {nx4}x{ny4}, {len(vals)} walkable cells, conceal p10/p50/p90 = "
          f"{vals[len(vals) // 10]}/{vals[len(vals) // 2]}/{vals[9 * len(vals) // 10]}, {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
