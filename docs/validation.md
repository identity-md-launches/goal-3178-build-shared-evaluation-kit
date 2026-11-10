<!-- Credit: Taylor (gtmcknight), solved-policy research, amm-sim@4812a6382311624e1506aca131ca35c378f930d9 (research/solved). -->
# Whole-kit validation (`validate_kit.py`)

`validate_kit.py` checks the structure and provenance of the merged kit tree. It
uses only the Python standard library, works offline and writes nothing unless
`--out` is given, and that path must be under `scratch/`. It downloads nothing
and runs no simulator. It never imports kit code: `ammfast/` is read with `ast`
and is never executed. Its JSON report is deterministic: it contains no
timestamps, absolute paths or timings, and lists findings in sorted order.

**Limit:** this tool checks committed evidence for shape, coverage, digests and
internal consistency. It recomputes `|cpython_edge - ammfast_edge|` from the
values the replica worker *reports*. It does **not** run CPython replica2,
ammfast or the engine. A PASS therefore means that the evidence the kit claims is
present and consistent. It is **not** independent parity proof, and it does not
certify code written at the same time as this tool.

## Commands

```sh
# Full kit (exit 0 only when every part passes):
timeout 470 python3 -B validate_kit.py
# Select individual parts:
timeout 470 python3 -B validate_kit.py --parts reference,kit,engine,ha
# Save a copy of the report, and print per-part seconds to stderr:
timeout 470 python3 -B validate_kit.py --out scratch/kit-validator/report.json --timing
# Another checkout:
timeout 470 python3 -B validate_kit.py --root /path/to/checkout
# Adversarial fixture tests (fixtures are written to scratch/kit-validator/fx-*):
timeout 470 python3 -B -m unittest discover -s tests -p test_validate_kit.py -v
```

Exit codes: `0` means every requested part passed. `1` means a requested part
is `missing` or `fail`. `2` means a usage error, such as an unknown or duplicate
part. A `--out` path outside `scratch/` is refused. Each part takes well under
one second (benchmark below), so there is nothing to checkpoint. To pick up an
interrupted run, rerun the same command.

## Report

```text
_credit, validator, version, status: "PASS" | "FAIL",
parts: {part: {status: "pass" | "fail" | "missing", summary}},
coverage: {expected_rows, verified_rows, banks: {path: {expected, rows_well_formed, part_status}}},
findings: [{part, kind: "missing" | "invalid", code, path, detail}],
inputs_sha256: {repo-relative path: sha256 of every file read},
limits
```

A part's status is `missing` when all of its findings come from absent
evidence: a missing file or missing rows. `verified_rows` counts only rows in
parts that passed. Audit files count as evidence, not as extra banks. The
expected total is 9 replica banks (12288 rows), plus HA2048 and engine512, for
**14848 rows**.

## Parts and checks

All JSON is parsed strictly. The parser rejects duplicate keys, the
`NaN`/`Infinity` literals and numbers that overflow (such as `1e999`). Every
kit JSON document must have `_credit` as its first field, and that field must
name Taylor (gtmcknight) and the full pin
`4812a6382311624e1506aca131ca35c378f930d9`. Bank rows must hold exactly the
seeds start..end, sorted and unique. Each seed is an integer (not a bool), and
each `edge` is a finite string matching `^-?[0-9]+\.[0-9]{6}$`.

| part | checks |
|---|---|
| `reference` | `reference/sources.json` sha256 equals the pinned `62147ff1…`. Every one of the 35 envelopes is checked for envelope sha256, metadata, base64+zlib decoding, decoded sha256 and size. The qt1 entry is `c68887cf…` and the calib entry is `fdf24c7a…`. |
| `kit` | `score.py`, `setup_fast.sh` and `gatep.py` exist and credit Taylor with the full pin in their first 15 lines. The Python files parse. |
| `ammfast` | `ammfast/__init__.py` exists. Every `ammfast/**/*.py` file is credited and parses. `run(seed, policy, head_params=None)` and `run_batch(seeds, policy, head_params=None)` are defined in `__init__.py` or imported into it. The ids `qt1-ref`, `TwoComp` and `COMBO` appear in the sources. |
| `engine` | The bank is engine/qt1, screening_only, complete, with `remaining_seeds` and `failures` empty. It has rows 9700000..9700511, each with a nonnegative integer `after_swap_peak_gas`. Provenance: `qt1_source_sha256` is the pinned qt1 digest (also decoded from its envelope). `engine_commit`, `native_sha256` and `wheel_version` match `bootstrap/pins.sh`. Every download (release wheel, engine tarball, solc) has the sha256 and URL given in `bootstrap/manifest.tsv`. `engine_eval_sha256` and `bootstrap_smoke_sha256` match the committed files. `workers == 8`. There is one config per row in seed order, and `configs_sha256` is recomputed. Audit: there is one unrounded float row per bank row, and `format(edge,".6f")` equals the bank edge. Every gas counter is a nonnegative integer, and the error counters (`oog revert other_halt tx_error bad_return`) are zero. The Submission/afterSwap calls all succeeded, and their peak gas equals the bank gas. `validation.json`: bank, audit, validator and inventory digests match the committed bytes. The run is complete and failure-free. Reruns equal the audit values exactly. Config and worker evidence match the bank. |
| `ha` | The bank is replica/HA with rows 10000..12047. Its provenance names the calib sha256, the Taylor commit, the selection `{field: kind, equals: HA}`, `edge_field: edge`, the seed range and the row counts. The calib file is decoded from its envelope. Rows are selected by the explicit `kind == "HA"`, each `(HA, seed)` must be unique, and each bank edge must equal `format(source edge, ".6f")`. |
| `replica` | There are nine banks, named `baselines/replica/{policy}-{start}-{end}.json`, for `qt1-ref`, `TwoComp` and `COMBO` over 60000..61023, 64000..65023 and 10000..12047. Each must be replica/{policy}, with a nonempty provenance whose optional `seed_range` matches the file name. The validation contract is checked as described below. |

