"""tests/test_setup_deadline.py - deadline, watchdog and resume tests for setup_fast.sh and
bootstrap/deadline.sh.

Credit: Taylor (gtmcknight), solved-policy research,
github.com/gtmcknight/amm-sim @ 4812a6382311624e1506aca131ca35c378f930d9 (research/solved).

Run from the repository root with any Python 3.8+ (standard library only, no network, no real
runtime; about half a minute):

    python3 tests/test_setup_deadline.py -v

Everything drives the real script through its --manifest hook with file:// payloads. The
"python archive" is a tarball whose python/bin/python3 is a shell script that answers the
installer's probes (version, venv, pip, verify_env, smoke) and can be told to stall exactly
once, spawning a grandchild that records in a scratch marker whether it was killed. So the
deadline, the owned-process-group kill, the atomic progress file and the resume are exercised
end to end with short deterministic delays. The platform is always the explicit test input
linux-x86_64; nothing here inspects the machine, and all state stays under scratch/.
"""
import os
import shutil
import subprocess
import sys
import tarfile
import time
import unittest
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup_fast.sh"
DEADLINE = ROOT / "bootstrap" / "deadline.sh"
SCRATCH = ROOT / "scratch"
TDIR = SCRATCH / "test_setup_deadline"
CREDIT_COMMIT = "4812a6382311624e1506aca131ca35c378f930d9"
PY_VERSION = "3.12.16"   # what bootstrap/pins.sh pins; the fake interpreter answers it

FAKE_PYTHON = r"""#!/bin/sh
# fake CPython for tests/test_setup_deadline.py: answers setup_fast.sh's probes, stalls once if told
STALL_ON='%(stall_on)s'; FLAG='%(flag)s'; MARK='%(marker)s'; SECS='%(secs)s'
stall() {
  if [ -n "$STALL_ON" ] && [ ! -e "$FLAG" ]; then
    : > "$FLAG"
    sh -c 'trap "echo killed > '"$MARK"'; exit 0" TERM; while :; do sleep 1; done' &
    sleep "$SECS"
    echo finished > "$MARK"
  fi
}
case "$1 $2" in
  "-I -c")
    case "$3" in
      *version_info*) [ "$STALL_ON" = probe ] && stall; echo '%(version)s';;
      *purelib*) echo "$(dirname "$0")/../site"; mkdir -p "$(dirname "$0")/../site";;
      *) echo "fake python: unexpected -c $3" >&2; exit 9;;
    esac;;
  "-I -m")
    case "$3" in
      venv) mkdir -p "$5/bin" && cp "$0" "$5/bin/python" && chmod 755 "$5/bin/python";;
      pip) [ "$STALL_ON" = pip ] && stall; exit 0;;
      *) echo "fake python: unexpected -m $3" >&2; exit 9;;
    esac;;
  *)
    case "$2" in
      *verify_env.py) [ "$STALL_ON" = verify ] && stall; echo 'verify_env: {"fake": true, "ok": true}';;
      *smoke_replica.py) echo 'smoke_replica: {"fake": true, "ok": true}';;
      *) echo "fake python: unexpected $*" >&2; exit 9;;
    esac;;
esac
"""


def sh_quote_free(path):
    assert "'" not in str(path) and "%" not in str(path), path
    return str(path)


