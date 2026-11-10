"""bootstrap/smoke_engine.py - one-seed engine smoke: VanillaStrategy vs VanillaStrategy, seed 9700000.

Credit: Taylor (gtmcknight), solved-policy research,
github.com/gtmcknight/amm-sim @ 4812a6382311624e1506aca131ca35c378f930d9 (research/solved).
The run shape (one amm_sim_rs.run_batch(X, Vanilla, configs, 8) call, gas counters reset
before and read after) follows Taylor's harness.py from release sim-6b6921f8.

It proves the prebuilt engine, the pinned solc and the harness package work together, and
prices one engine run. It compiles the checkout's own contracts/src/VanillaStrategy.sol (the
30 bps normalizer) and runs it against itself on exactly one seed. The seed is fixed: any value
other than 9700000 is refused, so setup can never spend a screening or test seed.

The config for a seed is MatchRunner._build_configs transcribed for one index: with
rng = numpy.random.default_rng(seed), draw retail_mean_size, retail_arrival_rate and gbm_sigma
in that order from BASELINE_VARIANCE. Building the list up to index 9700000 would allocate
9.7 million configs, so the transcription is checked against the real _build_configs on
indices 0..3 (config construction only; no simulation runs on those).
"""
import argparse
import hashlib
import json
import sys
import time

ALLOWED_SEED = 9700000
N_WORKERS = 8
FIELDS = ("n_steps", "initial_price", "initial_x", "initial_y", "gbm_mu", "gbm_sigma", "gbm_dt",
          "retail_arrival_rate", "retail_mean_size", "retail_size_sigma", "retail_buy_prob", "seed")


def config_for(seed):
    import numpy as np
    from amm_competition.competition.config import BASELINE_VARIANCE as V, build_config

    rng = np.random.default_rng(seed=seed)
    retail_mean_size = rng.uniform(V.retail_mean_size_min, V.retail_mean_size_max)
    retail_arrival_rate = rng.uniform(V.retail_arrival_rate_min, V.retail_arrival_rate_max)
    gbm_sigma = rng.uniform(V.gbm_sigma_min, V.gbm_sigma_max)
    return build_config(seed=seed, gbm_sigma=gbm_sigma, retail_arrival_rate=retail_arrival_rate,
                        retail_mean_size=retail_mean_size)


def check_transcription():
    """Compare config_for(i) with MatchRunner._build_configs()[i] for i in 0..3, field by field."""
    from amm_competition.competition.config import BASELINE_VARIANCE, build_base_config
    from amm_competition.competition.match import MatchRunner

    runner = MatchRunner(n_simulations=4, config=build_base_config(seed=None),
                         n_workers=N_WORKERS, variance=BASELINE_VARIANCE)
    compared = 0
    for i, ref in enumerate(runner._build_configs()):
        mine = config_for(i)
        for f in FIELDS:
            if not hasattr(ref, f):
                continue
            compared += 1
            if getattr(ref, f) != getattr(mine, f):
                raise SystemExit("smoke_engine: config transcription differs at index %d field %s" % (i, f))
    return compared


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=ALLOWED_SEED)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if a.seed != ALLOWED_SEED:
        print("smoke_engine: REFUSED seed %d: the setup smoke may only run engine seed %d"
              % (a.seed, ALLOWED_SEED), file=sys.stderr)
        return 2

    import amm_sim_rs
    from amm_competition.evm.baseline import get_vanilla_bytecode_and_abi

    t = time.perf_counter()
    bytecode = get_vanilla_bytecode_and_abi()[0]
    compile_s = time.perf_counter() - t
    vanilla = list(bytecode)
    compared = check_transcription()
    cfg = config_for(a.seed)

    amm_sim_rs.gas_stats_reset()
    t = time.perf_counter()
    result = amm_sim_rs.run_batch(vanilla, vanilla, [cfg], N_WORKERS)
    run_s = time.perf_counter() - t
    stats = amm_sim_rs.gas_stats()
    rows = list(result.results)
    sub = [s for s in stats if s["role"] == "Submission"]
    swap = [s for s in sub if s["callback"] == "afterSwap"]
    errors = {k: sum(s[k] for s in sub) for k in ("oog", "revert", "other_halt", "tx_error", "bad_return")}
    ok = len(rows) == 1 and rows[0].seed == a.seed and not any(errors.values())

    res = {
        "what": "VanillaStrategy vs VanillaStrategy, one engine seed, setup smoke only",
        "seed": a.seed,
        "n_sims": len(rows),
        "edge_submission": rows[0].edges["submission"] if rows else None,
        "edge_normalizer": rows[0].edges["normalizer"] if rows else None,
        "afterSwap_calls": sum(s["calls"] for s in swap),
        "afterSwap_peak_gas": max((s["max_ok_gas"] for s in swap), default=0),
        "submission_errors": errors,
        "vanilla_runtime_sha256": hashlib.sha256(bytes(bytecode)).hexdigest(),
        "vanilla_runtime_bytes": len(bytecode),
        "config_fields_checked_against_MatchRunner": compared,
        "compile_seconds": round(compile_s, 3),
        "run_seconds": round(run_s, 3),
        "n_workers": N_WORKERS,
        "ok": ok,
    }
    line = json.dumps(res, sort_keys=True)
    if a.out:
        with open(a.out, "w") as f:
            f.write(line + "\n")
    print("smoke_engine: " + line)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
