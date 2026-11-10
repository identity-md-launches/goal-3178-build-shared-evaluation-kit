# bootstrap/pins.sh - non-download pins for setup_fast.sh (sourced, POSIX sh assignments only).
# Credit: Taylor (gtmcknight), solved-policy research,
# github.com/gtmcknight/amm-sim @ 4812a6382311624e1506aca131ca35c378f930d9 (research/solved).
# The engine wheel, its .so hash, harness.py and the fast-setup idea are Taylor's
# release github.com/gtmcknight/amm-sim/releases/tag/sim-6b6921f8.
# Download URLs, sizes and sha256 live in bootstrap/manifest.tsv; this file holds what is
# checked after installation.
IMD_PY_VERSION=3.12.16
IMD_PBS_RELEASE=20261026
IMD_NUMPY_VERSION=2.5.4
IMD_NUMBA_VERSION=0.68.1
IMD_LLVMLITE_VERSION=0.50.0
IMD_PYREVM_VERSION=0.3.7
IMD_PYSOLCX_VERSION=2.1.0
IMD_SOLC_VERSION=0.8.24
IMD_ENGINE_REPO=benedictbrady/amm-challenge
IMD_ENGINE_COMMIT=6b6921f8d463314aa7c96234e1be25a25245f704
# git tree object of that commit; the pinned source tarball re-hashes to it (see docs/setup.md).
IMD_ENGINE_TREE=87a2faad51fe2c2902bb28a67e7e49b257f09f2c
IMD_ENGINE_WHEEL_VERSION=0.1.0.post1
# sha256 of amm_sim_rs/amm_sim_rs.abi3.so inside the post1 wheel (Taylor's published value).
IMD_ENGINE_SO_SHA256=ff88b6353c2aad7342f0a26194f7347e78b9c3b38d683a7449006d423978cd14
# The only engine seed any smoke in this item may run.
IMD_SMOKE_ENGINE_SEED=9700000