class Harness(unittest.TestCase):
    """Per-test scratch dir, fake artifacts, manifest and the installer invocation."""

    def setUp(self):
        self.name = self.id().split(".")[-1]
        self.dir = TDIR / self.name
        shutil.rmtree(str(self.dir), ignore_errors=True)
        (self.dir / "src").mkdir(parents=True)
        self.cache = self.dir / "cache"
        self.scratch = self.dir / "scratch"
        self.marker = self.dir / "grandchild.marker"
        self.flag = self.dir / "stalled.once"
        self.fakebin = self.dir / "fakebin"
        self.fakebin.mkdir()

    def fake_python_archive(self, stall_on="", secs=30):
        script = FAKE_PYTHON % {"stall_on": stall_on, "flag": sh_quote_free(self.flag),
                                "marker": sh_quote_free(self.marker), "secs": secs, "version": PY_VERSION}
        tree = self.dir / "tree" / "python" / "bin"
        tree.mkdir(parents=True)
        (tree / "python3").write_text(script)
        (tree / "python3").chmod(0o755)
        path = self.dir / "src" / "cpython-fake.tar.gz"
        with tarfile.open(str(path), "w:gz") as tf:
            tf.add(str(self.dir / "tree" / "python"), arcname="python")
        return path

    def manifest(self, stall_on="", secs=30):
        py = self.fake_python_archive(stall_on, secs)
        whl = self.dir / "src" / "fake-1.0-py3-none-any.whl"
        whl.write_bytes(b"not really a wheel " * 100)
        lines = ["# test manifest. Credit: Taylor (gtmcknight), amm-sim@" + CREDIT_COMMIT]
        for kind, p in (("python", py), ("wheel", whl)):
            data = p.read_bytes()
            lines.append("\t".join(["linux-x86_64:replica", kind, sha256(data).hexdigest(), str(len(data)),
                                    p.name, p.as_uri()]))
        m = self.dir / "manifest.tsv"
        m.write_text("\n".join(lines) + "\n")
        return m

    def fake_tool(self, name, body):
        """Put a fake `name` first on PATH for the next run; body is sh source."""
        p = self.fakebin / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755)

    def run_setup(self, manifest, *extra, timeout=60, popen=False):
        env = dict(os.environ)
        env["PATH"] = str(self.fakebin) + os.pathsep + env.get("PATH", "")
        cmd = ["bash", str(SETUP), "--platform", "linux-x86_64", "--replica-only", "--manifest", str(manifest),
               "--cache", str(self.cache), "--scratch", str(self.scratch)] + [str(a) for a in extra]
        if popen:
            return subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    universal_newlines=True)
        t0 = time.time()
        p = subprocess.run(cmd, cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=timeout)
        p.wall = time.time() - t0
        return p

    def wait_marker(self, seconds=4.0):
        end = time.time() + seconds
        while time.time() < end:
            if self.marker.exists():
                return self.marker.read_text().strip()
            time.sleep(0.1)
        return "none"

    def ok_lines(self):
        log = self.scratch / ".setup_fast" / "timings.log"
        return log.read_text().count(" OK ") if log.exists() else 0

    def assert_expired(self, p, during):
        self.assertEqual(p.returncode, 75, p.stdout + p.stderr)
        self.assertIn("deadline of", p.stdout)
        self.assertIn("during: " + during, p.stdout)
        self.assertIn("resume: rerun the same command", p.stdout)
        self.assertNotIn("setup_fast: OK", p.stdout, "no success may be reported after expiry")
        self.assertFalse((self.scratch / ".setup_fast" / "installed.key").exists(), "no stamp after expiry")
        self.assertEqual(list((self.scratch / ".setup_fast").glob("deadline.expired.*")), [], "flag file cleaned")

    def summary(self, p):
        lines = [l for l in p.stdout.splitlines() if l.startswith("setup_fast: OK ")]
        self.assertTrue(lines, "no summary in:\n" + p.stdout + p.stderr)
        return dict(kv.split("=", 1) for kv in lines[-1].split()[2:])


