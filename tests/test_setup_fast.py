"""tests/test_setup_fast.py - tests for setup_fast.sh and bootstrap/.

Credit: Taylor (gtmcknight), solved-policy research,
github.com/gtmcknight/amm-sim @ 4812a6382311624e1506aca131ca35c378f930d9 (research/solved).

Run from the repository root with any Python 3.8+ (standard library only):

    python3 tests/test_setup_fast.py -v

Three groups:
  * ManifestPins / Refusals / FetchLogic need no network. FetchLogic drives the real script
    through its --manifest test hook with file:// payloads, so hash mismatch, missing cache,
    cached offline rerun, corrupt cache, bounded size and the deadline are exercised in seconds.
  * LinuxIntegration does the real thing on a Linux x86_64 host: cold or cached engine setup,
    real imports, an offline rerun, an offline forced reinstall with the bounded smokes
    (engine seed 9700000 only), and a replica-only install. It is skipped on other hosts, or
    when the cache is empty and the network is unreachable.
  * MacArtifacts downloads and hash-verifies the macOS arm64 files and inspects them
    (Mach-O arm64 interpreter, arm64 wheel tags). It does not execute them.

Every invocation passes --cache scratch/cache (or a subdirectory of it) and keeps all state
under scratch/. Offline runs put a failing fake `curl` first on PATH to prove no transfer is tried.
"""
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import unittest
import zipfile
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup_fast.sh"
MANIFEST = ROOT / "bootstrap" / "manifest.tsv"
PINS = ROOT / "bootstrap" / "pins.sh"
SCRATCH = ROOT / "scratch"
CACHE = SCRATCH / "cache"
TDIR = SCRATCH / "test_setup_fast"
CREDIT_COMMIT = "4812a6382311624e1506aca131ca35c378f930d9"
ENGINE_COMMIT = "6b6921f8d463314aa7c96234e1be25a25245f704"
POST1_WHEEL_SHA = "aedb68280aca34c110968f00148387c95885207913fb2cf19a1ab4c5cfd6887a"
SMOKE_SEED = 9700000
HOST = (platform.system(), platform.machine())
IS_LINUX_X86 = HOST == ("Linux", "x86_64")
IS_MAC_ARM = HOST == ("Darwin", "arm64")


def fake_curl_dir():
    """A directory whose `curl` records that it was called and fails."""
    d = TDIR / "fakebin"
    d.mkdir(parents=True, exist_ok=True)
    marker = d / "curl-was-called"
    if marker.exists():
        marker.unlink()
    script = d / "curl"
    script.write_text("#!/bin/sh\necho called >> '%s'\nexit 99\n" % marker)
    script.chmod(0o755)
    return d, marker


def run_setup(*args, no_network=False, timeout=470):
    env = dict(os.environ)
    marker = None
    if no_network:
        d, marker = fake_curl_dir()
        env["PATH"] = "%s%s%s" % (d, os.pathsep, env.get("PATH", ""))
    p = subprocess.run(["bash", str(SETUP)] + [str(a) for a in args], cwd=str(ROOT), env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                       timeout=timeout)
    p.curl_called = bool(marker and marker.exists())
    return p


def manifest_rows(path=MANIFEST):
    rows = []
    for line in Path(path).read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        rows.append(line.split("\t"))
    return rows


