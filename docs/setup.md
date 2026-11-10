# Setup: `setup_fast.sh` and `bootstrap/`

Credit: Taylor (gtmcknight), solved-policy research,
github.com/gtmcknight/amm-sim @ 4812a6382311624e1506aca131ca35c378f930d9 (research/solved).
The prebuilt `amm_sim_rs` wheel, its `.so` hash, `harness.py` and the original no-Rust
`setup-fast.sh` are Taylor's release
[sim-6b6921f8](https://github.com/gtmcknight/amm-sim/releases/tag/sim-6b6921f8). This kit keeps
that idea and pins every downloaded byte.

## One command

Run from the repository root. `--platform` is always explicit; it is never guessed.

```sh
# Linux x86_64: replica toolchain + engine (prebuilt wheel, harness, solc)
bash setup_fast.sh --platform linux-x86_64 --cache scratch/cache

# Linux x86_64: replica toolchain only (CPython 3.12 + numpy/numba/llvmlite)
bash setup_fast.sh --platform linux-x86_64 --replica-only --cache scratch/cache

# macOS arm64 (macOS 12 or later): replica only; the engine wheel is Linux x86_64
bash setup_fast.sh --platform macos-arm64 --replica-only --cache scratch/cache

# then, in every later command
. scratch/env.sh && python -I your_script.py
```

Items of this goal always pass `--cache scratch/cache`. Without `--cache` the reusable cache
is `$HOME/.cache/imd-amm`. A rerun of the same command is the resume command: verified files
are never downloaded twice and a finished install is not redone.

| Flag | Meaning |
|---|---|
| `--platform linux-x86_64` or `macos-arm64` | required |
| `--replica-only` | skip the engine; required on macOS |
| `--cache PATH` | download cache, default `$HOME/.cache/imd-amm` |
| `--offline` | no network at all; lists every file the cache lacks and exits 5 |
| `--scratch PATH` | where all runtime state goes, default `<repo>/scratch` |
| `--fetch-only` | download and verify into the cache, install nothing; works on any host |
| `--smoke` | run `bootstrap/smoke_replica.py`, and in engine mode `bootstrap/smoke_engine.py` |
| `--force` | reinstall from the cache even if scratch already matches the pins |
| `--deadline SEC` | stop cleanly with exit 75 before SEC seconds are used (default 420, max 470) |
| `--manifest FILE` | test hook; the only way `file://` URLs are accepted |

Exit codes: 0 ok; 2 usage, or a refused platform or mode; 3 the host is not the requested
platform; 4 sha256 or size mismatch; 5 offline cache miss; 6 download failed after 3 attempts;
7 install or verification failed; 75 deadline reached, rerun to resume.

The host needs `bash`, `tar`, `sha256sum` or `shasum`, and `curl` for online runs. Nothing
else: no Rust, no compiler, no maturin, no uv, no package index. Linux needs glibc 2.28 or
later (the numpy, numba and llvmlite wheels are `manylinux_2_28`).

## Where things are

Everything below is under `scratch/` (or the `--scratch` path), which `.gitignore` excludes.

| Path | What |
|---|---|
| `scratch/env.sh` | source this; sets the variables below and puts the venv first on `PATH` |
| `scratch/venv/bin/python` | **the interpreter**, CPython 3.12.16 (`$IMD_AMM_PYTHON`) |
| `scratch/python/<hash>/python/` | the extracted standalone CPython the venv points at |
| `scratch/engine/amm-challenge/` | **the engine checkout**, benedictbrady/amm-challenge @ `6b6921f8d463314aa7c96234e1be25a25245f704` (`$IMD_AMM_ENGINE`); `amm_competition` imports from here |
| `scratch/engine/harness.py` | Taylor's paired harness for the post1 wheel (`$IMD_AMM_HARNESS`) |
| `scratch/solcx/solc-v0.8.24` | the Solidity compiler py-solc-x finds through `$SOLCX_BINARY_PATH` |
| `scratch/numba_cache/`, `scratch/tmp/`, `scratch/xc/` | `$NUMBA_CACHE_DIR`, `$TMPDIR`, `$XDG_CACHE_HOME` |
| `scratch/.setup_fast/` | install stamp, `timings.log`, `smoke_replica.json`, `smoke_engine.json` |
| `<cache>/sha256/<sha256>/<filename>` | cached downloads; `<cache>/tmp/` holds in-flight transfers |

`env.sh` exports `IMD_AMM_PLATFORM`, `IMD_AMM_MODE` (`engine` or `replica`),
`IMD_AMM_SCRATCH`, `IMD_AMM_PYTHON`, `VIRTUAL_ENV`, `TMPDIR`, `XDG_CACHE_HOME`,
`NUMBA_CACHE_DIR`, `PYTHONNOUSERSITE=1`, `PIP_NO_INDEX=1`, and in engine mode
`IMD_AMM_ENGINE`, `IMD_AMM_HARNESS` and `SOLCX_BINARY_PATH`. The engine-only variables are
absent after `--replica-only`, and `import amm_sim_rs` fails there by design.

The venv has no `pip`. To add a package, pin its wheel in `bootstrap/manifest.tsv`.

## What the script does

1. **Refuses** early: missing or unknown `--platform`, macOS without `--replica-only`, or a
   host that is not the requested platform (unless `--fetch-only`). A refused run creates nothing.
2. **Fetches** each pinned file of the chosen set. A cached file is re-hashed before every use.
   A download goes to `<cache>/tmp/<name>.<pid>.part` with `curl` limited to https, a 20 s
   connect timeout, 240 s per transfer and the pinned size as `--max-filesize`; size then
   sha256 are checked, and only then is the file renamed into the cache, so the cache never
   holds a partial or unverified file. A mismatch deletes the download and exits 4.
3. **Installs** into scratch: extracts the standalone CPython, creates a venv without pip, and
   installs the wheels with that archive's bundled pip using
   `--no-index --no-deps --require-hashes`, so pip re-checks the same hashes and cannot reach
   an index. In engine mode it extracts the engine source, copies the harness and solc, and adds
   the checkout to the venv with a `.pth` file, which avoids `pip install -e .` and the
   setuptools download that would need.
4. **Verifies** with `bootstrap/verify_env.py`: real imports, versions equal to
   `bootstrap/pins.sh`, a numba JIT call, and in engine mode the sha256 of the installed
   `amm_sim_rs.abi3.so`, the `gas_stats` API of the post1 wheel, solc 0.8.24 being the only
   compiler py-solc-x sees, and `amm_competition` importing from the pinned checkout.
   No simulation runs here, so engine preparation consumes no seed.
5. **Prints** one summary line with `setup=cold` (something was downloaded) or `setup=cached`,
   `install=fresh` or `reused`, and the seconds spent in fetch, install, verify and smoke.

## Smokes

`bootstrap/smoke_replica.py` is a synthetic, seeded numba AMM loop with the shape of a replica
run (10,000 steps). It is not replica2 and its numbers are not policy edges. It benchmarks 4
runs, then caps its batch at 256 runs or 4 seconds, checks the run is reproducible and
reports runs per second per core.

`bootstrap/smoke_engine.py` compiles the checkout's `VanillaStrategy.sol` and runs it against
itself on engine seed **9700000**, with one `amm_sim_rs.run_batch(X, Vanilla, [config], 8)`
call. Any other seed is refused before anything runs. The config is
`MatchRunner._build_configs` transcribed for one index, checked field by field against the real
method on indices 0..3 (construction only). Do not use `harness.py`'s `build_configs` for
seeds near 9,700,000: it builds every config from 0 up to the last seed.

## Pinned downloads

`bootstrap/manifest.tsv` is the source of truth. Versions: CPython 3.12.16
(python-build-standalone 20261026, `install_only_stripped`), numpy 2.5.4, numba 0.68.1,
llvmlite 0.50.0, amm_sim_rs 0.1.0.post1, pyrevm 0.3.7, py-solc-x 2.1.0, solc 0.8.24,
requests 2.34.2, urllib3 2.8.0, idna 3.20, certifi 2026.10.23, charset-normalizer 3.5.2,
packaging 26.3.

| Set | Kind | Bytes | sha256 | URL |
|---|---|---|---|---|
| linux-x86_64:replica | python | 34505639 | `ce4f6d0a6cfdb3bed6e44c2f9affd85943c76a42e25ad3a259002f31d6fb8a20` | https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.12.16+20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz |
| linux-x86_64:replica | wheel | 16727283 | `fbde6962867ee75b48b0ee29b2b9372ec5d617799dbaf38e82dc0596f2f7738a` | https://files.pythonhosted.org/packages/43/a3/c799c62e19c337e6d3770b08e475887fb30ce8477d3c09efca6b2f0228a6/numpy-2.5.4-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl |
| linux-x86_64:replica | wheel | 3890001 | `8b33fee239194b9b6cb9b176f3c2563b94e5882fd846748ad0cf960daeee5406` | https://files.pythonhosted.org/packages/43/23/0813ab091f60403c33cb741392ae77db13324587748df6f52167bf3a2404/numba-0.68.1-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl |
| linux-x86_64:replica | wheel | 59696588 | `d501e5103076b9a14be885d2574dc2f6793171aa54a853d1244e011d476f1399` | https://files.pythonhosted.org/packages/4c/72/22b6449e15bec4cc86c62b659e6c625ab777d01e87aaec717ecef440f87a/llvmlite-0.50.0-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl |
| linux-x86_64:engine | wheel | 727586 | `aedb68280aca34c110968f00148387c95885207913fb2cf19a1ab4c5cfd6887a` | https://github.com/gtmcknight/amm-sim/releases/download/sim-6b6921f8/amm_sim_rs-0.1.0.post1-cp310-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl |
| linux-x86_64:engine | wheel | 4317826 | `d055860c3352d6bc180b80255d8563f0b3c0fe6ce62b5c2a52e1bc7ecc4fad4f` | https://files.pythonhosted.org/packages/7c/6c/17f60815e678474fa8bb36363e4a9f336fa444abb983c2a1e4bb08f681ae/pyrevm-0.3.7-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl |
| linux-x86_64:engine | wheel | 18126 | `4bf45aa96c29ed0a3654dcb6951f0f14c648d2be3f938260babbbd1ace6dde04` | https://files.pythonhosted.org/packages/8d/6d/3456e1c77b7efe9e277c5202bb8b879048117369f97c2b3dcefbc4c43071/py_solc_x-2.1.0-py3-none-any.whl |
| linux-x86_64:engine | wheel | 129956 | `d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c` | https://files.pythonhosted.org/packages/63/34/ba1c580383c9eada3711951fef0795c80b829a078d72188184bcab9dd527/packaging-26.3-py3-none-any.whl |
| linux-x86_64:engine | wheel | 73075 | `2a0d60c172f83ac6ab31e4554906c0f3b3588d37b5cb939b1c061f4907e278e0` | https://files.pythonhosted.org/packages/a0/f4/c67b0b3f1b9245e8d266f0f112c500d50e5b4e83cb6f3b71b6528104182a/requests-2.34.2-py3-none-any.whl |
| linux-x86_64:engine | wheel | 257601 | `3d31298449090ab8d47b7b1b2a555ff73cac7ed438a08b7ac160980c7ebed649` | https://files.pythonhosted.org/packages/7f/c5/38806a25ab5e65fc178f39affeda20858efafede2fce1ffc2556cfc9fe73/charset_normalizer-3.5.2-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl |
| linux-x86_64:engine | wheel | 69583 | `ab7ae7122974553370f0bdb919e1a960b2cd1bc1ef0276416d896db81c14582c` | https://files.pythonhosted.org/packages/58/a2/bb081bab032533a855d44de1d56f8e8426114ff1ba5d1f07a438a0a654f8/idna-3.20-py3-none-any.whl |
| linux-x86_64:engine | wheel | 135717 | `0cf3cae568d36aa9576b28dfb35f11328f1cb974ca7647d9475ebb86c75ac6e3` | https://files.pythonhosted.org/packages/92/9d/c4e665119135114480843e7ab388fa94d8480650450e6f8e26b70d323a4c/urllib3-2.8.0-py3-none-any.whl |
| linux-x86_64:engine | wheel | 135082 | `e9b54de76e530bb6cf0ccd036787103acb58fd42ba930ea8bf948ddbaa2f61d1` | https://files.pythonhosted.org/packages/32/d1/a3652fe2c19134d0d15922177821d863cbd7e842d53a21bbe7e8e3a1b5ac/certifi-2026.10.23-py3-none-any.whl |
| linux-x86_64:engine | engine-src | 58742 | `fa91b175c4b0db13fa44657903f55f664c80fb34796172c94d57ebebba71ec9e` | https://codeload.github.com/benedictbrady/amm-challenge/tar.gz/6b6921f8d463314aa7c96234e1be25a25245f704 |
| linux-x86_64:engine | harness | 5222 | `0fe3bcb3f836bf9bbd8f6b88b06c7cbd2971bfd5999c4b366aba25675f86c590` | https://github.com/gtmcknight/amm-sim/releases/download/sim-6b6921f8/harness.py |
| linux-x86_64:engine | solc | 15020760 | `fb03a29a517452b9f12bcf459ef37d0a543765bb3bbc911e70a87d6a37c30d5f` | https://binaries.soliditylang.org/linux-amd64/solc-linux-amd64-v0.8.24+commit.e11b9ed9 |
| macos-arm64:replica | python | 25035343 | `353d5f1b4fd2715e7053f12f068435d5bbad16b91e057006b5d01e0925709a7e` | https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.12.16+20261026-aarch64-apple-darwin-install_only_stripped.tar.gz |
| macos-arm64:replica | wheel | 12015718 | `b11e8fda06a7d69f15ebf542660b74466c2e51094800c1fb794f47ad4faeef17` | https://files.pythonhosted.org/packages/ff/a9/bac826765e971d8e16e2064e9ac7525fd69b40ac17c905033a7f5442023f/numpy-2.5.4-cp312-cp312-macosx_11_0_arm64.whl |
| macos-arm64:replica | wheel | 2762483 | `7de5e5d00e6c8967fc861be2a015eb8ad701ab3cf65d7f4a2c4d950990cf65d3` | https://files.pythonhosted.org/packages/d4/a7/2e54558d08bb64c8718ad398314605b6c73d154a593cf2bbf1360047fa4e/numba-0.68.1-cp312-cp312-macosx_12_0_arm64.whl |
| macos-arm64:replica | wheel | 40534277 | `55f50a6b7c0b8de88b05d6bc407d70a60486ce024013997dc97e202bd187c75b` | https://files.pythonhosted.org/packages/d9/1f/2576416b3e9b73f77b8331b7f2e41ce5ae7bbff0489eb16d98099a71693c/llvmlite-0.50.0-cp312-cp312-macosx_12_0_arm64.whl |

How each pin was derived and cross-checked on 2026-11-10:

- **CPython archives**: downloaded and hashed; equal to the `digest` GitHub publishes for
  each release asset.
- **PyPI wheels**: the Linux set is what pip 26.2.2 resolved for
  `numpy==2.5.4 numba==0.68.1 py-solc-x==2.1.0 pyrevm==0.3.7` with `--only-binary=:all:`,
  so it includes every transitive wheel. Each file's sha256 and size equal PyPI's JSON API.
  The macOS wheels are the `cp312` `arm64` files of the same three versions.
- **amm_sim_rs post1 wheel** and **harness.py**: equal to the release asset digests and to the
  wheel hash in Taylor's release README. The `.so` inside hashes to
  `ff88b6353c2aad7342f0a26194f7347e78b9c3b38d683a7449006d423978cd14`, the value that README
  publishes; `verify_env.py` re-checks it on every run.
- **solc**: equal to the sha256 in `https://binaries.soliditylang.org/linux-amd64/list.json`.
- **Engine pin**: the GitHub API confirms commit `6b6921f8d463314aa7c96234e1be25a25245f704`
  in benedictbrady/amm-challenge (2026-02-07, tree `87a2faad51fe2c2902bb28a67e7e49b257f09f2c`).
  The pinned tarball was extracted and `git add -A && git write-tree` returned that same tree
  hash, so the archive is byte-for-byte that commit. GitHub generates this tarball on request;
  if its bytes ever change, setup fails closed with exit 4 and cached copies keep working.

## macOS arm64 status

The four macOS files download, match their pins, and are what they claim: the interpreter is a
64-bit arm64 Mach-O and the wheels are tagged `macosx_11_0_arm64` (numpy) and
`macosx_12_0_arm64` (numba, llvmlite). **Nothing has been executed on macOS.** The first run
there is `python3 tests/test_setup_fast.py -v`, whose `MacIntegration` test does a real
replica-only setup.

## Tests

```sh
python3 tests/test_setup_fast.py -v      # any Python 3.8+, standard library only
```

They cover hash mismatch, size bound, missing cache offline, cached offline rerun, a corrupt
cache entry, a failed transfer, the deadline, platform and mode refusal, the manifest's
pins, a real Linux engine setup with imports, an offline rerun, an offline forced reinstall
with both smokes, replica-only, and the macOS files. Offline runs put a failing fake `curl`
first on `PATH`. All state stays under `scratch/`, and every run passes `--cache scratch/cache`
or a subdirectory of it.

## Changing a pin

Edit the row in `bootstrap/manifest.tsv` (url, size, sha256) and the matching version in
`bootstrap/pins.sh`, run `bash setup_fast.sh --platform <p> --fetch-only --cache scratch/cache`
to prove the new bytes match, then rerun the tests. The install stamp includes every hash, so
the next setup reinstalls by itself.
