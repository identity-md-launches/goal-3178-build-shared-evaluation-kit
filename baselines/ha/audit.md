<!-- Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9, research/solved, solved-policy research. -->
# HA import audit

Foundation checkout: `4a02cd145745a9d1c45d2ca8d669b650dce619f8`.
Work was capped at 55 minutes; every shell command used a timeout below 480 seconds.
No engine simulations, other policy simulations, tuning, or Gate P runs occurred.
The bank is imported historical data.

## Byte identity and source interpretation

All upstream source URLs start with
`https://raw.githubusercontent.com/gtmcknight/amm-sim/4812a6382311624e1506aca131ca35c378f930d9/research/solved/`.
The committed `reference/materialize.py` verifies the inventory and all 35
originals against their envelope and original-byte hashes. No upstream code was
executed to generate this bank.

| File | SHA256 |
| --- | --- |
| `calib_out_2048.json` (2,948,144 bytes) | `fdf24c7aa2b604d770336a5783276da8ed4d50bca9dbcc02e98b4da58f608d26` |
| `evalpol.py` | `6e67667bfed7798bdd3384652a6efc6ba0b1fce653740d0f9af2bcbcb8e6a27c` |
| `replica2.py` | `6e37b4e61e1ffa5ffb1e43912ddb8659105989aa986eb48fcd1d7608f8dca685` |
| `HA-10000-12047.json` (127,347 bytes) | `09719da22be28598e9c1d0bb1eb935d7b0b4eeafef3321e0fabe4f3ec9e2b7a9` |

`evalpol.py:11` selects `calib_out_2048.json`; lines 92–94 explicitly set
`range(10000,12048)` and map each row by `kind` and `seed`. Lines 99–100 compare
against `ref[k][s][edge]`, including `k=HA`. These are explicit seed identities,
not an inferred order. `replica2.py:37,74,85,110,129,134` initialize, accumulate,
and return `e1` as `edge`; `e2` is returned as `norm`. Thus the imported quantity
is the policy pool’s own trade edge, including retail and arb contributions.
The archived HA mean is 529.5574342875143 before rounding, consistent with
Taylor’s `research/solved/README.md:5` (529.557). This mean is corroboration only;
the exporter never uses an averaged substitute.

The pinned recursive tree and `research/replica.py` were inspected for a
calibration generator. No standalone generator/HA driver or original invocation
was found; `research/replica.py` defines an earlier metric implementation, not a
calibration export driver. This limits historical reproduction, not the explicit
mapping supported by the data and `evalpol.py`. Original runtime version and
exact calibration-generation command remain unknown and are recorded as such.
No missing historical value was guessed.

## Measurements and validation

Before implementing the exporter, a real workload parsed the 2,948,144-byte
source on CPython 3.12.3: 0.053949 s, then selected HA records and formatted and
serialized the first 64 in 0.002990 s. This bounded the full workload well below
the item cap. Checkpoints are under `scratch/ha/`.

Pinned Linux x86_64 replica-only setup: 9.515 s total (fetch 2.146, install
5.272, verify 2.069), four downloads / 114,819,511 bytes. CPython 3.12.16.
No setup smoke or simulation was requested. All caches and installations are
under scratch.

Full exports: 10 byte-identical exports in 0.801574 s, 0.080157 s/export;
canonical validation: 0.075190 s. Each export SHA-checks the source, validates all
8192 records and produces 2048 HA records. All four kinds have exactly the
2048 expected integer seeds. First HA edge: `440.443361` (10000); last:
`409.823332` (12047). All 2048 exported edges match independently selected source
values formatted to six decimals. No numerical error tolerance or sampling is
used for bank validation.

Offline test command (see `docs/ha.md` for the complete setup/export sequence):

```sh
timeout 60s scratch/venv/bin/python -B -m unittest discover -s tests -p test_ha.py -v
```

15 tests passed in 5.851 s, zero failures/errors/skips. Coverage includes all
2048 values, source/evidence hashes, cardinality/endpoints, ascending unique
seeds, shuffled input mapping, deterministic exports and committed bank bytes,
malformed JSON/encoding, duplicate JSON keys, missing/extra records, missing
fields, duplicate seeds with unchanged cardinality, bad types and out-of-range
seeds, NaN/infinities/numeric overflow, non-HA record validation, altered bank
metadata/values/order, digest mismatch, CLI export/check/failure, and source
preservation. Counts describe functional coverage, not measured line coverage.

Repeat the full benchmark using the same timed operations:

```sh
timeout 30s scratch/venv/bin/python -B - <<'PY'
import importlib.util, time
from pathlib import Path
spec = importlib.util.spec_from_file_location('ha', 'baselines/ha/export.py')
ha = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ha)
raw = Path('scratch/ha/reference/taylor/calib_out_2048.json').read_bytes()
t = time.perf_counter()
outputs = [ha.export_bytes(raw) for _ in range(10)]
print('ten exports seconds:', time.perf_counter() - t)
assert all(b == outputs[0] for b in outputs)
bank = Path('baselines/ha/HA-10000-12047.json').read_bytes()
t = time.perf_counter()
ha.validate_bank(raw, bank)
print('validation seconds:', time.perf_counter() - t)
PY
```

No tests failed. One shell heredoc quoting warning truncated this audit's draft
benchmark block; the block was repaired and its command checked before delivery.
Deliberately invalid inputs in tests were rejected as expected. macOS arm64,
other architectures and other Python versions have not undergone the full suite.
No replica or engine parity claim is made by this import item. Resume by repeating
the bounded commands in `docs/ha.md`; no simulation checkpoint is needed.
