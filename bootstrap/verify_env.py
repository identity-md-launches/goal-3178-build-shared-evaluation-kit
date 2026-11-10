"""bootstrap/verify_env.py - prove the environment setup_fast.sh built is the pinned one.

Credit: Taylor (gtmcknight), solved-policy research,
github.com/gtmcknight/amm-sim @ 4812a6382311624e1506aca131ca35c378f930d9 (research/solved).
The engine wheel, its .so hash and the check itself follow Taylor's setup-fast.sh
(release sim-6b6921f8).

Run by setup_fast.sh with the venv interpreter. It really imports every package, compares
versions with bootstrap/pins.sh, JIT-compiles one trivial numba function, and in engine mode
hashes the installed amm_sim_rs binary and checks the harness and solc are the pinned ones.
It runs no simulation and touches no engine seed. Prints one JSON line; exit 0 only if all pass.
"""
import argparse
import hashlib
import json
import os
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("replica", "engine"), required=True)
    for name in ("python", "numpy", "numba", "llvmlite", "pyrevm", "solcx", "solc",
                 "engine-root", "engine-wheel", "so-sha256"):
        ap.add_argument("--" + name, required=True)
    a = ap.parse_args()

    fails = []
    info = {"mode": a.mode, "executable": sys.executable}

    def expect(label, got, want):
        info[label] = got
        if got != want:
            fails.append("%s is %s, pinned %s" % (label, got, want))

    expect("python", "%d.%d.%d" % sys.version_info[:3], a.python)

    import numpy
    import llvmlite
    import numba
    expect("numpy", numpy.__version__, a.numpy)
    expect("llvmlite", llvmlite.__version__, a.llvmlite)
    expect("numba", numba.__version__, a.numba)

    @numba.njit(cache=False)
    def _tri(n):
        s = 0
        for i in range(n + 1):
            s += i
        return s

    if _tri(1000) != 500500:
        fails.append("numba JIT returned a wrong value")
    info["numba_jit"] = "ok"

    if a.mode == "engine":
        from importlib import metadata

        import amm_sim_rs
        import pyrevm  # noqa: F401
        import solcx

        expect("amm_sim_rs", metadata.version("amm_sim_rs"), a.engine_wheel)
        expect("pyrevm", metadata.version("pyrevm"), a.pyrevm)
        expect("py-solc-x", metadata.version("py-solc-x"), a.solcx)
        so = os.path.join(os.path.dirname(amm_sim_rs.__file__), "amm_sim_rs.abi3.so")
        with open(so, "rb") as f:
            expect("amm_sim_rs.abi3.so sha256", hashlib.sha256(f.read()).hexdigest(), a.so_sha256)
        for attr in ("run_batch", "SimulationConfig", "gas_stats", "gas_stats_reset"):
            if not hasattr(amm_sim_rs, attr):
                fails.append("amm_sim_rs lacks %s: not the instrumented post1 wheel" % attr)

        installed = [str(v) for v in solcx.get_installed_solc_versions()]
        info["solc_installed"] = installed
        if installed != [a.solc]:
            fails.append("solc versions visible to py-solc-x are %s, pinned [%s]" % (installed, a.solc))

        import amm_competition
        from amm_competition.competition import config as engine_config

        root = os.path.realpath(a.engine_root)
        where = os.path.realpath(os.path.dirname(os.path.dirname(amm_competition.__file__)))
        info["amm_competition"] = where
        if where != root:
            fails.append("amm_competition imports from %s, expected the pinned checkout %s" % (where, root))
        info["engine_n_steps"] = engine_config.BASELINE_SETTINGS.n_steps
        if not os.path.isfile(os.path.join(root, "contracts", "src", "VanillaStrategy.sol")):
            fails.append("engine checkout has no contracts/src/VanillaStrategy.sol")

    info["ok"] = not fails
    if fails:
        info["failures"] = fails
    print("verify_env: " + json.dumps(info, sort_keys=True))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
