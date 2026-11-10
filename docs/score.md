<!-- Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9, research/solved, solved-policy research. -->
# Paired sequential screening

`score.py` compares candidate **edge minus baseline edge**, paired by exact integer
seed. It consumes the bank contract in [contracts.md](contracts.md). Offline row
scoring uses Python's standard library only. Live backends are imported lazily;
the scorer does not install dependencies, compile strategies, generate banks,
select configurations, or tune policies.

These are **screening heuristics, not sequentially calibrated confidence
guarantees**. Repeated looks, seed selection, and policy selection can bias results.
Engine seeds 9700000..9700511 are screening-only, never an independent final
assessment. Gas is validated as metadata, not scored as edge.

## Rounds and numerical rules

Seeds are evaluated in ascending order in cumulative rounds of 128. A final
partial round is evaluated and printed, including all preceding observations.
Only the next round is requested: no prefetch and no subsequent simulation after
a stop. Candidate files are fully loaded and validated before scoring; this does
not entail any simulation. The entire selected bank is scored unless stopped.
There is no implicit seed intersection, subset selection, or missing-row skipping.

After each round stdout contains exactly one JSON object:

```json
{"n":2,"mean_diff":2.0,"se":1.0,"worst5":[{"seed":10000,"diff":1.0},{"seed":10001,"diff":3.0}]}
```

`mean_diff` is the cumulative arithmetic mean. `se` is the sample standard
error, `sd(diff, ddof=1) / sqrt(n)`. `worst5` contains at most five cumulative
observations sorted by `(diff, seed)`, lowest first. Differences and live values
are never rounded before the tests. Stored baseline strings retain their supplied
six-decimal precision, parsed as Python floats; the scorer cannot recover their
original unrounded values. Candidate decimal strings and numeric edges are
accepted; their precision is whatever their producer supplied.

After printing, stop if `mean_diff + 3*se < 0`. When `--bar` is supplied, also
stop if `mean_diff - 3*se > bar`. Both comparisons are **strict**: equality does
not stop. The worse test takes precedence if a negative bar makes both tests
true. Zero variance means zero SE for `n >= 2`. Bars must be finite. An empty
bank produces one explicit summary with `n=0`, `mean_diff=null`, `se=null`, and
`worst5=[]`, with no backend import or call. At `n=1`, mean and worst observation
are reported, `se=null`, and neither statistical stop is allowed. Nonfinite
input edges, overflowed differences, or nonfinite summary statistics fail.

Stderr carries a separate JSON provenance record before scoring, with independent
`baseline` and `candidate` objects, followed by a final `status` record containing
`reason` (`exhausted`, `worse`, `above_bar`), `n`, and `available`. Baseline and
candidate **may have different policy identities**. File provenance includes
SHA256, supplied source metadata, and precision; live provenance records the
candidate policy, runner entry point, head parameters, and strategy path. It
does not certify backend versions, code hashes, or simulator parity. Preserve
backend setup manifests and strategy/source hashes alongside a live run when
publishing results. The scorer does not reinterpret a provenance claim as a
verified statement.

## Files and validation

Use a bank JSON object with `_credit` first, `backend`, `policy`, nonempty
`provenance` object, and `rows`, as specified in `docs/contracts.md`. Bank rows
must be unique and ascending, and edges must be finite strings matching
`^-?[0-9]+\.[0-9]{6}$`. Replica baseline policies are `qt1-ref`, `TwoComp`,
`COMBO`, `HA`; engine baseline policy is `qt1`.

Candidate row files use the same envelope, but policy may be any nonempty
identity, rows may be shuffled, and edges may be finite JSON numbers or decimal
strings. Every baseline seed must appear exactly once and no extra seed may
appear. Both file roles require nonempty credit text; attribution and source
claims remain the producer's responsibility. Duplicate JSON keys are rejected.
For example (illustrative data, not a generated bank):

```json
{
  "_credit": "Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9 research/solved",
  "backend": "replica",
  "policy": "my-policy-v1",
  "provenance": {"source": "illustrative fixture, not simulation"},
  "rows": [{"seed":10000,"edge":1.234567890123}]
}
```

