<!-- Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9, research/solved, solved-policy research. -->
# ammfast: the compiled replica

`ammfast` runs Taylor (gtmcknight)'s `replica2.run` with the `qt1-ref`, `TwoComp` or `COMBO`
policy in one compiled Numba call per run. It implements the replica API in
[contracts.md](contracts.md); the banks built with it are described in
[replica-banks.md](replica-banks.md).

## Use

```sh
bash setup_fast.sh --platform linux-x86_64 --replica-only   # once; pinned CPython 3.12 + numpy/numba/llvmlite
. scratch/env.sh
python -B -c "import ammfast; print(ammfast.run(10000, 'COMBO'))"
# {'seed': 10000, 'edge': 444.1953300340616}
python -B -m unittest discover -s tests -p test_ammfast.py -v
```

Run from the repository root (or put it on `PYTHONPATH`). The package reads its reference files
from `reference/envelopes/` next to it, so it works from a checkout and is not an installable wheel.

## API

```python
ammfast.run(seed, policy, head_params=None, *, n_steps=10000)        -> {"seed": int, "edge": float}
ammfast.run_batch(seeds, policy, head_params=None, *, n_steps=10000) -> [row, ...]
ammfast.reference_run(seed, policy, head_params=None, *, n_steps=10000)   # CPython oracle, same row
ammfast.validate_head_params(head_params) -> tuple of 18 floats, or None
ammfast.POLICIES == ('qt1-ref', 'TwoComp', 'COMBO')
```

- `policy` is exactly `qt1-ref`, `TwoComp` or `COMBO`. Anything else, including the archived ids
  `qmref`, `twocomp`, `combo`, raises `ValueError`. There is no default policy.
- `seed` is an integer in `0..4284967295` (`ammfast.SEED_MAX`). replica2 seeds three
  `random.Random` streams with `seed`, `seed + 1` and `10_000_000 + seed`; the compiled seeding
  reproduces CPython's for one 32-bit word, so all three must stay below 2**32. Bools, floats,
  strings and out-of-range values raise `ValueError`.
- `edge` is the finite, unrounded binary64 policy edge (`replica2.run(...)["edge"]`). A non-finite
  result raises `ArithmeticError` instead of being returned.
- `run_batch` returns the rows `run` returns for each seed, in input order, duplicates kept, and
  `[]` for an empty input. It accepts any iterable of integers and performs one compiled call for
  the whole batch.
- Inputs are validated before anything is compiled or simulated, are never mutated, and no policy
  state outlives a run: the kernel allocates the RNG and policy state inside each run.
- `n_steps` is keyword-only and defaults to replica2's 10000. It exists for short tests; banks and
  the scorer use the default.
- Without numpy/numba, validation and `reference_run` still work; `run`/`run_batch` raise
  `ImportError` naming the setup command.

`python -B score.py --baseline BANK --replica --policy COMBO` uses `ammfast:run_batch` as its
default adapter; no extra glue is needed.

## What one call does

`ammfast/_kernel.py` holds the whole run in compiled code: the config draw, a CPython-compatible
Mersenne Twister (`random()`, `gauss()` with its cached second variate, `lognormvariate`,
`uniform`), the two-pool market loop with arbitrage and routed retail, and every policy callback.
There is no Python dispatch per trade or per step. The policy code receives only
`(is_buy, timestamp, reserveX, reserveY)`; the true price, the rival pool and the run parameters
are never passed to it.

The kernel is the archived, parity-checked `numba/fastsim.py`
(`reference/envelopes/numba__fastsim.py.json`, original sha256 `4d3d2187…596b`) with mechanical
deltas only: per-callback logging and the `cfg` override are removed, `sim` returns just the edge,
and `sim_batch` loops over seeds. No arithmetic statement changed and there is no fastmath; squares
stay written as `x ** TWO[0]`, the archive's way of keeping them on `pow` like CPython's `** 2`.
`tests/test_ammfast.py` re-derives this diff from the archived source and fails on any other change.

| Public ID | Reference class (unmodified, sha256-pinned) | Archived id |
|---|---|---|
| `qt1-ref` | `qt1/qmref.py` `QMRef` | `qmref` |
| `TwoComp` | `twocomp/twocomp.py` `TwoComp` | `twocomp` |
| `COMBO` | `combo/combo.py` `Combo` (combo-offsets) | `combo` |

