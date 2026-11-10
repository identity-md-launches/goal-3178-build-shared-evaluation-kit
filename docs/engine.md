<!-- Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9 research/solved, solved-policy research. -->
# Engine screening evaluation

`engine_eval.run_batch(seeds, strategy_path)` returns ordered dictionaries with
`seed`, a finite **unrounded** `edge`, and integer `after_swap_peak_gas`. The edge
is the engine's `edges["submission"]`, not submission minus Vanilla's edge.
Duplicates are retained and simulated independently. `[]` returns `[]` without
loading the engine, compiler, or source. The entire input iterable is checked
before compilation or execution: only integer seeds **9700000..9700511** are
accepted (booleans and floats are rejected). All 512 seeds are **screening-only**;
none is a holdout or an independent final evaluation.

```sh
timeout 470 bash setup_fast.sh --platform linux-x86_64 --cache scratch/cache --deadline 420
python3 -B reference/materialize.py --out scratch/reference
. scratch/env.sh
timeout 470 python -B -c 'import engine_eval; print(engine_eval.run_batch([9700000], "scratch/reference/qt1/Strategy.sol"))'
```

Use a candidate Solidity path in place of qt1. The source must define `Strategy`,
using the upstream compiler's base contracts (`AMMStrategyBase.sol`,
`IAMMStrategy.sol`). Compiler validation and compilation errors propagate;
there is no fallback strategy. Source bytes, base-contract digests, and compiler
implementation identify the in-process compilation cache. The compiler binary
is hash-checked against the foundation manifest on each cache lookup. No solc
installation occurs implicitly. Compilation is reused across scoring rounds;
no seeds outside the caller's current request are simulated or prefetched.

Each seed is a separate
`amm_sim_rs.run_batch(X, VanillaStrategy, [config], 8)` call using the installed
release wheel. A module lock protects reset/run/read of process-global gas
counters. Do not call the native engine directly from other threads while this
module is running. Different processes have separate counters. Gas is the maximum
`max_ok_gas` among **Submission/afterSwap** entries for that singleton call.
Initialization gas and Baseline gas never enter this metric. Missing swap
counters, unsuccessful calls, misaligned/missing/extra results, nonfinite edges,
and all OOG/revert/other-halt/transaction/bad-return counters cause failure.
Errors in either participant are rejected; engine exceptions also propagate.

Configuration reuses `bootstrap.smoke_engine.config_for`, with NumPy
`default_rng(seed)` draws for retail mean size, arrival rate, then GBM sigma.
`config_equivalence()` compares all 12 fields with upstream
`MatchRunner._build_configs()` at indices 0..3, constructing only four configs
and **never simulating those indices**. Bank provenance records all 512 allowed
configs and their digest; constructing configs is not simulation. No list of
9.7 million configs is allocated.

## Bank generation and resumption

The file `baselines/engine/qt1-9700000-9700511.json` has the bank schema from
`docs/contracts.md`, plus `status`, `screening_only`, and exact `remaining_seeds`.
Only `status: "complete"` with 512 rows is a complete bank. An incomplete bank
must not be used as though absent seeds had zero edges. All stored edges use
`format(edge, ".6f")`; `qt1-audit.json` preserves unrounded edges, per-seed run
times, and complete native gas counters. `simulation_seconds` is the sum of
per-call elapsed times, which overlap in process mode; it is not total wall time
or CPU time. Command wall times are recorded in the measurement evidence.

```sh
. scratch/env.sh
# Sequential driver, at most 48 seeds per command:
timeout 470 python -B engine_eval.py --max-seeds 48 --budget 400
# Alternatively, independent processes; each native singleton still uses 8 workers:
timeout 470 python -B baselines/engine/generate.py --jobs 2 --max-seeds 48 --budget 400
# Repeat the SAME command until status is complete. Do not run both concurrently.
```

Both commands resume `scratch/engine-bank/progress.json` and share its format.
They hash-check the exact qt1 source, release wheel, native library, and pinned
compiler. Resume binds source, installed Python/Solidity files, runtime versions,
engine_eval implementation, config transcription, every config, compiler,
bytecodes, and cached distribution digests. A changed binding refuses stale
state before simulation. Preserve the checkpoint when restarting; do not edit
its binding to suppress a refusal. To intentionally use changed inputs, choose
a new `--checkpoint` and `--output` and start a new bank.

Each successful seed is atomically checkpointed with fsync and rename. Rows must
form a contiguous prefix; audits must agree with their seed and gas values.
Process locks prevent competing generators from overwriting progress. Failed
seeds are recorded and never appended as successful rows. On interruption, a
seed without a committed row may run again; committed seeds are skipped.
Outputs are refreshed at each command's end or caught failure. If the external
`timeout` kills a process, resume from the checkpoint to refresh published files.
The bank and audit are separate atomic files; a hard kill between replacements
can leave different lengths until the next resume republishes both.

The process driver dispatches at most `--jobs` seeds at once, consumes them in
seed order, and checkpoints each. If interrupted, later workers' uncommitted
results are discarded and rerun. It never dispatches beyond `--max-seeds` or the
allowed interval. The budget reserves time for in-flight work based on observed
cost; the outer 470-second timeout is the hard command bound. No Rust build,
policy modification, or tuning is involved. State, caches, installations, and
logs belong under `scratch/`.

## Validation

```sh
timeout 60 python3 -B -m unittest discover -s tests -p test_engine_eval.py
. scratch/env.sh
timeout 470 python -B baselines/engine/validate.py
```

Tests exercise full-input rejection before execution, empty-input laziness,
order/duplicate preservation, gas isolation, all failure counters, result
alignment, finite values, compilation reuse/invalidation, atomic writes,
corrupt/stale checkpoint refusal, and interrupted/resumed equivalence.
`baselines/engine/validation.json` records independent reruns and integrity
checks; `baselines/engine/measurement.json` records the initial benchmark,
candidate-source integration, timings, and unit-test results.
The initial benchmark uses pinned qt1 on seed 9700000, before choosing chunks.
Independent reruns compare unrounded edge and afterSwap gas, not only rounded
bank strings. See the item result for final coverage and exact remaining seeds.

Only Linux x86_64 was exercised. The release engine wheel does not support
macOS arm64; that foundation platform supports replica work only. Windows and
other platforms are untested.