Backend must agree between baseline and candidate. This scorer supports only
`edge`; optional `metric` fields at document, provenance, or row level must be
`edge`. Explicit backend fields in provenance or rows must agree with the source
envelope. Source formats cannot be mixed: a row file uses the file envelope;
a live source uses the chosen replica/engine signature. The supplied provenance
schema has no universal configuration identifier, so compatibility of market
parameters, versions, and strategy sources must also be reviewed by the caller.
Different policies or different source commit names alone are not incompatibility.

Engine rows, including candidates, require nonnegative integer
`after_swap_peak_gas`. All engine seeds must be integers in 9700000..9700511;
the entire bank is range-checked **before any simulation**, even if an early
round would have stopped. Booleans are not accepted as integers or edges. Live
results are checked for duplicates, missing/extra requested seeds, nonfinite
edges, contradictory metadata, and engine gas/range. Result defects can only be
detected after that round's call; a failure prevents subsequent calls.

## CLI examples

Paths below describe user-provided inputs; substitute existing bank and candidate
paths. Run from the repository root. Commands are bounded below eight minutes.
For live use, the shared pinned runtime is normally prepared with
`timeout 470s bash setup_fast.sh --platform linux-x86_64 --cache scratch/cache --deadline 420`
and references with
`timeout 60s python3 -B reference/materialize.py --out scratch/reference`.
The platform argument selects pinned artifacts without a host probe.
Neither command is needed for offline scoring.

Offline row comparison (ordinary Python, no NumPy/Numba/engine dependency):

```sh
timeout 120s python3 -B score.py --baseline baselines/selected-replica.json --rows scratch/candidate.json > scratch/rounds.jsonl 2> scratch/provenance.jsonl
```

Replica live run, using a different policy than the baseline if desired:

```sh
timeout 470s scratch/venv/bin/python -B score.py --baseline baselines/selected-replica.json --replica --policy COMBO --bar 0.1 > scratch/rounds.jsonl 2> scratch/provenance.jsonl
```

`--head-params scratch/head.json` passes the decoded JSON to the replica backend;
the backend owns shape and supported-value validation. No head means `None`.
The standard replica backend accepts the three public policy IDs in the contract.
Use a custom adapter for other candidate implementations.

Engine live run (Linux engine runtime and a bank wholly inside the screening range):

```sh
timeout 470s scratch/venv/bin/python -B score.py --baseline baselines/selected-engine.json --engine --policy my-strategy-v1 --strategy-path scratch/MyStrategy.sol > scratch/engine-rounds.jsonl 2> scratch/engine-provenance.jsonl
```

`--policy` is the candidate identity, required in both live modes. For engine
mode, `--strategy-path` is required and passed verbatim to the backend; that
backend validates/compiles the strategy. Replica mode rejects strategy paths;
engine mode rejects head parameters. Row mode rejects all live-only options.

## Lazy adapter interface

The defaults call exactly these interfaces, with a fresh list of requested seeds:

```python
ammfast.run_batch(seeds, policy, head_params=None)
engine_eval.run_batch(seeds, strategy_path)
```

`--adapter my_runner:run_batch` replaces the default function while retaining the
chosen mode's signature. The module must be importable in that runtime. Replica
adapters receive head parameters by keyword. Engine adapters receive the strategy
path as the second positional argument. Return/yield a finite iterable of row
dicts for **only the requested seeds**, in any order:
`{"seed": int, "edge": finite_number}`; engine rows also need
`"after_swap_peak_gas": nonnegative_int`. Compute only these seeds, with no
hidden full-bank evaluation. Adapters are trusted Python code; the scorer can
validate their results and calls but cannot police internal prefetch or output.
Adapters must keep stdout reserved for the scorer's JSON summaries and send any
logging to stderr. The scorer does not cache results or call a runner for an
empty bank. A module import is attempted only when a round actually executes.