class InstallerDeadline(Harness):
    def test_zero_deadline_on_warm_cache_offline_and_fetch_only(self):
        m = self.manifest()
        self.assertEqual(self.run_setup(m, "--fetch-only").returncode, 0)
        self.assertEqual(self.ok_lines(), 1)
        for extra in (["--offline"], ["--offline", "--fetch-only"], ["--fetch-only"]):
            p = self.run_setup(m, "--deadline", "0", *extra)
            self.assert_expired(p, "start")
            self.assertLess(p.wall, 5.0)
        self.assertEqual(self.ok_lines(), 1, "expired runs log no success")
        self.assertFalse((self.scratch / "env.sh").exists())

    def test_delayed_cache_hashing_is_bounded_and_expires(self):
        m = self.manifest()
        self.assertEqual(self.run_setup(m, "--fetch-only").returncode, 0)
        # a sha256sum that takes 3 s per file: the warm offline path now takes longer than --deadline 1
        self.fake_tool("sha256sum", 'sleep 3\nexec "%s" -c \'import hashlib, sys\n'
                       'd = open(sys.argv[1], "rb").read() if len(sys.argv) > 1 else sys.stdin.buffer.read()\n'
                       'print(hashlib.sha256(d).hexdigest() + "  " + (sys.argv[1] if len(sys.argv) > 1 else "-"))\' "$@"\n'
                       % sys.executable)
        p = self.run_setup(m, "--offline", "--fetch-only", "--deadline", "1")
        self.assert_expired(p, "cache check of cpython-fake.tar.gz")
        self.assertLess(p.wall, 2.8, "the slow hash must be killed at the deadline, not waited for")
        p = self.run_setup(m, "--offline", "--deadline", "1")
        self.assert_expired(p, "cache check of cpython-fake.tar.gz")
        self.assertEqual(self.ok_lines(), 1)

    def test_fetch_deadline_keeps_verified_files_and_resumes(self):
        m = self.manifest()
        # a curl that takes 0.7 s per file: the first fits in --deadline 1, the second is cut off
        self.fake_tool("curl", 'out=""; url=""\nwhile [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift 2;; '
                       'file://*) url=${1#file://}; shift;; *) shift;; esac; done\nsleep 0.7\ncp "$url" "$out"\n')
        p = self.run_setup(m, "--fetch-only", "--deadline", "1")
        self.assert_expired(p, "download of fake-1.0-py3-none-any.whl")
        cached = sorted(x.name for x in (self.cache / "sha256").glob("*/*"))
        self.assertEqual(cached, ["cpython-fake.tar.gz"], "the verified first file stays, the cut one is absent")
        self.assertEqual(list((self.cache / "tmp").glob("*")), [], "no partial download survives")
        p = self.run_setup(m, "--fetch-only", "--deadline", "60")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        f = self.summary(p)
        self.assertEqual((f["cache_hits"], f["downloads"]), ("1", "1"))

    def test_interrupted_install_kills_descendants_then_resumes(self):
        m = self.manifest(stall_on="probe", secs=30)
        p = self.run_setup(m, "--deadline", "2")
        self.assert_expired(p, "interpreter probe")
        self.assertLess(p.wall, 6.0, "the stalled interpreter must be killed, not waited for")
        self.assertEqual(self.wait_marker(), "killed", "the grandchild of the stalled probe must be killed too")
        self.assertFalse((self.scratch / "env.sh").exists())
        self.assertFalse((self.scratch / "venv").exists())
        self.assertTrue(list((self.scratch / "python").glob("*/.ok")), "the finished extraction is kept")
        p = self.run_setup(m, "--deadline", "60")        # the fake stalls only once: this run is fast
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        f = self.summary(p)
        self.assertEqual(f["install"], "fresh")
        self.assertTrue((self.scratch / "env.sh").exists())
        self.assertTrue((self.scratch / ".setup_fast" / "installed.key").exists())
        self.assertEqual(self.ok_lines(), 1)

    def test_interrupted_pip_then_resume_redoes_venv(self):
        m = self.manifest(stall_on="pip", secs=30)
        p = self.run_setup(m, "--deadline", "2")
        self.assert_expired(p, "wheel installation")
        self.assertEqual(self.wait_marker(), "killed")
        progress = self.scratch / ".setup_fast" / "progress"
        self.assertFalse(progress.exists() and "wheels" in progress.read_text(), "an unfinished step is never marked")
        p = self.run_setup(m, "--deadline", "60")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.summary(p)["install"], "fresh")
        self.assertIn(" wheels", progress.read_text())

    def test_interrupted_verify_then_resume_skips_finished_steps(self):
        m = self.manifest(stall_on="verify", secs=30)
        p = self.run_setup(m, "--deadline", "2")
        self.assert_expired(p, "verification")
        self.assertEqual(self.wait_marker(), "killed")
        progress = self.scratch / ".setup_fast" / "progress"
        self.assertTrue(progress.exists() and progress.read_text().strip().endswith(" wheels"), "atomic progress")
        p = self.run_setup(m, "--deadline", "60")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("resume: venv and wheels already installed", p.stdout)
        self.assertEqual(self.summary(p)["install"], "resumed")
        p = self.run_setup(m, "--deadline", "60")
        self.assertEqual(self.summary(p)["install"], "reused")

    def test_unexpired_success_and_replica_env_clears_engine_settings(self):
        m = self.manifest()
        p = self.run_setup(m, "--deadline", "60")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        f = self.summary(p)
        self.assertEqual((f["install"], f["deadline_s"]), ("fresh", "60"))
        env = (self.scratch / "env.sh").read_text()
        self.assertIn("unset IMD_AMM_ENGINE IMD_AMM_HARNESS SOLCX_BINARY_PATH", env)
        self.assertNotIn("export IMD_AMM_ENGINE", env)
        # D2: fabricated engine settings, as a previously sourced engine env.sh would leave them,
        # must be gone after sourcing the replica env.sh. Only these names are printed.
        code = ('export IMD_AMM_ENGINE=/fabricated/engine IMD_AMM_HARNESS=/fabricated/harness.py '
                'SOLCX_BINARY_PATH=/fabricated/solcx IMD_AMM_MODE=engine; . "$1/env.sh"; '
                'printf "%s|%s|%s|%s" "${IMD_AMM_ENGINE:-}" "${IMD_AMM_HARNESS:-}" "${SOLCX_BINARY_PATH:-}" "$IMD_AMM_MODE"')
        r = subprocess.run(["bash", "-c", code, "bash", str(self.scratch)], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, "|||replica"), r.stderr)

    def test_sigterm_to_installer_kills_owned_child_group(self):
        m = self.manifest(stall_on="probe", secs=30)
        proc = self.run_setup(m, "--deadline", "60", popen=True)
        deadline = time.time() + 10
        while time.time() < deadline and not self.flag.exists():   # the fake is now stalling
            time.sleep(0.1)
        time.sleep(0.3)
        proc.terminate()
        t0 = time.time()
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 130, out + err)
        self.assertLess(time.time() - t0, 6.0)
        self.assertIn("interrupted; rerun the same command", err)
        self.assertEqual(self.wait_marker(), "killed")
        self.assertNotIn("setup_fast: OK", out)