def pins():
    out = {}
    for line in PINS.read_text().splitlines():
        m = re.match(r"^([A-Z_]+)=(\S+)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def summary_fields(stdout):
    lines = [l for l in stdout.splitlines() if l.startswith("setup_fast: OK ")]
    assert lines, "no summary line in:\n" + stdout
    return dict(kv.split("=", 1) for kv in lines[-1].split()[2:])


class ManifestPins(unittest.TestCase):
    def test_rows_are_fully_pinned_https_downloads(self):
        rows = manifest_rows()
        self.assertGreaterEqual(len(rows), 20)
        seen_sha, seen_name = set(), set()
        for r in rows:
            self.assertEqual(len(r), 6, r)
            mset, kind, sha, size, name, url = r
            self.assertIn(mset, ("linux-x86_64:replica", "linux-x86_64:engine", "macos-arm64:replica"))
            self.assertIn(kind, ("python", "wheel", "engine-src", "harness", "solc"))
            self.assertRegex(sha, r"^[0-9a-f]{64}$")
            self.assertGreater(int(size), 0)
            self.assertTrue(url.startswith("https://"), url)
            self.assertNotIn("latest", url)
            self.assertNotIn("/main/", url)
            self.assertNotIn(sha, seen_sha)
            self.assertNotIn((mset.split(":")[0], name), seen_name)
            seen_sha.add(sha)
            seen_name.add((mset.split(":")[0], name))
            if kind == "wheel":
                self.assertTrue(name.endswith(".whl"), "no source builds: " + name)
            for banned in ("maturin", "rustup", "cargo", "setuptools", "ziglang"):
                self.assertNotIn(banned, name.lower())

    def test_platform_sets(self):
        rows = manifest_rows()
        self.assertFalse([r for r in rows if r[0] == "macos-arm64:engine"], "macOS is replica only")
        p = pins()
        for plat, tag in (("linux-x86_64", "x86_64-unknown-linux-gnu"), ("macos-arm64", "aarch64-apple-darwin")):
            mine = [r for r in rows if r[0] == plat + ":replica"]
            py = [r for r in mine if r[1] == "python"]
            self.assertEqual(len(py), 1)
            self.assertIn("cpython-%s+%s-%s" % (p["IMD_PY_VERSION"], p["IMD_PBS_RELEASE"], tag), py[0][4])
            self.assertTrue(p["IMD_PY_VERSION"].startswith("3.12."))
            wheels = sorted(r[4] for r in mine if r[1] == "wheel")
            self.assertEqual(len(wheels), 3)
            self.assertTrue(wheels[0].startswith("llvmlite-%s-cp312-" % p["IMD_LLVMLITE_VERSION"]))
            self.assertTrue(wheels[1].startswith("numba-%s-cp312-" % p["IMD_NUMBA_VERSION"]))
            self.assertTrue(wheels[2].startswith("numpy-%s-cp312-" % p["IMD_NUMPY_VERSION"]))
            for w in wheels:
                self.assertIn("x86_64" if plat == "linux-x86_64" else "arm64", w)

    def test_engine_pins(self):
        rows = [r for r in manifest_rows() if r[0] == "linux-x86_64:engine"]
        p = pins()
        self.assertEqual(p["IMD_ENGINE_COMMIT"], ENGINE_COMMIT)
        self.assertEqual(p["IMD_SMOKE_ENGINE_SEED"], str(SMOKE_SEED))
        eng = [r for r in rows if r[4].startswith("amm_sim_rs-")]
        self.assertEqual(len(eng), 1)
        self.assertEqual(eng[0][2], POST1_WHEEL_SHA)
        self.assertIn("0.1.0.post1-cp310-abi3-manylinux2014_x86_64", eng[0][4])
        self.assertIn("gtmcknight/amm-sim/releases/download/sim-6b6921f8/", eng[0][5])
        src = [r for r in rows if r[1] == "engine-src"]
        self.assertEqual(len(src), 1)
        self.assertTrue(src[0][5].endswith("benedictbrady/amm-challenge/tar.gz/" + ENGINE_COMMIT))
        solc = [r for r in rows if r[1] == "solc"]
        self.assertEqual(len(solc), 1)
        self.assertIn("v%s+commit." % p["IMD_SOLC_VERSION"], solc[0][4])
        self.assertEqual(len([r for r in rows if r[1] == "harness"]), 1)
        names = " ".join(r[4] for r in rows)
        for dep in ("pyrevm-%s-" % p["IMD_PYREVM_VERSION"], "py_solc_x-%s-" % p["IMD_PYSOLCX_VERSION"],
                    "requests-", "packaging-", "urllib3-", "idna-", "certifi-", "charset_normalizer-"):
            self.assertIn(dep, names)

    def test_every_delivered_file_credits_taylor(self):
        files = [SETUP, ROOT / ".gitignore", Path(__file__), ROOT / "docs" / "setup.md"]
        files += sorted((ROOT / "bootstrap").iterdir())
        for f in files:
            if f.is_dir():
                continue
            head = "\n".join(f.read_text().splitlines()[:12])
            self.assertIn("gtmcknight", head, f)
            self.assertIn(CREDIT_COMMIT, head, f)

    def test_default_cache_and_no_host_package_manager(self):
        text = SETUP.read_text()
        self.assertIn('CACHE="$HOME/.cache/imd-amm"', text)
        self.assertIn("--no-index", text)
        self.assertIn("--require-hashes", text)
        for banned in ("uv pip", "maturin", "cargo ", "rustup", "apt-get", "brew ", "install_solc", "/latest/"):
            self.assertNotIn(banned, text)


class Refusals(unittest.TestCase):
    def args(self, name):
        return ["--cache", CACHE / "_test" / name, "--scratch", TDIR / name / "scratch", "--offline"]

    def test_platform_is_required(self):
        p = run_setup(*self.args("noplat"), no_network=True)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("--platform is required", p.stderr)

    def test_unknown_platform_refused(self):
        p = run_setup("--platform", "windows-x86_64", *self.args("badplat"), no_network=True)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("unsupported --platform", p.stderr)

    def test_macos_engine_mode_refused(self):
        for extra in ([], ["--fetch-only"]):
            p = run_setup("--platform", "macos-arm64", *extra, *self.args("macengine"), no_network=True)
            self.assertEqual(p.returncode, 2, p.stderr)
            self.assertIn("replica work only", p.stderr)
            self.assertFalse(p.curl_called)

    def test_foreign_platform_install_refused(self):
        if IS_MAC_ARM:
            plat, extra = "linux-x86_64", []
        else:
            plat, extra = "macos-arm64", ["--replica-only"]
        name = "foreign"
        shutil.rmtree(str(TDIR / name), ignore_errors=True)
        p = run_setup("--platform", plat, *extra, *self.args(name), no_network=True)
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertIn("Nothing was installed", p.stderr)
        self.assertFalse((TDIR / name / "scratch").exists(), "a refused run must not create state")

    def test_bad_flags_refused(self):
        p = run_setup("--platform", "linux-x86_64", "--with-rust", *self.args("badflag"), no_network=True)
        self.assertEqual(p.returncode, 2, p.stderr)
        p = run_setup("--platform", "linux-x86_64", "--fetch-only", "--deadline", "9999", *self.args("badflag"),
                      no_network=True)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("470", p.stderr)


class FetchLogic(unittest.TestCase):
    """The real fetch/verify/cache code path, fed by a tiny manifest of file:// payloads."""

    def setUp(self):
        self.name = self.id().split(".")[-1]
        self.dir = TDIR / self.name
        self.cache = CACHE / "_test" / self.name
        for d in (self.dir, self.cache):
            shutil.rmtree(str(d), ignore_errors=True)
        (self.dir / "src").mkdir(parents=True)
        self.payloads = {}
        for fname, data in (("alpha.bin", b"alpha " * 1000), ("beta.whl", b"beta-wheel " * 3000)):
            (self.dir / "src" / fname).write_bytes(data)
            self.payloads[fname] = data

    def manifest(self, overrides=None, urls=None):
        overrides = overrides or {}
        lines = ["# test manifest. Credit: Taylor (gtmcknight), amm-sim@" + CREDIT_COMMIT]
        for fname, data in self.payloads.items():
            sha, size = sha256(data).hexdigest(), len(data)
            sha, size = overrides.get(fname, (sha, size))
            url = (urls or {}).get(fname, (self.dir / "src" / fname).as_uri())
            kind = "python" if fname == "alpha.bin" else "wheel"
            lines.append("\t".join(["linux-x86_64:replica", kind, sha, str(size), fname, url]))
        path = self.dir / "manifest.tsv"
        path.write_text("\n".join(lines) + "\n")
        return path

    def fetch(self, manifest, *extra, **kw):
        return run_setup("--platform", "linux-x86_64", "--replica-only", "--fetch-only", "--manifest", manifest,
                         "--cache", self.cache, "--scratch", self.dir / "scratch", *extra, **kw)

    def cached_files(self):
        return sorted(p.name for p in (self.cache / "sha256").glob("*/*")) if (self.cache / "sha256").exists() else []

    def leftovers(self):
        return list((self.cache / "tmp").glob("*")) if (self.cache / "tmp").exists() else []

    def test_hash_mismatch_is_fatal_and_caches_nothing(self):
        data = self.payloads["beta.whl"]
        p = self.fetch(self.manifest({"beta.whl": ("0" * 64, len(data))}))
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertIn("sha256 mismatch for beta.whl", p.stderr)
        self.assertIn(sha256(data).hexdigest(), p.stderr)
        self.assertEqual(self.cached_files(), ["alpha.bin"], "only the verified file may be cached")
        self.assertEqual(self.leftovers(), [], "no partial file may survive")
        self.assertNotIn("setup_fast: OK", p.stdout)

    def test_size_bound_is_enforced(self):
        data = self.payloads["alpha.bin"]
        for wrong in (len(data) - 1, len(data) + 1):
            shutil.rmtree(str(self.cache), ignore_errors=True)
            p = self.fetch(self.manifest({"alpha.bin": (sha256(data).hexdigest(), wrong)}))
            self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
            self.assertIn("size mismatch for alpha.bin", p.stderr)
            self.assertEqual(self.cached_files(), [])
            self.assertEqual(self.leftovers(), [])

    def test_missing_cache_offline_is_diagnosed(self):
        m = self.manifest()
        p = self.fetch(m, "--offline", no_network=True)
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertFalse(p.curl_called, "--offline must never start a transfer")
        self.assertIn("2 of 2 pinned files are not in the cache", p.stderr)
        for fname, data in self.payloads.items():
            self.assertIn("missing: " + fname, p.stderr)
            self.assertIn("sha256=" + sha256(data).hexdigest(), p.stderr)
            self.assertIn((self.dir / "src" / fname).as_uri(), p.stderr)
        self.assertIn("run once without --offline", p.stderr)
        self.assertEqual(self.cached_files(), [])

    def test_cached_offline_rerun(self):
        m = self.manifest()
        p = self.fetch(m)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        f = summary_fields(p.stdout)
        self.assertEqual((f["setup"], f["downloads"], f["cache_hits"]), ("cold", "2", "0"))
        self.assertEqual(int(f["download_bytes"]), sum(len(d) for d in self.payloads.values()))
        self.assertEqual(self.cached_files(), ["alpha.bin", "beta.whl"])
        for fname, data in self.payloads.items():
            cached = self.cache / "sha256" / sha256(data).hexdigest() / fname
            self.assertEqual(cached.read_bytes(), data)
        shutil.rmtree(str(self.dir / "src"))  # the sources are gone: only the cache can satisfy a rerun
        p = self.fetch(m, "--offline", no_network=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(p.curl_called)
        f = summary_fields(p.stdout)
        self.assertEqual((f["setup"], f["downloads"], f["cache_hits"]), ("cached", "0", "2"))
        self.assertTrue((self.dir / "scratch" / ".setup_fast" / "timings.log").read_text().count("OK ") == 2)

    def test_corrupt_cache_entry_is_caught_then_repaired(self):
        m = self.manifest()
        self.assertEqual(self.fetch(m).returncode, 0)
        data = self.payloads["beta.whl"]
        cached = self.cache / "sha256" / sha256(data).hexdigest() / "beta.whl"
        cached.write_bytes(b"tampered")
        p = self.fetch(m, "--offline", no_network=True)
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertIn("cache entry is corrupt", p.stderr)
        self.assertFalse(p.curl_called)
        p = self.fetch(m)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        f = summary_fields(p.stdout)
        self.assertEqual((f["downloads"], f["cache_hits"]), ("1", "1"))
        self.assertEqual(cached.read_bytes(), data)

    def test_failed_transfer_is_reported(self):
        p = self.fetch(self.manifest(urls={"beta.whl": (self.dir / "src" / "absent.whl").as_uri()}))
        self.assertEqual(p.returncode, 6, p.stdout + p.stderr)
        self.assertIn("download failed after 3 attempts", p.stderr)
        self.assertEqual(self.leftovers(), [])

    def test_deadline_stops_cleanly_with_resume_hint(self):
        p = self.fetch(self.manifest(), "--deadline", "0")
        self.assertEqual(p.returncode, 75, p.stdout + p.stderr)
        self.assertIn("resume: rerun the same command", p.stdout)
        self.assertEqual(self.cached_files(), [])

    def test_non_https_url_refused(self):
        p = self.fetch(self.manifest(urls={"alpha.bin": "http://example.invalid/alpha.bin"}))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("only https://", p.stderr)


def in_env(scratch, code):
    """Run python code through the generated env.sh, the way next-wave items do."""
    cmd = '. "$1/env.sh" && exec "$IMD_AMM_PYTHON" -I -c "$2"'
    return subprocess.run(["bash", "-c", cmd, "bash", str(scratch), code], cwd=str(ROOT),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=300)


@unittest.skipUnless(IS_LINUX_X86, "real setup needs a Linux x86_64 host (this is %s %s)" % HOST)
class LinuxIntegration(unittest.TestCase):
    scratch = TDIR / "it"
    first = None

    @classmethod
    def setUpClass(cls):
        shutil.rmtree(str(cls.scratch), ignore_errors=True)
        cls.first = run_setup("--platform", "linux-x86_64", "--cache", CACHE, "--scratch", cls.scratch)
        if cls.first.returncode in (5, 6, 75):
            raise unittest.SkipTest("cache not populated and network unavailable: " + cls.first.stderr[-400:])

    def test_1_setup_and_real_imports(self):
        self.assertEqual(self.first.returncode, 0, self.first.stdout + self.first.stderr)
        f = summary_fields(self.first.stdout)
        self.assertEqual(f["install"], "fresh")
        self.assertIn(f["setup"], ("cold", "cached"))
        self.assertEqual(int(f["downloads"]) + int(f["cache_hits"]), 16)
        p = pins()
        code = ("import sys, os, numpy, numba, llvmlite, amm_sim_rs, pyrevm, solcx, amm_competition;"
                "print(sys.prefix); print('%d.%d.%d' % sys.version_info[:3], numpy.__version__, numba.__version__,"
                " llvmlite.__version__); print(os.path.dirname(os.path.dirname(amm_competition.__file__)));"
                "print(os.environ['IMD_AMM_ENGINE'], os.environ['SOLCX_BINARY_PATH'], os.environ['NUMBA_CACHE_DIR'])")
        r = in_env(self.scratch, code)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout.splitlines()
        self.assertEqual(out[0], str(self.scratch / "venv"))
        self.assertEqual(out[1].split(), [p["IMD_PY_VERSION"], p["IMD_NUMPY_VERSION"], p["IMD_NUMBA_VERSION"],
                                          p["IMD_LLVMLITE_VERSION"]])
        self.assertEqual(out[2], str(self.scratch / "engine" / "amm-challenge"))
        for path in out[3].split():
            self.assertTrue(path.startswith(str(self.scratch) + "/"), "state must stay under scratch: " + path)
        self.assertTrue((self.scratch / "engine" / "harness.py").is_file())
        self.assertFalse((self.scratch / "venv" / "bin" / "pip").exists(), "the venv carries no installer")

    def test_2_offline_rerun_is_cached_and_reused(self):
        p = run_setup("--platform", "linux-x86_64", "--cache", CACHE, "--scratch", self.scratch, "--offline",
                      no_network=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(p.curl_called)
        f = summary_fields(p.stdout)
        self.assertEqual((f["setup"], f["downloads"], f["cache_hits"], f["install"]), ("cached", "0", "16", "reused"))
        self.assertIn('"ok": true', p.stdout)

    def test_3_offline_forced_reinstall_and_bounded_smokes(self):
        p = run_setup("--platform", "linux-x86_64", "--cache", CACHE, "--scratch", self.scratch, "--offline",
                      "--force", "--smoke", no_network=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(p.curl_called)
        f = summary_fields(p.stdout)
        self.assertEqual((f["setup"], f["install"]), ("cached", "fresh"))
        import json
        rep = json.loads([l for l in p.stdout.splitlines() if l.startswith("smoke_replica: ")][0].split(": ", 1)[1])
        self.assertTrue(rep["ok"] and rep["reproducible"])
        self.assertLessEqual(rep["runs"], 256)
        eng = json.loads([l for l in p.stdout.splitlines() if l.startswith("smoke_engine: ")][0].split(": ", 1)[1])
        self.assertTrue(eng["ok"])
        self.assertEqual((eng["seed"], eng["n_sims"]), (SMOKE_SEED, 1))
        self.assertEqual(sum(eng["submission_errors"].values()), 0)
        self.assertGreater(eng["afterSwap_peak_gas"], 0)

    def test_4_engine_smoke_refuses_any_other_seed(self):
        cmd = '. "$1/env.sh" && exec "$IMD_AMM_PYTHON" -I "$2" --seed "$3"'
        for seed in ("9700001", "0", "9699999"):
            r = subprocess.run(["bash", "-c", cmd, "bash", str(self.scratch),
                                str(ROOT / "bootstrap" / "smoke_engine.py"), seed], cwd=str(ROOT),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("REFUSED", r.stderr)
            self.assertEqual(r.stdout, "")

    def test_5_replica_only_has_no_engine(self):
        scratch = TDIR / "it_replica"
        shutil.rmtree(str(scratch), ignore_errors=True)
        p = run_setup("--platform", "linux-x86_64", "--replica-only", "--cache", CACHE, "--scratch", scratch,
                      "--offline", no_network=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        f = summary_fields(p.stdout)
        self.assertEqual((f["mode"], f["cache_hits"], f["install"]), ("replica", "4", "fresh"))
        env = (scratch / "env.sh").read_text()
        self.assertNotIn("IMD_AMM_ENGINE", env)
        self.assertFalse((scratch / "engine").exists())
        r = in_env(scratch, "import numba, numpy; print(numba.njit(lambda x: x + 1)(41))")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "42"), r.stderr)
        r = in_env(scratch, "import amm_sim_rs")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ModuleNotFoundError", r.stderr)


@unittest.skipUnless(IS_MAC_ARM, "macOS execution needs a Darwin arm64 host (this is %s %s)" % HOST)
class MacIntegration(unittest.TestCase):
    """Real replica-only setup on macOS arm64. Written on Linux and never run there: until someone
    runs it on a Mac, macOS execution is unproven."""

    def test_replica_only_setup_and_numba(self):
        scratch = TDIR / "it_mac"
        shutil.rmtree(str(scratch), ignore_errors=True)
        p = run_setup("--platform", "macos-arm64", "--replica-only", "--cache", CACHE, "--scratch", scratch,
                      "--smoke")
        if p.returncode in (5, 6, 75):
            self.skipTest("cache not populated and network unavailable: " + p.stderr[-300:])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(summary_fields(p.stdout)["install"], "fresh")
        r = in_env(scratch, "import numba, numpy; print(numba.njit(lambda x: x + 1)(41))")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "42"), r.stderr)


class MacArtifacts(unittest.TestCase):
    """Availability and integrity of the macOS arm64 set. Nothing here executes macOS code."""

    def test_macos_files_download_verify_and_are_arm64(self):
        p = run_setup("--platform", "macos-arm64", "--replica-only", "--fetch-only", "--cache", CACHE,
                      "--scratch", TDIR / "mac_fetch" / "scratch")
        if p.returncode in (5, 6, 75):
            self.skipTest("macOS files not cached and network unavailable: " + p.stderr[-300:])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        f = summary_fields(p.stdout)
        self.assertEqual(int(f["downloads"]) + int(f["cache_hits"]), 4)
        self.assertEqual(f["install"], "not-requested")
        for mset, kind, sha, size, name, _url in manifest_rows():
            if mset != "macos-arm64:replica":
                continue
            path = CACHE / "sha256" / sha / name
            self.assertEqual(path.stat().st_size, int(size))
            if kind == "python":
                with tarfile.open(str(path)) as tf:
                    head = tf.extractfile("python/bin/python3.12").read(8)
                magic, cputype = struct.unpack("<II", head)
                self.assertEqual(magic, 0xFEEDFACF, "64-bit Mach-O")
                self.assertEqual(cputype, 0x0100000C, "CPU_TYPE_ARM64")
            else:
                with zipfile.ZipFile(str(path)) as zf:
                    wheel = [n for n in zf.namelist() if n.endswith(".dist-info/WHEEL")][0]
                    tags = [l for l in zf.read(wheel).decode().splitlines() if l.startswith("Tag: ")]
                self.assertTrue(tags and all(re.match(r"Tag: cp312-cp312-macosx_1[12]_0_arm64$", t) for t in tags), tags)


if __name__ == "__main__":
    unittest.main(verbosity=2)
