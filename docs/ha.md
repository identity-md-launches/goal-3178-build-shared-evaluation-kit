<!-- Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9, research/solved, solved-policy research. -->
# Imported HA calibration bank

`baselines/ha/HA-10000-12047.json` contains exactly 2048 ascending, unique
replica seeds and six-decimal edge strings in the [bank contract](contracts.md).
It imports Taylor’s archived calibration; it is not an ammfast output or a rerun.
The exporter uses only the Python standard library and requires the exact pinned
calibration bytes. It has no network access code and no simulation dependencies.

The source has 8192 records, with four kinds for each seed: `oracle`, `T`, `HA`,
and `fixed30`. Select `kind == "HA"`, use the record’s explicit `seed`, and export
its `edge` using `format(edge, ".6f")`. JSON numbers are parsed as Python floats;
rounding happens once on serialization. No subtraction of `norm`, averaging,
row-position inference, or substitution is performed.

This mapping follows Taylor’s pinned
[evalpol.py lines 11 and 92–100](https://github.com/gtmcknight/amm-sim/blob/4812a6382311624e1506aca131ca35c378f930d9/research/solved/evalpol.py#L92),
which reads this exact calibration file, indexes records by kind and seed, and
uses `ref[k][s]["edge"]` for HA comparisons. In
[replica2.py](https://github.com/gtmcknight/amm-sim/blob/4812a6382311624e1506aca131ca35c378f930d9/research/solved/replica2.py#L134),
`edge=e1` accumulates the policy pool’s trade edge (retail gains and arbitrage
losses); `norm=e2` belongs to the rival pool. The pinned
[README](https://github.com/gtmcknight/amm-sim/blob/4812a6382311624e1506aca131ca35c378f930d9/research/solved/README.md)
identifies these HA seeds and their mean, 529.557. The original calibration driver,
its invocation, and its Python version are not present in the pinned tree;
we establish the import semantics from the explicit data and pinned consumer
and metric code, without claiming to reconstruct the original generation run.

From the repository root on Linux x86_64:

```sh
mkdir -p scratch/ha
timeout 450s bash setup_fast.sh --platform linux-x86_64 --replica-only --cache scratch/cache --deadline 420
timeout 30s scratch/venv/bin/python -B reference/materialize.py --out scratch/ha/reference
timeout 30s scratch/venv/bin/python -B baselines/ha/export.py --source scratch/ha/reference/taylor/calib_out_2048.json --out baselines/ha/HA-10000-12047.json
timeout 30s scratch/venv/bin/python -B baselines/ha/export.py --source scratch/ha/reference/taylor/calib_out_2048.json --check baselines/ha/HA-10000-12047.json
timeout 60s scratch/venv/bin/python -B -m unittest discover -s tests -p test_ha.py -v
```

Setup is the only potentially online step. The materializer verifies the committed
envelopes and extracts unchanged upstream bytes. Tests materialize those envelopes
into a temporary directory under `scratch/ha`; they never download anything or run
policies. `-B` prevents bytecode caches outside scratch. Setup and materialization
are resumable with the same commands. For an interrupted export, rerun the export
and check commands; the small bank is regenerated in full. `--check` requires
canonical bytes, including provenance and key/row ordering, not just matching
numeric values. An invalid source fails before the output is written.

A preinstalled Python 3 can run the exporter and tests without setup. This item
was checked on Linux x86_64 with CPython 3.12.16; macOS arm64 was not tested.
For macOS the foundation offers `--platform macos-arm64 --replica-only`; provide
an external command timeout equivalent when GNU `timeout` is unavailable.

See [audit evidence](../baselines/ha/audit.md) for source digests, timings,
all-row coverage, deterministic export checks, and benchmark commands.
