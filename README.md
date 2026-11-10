<!-- Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9 research/solved, solved-policy research. -->
# Shared AMM evaluation kit

Built on [Taylor (gtmcknight)'s solved-policy research](https://github.com/gtmcknight/amm-sim/tree/4812a6382311624e1506aca131ca35c378f930d9/research/solved),
`amm-sim@4812a6382311624e1506aca131ca35c378f930d9 research/solved`, for the
[Paradigm AMM puzzle](https://paradigm.xyz/puzzles/amm).
This kit shares pinned setup, fast replica runs, baseline banks and paired scoring.

The reproducible source snapshot below is **P = `79f068cef81dadee171531f3381e207c193c959a`**.
Measurements on this page were made against P on Linux x86_64. The final trunk
commit containing this README is named by the goal's write-up; its seven changed
files are listed separately below without hashes. Other work in that commit was
not visible to this README worker and is not certified here.

At P, `validate_kit.py` reports **one finding**: its `gates`/`spots` key names do
not match the replica evidence's `gates384`/`spots576`. The repair is due in this
README's commit; **PASS 14848/14848 is expected after that repair**, not an observed
result at P. The individual replica validator passes its evidence checks. Separately, three
live-replica/bank tests fail on this runtime with discrepancies larger than
rounding; see the validation section for exact observations and limits.

| Piece | What it does | Details |
|---|---|---|
| `setup_fast.sh`, `bootstrap/` | SHA256-pinned interpreter, wheels and engine; reusable cache; no Rust build | [Setup](docs/setup.md) |
| `reference/materialize.py` | Verify and decode 35 byte-preserving upstream originals offline | [Reference](docs/reference.md), [licenses](reference/LICENSES.md) |
| `gatep.py` | Verify Taylor inputs, run his unmodified gates, reuse successful output | [Gate P](docs/reference.md#gate-p-cache) |
| `ammfast/` | One compiled call per replica run; ordered batches; COMBO residual head | [Replica API](docs/ammfast.md) |
| `baselines/` | Nine replica banks, imported HA, and qt1 engine screening bank | [Replica banks](docs/replica-banks.md), [HA](docs/ha.md), [engine](docs/engine.md) |
| `score.py` | Paired sequential scoring from rows or live replica/engine adapters | [Scoring](docs/score.md) |
| `engine_eval.py` | Compile a Solidity candidate, construct exact configs, return edge and afterSwap peak gas | [Engine](docs/engine.md) |
| `validate_kit.py`, `tests/` | Offline evidence validation and targeted runtime/integration checks | [Validation](docs/validation.md), [contracts](docs/contracts.md) |

## Setup and runtime

Run from the checkout root. Commands shown as observed below were run through a
465-second external timeout; setup also enforces its own 420-second deadline.
All installations, caches, checkpoints and logs used `scratch/`. Benchmark before
choosing live batch sizes; keep every command under 470 seconds and resume bounded
work from its checkpoint. No Rust compilation or policy tuning is needed.

One command installs the Linux x86_64 replica and release engine runtime:

```sh
bash setup_fast.sh --platform linux-x86_64 --cache "$PWD/scratch/cache" --scratch "$PWD/scratch"
```

Observed: exit 0, 20.045 s.

The same command was repeated: exit 0, 0.670 s. The installer's own timing lines
are distinct from the surrounding command wall times:

```text
setup_fast: OK platform=linux-x86_64 mode=engine setup=cold downloads=16 download_bytes=135768787 cache_hits=0 install=fresh fetch_s=16.701 install_s=2.382 verify_s=0.919 smoke_s=0.000 total_s=20.017 deadline_s=420
setup_fast: OK platform=linux-x86_64 mode=engine setup=cached downloads=0 download_bytes=0 cache_hits=16 install=reused fetch_s=0.247 install_s=0.006 verify_s=0.346 smoke_s=0.000 total_s=0.611 deadline_s=420
```

Every download has a size and SHA256 in [bootstrap/manifest.tsv](bootstrap/manifest.tsv).
Cache hits are rehashed. Without `--cache`, the reusable default is
`$HOME/.cache/imd-amm`; these checks explicitly used the checkout cache instead.
`--offline` requires a populated cache. Exit 75 means the deadline was reached:
rerun the same setup command to reuse verified downloads and completed steps.
Source `. scratch/env.sh` in each new shell to use the pinned runtime and keep
Numba and temporary caches under scratch. Use `python3 -B` to avoid source-tree bytecode.

Replica-only installation was also executed, in a separate scratch runtime:

```sh
bash setup_fast.sh --platform linux-x86_64 --replica-only --cache "$PWD/scratch/cache" --scratch "$PWD/scratch/replica-only"
```

Observed: exit 0, 3.002 s.

Use `. scratch/replica-only/env.sh` for that installation. It omits the engine,
Solidity compiler and engine environment variables. The remaining examples use
the full Linux installation's `. scratch/env.sh`.

macOS arm64 supports **replica work only** (macOS 12+). This command was **never
executed here**, and no macOS parity or timing claim is made:

```sh
bash setup_fast.sh --platform macos-arm64 --replica-only --cache "$PWD/scratch/cache" --scratch "$PWD/scratch"
```

## Reference originals and Gate P

The materializer verifies the inventory, compressed envelope digests, decoded
sizes/digests and Numba bundle membership before writing unchanged originals.
It is offline and idempotent; a changed destination is rejected.

```sh
. scratch/env.sh && python3 -B reference/materialize.py --out scratch/reference
```

Observed: exit 0, 0.338 s.

Result: 35 verified originals, including Taylor's `research/solved`, qt1,
TwoComp, COMBO and the archived starter tools.

```sh
. scratch/env.sh && python3 -B gatep.py --reference scratch/reference/taylor --cache scratch/cache/gatep
```

Observed: exit 0, 2.449 s.

Repeating that exact command returned exit 0 in 0.064 s with `verified cache hit`.
Gate P checks every Taylor file by SHA256 on every invocation. Successful output
is cached by Taylor commit, full Python version/implementation and input digest.
Sharing the local cache provides once-per-machine reuse; a new cache or interpreter
runs the gate again. The default cache is `$HOME/.cache/imd-amm/gatep`.
It validates all twelve upstream values; it does not establish compiled-replica parity.

## ammfast: run, batch and residual head

`ammfast.run(seed, policy, head_params=None)` returns `{"seed": int, "edge": float}`
from one compiled simulation call. `run_batch(seeds, policy, head_params=None)`
returns the same rows in input order, retaining duplicates, in one compiled batch
call. Default runs have 10,000 steps. Policy IDs are exactly `qt1-ref`, `TwoComp`
and `COMBO`; edges remain unrounded until bank serialization.

```sh
. scratch/env.sh && python3 -B -c 'import ammfast; print(ammfast.run(10000, "COMBO")); print(ammfast.run_batch([10000, 10001], "COMBO")); print(ammfast.run(10000, "COMBO", head_params=[0.0] * 18))'
```

Observed: exit 0, 1.086 s.

The single COMBO edge was `444.1953300340938`; the batch's first row and zero-head
run matched it exactly. `head_params` is COMBO-only: 18 finite weights, either a
sequence or `{"weights": [...]}` with optional `_credit`. The first eight weights
and bias control the own-side correction; the other eight and bias control the
other side. Frozen features feed bounded tanh corrections of at most 8 bps before
clamping. All-zero weights reproduce COMBO. No trained head weights are supplied;
see [the precise head definition](docs/ammfast.md#residual-head-combo-only).

The committed [replica validation evidence](baselines/replica/validation.json)
records **384/384** comparisons within **1e-9**: all three policies on
`10000..10063` and `64000..64063`, maximum absolute error
`1.1937117960769683e-11`. Its single-core warm batch rates and cold JIT timing are:

| Policy | Runs/s/core (warm) |
|---|---:|
| qt1-ref | 102.2938399544792 |
| TwoComp | 88.4128707888032 |
| COMBO | 77.85565121419603 |

The recorded cold JIT time is **13.85819065431133 s** (fresh interpreter, empty
Numba cache, import, table decode, compile and first run; slowest policy).
These are the measurements in `validation.json`, not a portable speed guarantee.
A fresh 384-pair rerun here also passed, maximum error `1.6825651982799172e-11`,
exit 0 in 31.147 s. The scratch benchmark first checked one COMBO run and an
eight-run batch before larger work; its first call took 5.285 s and eight warm
runs took 0.046 s. Different last bits can occur across machines/libraries.

## Baseline banks

| Backend / policy | Inclusive seeds | Rows |
|---|---|---:|
| Replica: qt1-ref, TwoComp, COMBO, each | 60000..61023 | 3 × 1024 |
| Replica: qt1-ref, TwoComp, COMBO, each | 64000..65023 | 3 × 1024 |
| Replica: qt1-ref, TwoComp, COMBO, each | 10000..12047 | 3 × 2048 |
| Replica: HA import | 10000..12047 | 2048 |
| Engine: qt1, screening only | 9700000..9700511 | 512 |

There are **nine replica banks / 12288 rows** and **11 banks / 14848 rows** overall.
Each bank has attribution, backend, policy, provenance and ascending unique rows;
edges are fixed-six-decimal strings. The replica evidence includes **576 CPython
spot checks**: **only the first 64 seeds of each bank** were checked, not all bank
rows or a random sample. Of those policy/seed pairs, 384 overlap the parity gate;
192 are additional. The fresh parity rerun above does not broaden that coverage.

HA is selected by `kind == "HA"` from Taylor's pinned `calib_out_2048.json`,
using each record's `seed` and `edge`; it is an import, not an ammfast rerun.
The engine bank contains 512 qt1 rows with `after_swap_peak_gas`, backed by
unrounded audit rows and gas counters. It uses the pinned release wheel and
`amm_sim_rs.run_batch(X, VanillaStrategy, configs, 8)`, with configs constructed
as in `MatchRunner._build_configs`. Singleton calls isolate each seed's
Submission/afterSwap peak gas. **All engine seeds are screening-only**; none is
an independent final evaluation, and no engine seed outside 9700000..9700511 is allowed.

Checks executed here:

```sh
. scratch/env.sh && python3 -B baselines/replica/validate.py
```

Observed: exit 0, 0.044 s.
```sh
. scratch/env.sh && python3 -B baselines/ha/export.py --source scratch/reference/taylor/calib_out_2048.json --check baselines/ha/HA-10000-12047.json
```

Observed: exit 0, 0.063 s.
```sh
. scratch/env.sh && python3 -B baselines/engine/validate.py --output scratch/engine-validation.json
```

Observed: exit 0, 9.860 s.

The replica check verified the nine banks, bindings, 384 gate and 576 spot records.
HA matched all 2048 imported rows. Engine validation checked all 512 bank/audit
rows and reran seeds 9700000, 9700128, 9700256, 9700383 and 9700511 with exact
unrounded edge and gas agreement. Always supply
`--output scratch/engine-validation.json`: the engine validator's default writes
`baselines/engine/validation.json` and rewrites committed timing fields.

## Paired scoring

`score.py` pairs candidate edge minus baseline edge by seed, in cumulative rounds
of 128, and prints `{n, mean_diff, se, worst5}` after each round. `se` is sample
standard error; `worst5` is the five lowest differences with their seeds.
It stops when `mean_diff + 3*se < 0`, or, with `--bar B`, when
`mean_diff - 3*se > B`. Equality does not stop; fewer than two rows has no SE or
statistical stop. Partial final rounds are supported. These are screening
heuristics, not sequentially calibrated confidence guarantees.

`--rows` scores precomputed rows without importing a live backend. This actual
bank-to-bank replay exhausted 1024 rows in eight rounds:

```sh
. scratch/env.sh && python3 -B score.py --baseline baselines/replica/qt1-ref-60000-61023.json --rows baselines/replica/COMBO-60000-61023.json
```

Observed: exit 0, 0.045 s.

Final `mean_diff=0.6379020156249982`, `se=0.01962762671565725`.

`--replica` uses `ammfast.run_batch`; `--bar` exercises above-bar early stopping:

```sh
. scratch/env.sh && python3 -B score.py --baseline baselines/replica/qt1-ref-60000-61023.json --replica --policy COMBO --bar 0.1
```

Observed: exit 0, 1.907 s.

This real run stopped `above_bar` after 128 of 1024 available seeds:
`mean_diff=0.752858348279358`, `se=0.05573079715178375`.
Use `--head-params scratch/head.json` to pass a user-supplied 18-weight COMBO head.
Scoring a policy against its own six-decimal bank can show differences up to
**5e-7 from rounding alone**; a nonzero difference is not automatically a regression.

For a reproducible engine smoke candidate, use the installed Vanilla source,
change its required contract name and add attribution (preparation also executed here):

```sh
. scratch/env.sh && python3 -B - <<'PY'
# Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9 research/solved.
from pathlib import Path
source = Path("scratch/engine/amm-challenge/contracts/src/VanillaStrategy.sol").read_text()
assert source.count("contract VanillaStrategy") == 1
credit = "// Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9 research/solved.\n"
Path("scratch/readme").mkdir(exist_ok=True)
Path("scratch/readme/Strategy.sol").write_text(credit + source.replace("contract VanillaStrategy", "contract Strategy"))
PY
```

Observed: exit 0, 0.021 s.

The initial real `engine_eval.run_batch([9700000], "scratch/readme/Strategy.sol")`
benchmark took 0.270 s, projecting 34.526 s for 128 calls, within the command cap.
Then `--engine` used the default engine adapter on the committed qt1 bank:

```sh
. scratch/env.sh && python3 -B score.py --baseline baselines/engine/qt1-9700000-9700511.json --engine --policy vanilla-smoke --strategy-path scratch/readme/Strategy.sol
```

Observed: exit 0, 11.985 s.

It stopped `worse` after exactly 128 of 512 available screening seeds:
`mean_diff=-181.35840023233058`, `se=3.8938152969370132`.
This is a smoke check of live scoring, not a tuned candidate. Both real scorer
commands finished well below eight minutes. Exit 0 includes an early stop;
read stderr's final status (`worse`, `above_bar`, `exhausted`) as well as stdout.
Exit 2 means invalid input/backend failure; interrupted output is incomplete.

## Engine adapter and resumable generation

`engine_eval.run_batch(seeds, strategy_path)` compiles `Strategy` once per source
binding, validates the entire requested seed list before execution, builds the
pinned configs and returns unrounded edge plus nonnegative integer afterSwap
peak gas. It rejects failed native calls and never prefetches later rounds.
The release wheel is Linux x86_64 only. Config equivalence compares 48 fields
at four construction indices without simulating those indices.

The CLI is a checkpointed qt1 bank generator. This bounded one-seed smoke wrote
only scratch files (one row, explicitly incomplete), leaving committed banks intact:

```sh
. scratch/env.sh && python3 -B engine_eval.py --max-seeds 1 --budget 30 --checkpoint scratch/readme/engine-progress.json --output scratch/readme/engine-bank.json
```

Observed: exit 0, 3.412 s.

Rerun that command to resume from the next allowed seed. For larger generation,
choose a chunk after benchmarking and preserve the checkpoint. The CLI defaults
to a tracked bank output, so use an explicit scratch `--output` for experiments.

## Validation and tests

`validate_kit.py` uses only the standard library, reads evidence and recomputes
hashes/coverage/parity differences; it does not run simulators. A PASS certifies
internal consistency of supplied evidence, not independent simulator correctness.

```sh
. scratch/env.sh && python3 -B validate_kit.py --out scratch/readme/kit-validation.json --timing
```

Observed: exit 1, 0.175 s.

Observed at P: `FAIL`, 2560/14848 verified rows, exactly one finding at
`baselines/replica/validation.json`: `gates must be a list` (`bad-validation`).
`validate_kit.py:600` requests `gates` and line 601 requests `spots`, while the
committed evidence uses `gates384` and `spots576`. The companion repair due in
this README's commit is expected to produce **PASS 14848/14848**. That repair
and its tests were not available here; rerun validation at the final trunk commit.

Tests exercised input validation, API/head parity, bank structure, adversarial
validator fixtures, setup deadlines, Gate P caching, paired scoring and all three
optional real engine integration tests. Exact command executed:

```sh
. scratch/env.sh && PYTHONPATH=tests IMD_SCORE_ENGINE=1 python3 -B -m unittest test_ammfast test_engine_eval test_gatep test_ha test_score test_score_integration test_setup_deadline test_setup_fast test_validate_kit test_replica_banks.BankTests test_replica_banks.ValidationTests test_replica_banks.LiveScorerTests -v
```

Observed: **exit 1, 78.081 s; 223 tests run, 212 passed, 3 failed, 8 skipped**.
All three failures are live-replica comparisons against committed banks:

| Test in `tests/test_replica_banks.py` | Line | Observed failure |
|---|---:|---|
| `test_cli_live_candidate_reproduces_published_pairing` | 252 | Mean 0.44524373541763534 vs 0.445421, tolerance 1e-5 |
| `test_cli_zero_head_file_matches_the_combo_bank_to_rounding` | 269 | Absolute mean 0.00015475572275436278 exceeds 5.0001e-7 |
| `test_in_process_live_candidate_and_unknown_policy` | 283 | Live 618.240984 vs bank 618.266729 |

A bounded follow-up recomputed the first 128 seeds of COMBO's 60000 and 64000
banks and TwoComp's 60000 bank. It found 2, 3 and 2 differences beyond rounding,
respectively. At the first differing seed in each sample, fresh ammfast and
CPython agreed exactly with each other, but differed from the committed bank:

| Policy / seed | Bank edge | Fresh ammfast = fresh CPython |
|---|---:|---:|
| COMBO / 60035 | 618.266729 | 618.2409840405087 |
| COMBO / 64013 | 427.035477 | 427.0324335194603 |
| TwoComp / 60016 | 409.736570 | 409.73952654643557 |

These discrepancies are **not explained by six-decimal rounding**. Their cause
and full-bank extent were not established here. They do not contradict the
fresh compiled-versus-CPython parity gate, but they limit reproducibility of the
committed banks on this runtime. Structural validation, including the expected
aggregate-validator repair, does not resolve them. Banks and simulator code were
not modified by this README item.

The eight skips were seven opt-in Linux setup integration tests and one macOS
setup test; Linux setup was separately executed above. The five
`test_replica_banks.CheckpointTests` were not selected because their binding test
calls host architecture inspection, outside this task's constraints. No claim
is made that the full suite passed. The initial `tests.test_*` invocation failed
with 11 module-import errors (exit 1, 0.044 s); the command above repaired the
invocation with `PYTHONPATH=tests`, without modifying tests or configuration.


Only Linux x86_64 was executed. macOS arm64, Windows and other platforms are not
verified. Gate P, replica parity, structural validation and engine screening
answer different questions; none converts replica edges into board scores.

## Raw file inventory at P

Every SHA256 below was computed from checkout bytes at P. `git ls-files` contains
95 files: **53 individually hashed files + 35 envelopes + 7 changed files**.
The directory row aggregates the envelopes; the separate seven-row table below
completes the tracked-file inventory without pretending to know this README's own
commit. All 11 bank URLs and six code URLs (`setup_fast.sh`,
`reference/materialize.py`, `gatep.py`, `ammfast/__init__.py`, `score.py`,
`engine_eval.py`) were fetched here: **17/17 SHA256 comparisons matched the checkout**.
Other rows were locally hashed, not individually fetched.

`reference/envelopes/` is a raw URL **prefix**, not a downloadable directory.
Its members and individual hashes are in `reference/sources.json`; fetch each
member at P or use a checkout. The directory digest below is SHA256 of UTF-8
lines `<member sha256>  <repository-relative path>\n`, sorted by path (35 lines).

| Path | SHA256 | Raw URL |
|---|---|---|
| `.gitignore` | `4e9c9e95c8a1d5eb18dd533c538f8e0972a27d3f3d02e346ee0e46338676044d` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/.gitignore) |
| `ammfast/__init__.py` | `5ce5b00b96ca7da11f37010d234803eca0e92adb2eebec18a35cb6d81c645b6c` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/ammfast/__init__.py) |
| `ammfast/_head.py` | `3ea24996bf90ce3dbb32fed745f9073c477b0ccd250befb5082a6ac8878ff86f` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/ammfast/_head.py) |
| `ammfast/_kernel.py` | `45613331edb2fd46afa607612e1e6f0c22f0132c9974df6445ce18afe04cc2ae` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/ammfast/_kernel.py) |
| `ammfast/_reference.py` | `6509935958e7a317163b58b7da67add653a73028378fde65db9c40dddbfd3de0` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/ammfast/_reference.py) |
| `baselines/engine/generate.py` | `e05c66d73d12cb650d48092adeca30496c7116744cc8ade1309960a72fd5ad61` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/engine/generate.py) |
| `baselines/engine/measurement.json` | `d9e32d65b0de559bf119003f83c67c6a2561577378959a9345b3b4b83af0ac92` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/engine/measurement.json) |
| `baselines/engine/qt1-9700000-9700511.json` | `f1031aae649d2ad8e871ee1cc055770a4c6b329ff3e71afb4500fca3c8621f90` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/engine/qt1-9700000-9700511.json) |
| `baselines/engine/qt1-audit.json` | `5256d2bd776a44f34780e47551c2d7d02f9a2e9fc8b8392fc6b46a92593d2ade` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/engine/qt1-audit.json) |
| `baselines/engine/validate.py` | `0ced42c7885beea82f96fac625c778e16c3f27823071bf3c22a1b8ec9cf863db` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/engine/validate.py) |
| `baselines/engine/validation.json` | `05b20ddee30180169cabd5caabea447ce7a2423adb0ed9b2875112905f460eca` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/engine/validation.json) |
| `baselines/ha/HA-10000-12047.json` | `09719da22be28598e9c1d0bb1eb935d7b0b4eeafef3321e0fabe4f3ec9e2b7a9` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/ha/HA-10000-12047.json) |
| `baselines/ha/audit.md` | `428f3dcb35b392b8e79ce84a9d437aec6c311ee4e139648c66a14fe8ea38a8c6` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/ha/audit.md) |
| `baselines/ha/export.py` | `d392b7bbab02a25eb8e50517412e7c9d579ccfcd9a96f0fe620a157826c700f2` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/ha/export.py) |
| `baselines/replica/COMBO-10000-12047.json` | `f707434285c5a067fee11cac983bfcb7b1301e47bf5a2c1f55e4984207d6739b` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/replica/COMBO-10000-12047.json) |
| `baselines/replica/COMBO-60000-61023.json` | `8256bba9edd0b5696c850f81447edd0ee24b1783c4f3b9d2390a90eaa0ef8cc1` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/replica/COMBO-60000-61023.json) |
| `baselines/replica/COMBO-64000-65023.json` | `9d59118d213f1f7a811b91a27ab625a6c732789d53b215f6226e58acec8aad95` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/replica/COMBO-64000-65023.json) |
| `baselines/replica/TwoComp-10000-12047.json` | `2fde9032094837e6c32728b0a160d25e78e382f31ebd67318ab8f5efad629b25` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/replica/TwoComp-10000-12047.json) |
| `baselines/replica/TwoComp-60000-61023.json` | `03723f9a1e827ff615d48b001c3181312618a9d31c52ff2109fdc4f0398dcb88` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/replica/TwoComp-60000-61023.json) |
| `baselines/replica/TwoComp-64000-65023.json` | `7c37b73ac7f16c4276f673c8ecfb326ab4421cab3ae9ab2f87db4f27e7d319f2` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/replica/TwoComp-64000-65023.json) |
| `baselines/replica/generate.py` | `375213daa8c2faa77dcd29c4c357d0cffd8d0bd1858b0da411cbf9a79eab5e04` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/replica/generate.py) |
| `baselines/replica/qt1-ref-10000-12047.json` | `6687dab88e27f2f6c37511d6328164eafd371fa640d85be353b74c47b98f511c` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/replica/qt1-ref-10000-12047.json) |
| `baselines/replica/qt1-ref-60000-61023.json` | `e98ecaa2c9a7ee36307971d28d6c3de845f5a10c034aea7d152288536e6fe72e` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/replica/qt1-ref-60000-61023.json) |
| `baselines/replica/qt1-ref-64000-65023.json` | `89675529770b1bc2e8ba584067d7ce776cd6ab88e79b58b3000852aa3d333f37` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/replica/qt1-ref-64000-65023.json) |
| `baselines/replica/validate.py` | `8812b083a47c52f2b4f68d58fbcd5d757434411db7ce96a340985ed9a41c4083` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/replica/validate.py) |
| `baselines/replica/validation.json` | `84fe88b056f389d9620be650e646fd5ed2fa880d72f9764ad2a6505b03e6e8af` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/baselines/replica/validation.json) |
| `bootstrap/deadline.sh` | `2ceb9adf5f1b2b492002dfc371f94dbb3c196ec439a05b914aa8887f7954a8af` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/bootstrap/deadline.sh) |
| `bootstrap/manifest.tsv` | `f9a0c49310aa6f501eac9a760e537af444d21f556239e3f00a9ed6f1d9628dad` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/bootstrap/manifest.tsv) |
| `bootstrap/pins.sh` | `cc5af39de3626b919659b5bc8ae9b5536363037a8310453a65784c3463127bcb` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/bootstrap/pins.sh) |
| `bootstrap/smoke_engine.py` | `a2bd7d3f2a696909d01796b99f694bbd6502d300d7e42c6ff05cb3995273908c` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/bootstrap/smoke_engine.py) |
| `bootstrap/smoke_replica.py` | `2381c87df4fe2ab8b8040dd28bfd1306361f59aadcb9f7ef39f009f7b980df51` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/bootstrap/smoke_replica.py) |
| `bootstrap/verify_env.py` | `f1b4da7eded3cff54500f28eecd0866d363d8634d03d2703a3c62527c36a6383` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/bootstrap/verify_env.py) |
| `docs/ammfast.md` | `ca7313e14a31e329ad2d38800710f18385eaae37bc464053d80d008648bb116e` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/docs/ammfast.md) |
| `docs/engine.md` | `fab6e8c2327c4c1eca83887368d8dfec174942b60f082b2aa50254e31714bc3f` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/docs/engine.md) |
| `docs/ha.md` | `35d0c10cf6aedc03e234de3a5cf1a63d73adc3f3e29c3424ed960c47cebde316` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/docs/ha.md) |
| `docs/replica-banks.md` | `09c6c941a38c356513561a0a1d67b44dd14934d3373296a7634a8a37ebd30416` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/docs/replica-banks.md) |
| `docs/setup.md` | `923841f81135b99caccd4629d0c3b0fd18ab45c85a110ab1ada9d6a5f97b5f91` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/docs/setup.md) |
| `engine_eval.py` | `0a3ed61457cb66c85a292c82acbb9657b5e5544a98c7aa04c7a6a5dccdc1d3b2` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/engine_eval.py) |
| `gatep.py` | `1d1d65bc18647530037cf3cff73fd8d4448f353d61b4494dd2ee7d556be122db` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/gatep.py) |
| `reference/LICENSES.md` | `8663f6c9195310067cdf8676ad4e6d5156be9267e57fc4860d2a96709ca172b6` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/reference/LICENSES.md) |
| `reference/materialize.py` | `79e316706e8b4a84ee29e261c817476120d7a502f9017ec2f7f76f8f4444aaad` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/reference/materialize.py) |
| `reference/sources.json` | `62147ff112987f666e2c1ad5041012276a66033baec543c2b014f766cd8f01f5` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/reference/sources.json) |
| `score.py` | `7b9fa5e37695a5d1b048efe91a8b6c45d31bd4245a733261774c871fe9105a43` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/score.py) |
| `setup_fast.sh` | `48e150d165ea3b385fb92e0e3a7aefbbe0d97c38c53efa737a587ed62c308d20` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/setup_fast.sh) |
| `tests/test_ammfast.py` | `5ab78e1b12550fc6a0facd0428177e3d583fd0874bc142aaa92fe3df97660772` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/tests/test_ammfast.py) |
| `tests/test_engine_eval.py` | `51c4ef53fe2cac78b776ae5c0a1adaca6b3a15cc8ddacf707bd8d112adc6653a` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/tests/test_engine_eval.py) |
| `tests/test_gatep.py` | `4d42c47b932c775390b60bfa79c4733fce7f0c3f92d0cab7ae4bfdbacaedcaf8` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/tests/test_gatep.py) |
| `tests/test_ha.py` | `1aee1654cfc5caeebb74c773584f9d9ec298f15421ae3286ae7fea57b4923e18` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/tests/test_ha.py) |
| `tests/test_replica_banks.py` | `92832495a00653f18973d330cf0d90f3eb3a2a3dba9357c92ae7e88894110853` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/tests/test_replica_banks.py) |
| `tests/test_score.py` | `9301f8deb1b842a1b981dbe4e4a1b6ba2c57e4009f9dec085e1ad89266e23dcc` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/tests/test_score.py) |
| `tests/test_score_integration.py` | `34f0d1ab549bb1ee27833072ecc1dcd84ed16802e7bb1fd8371b3ead25f3300d` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/tests/test_score_integration.py) |
| `tests/test_setup_deadline.py` | `9cd163537af7c1f952fe58b172a064f2bbcb79b850b25fa19cea447f9acc821c` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/tests/test_setup_deadline.py) |
| `tests/test_setup_fast.py` | `44668751eb1ed768cc606ab2a66a551fc32d2561177c1286e0a1dbd3935184f8` | [raw](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/tests/test_setup_fast.py) |
| `reference/envelopes/` (35 files) | `21a5688aa2a1d0d7940ea69d3f667f612f76f6b6c89f53539ca29ef1c95f078a` (aggregate) | [raw prefix](https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/79f068cef81dadee171531f3381e207c193c959a/reference/envelopes/) |

