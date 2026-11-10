<!-- Credit: Taylor (gtmcknight), solved-policy research, amm-sim@4812a6382311624e1506aca131ca35c378f930d9, research/solved. -->
# Offline reference and Gate P

The foundation preserves Taylor's entire `research/solved` directory at
`4812a6382311624e1506aca131ca35c378f930d9`, including the complete runtime
closure of `gates.py`: `replica2.py`, `evalpol.py`, `real.py`, `latt.py`,
`pol_mid.json`, and `qm_k1e-5.json`. It also includes `calib_out_2048.json`,
README, and the remaining solved research scripts. Those additional solver
scripts can require NumPy/Numba; materialization and Gate P require only the
Python standard library.

The starter supplies all files under `reference/qt1`, `reference/twocomp`,
`reference/combo`, plus `tools/setup.sh`, `tools/check.py`, `tools/engine.py`,
and the tools README, at `4eb3e985fd31e3cd17832501b95b6d2491aca73d`.
The Numba artifact supplies the exact decoded `source_bundle.json` and its three
members (`fastsim.py`, `driver.py`, `extract_refs.py`). Artifact commands were
not executed. The archived scripts are research inputs: in particular, the
Numba driver expects additional HEAD8 files and a flattened reference directory.
Extraction does not promise that driver is a standalone installed kit. The
starter setup script can download or build dependencies; the kit
provides the pinned, no-Rust `setup_fast.sh` separately (see [setup.md](setup.md)).

## Offline extraction

From the repository root on Linux x86_64 or macOS arm64:

```sh
python3 -B reference/materialize.py --out scratch/reference
python3 -B gatep.py --reference scratch/reference/taylor --cache scratch/cache/gatep
python3 -B -m unittest discover -s tests -p test_gatep.py -v
```

These commands need no network, third-party packages, Rust, engine, or credentials.
The tests place all state under `scratch/`. macOS is supported by the standard
library/fcntl design but was not tested in this item. Python 3.9+ is required;
only the interpreter reported in the result was actually tested.

The output contains `taylor/`, `qt1/`, `twocomp/`, `combo/`, `numba/`, and `tools/`.
All originals retain exact upstream bytes; credits live in their text envelopes
so adding attribution never changes an upstream hash. Tools are extracted as
data, not executed or automatically made executable. Run a shell script with
`bash` only after reviewing its documented behavior and seed limits.

`reference/sources.json` records URLs, decoded byte lengths and SHA256s, envelope
SHA256s, commits, and available license notices. `reference/LICENSES.md` records
that neither pinned repository supplies a standalone license file and preserves
the distinction between qt1's MIT SPDX identifier and sources with no explicit
license grant. There is no invented blanket license.

Envelopes are JSON with `_credit` first and base64-encoded zlib-compressed bytes.
The manifest's SHA256 is pinned in `reference/materialize.py`. Materialization
verifies the manifest, all envelopes, their decoded sizes/digests, and the Numba
member mapping before writing. It is idempotent for unchanged destinations and
can resume a partial extraction. Extra files, changed files, and symlink paths
are rejected. Remove the explicitly chosen failed scratch extraction before
re-materializing; the tool does not silently overwrite it. Files are installed
using atomic replacements; an interrupted extraction can be resumed.

Required original digests:

| Original | SHA256 |
|---|---|
| decoded Numba source bundle JSON | `a5d32e4c06dc93881821d568d8d89ca50adfdba56d5dccd92afef6c6c2157952` |
| qt1/Strategy.sol | `c68887cff61180e12ba2b1ceb333edf76a830f91f63b5d775a8806fb9ec08aca` |

## Gate P cache

Without `--cache`, the default is `~/.cache/imd-amm/gatep`. This item's checks
always pass `--cache scratch/cache/gatep`; no host identifiers or configuration
are used. A local cache provides once-per-machine behavior when callers share
that cache. Deleting/moving it, changing the interpreter or inputs, or using a
separate cache requires another run. No hostname or machine-ID fingerprint is
needed.

Each invocation verifies every bundled Taylor file against the pinned manifest,
including README and calibration rows, even on a cache hit. Missing/extra files,
symlinks, and stale bytecode directories fail verification. A per-key advisory
`flock` serializes concurrent callers. After acquiring it, inputs are verified
again. A miss runs the **unmodified** `gates.py` in a temporary verified snapshot
inside the cache using the same interpreter with `-I -B`. The snapshot prevents
changes to the caller's reference tree from altering the running code and excludes
unlisted modules/bytecode. Inputs are checked again after execution.

All twelve values must equal the six-decimal values in the verified upstream
README, with the exact header, seed order, column count and output formatting.
A nonzero exit, stderr, wrong/missing/extra output, or timeout never creates a
successful cache record. Child timeouts are killed and reaped by `subprocess.run`.
The cache file is written with fsync and atomic replace while holding the lock.
Only successful stdout is cached, together with its SHA256 and validation metadata.

The key hashes the full upstream commit, `sys.version` (including Python build
and compiler), Python implementation, and a digest of the sorted Taylor
filename/SHA256 mapping. Cache hits revalidate identity, successful exit/status,
empty stderr, finite bounded elapsed time, stdout digest, and all twelve values.
A malformed or altered record fails closed; it is not treated as a cache miss.
To recover, remove that specific cache JSON file and retry. The checks detect
corruption against this checkout's pins; this is not signed remote execution
attestation and cannot prevent an actor who can rewrite the verifier itself.

`--timeout SECONDS` bounds lock waiting and execution, with default/max 450 seconds
and a positive finite value required. Hashing and filesystem overhead are outside
hard subprocess enforcement; callers should still cap the command at 480 seconds.
The normal run completes far below that budget on the tested machine. Interrupted
attempts do not cache partial results; rerunning the same command resumes from
any complete valid success record. Temporary directories from a hard kill can be
removed from the selected scratch cache. Gate P does not run engine seeds or tune
policies, and does not certify the Numba or Solidity ports.

## Publication

The authoritative inventory contains raw upstream URLs. [README.md](../README.md)
is the publication entry point carrying the pinned URL table for the kit.
Use that table for goal-repository raw URLs; this document does not assign a
commit hash for this wave.
