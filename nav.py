"""Ground-level navigation: a 2 m walk grid built from the World, time-sliced A*, and path following.

A cell is blocked if any of its four 1 m lattice points has a solid (or a low bridge) between
ground + step_up and ground + mech height, or the ground inside it is too steep. Blocked cells are
dilated by one cell so a 2.5 m radius mech centered on a free cell clears walls.
"""
import heapq
import math

SQ2 = math.sqrt(2.0)
NB = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
      (1, 1, SQ2), (1, -1, SQ2), (-1, 1, SQ2), (-1, -1, SQ2))


class NavGrid:
    def __init__(self, world, margin=6.0):
        self.w = world
        self.cell = 2.0
        self.nx = world.nx // 2
        self.ny = world.ny // 2
        self.ox = world.minx + 0.5  # center of cell (0, 0) in world x
        self.oy = world.miny + 0.5
        NX, NY = self.nx, self.ny
        wnx = world.nx
        g, top, base, btop, bbase = world.ground, world.top, world.base, world.btop, world.bbase
        su, hh = world.step_up, world.height
        raw = bytearray(NX * NY)
        m = int(margin / 2.0)
        for J in range(NY):
            for I in range(NX):
                if I < m or J < m or I >= NX - m or J >= NY - m:
                    raw[J * NX + I] = 1
                    continue
                k0 = (2 * J) * wnx + 2 * I
                gmin, gmax = 1e9, -1e9
                bad = False
                for k in (k0, k0 + 1, k0 + wnx, k0 + wnx + 1):
                    gk = g[k]
                    if gk < gmin:
                        gmin = gk
                    if gk > gmax:
                        gmax = gk
                    lo, hi = gk + su, gk + hh
                    if (top[k] > lo and base[k] < hi) or (btop[k] > lo and bbase[k] < hi):
                        bad = True
                        break
                if bad or gmax - gmin > 2.2:
                    raw[J * NX + I] = 1
        # dilate by one cell
        blk = bytearray(raw)
        for J in range(1, NY - 1):
            row = J * NX
            for I in range(1, NX - 1):
                k = row + I
                if raw[k]:
                    continue
                if (raw[k - 1] or raw[k + 1] or raw[k - NX] or raw[k + NX] or
                        raw[k - NX - 1] or raw[k - NX + 1] or raw[k + NX - 1] or raw[k + NX + 1]):
                    blk[k] = 1
        self.blk = blk
        self.raw = raw
        # connected regions (8-neighborhood without corner cutting is close enough to 4-neighborhood here)
        comp = [0] * (NX * NY)
        label = 0
        for k0 in range(NX * NY):
            if blk[k0] or comp[k0]:
                continue
            label += 1
            comp[k0] = label
            stack = [k0]
            while stack:
                k = stack.pop()
                I, J = k % NX, k // NX
                for k2, ok in ((k + 1, I + 1 < NX), (k - 1, I > 0), (k + NX, J + 1 < NY), (k - NX, J > 0)):
                    if ok and not blk[k2] and not comp[k2]:
                        comp[k2] = label
                        stack.append(k2)
        self.comp = comp

    def comp_at(self, I, J):
        if 0 <= I < self.nx and 0 <= J < self.ny:
            return self.comp[J * self.nx + I]
        return 0

    # ------------------------------------------------------------------ cells
    def cell_of(self, x, y):
        I = int((x - self.ox) / 2.0 + 0.5)
        J = int((y - self.oy) / 2.0 + 0.5)
        return I, J

    def center(self, I, J):
        return self.ox + 2.0 * I, self.oy + 2.0 * J

    def free(self, I, J):
        return 0 <= I < self.nx and 0 <= J < self.ny and not self.blk[J * self.nx + I]

    def nearest_free(self, I, J, rmax=6, comp=0):
        """Nearest free cell (optionally inside region `comp`) within rmax cells."""
        if self.free(I, J) and (not comp or self.comp_at(I, J) == comp):
            return I, J
        for r in range(1, rmax + 1):
            best = None
            for dI in range(-r, r + 1):
                for dJ in (-r, r) if abs(dI) != r else range(-r, r + 1):
                    if self.free(I + dI, J + dJ) and (not comp or self.comp_at(I + dI, J + dJ) == comp):
                        d = dI * dI + dJ * dJ
                        if best is None or d < best[0]:
                            best = (d, I + dI, J + dJ)
            if best:
                return best[1], best[2]
        return None

    def line_clear(self, x0, y0, x1, y1, raw=False):
        """Straight walk from (x0, y0) to (x1, y1) stays on free cells."""
        grid = self.raw if raw else self.blk
        d = math.hypot(x1 - x0, y1 - y0)
        n = max(1, int(d / 1.0))
        NX = self.nx
        for s in range(1, n + 1):
            f = s / n
            I, J = self.cell_of(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f)
            if not (0 <= I < NX and 0 <= J < self.ny) or grid[J * NX + I]:
                return False
        return True