### Seven files addressed by the final trunk commit

These are the seven files specified for this README's commit. This worker changed
only README.md; it makes no claim about the invisible companion changes. No hash
is listed here: a file cannot name its own commit. The goal's write-up names the
actual `<trunk-commit>` to substitute in this URL shape:

```text
https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/<trunk-commit>/<path>
```

| Path | Final raw URL shape (replace `<trunk-commit>`) |
|---|---|
| `README.md` | `https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/<trunk-commit>/README.md` |
| `validate_kit.py` | `https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/<trunk-commit>/validate_kit.py` |
| `tests/test_validate_kit.py` | `https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/<trunk-commit>/tests/test_validate_kit.py` |
| `docs/validation.md` | `https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/<trunk-commit>/docs/validation.md` |
| `docs/contracts.md` | `https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/<trunk-commit>/docs/contracts.md` |
| `docs/reference.md` | `https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/<trunk-commit>/docs/reference.md` |
| `docs/score.md` | `https://raw.githubusercontent.com/identity-md-launches/goal-3178-build-shared-evaluation-kit/<trunk-commit>/docs/score.md` |

## Upstream sources and required pins

The source inventory retains every original URL, digest and available license
notice. Taylor's research is credited throughout the kit; no blanket license is
invented for upstream files without an explicit grant.