The fee tables are decoded once per process from `qt1/blob.bin` by the reference decoder
(`qmref.Tables`), about 1.5 s. `ammfast/_reference.py` pins the size and SHA256 of each original it
executes (`taylor/replica2.py`, `qt1/qmref.py`, `qt1/blob.bin`, `twocomp/twocomp.py`,
`combo/combo.py`) and refuses a mismatch.

## Residual head (COMBO only)

`head_params` is 18 finite real weights: a list, tuple or 1-D array, or a mapping
`{"weights": [...]}` that may also carry `_credit` (so a JSON file passed with
`score.py --head-params` can keep its credit first). Wrong length, non-numbers, bools, NaN or
infinity, extra mapping keys, or a head on `qt1-ref`/`TwoComp` raise `ValueError`.

After each callback COMBO has its unclamped fees `(fb, fa)`. The head adds a bounded correction
before the single clamp to `[0, 0.1]`:

```text
x_i   = clip((feat_i - MU_i) / SD_i, -3, 3)                       i = 0..7
h_k   = tanh(HC_k + nsum(HU_k,i * x_i))                           k = 0..7
z_own = w[8]  + sum_k w[k]     * h_k        z_other = w[17] + sum_k w[9 + k] * h_k
own-side fee += 8e-4 * tanh(z_own)          other-side fee += 8e-4 * tanh(z_other)
```

- `w[0:8]`, `w[8]`: output weights and bias for the side that just traded (bid when `is_buy`,
  else ask); `w[9:17]`, `w[17]`: the same for the other side. Each correction is below 8 bps.
- Features, from the callback's own history only (sign `g` = +1 if `is_buy` else −1; `m`, `v` the
  filter mean and variance after the update; `s` the new spot; `s_prev` the spot at the previous
  callback): `g(m − ln s_prev)·1e4`, `sqrt(v)·1e4`, the last arb posterior `pi` (1.0 before the
  first step change), `g(ln s − m)·1e4`, `g(ln s2h − m)·1e4`, the phase (0, 1, 2),
  `log1p(|ln(s/s_prev)|·1e4)`, `log1p(silent steps, capped at 12)`.
- `MU`, `SD`, the cap and the hidden projection `HU`, `HC` (drawn from `random.Random(20261109)`)
  are the archived kernel's frozen constants in `ammfast/_head.py`. They are a fixed feature map.
- `nsum` is Neumaier compensated summation, which is what CPython ≥ 3.12's `sum()` does for
  floats; the output sums are plain left-to-right adds. Both are kept as in the archive.
- All-zero weights give COMBO's edges bit for bit (`8e-4 * tanh(0) == 0`), tested.
- **No trained weights are shipped or implied.** The archive's HEAD8 vector and `head.py` are not in
  this repository's references. The hook is tested against `ammfast/_reference.py`'s `ComboHead`,
  a plain-Python restatement of `combo.Combo.on_trade` with the head lines marked, run inside
  Taylor's unmodified `replica2.run`; with zero weights that class equals `combo.Combo` exactly.

## Parity and speed

Recorded in `baselines/replica/validation.json` (Linux x86_64, CPython 3.12.16, numba 0.68.1,
numpy 2.5.4, llvmlite 0.50.0):

- Gate: 3 policies × seeds `10000..10063` + `64000..64063` = 384 pairs of `ammfast.run` against
  CPython `replica2.run` with a fresh policy object per run. Maximum absolute error
  `1.19e-11` (343 pairs identical to the bit); limit `1e-9`. PASS.
- Single worker, warm, end to end through `run_batch`: `qt1-ref` 102.3, `TwoComp` 88.4, `COMBO`
  77.9 runs per second per core. CPython takes 0.16 / 0.21 / 0.24 s per run, so about 16–19×.
- Cold (fresh interpreter, empty Numba cache, import + table decode + compile + first run):
  13.9 s. Warm start of a fresh interpreter with the cache: 2.3–2.4 s. A warm run is about 10 ms.

These were measured on a shared machine and move with its load.

## Limits

- Only Linux x86_64 was executed. macOS arm64 is expected to work for replica runs but is
  **untested** here; another libm or LLVM can change the last bits, so re-run the gate there.
- Numba caches compiled code in `NUMBA_CACHE_DIR` (`scratch/numba_cache` after `. scratch/env.sh`),
  otherwise in `ammfast/__pycache__/`.
- One process uses one core. For parallel work, split seeds across processes as
  `baselines/replica/generate.py` does.
- Replica edges are not board scores: the replica's RNG streams differ from the Rust engine's.
