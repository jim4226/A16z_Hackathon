"""Live bench: drive the generated firmware over USB serial and record what really happened.

The terminal shows a live force bar (good for a screen recording); the trace is
saved as CSV and the result is logged against Morph's prediction. --simulate
produces a synthetic trace for rehearsal only and is labelled as such everywhere.
"""

from __future__ import annotations

import csv
import os
import random
import sys
import time


def _bar(force, target, limit, width=36):
    scale = max(limit, target * 2, 1e-6)
    n = int(min(force / scale, 1.0) * width)
    t = int(min(target / scale, 1.0) * width)
    cells = ["█" if i < n else "·" for i in range(width)]
    if t < width:
        cells[t] = "|"
    return "".join(cells)


def _samples_serial(port, seconds, target):
    import serial  # pyserial

    with serial.Serial(port, 115200, timeout=0.2) as s:
        time.sleep(2.0)                    # most boards reset when the port opens
        s.reset_input_buffer()
        s.write(f"o\nt{target:.2f}\n".encode())
        time.sleep(1.0)
        s.write(b"g\n")
        t0 = time.time()
        while time.time() - t0 < seconds:
            line = s.readline().decode(errors="ignore").strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) != 4:
                continue
            try:
                yield time.time() - t0, float(parts[1]), float(parts[2]), parts[3]
            except ValueError:
                continue


def _samples_simulated(seconds, target, seed=0):
    rng = random.Random(seed)
    t, f, angle = 0.0, 0.0, 20.0
    contact_angle = 20.0 + rng.uniform(35, 45)
    state = "CLOSING"
    while t < seconds:  # mirrors the firmware: close until target, then bang-bang trim
        if state == "CLOSING":
            if f >= target:
                state = "HOLD"
            else:
                angle += 0.5
        elif f < target * 0.85:
            angle += 0.25
        elif f > target * 1.15:
            angle -= 0.25
        if angle > contact_angle:
            f_cmd = (angle - contact_angle) * 0.9
            f += (f_cmd - f) * 0.35
        f_meas = max(f + rng.gauss(0, 0.04 * max(target, 0.5)), 0.0)
        yield t, f_meas, angle, state
        t += 0.02
        time.sleep(0.004)


def run(project, rev: str, port: str | None, seconds: float = 8.0, simulate: bool = False) -> dict:
    res = project.load_results(rev)
    ep = res.get("electronics") or {}
    if not ep or ep.get("sensor") == "none":
        raise SystemExit(f"{rev} has no force sensor: morph change NAME finger.sensor=fsr402 first")
    target, limit = ep["target_force_n"], ep["limit_n"]
    outdir = os.path.join(project.rev_dir(rev), "bench")
    os.makedirs(outdir, exist_ok=True)
    n = len([f for f in os.listdir(outdir) if f.endswith(".csv")]) + 1
    path = os.path.join(outdir, f"run_{n:02d}{'_SIMULATED' if simulate else ''}.csv")

    src = _samples_simulated(seconds, target) if simulate else _samples_serial(port, seconds, target)
    rows = []
    label = "SIMULATED (no hardware)" if simulate else port
    print(f"bench {rev} on {label}: target {target} N, hard limit {limit} N")
    for t, f, a, st in src:
        rows.append((round(t, 3), round(f, 3), a, st))
        sys.stdout.write(f"\r t={t:5.2f}s  F={f:6.2f} N  {_bar(f, target, limit)}  {st:<8}")
        sys.stdout.flush()
    print()
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["t_s", "force_n", "angle_deg", "state"])
        w.writerows(rows)
    if not rows:
        raise SystemExit("no samples received: check port, baud 115200, and that morph_grip.ino is flashed")

    forces = [r[1] for r in rows]
    reach = next((r[0] for r in rows if r[1] >= 0.95 * target), None)
    tail = sorted(forces[int(len(forces) * 0.6):])
    hold = tail[len(tail) // 2] if tail else 0.0
    peak = max(forces)
    last = [r[1] for r in rows if r[0] >= rows[-1][0] - 1.0]
    obj_max = res["electronics"].get("object_limit_n")
    actual = {
        "hold_force_n": round(hold, 3), "peak_force_n": round(peak, 3),
        "time_to_target_s": reach, "overshoot_pct": round((peak / target - 1) * 100, 1) if target else None,
        "held_at_end": bool(last) and min(last) >= 0.5 * target,
        "under_damage_limit": obj_max is None or peak < obj_max,
        "bench_csv": os.path.relpath(path, project.root), "simulated": simulate,
    }
    entry = project.measure(rev, actual)
    print(f"hold {hold:.2f} N (predicted target {target}), peak {peak:.2f} N, "
          f"reached target in {reach if reach is not None else 'never'} s"
          + ("   [SIMULATED: rehearsal only]" if simulate else ""))
    return entry