- [Taylor solved README](https://raw.githubusercontent.com/gtmcknight/amm-sim/4812a6382311624e1506aca131ca35c378f930d9/research/solved/README.md)
- [Taylor gates.py](https://raw.githubusercontent.com/gtmcknight/amm-sim/4812a6382311624e1506aca131ca35c378f930d9/research/solved/gates.py)
- [Taylor QM table](https://raw.githubusercontent.com/gtmcknight/amm-sim/4812a6382311624e1506aca131ca35c378f930d9/research/solved/qm_k1e-5.json)
- [Taylor HA calibration](https://raw.githubusercontent.com/gtmcknight/amm-sim/4812a6382311624e1506aca131ca35c378f930d9/research/solved/calib_out_2048.json)
- [Starter qt1 Solidity](https://raw.githubusercontent.com/identity-md-launches/goal-3170-beat-network-s-amm-entry/4eb3e985fd31e3cd17832501b95b6d2491aca73d/reference/qt1/Strategy.sol)
- [Starter TwoComp](https://raw.githubusercontent.com/identity-md-launches/goal-3170-beat-network-s-amm-entry/4eb3e985fd31e3cd17832501b95b6d2491aca73d/reference/twocomp/twocomp.py)
- [Starter COMBO](https://raw.githubusercontent.com/identity-md-launches/goal-3170-beat-network-s-amm-entry/4eb3e985fd31e3cd17832501b95b6d2491aca73d/reference/combo/combo.py)
- [Starter setup](https://raw.githubusercontent.com/identity-md-launches/goal-3170-beat-network-s-amm-entry/4eb3e985fd31e3cd17832501b95b6d2491aca73d/tools/setup.sh)
- [Starter check](https://raw.githubusercontent.com/identity-md-launches/goal-3170-beat-network-s-amm-entry/4eb3e985fd31e3cd17832501b95b6d2491aca73d/tools/check.py)
- [Starter engine helper](https://raw.githubusercontent.com/identity-md-launches/goal-3170-beat-network-s-amm-entry/4eb3e985fd31e3cd17832501b95b6d2491aca73d/tools/engine.py)
- [Numba source artifact](https://api.imd.fun/artifacts/b9c917ebb99152aafd12993b327f0a82529efe414a4a8fea8f4a0e3493cc1f8f)

Required verified SHA256s: decoded Numba JSON
`a5d32e4c06dc93881821d568d8d89ca50adfdba56d5dccd92afef6c6c2157952`;
qt1 `Strategy.sol`
`c68887cff61180e12ba2b1ceb333edf76a830f91f63b5d775a8806fb9ec08aca`.