### Replica worker contract: `baselines/replica/validation.json`

```text
{"_credit": "...Taylor (gtmcknight)...4812a6382311624e1506aca131ca35c378f930d9...",
 "complete": true,
 "bindings": {"<repo-relative path>": "<sha256>", ...},
 "gates384": [{"policy","seed","cpython_edge","ammfast_edge","abs_error"}, ...],   // 384
 "spots576": [ same shape ],                                                        // 576
 "timings": {"runs_per_second_per_core": {"qt1-ref": n, "TwoComp": n, "COMBO": n},
             "cold_jit_seconds": n}}
```

* `bindings` must cover `reference/sources.json`, all nine banks and **every**
  `ammfast/**/*.py`. Each digest is 64 lowercase hex characters and must equal
  the sha256 of the current file. A changed, absent or unbound implementation
  file fails. A path is refused if it is absolute, contains `..` or `.` parts,
  contains backslashes, is not normalized, goes through a symlink or resolves
  outside the tree. The file must not bind itself, because that would be a
  self-hash cycle.
* `gates384` must hold exactly the three policies × (10000..10063 + 64000..64063).
  `spots576` must hold exactly the three policies × the first 64 seeds of each
  window (60000.., 64000.., 10000..). Every record has exactly those five keys
  and finite numeric edges. The tool recomputes `abs(cpython_edge -
  ammfast_edge)`, requires it to be `<= 1e-9`, and requires the reported
  `abs_error` to agree with it within `1e-12`. For any record whose seed is
  inside a bank, `format(ammfast_edge, ".6f")` must equal that bank's edge.
* `timings` needs one finite, positive rate per policy and a finite,
  nonnegative cold-JIT time. There is **no speed threshold**.

## Status on the repaired trunk (base 79f068cef81dadee171531f3381e207c193c959a)

With the evidence-key repair, the full validator exits **0**, status **PASS**,
with no findings. All six parts (`reference`, `kit`, `ammfast`, `engine`, `ha`,
`replica`) pass. Coverage is **14848/14848**: replica 12288, HA 2048 and engine
512. The replica summary reports 384 gates and 576 spots; its output keys remain
`gates` and `spots`. The worst gate and spot absolute errors are both
**1.1937117960769683e-11**, below the unchanged 1e-9 bound.

Benchmark: the initial failing full-tree check took 0.57 s wall time; the repaired
check took 0.62 s. With `--timing`, part times were reference 0.1031 s, kit
0.0072 s, ammfast 0.0335 s, engine 0.0579 s, HA 0.1899 s and replica 0.1067 s.
These checks inspect committed evidence; no simulator was run.

## Future simulator integration (not run by this tool)

The checks above are structural only. Scientific confirmation needs the pinned
runtimes. The following commands come from the existing kit docs and run in
`scratch/`:

```sh
bash setup_fast.sh --platform linux-x86_64 --cache scratch/cache        # pinned runtimes
python3 -B reference/materialize.py --out scratch/reference
python3 -B gatep.py --reference scratch/reference/taylor --cache scratch/cache/gatep
. scratch/env.sh && timeout 470 python -B baselines/engine/validate.py --output scratch/engine-validation.json
```

The explicit engine `--output` keeps rerun results under `scratch/`: the default
`--output` rewrites the timing fields of the committed
`baselines/engine/validation.json`.

For the replica, rerun CPython replica2 (`scratch/reference/taylor/replica2.py`)
and `ammfast.run` on a fresh sample of seeds, chosen independently of the
committed `gates384` and `spots576`. Compare the per-seed edges within 1e-9 and
compare their `.6f` strings with the banks. Use the generation command recorded
in each replica bank's `provenance`. This tool does not supply or vouch for that
command. Then rerun `validate_kit.py` and refresh the bindings. Engine seeds stay
inside 9700000..9700511 and are for screening only.