For Python callers, `load_dataset(path, bank=True)` validates the baseline;
`RowCandidate(load_dataset(path))` or `LiveCandidate(...)` constructs the source;
`evaluate(bank, candidate, bar=None, emit=callback)` emits dictionaries and returns
the status dictionary. Use these constructors rather than manually manufacturing
`Dataset` objects to retain file-schema validation.

## Exits, checks, and reruns

Exit 0 means evaluation completed normally, including any screening stop; inspect
`status.reason` to distinguish outcomes. Exit 2 means CLI, input validation, or
backend failure; stderr includes the exception type and message. Exit 130 means
interrupted. A timeout wrapper can return 124. Earlier round lines may exist
before a failure: without the final status record they are partial, not a
completed evaluation. Output is flushed after each round, allowing checkpointed
inspection. There is no automatic resume of live state: rerun the same command,
or retain live results as a valid candidate row document for offline rescoring.
Do not combine separately scored subsets as if they were cumulative rounds.

Run the deterministic tests with:

```sh
timeout 60s python3 -B -m unittest discover -s tests -p test_score.py -v
```

These 35 deterministic tests use fixtures and stubs, including call-count spies
for both backends. They do not establish simulator parity. Test temporary files
stay under `scratch/score-tests`. No third-party test dependency is needed.
Default discovery of `test_score*.py` also discovers three optional integration
tests and skips them unless explicitly enabled.

## Optional real engine integration

Run from the checkout on the pinned Linux x86_64 runtime. The installer takes
an explicit platform and needs no copied script or host-probe patch.

```sh
bash setup_fast.sh --platform linux-x86_64 --cache "$PWD/scratch/cache" --scratch "$PWD/scratch"
. scratch/env.sh
python3 -B reference/materialize.py --out scratch/reference
timeout 470 env IMD_SCORE_ENGINE=1 python -B -m unittest discover -s tests -p test_score_integration.py -v
```

The test creates `scratch/score-integration/Strategy.sol` from the pinned
`contracts/src/VanillaStrategy.sol`, changing only `contract VanillaStrategy`
to `contract Strategy`. Hash assertions apply to this test candidate, not to
arbitrary scorer candidates or existing bank provenance. It benchmarks seed
9700000 first. If the conservative benchmark × 128 exceeds 400 seconds, the
live128 test **skips**, which is missing live coverage, not a passing integration.
On a slower runtime, collect bounded chunks with per-seed checkpoints and label
subsequent scoring as offline replay; never call that a live128 round.

The live test invokes the actual CLI entry point and default
`engine_eval.run_batch` through `LiveCandidate`. Its baseline is the committed
qt1 bank restricted to 9700001..9700511. It asserts one adapter request for
9700001..9700128, exactly those 128 singleton evaluations, one stdout summary,
sample SE independently computed with a two-pass formula, worst-five ordering,
strict worse stopping, and no calls for 9700129..9700511. Wrappers forward real
computed edges without replacement. A separate test reruns qt1 at only
9700000, 9700128, 9700511 and requires exact unrounded edge/gas agreement with
`baselines/engine/qt1-audit.json`, plus six-decimal agreement with
`baselines/engine/qt1-9700000-9700511.json`.

The third test is explicitly **offline replay**: committed qt1 audit rows
9700000..9700127 against their rounded bank. It checks above-bar stopping just
below the observed lower bound and no stop exactly at that bound. The existing
stdlib fixtures cover exact worse-boundary equality, zero variance, n<2,
invalid ordering/duplicates/missing/nonfinite rows, and requested-only calls.
These fixture assertions are not additional live simulations.

Evidence is written under `scratch/score-integration/`: source/runtime hashes
in `runtime.json`, benchmark, `live-progress.json` after every seed,
`live-result.json` only after successful live assertions, CLI stdout/stderr,
`qt1-check.json`, and `replay-boundary.json`. Each run starts its live round
afresh; a stale result file from an earlier run is not evidence for a failed
rerun. Per-seed progress can be used to recover computed rows for offline
analysis, but is not automatically resumed as live CLI work. Only complete
results with a successful test exit establish the stated integration.

