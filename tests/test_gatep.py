# Credit: Taylor (gtmcknight), solved-policy research,
# amm-sim@4812a6382311624e1506aca131ca35c378f930d9.
"""Offline tests; all temporary state and actual subprocess caches use scratch/."""
import concurrent.futures
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
import gatep
from reference import materialize as M


class FoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT / 'scratch').mkdir(exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(prefix='test-gatep-', dir=ROOT / 'scratch')
        cls.root = Path(cls.tmp.name)
        cls.ref = cls.root / 'reference'
        M.materialize(cls.ref)
        cls.cache = cls.root / 'baseline-cache'
        cls.stdout, cls.hit, cls.record_path = gatep.run_gate(cls.ref / 'taylor', cls.cache)
        cls.record = json.loads(cls.record_path.read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.case_tmp = tempfile.TemporaryDirectory(prefix='case-', dir=self.root)
        self.case = Path(self.case_tmp.name)
        self.addCleanup(self.case_tmp.cleanup)

    def copied_cache(self):
        return Path(shutil.copytree(self.cache, self.case / 'cache'))

    def copied_taylor(self):
        return Path(shutil.copytree(self.ref / 'taylor', self.case / 'taylor'))

    def fake_success(self, *args, **kwargs):
        argv = args[0]
        self.assertEqual(argv[:3], [sys.executable, '-I', '-B'])
        stage = Path(argv[3]).parent
        self.assertNotEqual(stage, self.ref / 'taylor')
        for name, data in gatep.verify_inputs(self.ref / 'taylor')[2].items():
            self.assertEqual((stage / name).read_bytes(), data)
        return subprocess.CompletedProcess(argv, 0, self.stdout, '')

    def test_real_gate_and_verified_cache_hit(self):
        self.assertFalse(self.hit)
        self.assertEqual(len(self.stdout.splitlines()), 4)
        with mock.patch.object(gatep.subprocess, 'run', side_effect=AssertionError('executed twice')):
            stdout, hit, _ = gatep.run_gate(self.ref / 'taylor', self.cache)
        self.assertTrue(hit)
        self.assertEqual(stdout, self.stdout)

    def test_required_hashes_and_all_originals(self):
        original = M.originals()
        self.assertEqual(M.sha256(original['numba/source_bundle.json']),
                         'a5d32e4c06dc93881821d568d8d89ca50adfdba56d5dccd92afef6c6c2157952')
        self.assertEqual(M.sha256(original['qt1/Strategy.sol']),
                         'c68887cff61180e12ba2b1ceb333edf76a830f91f63b5d775a8806fb9ec08aca')
        self.assertEqual({x.split('/')[0] for x in original},
                         {'taylor', 'qt1', 'twocomp', 'combo', 'tools', 'numba'})
        for name, content in original.items():
            self.assertEqual((self.ref / name).read_bytes(), content)
        self.assertLess(sum(p.stat().st_size for p in M.ROOT.rglob('*') if p.is_file()), 8 * 1024**2)

    def test_idempotent_materialization(self):
        self.assertEqual(M.materialize(self.ref), 35)

    def test_materialization_refuses_overwrite_or_extra_files(self):
        out = self.case / 'out'
        (out / 'taylor').mkdir(parents=True)
        bad = out / 'taylor/gates.py'
        bad.write_bytes(b'changed')
        with self.assertRaises(M.IntegrityError):
            M.materialize(out)
        self.assertEqual(bad.read_bytes(), b'changed')
        bad.unlink()
        (out / 'extra.py').write_bytes(b'extra')
        with self.assertRaises(M.IntegrityError):
            M.materialize(out)

    def test_materialization_rejects_symlink_destination(self):
        link = self.case / 'link'
        link.symlink_to(self.ref, target_is_directory=True)
        with self.assertRaises(M.IntegrityError):
            M.materialize(link)

    def test_materialization_rejects_directory_in_place_of_file(self):
        out = self.case / 'out'
        (out / 'taylor/gates.py').mkdir(parents=True)
        with self.assertRaisesRegex(M.IntegrityError, 'directory'):
            M.materialize(out)

    def test_materialization_resumes_partial_extraction(self):
        out = self.case / 'out'
        (out / 'qt1').mkdir(parents=True)
        shutil.copyfile(self.ref / 'qt1/Strategy.sol', out / 'qt1/Strategy.sol')
        self.assertEqual(M.materialize(out), 35)
        gatep.verify_inputs(out / 'taylor')

    def test_inventory_tampering(self):
        (self.case / 'sources.json').write_bytes((M.ROOT / 'sources.json').read_bytes() + b' ')
        with mock.patch.object(M, 'ROOT', self.case), self.assertRaisesRegex(M.IntegrityError, 'sources.json'):
            M.originals()

    def test_envelope_tampering(self):
        manifest = M.inventory()
        shutil.copyfile(M.ROOT / 'sources.json', self.case / 'sources.json')
        e = manifest['files'][0]
        dest = self.case / e['envelope']
        dest.parent.mkdir(parents=True)
        dest.write_bytes((M.ROOT / e['envelope']).read_bytes() + b' ')
        with mock.patch.object(M, 'ROOT', self.case), self.assertRaisesRegex(M.IntegrityError, 'envelope SHA256'):
            M.originals()

    def test_unsafe_member_names(self):
        for name in ['/tmp/escape', '../escape', 'a/../b', 'a\\b', 'a//b', './a', '']:
            with self.subTest(name=name), self.assertRaises(M.IntegrityError):
                M.safe_path(name)

    def test_each_taylor_input_checked_even_on_cache_hit(self):
        ref = self.copied_taylor()
        for p in sorted(ref.iterdir()):
            original = p.read_bytes()
            p.write_bytes(original + b'\n')
            try:
                with self.subTest(file=p.name), self.assertRaisesRegex(M.IntegrityError, 'Taylor SHA256'):
                    gatep.run_gate(ref, self.cache)
            finally:
                p.write_bytes(original)

    def test_missing_and_extra_inputs(self):
        ref = self.copied_taylor()
        (ref / 'qm_k1e-5.json').unlink()
        with self.assertRaisesRegex(M.IntegrityError, 'file set mismatch'):
            gatep.run_gate(ref, self.cache)
        shutil.copyfile(self.ref / 'taylor/qm_k1e-5.json', ref / 'qm_k1e-5.json')
        (ref / 'evil.py').write_bytes(b'')
        with self.assertRaisesRegex(M.IntegrityError, 'file set mismatch'):
            gatep.run_gate(ref, self.cache)

    def test_input_symlink_and_bytecode_rejected(self):
        ref = self.copied_taylor()
        (ref / 'gates.py').unlink()
        (ref / 'gates.py').symlink_to(self.ref / 'taylor/gates.py')
        with self.assertRaises(M.IntegrityError):
            gatep.run_gate(ref, self.cache)
        (ref / 'gates.py').unlink()
        shutil.copyfile(self.ref / 'taylor/gates.py', ref / 'gates.py')
        (ref / '__pycache__').mkdir()
        with self.assertRaises(M.IntegrityError):
            gatep.run_gate(ref, self.cache)

    def test_cache_rejects_tampered_fields(self):
        cache = self.copied_cache()
        target = cache / self.record_path.name
        changes = {'identity': {}, 'returncode': 1, 'stderr': 'failure', 'status': 'timeout',
                   'elapsed_seconds': float('nan'), 'stdout_sha256': '0' * 64,
                   'stdout': self.stdout.replace('448.261189', '448.261188')}
        for key, value in changes.items():
            record = dict(self.record, **{key: value})
            target.write_text(json.dumps(record))
            with self.subTest(field=key), self.assertRaises(M.IntegrityError):
                gatep.run_gate(self.ref / 'taylor', cache)
        for content in ['{', '[]', 'null']:
            target.write_text(content)
            with self.subTest(content=content), self.assertRaises(M.IntegrityError):
                gatep.run_gate(self.ref / 'taylor', cache)

    def test_all_twelve_values_and_bad_output_shapes(self):
        for line in self.stdout.splitlines()[1:]:
            for value in line.split()[1:]:
                with self.subTest(value=value), self.assertRaises(M.IntegrityError):
                    gatep.validate_stdout(self.stdout.replace(value, '0.000000'), self.stdout)
        for output in ['', self.stdout + 'junk\n', self.stdout.replace('10001', '10000'),
                       self.stdout.replace('448.261189', 'nan'), self.stdout.rstrip(),
                       self.stdout.replace('448.261189', '4.48261189e2')]:
            with self.subTest(output=output), self.assertRaises(M.IntegrityError):
                gatep.validate_stdout(output, self.stdout)

    def test_python_version_separates_cache(self):
        cache = self.copied_cache()
        with mock.patch.object(gatep.sys, 'version', sys.version + ' test-version'), \
                mock.patch.object(gatep.subprocess, 'run', side_effect=self.fake_success) as run:
            _, hit, target = gatep.run_gate(self.ref / 'taylor', cache)
        self.assertFalse(hit)
        self.assertNotEqual(target.name, self.record_path.name)
        self.assertEqual(run.call_count, 1)

    def test_timeout_does_not_cache_and_retry_succeeds(self):
        cache = self.case / 'cache'
        with mock.patch.object(gatep.subprocess, 'run', side_effect=subprocess.TimeoutExpired('gates.py', 1)):
            with self.assertRaises(TimeoutError):
                gatep.run_gate(self.ref / 'taylor', cache)
        self.assertEqual(list(cache.glob('*.json')), [])
        self.assertEqual(list(cache.glob('gatep-*')), [])
        with mock.patch.object(gatep.subprocess, 'run', side_effect=self.fake_success):
            _, hit, _ = gatep.run_gate(self.ref / 'taylor', cache)
        self.assertFalse(hit)

    def test_real_subprocess_timeout(self):
        cache = self.case / 'timeout-cache'
        with self.assertRaises(TimeoutError):
            gatep.run_gate(self.ref / 'taylor', cache, timeout=0.5)
        self.assertEqual(list(cache.glob('*.json')), [])
        self.assertEqual(list(cache.glob('gatep-*')), [])

    def test_process_failures_never_cache(self):
        for i, (code, stdout, stderr) in enumerate([(1, self.stdout, ''), (0, self.stdout, 'error'),
                                                  (0, '', ''), (0, self.stdout + 'extra', '')]):
            cache = self.case / str(i)
            with mock.patch.object(gatep.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, stdout, stderr)):
                with self.assertRaises(M.IntegrityError):
                    gatep.run_gate(self.ref / 'taylor', cache)
            self.assertEqual(list(cache.glob('*.json')), [])

    def test_changed_during_execution_never_caches(self):
        ref = self.copied_taylor()
        cache = self.case / 'cache'
        def change(*args, **kwargs):
            (ref / 'pol_mid.json').write_bytes(b'changed')
            return subprocess.CompletedProcess([], 0, self.stdout, '')
        with mock.patch.object(gatep.subprocess, 'run', side_effect=change), self.assertRaises(M.IntegrityError):
            gatep.run_gate(ref, cache)
        self.assertEqual(list(cache.glob('*.json')), [])

    def test_cache_symlink_rejected(self):
        cache = self.case / 'cache'
        cache.mkdir()
        (cache / self.record_path.name).symlink_to(self.record_path)
        with self.assertRaises(M.IntegrityError):
            gatep.run_gate(self.ref / 'taylor', cache)

    def test_invalid_timeout(self):
        for value in [0, -1, 451, float('inf'), float('nan')]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                gatep.run_gate(self.ref / 'taylor', self.cache, value)

    def test_lock_timeout(self):
        lock = self.case / 'lock'
        with gatep.cache_lock(lock, time.monotonic() + 1):
            with self.assertRaises(TimeoutError):
                with gatep.cache_lock(lock, time.monotonic() + 0.03):
                    self.fail('acquired held lock')

    def test_concurrent_cli_runs_execute_once(self):
        cache = self.case / 'concurrent-cache'
        cmd = [sys.executable, '-I', '-B', str(ROOT / 'gatep.py'), '--reference',
               str(self.ref / 'taylor'), '--cache', str(cache)]
        def invoke(_):
            return subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(invoke, range(3)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, self.stdout)
        self.assertEqual(sum('executed gates.py' in r.stderr for r in results), 1)
        self.assertEqual(sum('verified cache hit' in r.stderr for r in results), 2)
        self.assertEqual(len(list(cache.glob('*.json'))), 1)
        self.assertEqual(list(cache.glob('gatep-*')), [])


if __name__ == '__main__':
    unittest.main()
