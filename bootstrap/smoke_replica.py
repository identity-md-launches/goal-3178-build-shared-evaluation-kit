"""bootstrap/smoke_replica.py - bounded, seeded numba workload to price the replica toolchain.

Credit: Taylor (gtmcknight), solved-policy research,
github.com/gtmcknight/amm-sim @ 4812a6382311624e1506aca131ca35c378f930d9 (research/solved).

This is NOT replica2 and its numbers are not edges of any policy: setup must stand alone, so
this is a synthetic constant-fee AMM loop with the same shape as a replica run (10,000 steps,
a GBM price, an arbitrage step and Poisson/lognormal retail flow per step). It answers three
questions for the next wave: does numba compile here, is a seeded run reproducible, and what
does one 10,000-step run cost on one core. It benchmarks 4 runs first and caps the main
batch so the whole script stays within --budget seconds (default 4, hard cap 256 runs).
"""
import argparse
import json
import math
import sys
import time

import numba
import numpy as np

N_STEPS = 10000


@numba.njit(cache=False)
def one_run(seed, n_steps, fee):
    np.random.seed(seed)
    x = 100.0
    y = 10000.0
    k = x * y
    p = 100.0
    sigma = 0.000882 + (0.001008 - 0.000882) * np.random.random()
    lam = 0.6 + 0.4 * np.random.random()
    mean_size = 19.0 + 2.0 * np.random.random()
    mu_ln = math.log(mean_size) - 0.5 * 1.2 * 1.2
    g = 1.0 - fee
    edge = 0.0
    for _ in range(n_steps):
        p *= math.exp(-0.5 * sigma * sigma + sigma * np.random.standard_normal())
        spot = y / x
        if spot < p * g:  # pool cheap: arbitrageur buys x
            x_new = math.sqrt(k / (p * g))
            dx = x - x_new
            dy = (k / x_new - y) / g
            edge += dy - dx * p
            x = x_new
            y = y + dy
            k = x * y
        elif spot > p / g:  # pool rich: arbitrageur sells x
            x_new = math.sqrt(k * g / p)
            dx = (x_new - x) / g
            dy = y - k / x_new
            edge += dx * p - dy
            x = x + dx
            y = y - dy
            k = x * y
        n = np.random.poisson(lam)
        for _ in range(n):
            size = math.exp(mu_ln + 1.2 * np.random.standard_normal())
            if np.random.random() < 0.5:  # retail buys x with `size` of y
                net = size * g
                x_new = k / (y + net)
                dx = x - x_new
                edge += size - dx * p
                x = x_new
                y = y + size
            else:  # retail sells x worth `size` of y
                dx = size / p
                x_new = x + dx * g
                dy = y - k / x_new
                edge += dx * p - dy
                x = x + dx
                y = y - dy
            k = x * y
    return edge


@numba.njit(cache=False)
def batch(seed0, n, n_steps, fee):
    out = np.empty(n)
    for i in range(n):
        out[i] = one_run(seed0 + i, n_steps, fee)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=4.0, help="seconds for the capped batch")
    ap.add_argument("--seed0", type=int, default=10000, help="first synthetic seed (not an engine seed)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    t = time.perf_counter()
    batch(a.seed0, 1, 16, 0.003)  # compile on a 16-step run
    compile_s = time.perf_counter() - t

    t = time.perf_counter()
    tiny = batch(a.seed0, 4, N_STEPS, 0.003)
    tiny_s = time.perf_counter() - t
    rate = 4 / tiny_s
    n = int(max(4, min(256, a.budget * rate)))

    t = time.perf_counter()
    r1 = batch(a.seed0, n, N_STEPS, 0.003)
    main_s = time.perf_counter() - t
    r2 = batch(a.seed0, 4, N_STEPS, 0.003)
    reproducible = bool(np.array_equal(r1[:4], r2) and np.array_equal(tiny, r2))
    finite = bool(np.all(np.isfinite(r1)))

    res = {
        "what": "synthetic numba AMM loop, not replica2; values are not policy edges",
        "python": "%d.%d.%d" % sys.version_info[:3],
        "numpy": np.__version__,
        "numba": numba.__version__,
        "threads_used": 1,
        "n_steps": N_STEPS,
        "compile_seconds": round(compile_s, 3),
        "tiny_runs": 4,
        "tiny_seconds": round(tiny_s, 4),
        "runs": n,
        "seconds": round(main_s, 4),
        "runs_per_second_per_core": round(n / main_s, 1),
        "seed0": a.seed0,
        "mean": float(r1.mean()),
        "first4": [float(v) for v in r1[:4]],
        "reproducible": reproducible,
        "finite": finite,
        "ok": reproducible and finite,
    }
    line = json.dumps(res, sort_keys=True)
    if a.out:
        with open(a.out, "w") as f:
            f.write(line + "\n")
    print("smoke_replica: " + line)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
