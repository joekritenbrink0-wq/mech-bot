"""Feed the kit's recorded stream to the bot in-process: per-tick timing and a sample of decisions.

    python -I tools/run_stream.py [path/to/stream.jsonl.gz] [map_dir]
"""
import gzip
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import mechfight as mf  # noqa: E402
import bot  # noqa: E402

KIT = os.path.join(os.path.dirname(os.path.dirname(HERE)), "mechfight-kit")
stream = sys.argv[1] if len(sys.argv) > 1 else os.path.join(KIT, "check", "stream.jsonl.gz")
map_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(KIT, "arena")

msgs = [json.loads(l) for l in gzip.open(stream, "rt")]
hello = next(m for m in msgs if m.get("type") == "hello")
hello["map"]["dir"] = map_dir
t = time.perf_counter()
b = bot.Bot(hello)
print(f"init {1000 * (time.perf_counter() - t):.0f} ms")
times = []
for m in msgs:
    if m.get("type") != "obs":
        continue
    mf.draw.clear()
    t = time.perf_counter()
    a = b.act(m)
    times.append(1000 * (time.perf_counter() - t))
    json.dumps(a)
    lab = mf.draw.take() or {}
    if m["tick"] % 20 == 0:
        me = m["self"]
        print(f"t={m['t']:5.1f} hp={me['hp']:6.0f} {lab.get('label', ''):18s} move={[round(v, 2) for v in a['move']]} "
              f"boost={a['boost']} aim={[round(v, 2) for v in a['aim']]} fire={a['fire']}")
times.sort()
n = len(times)
print(f"ticks {n}  p50 {times[n // 2]:.2f} ms  p99 {times[int(n * 0.99)]:.2f} ms  max {times[-1]:.2f} ms")