The merged tree implements the real replica backend in `ammfast/`; the scorer's
default `ammfast.run_batch(seeds, policy, head_params=None)` adapter calls it
directly. Committed replica parity and bank spot-check evidence is described in
[ammfast.md](ammfast.md) and [replica-banks.md](replica-banks.md). The engine tests
above and deterministic scorer stubs do not themselves establish replica parity.
Engine screening evidence does not imply replica, macOS, Windows, alternate
wheel, or different market-config compatibility. The scorer deliberately adds
no mandatory runtime fingerprint: callers must review source/runtime compatibility.

### Real replica commands

Prepare the replica-only runtime directly:

```sh
bash setup_fast.sh --platform linux-x86_64 --cache "$PWD/scratch/cache" --scratch "$PWD/scratch" --replica-only
. scratch/env.sh
python3 -B reference/materialize.py --out scratch/reference
python -B score.py --baseline baselines/replica/qt1-ref-64000-65023.json --replica --policy COMBO --bar 0.1
python -B score.py --baseline baselines/replica/TwoComp-64000-65023.json --replica --policy qt1-ref
```

Both examples select a 1024-seed bank (`64000..65023`). The scorer requests
`64000..64127` first and only requests a subsequent round if no stop applies.
During this documentation refresh, setup exited 3 because the pinned Linux
interpreter could not execute (child exit 126 at `setup_fast.sh:280`). Neither
scorer command ran: there are no measured round lines, stop reasons, requested
seeds, or scorer seconds to quote from this attempt. These commands are runnable
examples for the specified runtime, not recorded successful evaluations.

### Previously recorded engine integration

At starting trunk `57ce0df30e24784fb9d0537de55e5b7f8521ecd7`, the optional
suite passed all three tests in 82.004 seconds. The initial one-seed benchmark
took 1.172 seconds (150.019-second conservative projection); the suite repeated
it in 1.208 seconds. The live128 scorer call took 47.945 seconds and emitted
`n=128, mean_diff=-181.07580014978117, se=3.9162517263946226`, then stopped
as `worse` with 511 seeds available. The qt1 three-seed check took 19.090 seconds.
There were no test failures. These are this item's measurements, not claims
about other workers' changes.

| Actual input | SHA256 |
| --- | --- |
| Upstream VanillaStrategy.sol | `62d40ccef0de99a385e332a36ddf9cbd1da73eda06bd7954492a9880cb91a572` |
| Contract-name-only candidate | `64b62042f06d3b48d61c1387c0b6f08b0c291c9d2419a0ca65a5fc1d1cc19f96` |
| engine_eval.py adapter | `0a3ed61457cb66c85a292c82acbb9657b5e5544a98c7aa04c7a6a5dccdc1d3b2` |
| qt1 source | `c68887cff61180e12ba2b1ceb333edf76a830f91f63b5d775a8806fb9ec08aca` |
| Engine bank | `f1031aae649d2ad8e871ee1cc055770a4c6b329ff3e71afb4500fca3c8621f90` |
| Unrounded audit | `5256d2bd776a44f34780e47551c2d7d02f9a2e9fc8b8392fc6b46a92593d2ade` |

Runtime: pinned CPython 3.12.16, NumPy 2.5.4, release amm-sim-rs 0.1.0.post1,
engine commit `6b6921f8d463314aa7c96234e1be25a25245f704`, solc 0.8.24
(optimizer 200, viaIR, Paris), eight workers per singleton native call.
The installed native library SHA256 was
`ff88b6353c2aad7342f0a26194f7347e78b9c3b38d683a7449006d423978cd14`.
Configuration equivalence checked all 12 fields at four indices (48 comparisons,
no simulation at those indices); all 512 allowed configs hashed to
`10c0fee88d6a982ae4aca9833d5ee228ffa75a2dfdcc732e6732b32541981afc`.
The result artifact records full coverage, hashes, commands, and resume limits.
