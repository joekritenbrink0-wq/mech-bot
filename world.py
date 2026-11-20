"""Static map model: terrain heights, solid (building/stadium) and bridge grids, line of sight, walkability.

Grids share the terrain's vertex lattice: index (i, j) is the point x = minX + i, y = minY + j (1 m step).
Each solid cell keeps the lowest base and highest top of the prisms covering it (good enough for campus
buildings, which stand on the ground). Bridges live in their own grid so you can walk and shoot under them.
"""
import json
import math
import os
from array import array

NONE_LO = 1e9
NONE_HI = -1e9


class World:
    def __init__(self, map_dir, cfg):
        with open(os.path.join(map_dir, "v2", "world.json")) as f:
            w = json.load(f)
        t = w["terrain"]
        self.minx, self.miny = float(t["minX"]), float(t["minY"])
        self.nx, self.ny = int(t["nx"]), int(t["ny"])
        scale = float(t.get("scale", 0.01))
        raw = array("h")
        with open(os.path.join(map_dir, "v2", t.get("file", "terrain.i16")), "rb") as f:
            raw.frombytes(f.read())
        if raw.itemsize == 2 and array("h", [1]).tobytes() != b"\x01\x00":
            raw.byteswap()  # file is little-endian
        self.ground = array("f", (v * scale for v in raw))
        n = self.nx * self.ny
        self.base = array("f", [NONE_LO]) * n
        self.top = array("f", [NONE_HI]) * n
        self.bbase = array("f", [NONE_LO]) * n
        self.btop = array("f", [NONE_HI]) * n
        for p in w["prisms"]:
            if p.get("kind") == "bridge":
                self._fill(p, self.bbase, self.btop)
            else:
                self._fill(p, self.base, self.top)
        m = cfg.get("mech", {})
        self.step_up = float(m.get("step_up", 1.2))
        self.height = float(m.get("height", 9.0))
        self.max_rise = math.tan(math.radians(float(m.get("max_slope_deg", 50))))

    # ------------------------------------------------------------------ rasterizing
    def _fill(self, p, lo_arr, hi_arr):
        lo, hi = float(p["base"]), float(p["top"])
        edges = []
        ymin, ymax = 1e18, -1e18
        for ring in [p["ring"]] + list(p.get("holes") or []):
            k = len(ring) // 2
            for e in range(k):
                x1, y1 = ring[2 * e], ring[2 * e + 1]
                x2, y2 = ring[2 * ((e + 1) % k)], ring[2 * ((e + 1) % k) + 1]
                if y1 != y2:
                    edges.append((x1, y1, x2, y2))
                ymin = min(ymin, y1)
                ymax = max(ymax, y1)
        if not edges:
            return
        nx, minx, miny = self.nx, self.minx, self.miny
        j0 = max(0, math.ceil(ymin - miny))
        j1 = min(self.ny - 1, math.floor(ymax - miny))
        for j in range(j0, j1 + 1):
            yc = miny + j
            xs = []
            for x1, y1, x2, y2 in edges:
                if (y1 <= yc < y2) or (y2 <= yc < y1):
                    xs.append(x1 + (yc - y1) * (x2 - x1) / (y2 - y1))
            if len(xs) < 2:
                continue
            xs.sort()
            row = j * nx
            for a in range(0, len(xs) - 1, 2):
                i0 = max(0, math.ceil(xs[a] - minx))
                i1 = min(nx - 1, math.floor(xs[a + 1] - minx))
                for i in range(i0, i1 + 1):
                    k = row + i
                    if lo < lo_arr[k]:
                        lo_arr[k] = lo
                    if hi > hi_arr[k]:
                        hi_arr[k] = hi

    # ------------------------------------------------------------------ queries
    def idx(self, x, y):
        i = int(x - self.minx + 0.5)
        j = int(y - self.miny + 0.5)
        if 0 <= i < self.nx and 0 <= j < self.ny:
            return j * self.nx + i
        return -1

    def ground_at(self, x, y):
        fx, fy = x - self.minx, y - self.miny
        i, j = int(fx), int(fy)
        if i < 0 or j < 0 or i >= self.nx - 1 or j >= self.ny - 1:
            k = self.idx(x, y)
            return self.ground[k] if k >= 0 else 0.0
        tx, ty = fx - i, fy - j
        g, nx = self.ground, self.nx
        k = j * nx + i
        a = g[k] + (g[k + 1] - g[k]) * tx
        b = g[k + nx] + (g[k + nx + 1] - g[k + nx]) * tx
        return a + (b - a) * ty

    def surface_at(self, x, y):
        """Highest standable surface at (x, y): roof top if a solid is there, else ground."""
        k = self.idx(x, y)
        if k < 0:
            return 0.0
        g = self.ground[k]
        return max(g, self.top[k]) if self.top[k] > NONE_HI else g

    def los(self, ax, ay, az, bx, by, bz, step=1.0, skip_start=2.0, skip_end=1.5):
        """True if the segment a->b is not blocked by buildings, bridges or terrain (trees ignored: they do not stop shots)."""
        dx, dy, dz = bx - ax, by - ay, bz - az
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        if d < 1e-6:
            return True
        n = int(d / step)
        if n < 1:
            return True
        sx, sy, sz = dx / n, dy / n, dz / n
        s0 = int(skip_start / (d / n)) + 1
        s1 = n - int(skip_end / (d / n))
        minx, miny, nx, ny = self.minx - 0.5, self.miny - 0.5, self.nx, self.ny
        base, top, bbase, btop, ground = self.base, self.top, self.bbase, self.btop, self.ground
        x, y, z = ax + sx * s0, ay + sy * s0, az + sz * s0
        for _ in range(s0, s1):
            i = int(x - minx)
            j = int(y - miny)
            if 0 <= i < nx and 0 <= j < ny:
                k = j * nx + i
                if base[k] <= z <= top[k] or bbase[k] <= z <= btop[k] or z < ground[k] - 0.5:
                    return False
            x += sx
            y += sy
            z += sz
        return True

    def blocked(self, x, y, feet_z, dist=1.0):
        """Would a mech whose feet are at feet_z be unable to stand at (x, y) after walking `dist` m?"""
        k = self.idx(x, y)
        if k < 0:
            return True
        lo, hi = feet_z + self.step_up, feet_z + self.height
        if self.top[k] > lo and self.base[k] < hi:
            return True
        if self.btop[k] > lo and self.bbase[k] < hi:
            return True
        if self.ground[k] > lo + self.max_rise * dist:
            return True
        return False