class PathJob:
    """Weighted A* that runs in slices: call step(budget_s) each tick until it returns True."""

    def __init__(self, nav, start, goal, weight=1.4, max_nodes=90000):
        self.nav = nav
        self.goal = goal
        self.weight = weight
        self.max_nodes = max_nodes
        self.done = False
        self.path = None
        self.expanded = 0
        s = nav.nearest_free(*start)
        g = None
        if s is not None:
            c = nav.comp_at(*s)
            g = nav.nearest_free(*goal, rmax=12, comp=c) or nav.nearest_free(*goal, rmax=40, comp=c)
        if s is None or g is None:
            self.done = True
            return
        self.s, self.g = s, g
        NX = nav.nx
        self.sk, self.gk = s[1] * NX + s[0], g[1] * NX + g[0]
        self.gcost = {self.sk: 0.0}
        self.came = {}
        self.open = [(self._h(s[0], s[1]), self.sk)]
        self.closed = set()

    def _h(self, I, J):
        dx, dy = abs(I - self.g[0]), abs(J - self.g[1])
        return self.weight * (max(dx, dy) + (SQ2 - 1) * min(dx, dy))

    def step(self, budget_s):
        import time
        if self.done:
            return True
        nav = self.nav
        NX, NY, blk = nav.nx, nav.ny, nav.blk
        t_end = time.perf_counter() + budget_s
        openh, gcost, came, closed = self.open, self.gcost, self.came, self.closed
        gk = self.gk
        n = 0
        while openh:
            _, k = heapq.heappop(openh)
            if k in closed:
                continue
            if k == gk:
                self._finish(k)
                return True
            closed.add(k)
            self.expanded += 1
            I, J = k % NX, k // NX
            gk0 = gcost[k]
            for dI, dJ, c in NB:
                I2, J2 = I + dI, J + dJ
                if I2 < 0 or J2 < 0 or I2 >= NX or J2 >= NY:
                    continue
                k2 = J2 * NX + I2
                if blk[k2] or k2 in closed:
                    continue
                if dI and dJ and (blk[J * NX + I2] or blk[J2 * NX + I]):
                    continue  # no corner cutting
                ng = gk0 + c
                if ng < gcost.get(k2, 1e18):
                    gcost[k2] = ng
                    came[k2] = k
                    heapq.heappush(openh, (ng + self._h(I2, J2), k2))
            n += 1
            if (n & 63) == 0:
                if self.expanded > self.max_nodes:
                    self.done = True
                    return True
                if time.perf_counter() > t_end:
                    return False
        self.done = True
        return True

    def _finish(self, k):
        NX = self.nav.nx
        cells = [k]
        while k in self.came:
            k = self.came[k]
            cells.append(k)
        cells.reverse()
        self.path = [self.nav.center(c % NX, c // NX) for c in cells]
        self.done = True


class Follower:
    """Keeps one path toward a goal, replans when the goal moves or we leave the path."""

    def __init__(self, nav):
        self.nav = nav
        self.job = None
        self.path = None
        self.goal = None
        self.idx = 0
        self.t_plan = -99.0
        self.fails = 0

    def direction(self, x, y, gx, gy, t, budget_s):
        """Heading toward (gx, gy) along a planned path; None if no path is ready (caller steers directly)."""
        nav = self.nav
        if nav.line_clear(x, y, gx, gy) and math.hypot(gx - x, gy - y) < 60:
            self.path = None
            return math.atan2(gy - y, gx - x)
        need = (self.goal is None or math.hypot(gx - self.goal[0], gy - self.goal[1]) > 12
                or (self.path is None and self.job is None and t - self.t_plan > 1.5)
                or (self.path is not None and t - self.t_plan > 6.0))
        if need and self.job is None:
            self.job = PathJob(nav, nav.cell_of(x, y), nav.cell_of(gx, gy))
            self.goal = (gx, gy)
            self.t_plan = t
        if self.job is not None:
            if self.job.step(budget_s):
                if self.job.path:
                    self.path = self.job.path
                    self.idx = 0
                    self.fails = 0
                else:
                    self.fails += 1
                    self.path = None
                self.job = None
        if not self.path:
            return None
        p = self.path
        # advance to the closest waypoint ahead of us
        best_i, best_d = self.idx, 1e18
        for i in range(self.idx, min(len(p), self.idx + 30)):
            d = (p[i][0] - x) ** 2 + (p[i][1] - y) ** 2
            if d < best_d:
                best_i, best_d = i, d
        if best_d > 15.0 ** 2:
            self.path = None  # we left the path: replan next tick
            return None
        self.idx = best_i
        # aim at the farthest waypoint within 24 m we can walk to in a straight line
        tgt = p[min(len(p) - 1, best_i + 1)]
        for i in range(min(len(p) - 1, best_i + 14), best_i, -1):
            if (p[i][0] - x) ** 2 + (p[i][1] - y) ** 2 < 24.0 ** 2 and nav.line_clear(x, y, p[i][0], p[i][1]):
                tgt = p[i]
                break
        if best_i >= len(p) - 2 and math.hypot(gx - x, gy - y) < 6:
            return math.atan2(gy - y, gx - x)
        return math.atan2(tgt[1] - y, tgt[0] - x)