class DeadlineCli(unittest.TestCase):
    def setUp(self):
        self.dir = TDIR / self.id().split(".")[-1]
        shutil.rmtree(str(self.dir), ignore_errors=True)
        self.dir.mkdir(parents=True)
        self.marker = self.dir / "marker"

    def run_dl(self, seconds, *cmd, timeout=30):
        t0 = time.time()
        p = subprocess.run(["bash", str(DEADLINE), "--flag-dir", str(self.dir), "--seconds", str(seconds), "--"]
                           + list(cmd), cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=timeout)
        p.wall = time.time() - t0
        return p

    def test_expiry_kills_the_whole_owned_group_and_exits_75(self):
        inner = 'trap "echo killed > %s; exit 0" TERM; while :; do sleep 1; done' % self.marker
        p = self.run_dl(1, "sh", "-c", "sh -c '%s' & sleep 30" % inner)
        self.assertEqual(p.returncode, 75, p.stdout + p.stderr)
        self.assertIn("budget of 1s used up", p.stderr)
        self.assertLess(p.wall, 4.0, "the grace period must not be waited out after the group died")
        end = time.time() + 3
        while time.time() < end and not self.marker.exists():
            time.sleep(0.1)
        self.assertEqual(self.marker.read_text().strip(), "killed")
        self.assertEqual(list(self.dir.glob("deadline.expired.*")), [], "flag file removed on exit")

    def test_exit_status_and_output_pass_through(self):
        p = self.run_dl(5, "sh", "-c", "echo hello; exit 3")
        self.assertEqual((p.returncode, p.stdout), (3, "hello\n"), p.stderr)
        p = self.run_dl(5, "true")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertLess(p.wall, 3.0, "a finished child must not wait for the watchdog's sleep")

    def test_zero_budget_runs_nothing(self):
        p = self.run_dl(0, "sh", "-c", "echo ran > %s" % self.marker)
        self.assertEqual(p.returncode, 75, p.stdout + p.stderr)
        self.assertFalse(self.marker.exists())

    def test_usage_errors(self):
        for args in (["--", "true"], ["--seconds", "x", "--", "true"], ["--seconds", "5"]):
            p = subprocess.run(["bash", str(DEADLINE)] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, timeout=30)
            self.assertEqual(p.returncode, 2, args)

    def test_library_uses_no_process_discovery_or_gnu_timeout(self):
        lines = DEADLINE.read_text().splitlines()
        code = "\n".join(l for l in lines if not l.lstrip().startswith("#"))
        for banned in ("pgrep", "pkill", "ps ", "setsid", "timeout ", "uname", "/proc"):
            self.assertNotIn(banned, code, banned)
        self.assertIn("gtmcknight", "\n".join(lines[:12]))
        setup = "\n".join(l for l in SETUP.read_text().splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("uname", setup, "the installer never inspects the machine")


if __name__ == "__main__":
    unittest.main(verbosity=2)
