# Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9,
# research/solved, solved-policy research.
"""Adversarial fixture tests for validate_kit.py (stdlib unittest, offline).

Fixture trees are written only under <repo>/scratch/kit-validator/. The replica
banks, ammfast stub and replica validation.json in fixtures are SYNTHETIC: they
exercise the validator's logic and are not evidence of anything about ammfast.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / 'scratch' / 'kit-validator'
spec = importlib.util.spec_from_file_location('validate_kit', ROOT / 'validate_kit.py')
vk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vk)

CREDIT = 'Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9 research/solved'
HEADER = '# Credit: ' + CREDIT + '\n'
COPY = ['reference', 'baselines/engine/qt1-9700000-9700511.json', 'baselines/engine/qt1-audit.json',
        'baselines/engine/validation.json', 'baselines/engine/validate.py',
        'baselines/ha/HA-10000-12047.json', 'bootstrap/manifest.tsv', 'bootstrap/pins.sh',
        'bootstrap/smoke_engine.py', 'engine_eval.py', 'score.py', 'setup_fast.sh', 'gatep.py']
AMMFAST = HEADER + '''"""Synthetic test stub; never imported by the validator."""
POLICIES = ("qt1-ref", "TwoComp", "COMBO")


def run(seed, policy, head_params=None):
    raise NotImplementedError


def run_batch(seeds, policy, head_params=None):
    return [run(s, policy, head_params) for s in seeds]
'''


def synthetic_edge(policy, seed):
    return 400.0 + (seed * 7919 % 100003) / 997.0 + vk.POLICIES.index(policy) * 1.5


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Fixture:
    def __init__(self, complete=True):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix='fx-', dir=SCRATCH))
        for rel in COPY:
            src, dst = ROOT / rel, self.root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            (shutil.copytree if src.is_dir() else shutil.copyfile)(src, dst)
        if complete:
            self.build_replica()

    def path(self, rel):
        return self.root / rel

    def load(self, rel):
        return json.loads(self.path(rel).read_text())

    def save(self, rel, doc):
        self.path(rel).parent.mkdir(parents=True, exist_ok=True)
        self.path(rel).write_text(json.dumps(doc, indent=1))

    def build_replica(self):
        self.path('ammfast').mkdir(exist_ok=True)
        self.path('ammfast/__init__.py').write_text(AMMFAST)
        for policy in vk.POLICIES:
            for a, b in vk.WINDOWS:
                rows = [{'seed': s, 'edge': format(synthetic_edge(policy, s), '.6f')}
                        for s in range(a, b + 1)]
                self.save(vk.replica_bank_path(policy, a, b), {
                    '_credit': CREDIT, 'backend': 'replica', 'policy': policy,
                    'provenance': {'note': 'synthetic fixture', 'seed_range': [a, b]}, 'rows': rows})

        def rec(policy, seed):
            a = synthetic_edge(policy, seed)
            c = a + 2.5e-10
            return {'policy': policy, 'seed': seed, 'cpython_edge': c, 'ammfast_edge': a,
                    'abs_error': abs(c - a)}
        self.validation = {
            '_credit': CREDIT, 'complete': True, 'bindings': {},
            'gates384': [rec(p, s) for p in vk.POLICIES for s in vk.GATE_SEEDS],
            'spots576': [rec(p, s) for p in vk.POLICIES for a, _ in vk.WINDOWS for s in range(a, a + 64)],
            'timings': {'runs_per_second_per_core': {p: 10.0 for p in vk.POLICIES},
                        'cold_jit_seconds': 3.0}}
        self.rebind()

    def rebind(self, extra=None):
        doc = self.validation
        doc['bindings'] = {rel: sha(self.path(rel)) for rel in
                           list(vk.REPLICA_BANKS) + [vk.SOURCES, 'ammfast/__init__.py']
                           if self.path(rel).exists()}
        doc['bindings'].update(extra or {})
        self.save(vk.REPLICA_VALIDATION, doc)

    def run(self, parts=vk.PARTS):
        return vk.validate(self.root, parts)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def codes(result, part=None):
    return {f['code'] for f in result['findings'] if part is None or f['part'] == part}


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        self.fx.cleanup()

    def assertFails(self, part, code, parts=None):
        r = self.fx.run(parts or (part,))
        self.assertEqual(r['status'], 'FAIL', r['findings'])
        self.assertIn(code, codes(r, part), r['findings'])
        return r


class CompleteFixture(Base):
    def test_complete_fixture_passes_with_full_coverage(self):
        r = self.fx.run()
        self.assertEqual(r['status'], 'PASS', r['findings'])
        self.assertEqual(r['coverage']['expected_rows'], 14848)
        self.assertEqual(r['coverage']['verified_rows'], 14848)
        self.assertEqual(r['parts']['replica']['summary']['gates'], 384)
        self.assertEqual(r['parts']['replica']['summary']['spots'], 576)

    def test_output_is_deterministic(self):
        a = json.dumps(self.fx.run(), sort_keys=True)
        b = json.dumps(self.fx.run(), sort_keys=True)
        self.assertEqual(a, b)

    def test_tiny_rate_passes_no_speed_threshold(self):
        self.fx.validation['timings'] = {'runs_per_second_per_core': {p: 1e-6 for p in vk.POLICIES},
                                         'cold_jit_seconds': 0}
        self.fx.rebind()
        self.assertEqual(self.fx.run(('replica',))['status'], 'PASS')


class ReplicaBanks(Base):
    def edit_bank(self, fn, rel='baselines/replica/COMBO-60000-61023.json', rebind=True):
        doc = self.fx.load(rel)
        fn(doc)
        self.fx.save(rel, doc)
        if rebind:
            self.fx.rebind()

    def test_missing_bank(self):
        self.fx.path('baselines/replica/TwoComp-64000-65023.json').unlink()
        self.fx.rebind()
        r = self.assertFails('replica', 'missing-file')
        self.assertEqual(r['parts']['replica']['status'], 'fail')  # bindings omit it too

    def test_missing_row(self):
        self.edit_bank(lambda d: d['rows'].pop(17))
        self.assertFails('replica', 'missing-rows')

    def test_duplicate_row(self):
        self.edit_bank(lambda d: d['rows'].__setitem__(5, dict(d['rows'][4])))
        self.assertFails('replica', 'duplicate-seed')

    def test_unsorted_rows(self):
        def swap(d):
            d['rows'][3], d['rows'][4] = d['rows'][4], d['rows'][3]
        self.edit_bank(swap)
        self.assertFails('replica', 'unsorted-seeds')

    def test_lower_boundary_seed(self):
        self.edit_bank(lambda d: d['rows'].insert(0, {'seed': 59999, 'edge': '1.000000'}))
        self.assertFails('replica', 'seed-out-of-range')

    def test_upper_boundary_seed(self):
        self.edit_bank(lambda d: d['rows'].append({'seed': 61024, 'edge': '1.000000'}))
        self.assertFails('replica', 'seed-out-of-range')

    def test_bad_edge_formats(self):
        for bad in ('1.23', '1.2345678', 'nan', '1e3', ' 1.000000', 1.0):
            with self.subTest(edge=bad):
                self.edit_bank(lambda d: d['rows'][0].__setitem__('edge', bad))
                self.assertFails('replica', 'bad-edge')

    def test_bool_seed(self):
        self.edit_bank(lambda d: d['rows'][0].__setitem__('seed', True))
        self.assertFails('replica', 'bad-row')

    def test_wrong_policy_and_backend(self):
        self.edit_bank(lambda d: d.__setitem__('policy', 'TwoComp'))
        self.assertFails('replica', 'bad-identity')
        self.edit_bank(lambda d: (d.__setitem__('policy', 'COMBO'), d.__setitem__('backend', 'engine')))
        self.assertFails('replica', 'bad-identity')

    def test_credit_not_first_and_short_pin(self):
        rel = 'baselines/replica/qt1-ref-10000-12047.json'
        doc = self.fx.load(rel)
        moved = {'backend': doc.pop('backend'), **doc}
        self.fx.save(rel, moved)
        self.fx.rebind()
        self.assertFails('replica', 'credit-not-first')
        doc['_credit'] = 'Taylor (gtmcknight), amm-sim@4812a63'
        self.fx.save(rel, {'_credit': doc.pop('_credit'), 'backend': 'replica', **doc})
        self.fx.rebind()
        self.assertFails('replica', 'bad-credit')

    def test_duplicate_json_key(self):
        rel = 'baselines/replica/COMBO-10000-12047.json'
        text = self.fx.path(rel).read_text().replace('"backend": "replica"',
                                                     '"backend": "replica", "backend": "replica"', 1)
        self.fx.path(rel).write_text(text)
        self.fx.rebind()
        self.assertFails('replica', 'duplicate-key')

    def test_nonfinite_numbers(self):
        for token in ('NaN', 'Infinity', '-Infinity', '1e999'):
            with self.subTest(token=token):
                self.fx.validation['timings']['cold_jit_seconds'] = 0
                self.fx.rebind()
                text = self.fx.path(vk.REPLICA_VALIDATION).read_text().replace(
                    '"cold_jit_seconds": 0', '"cold_jit_seconds": ' + token)
                self.fx.path(vk.REPLICA_VALIDATION).write_text(text)
                self.assertFails('replica', 'nonfinite-number')


class ReplicaValidation(Base):
    def test_missing_validation(self):
        self.fx.path(vk.REPLICA_VALIDATION).unlink()
        r = self.assertFails('replica', 'missing-file')
        self.assertEqual(r['parts']['replica']['status'], 'missing')

    def test_stale_digest_after_bank_edit(self):
        rel = 'baselines/replica/qt1-ref-64000-65023.json'
        doc = self.fx.load(rel)
        doc['rows'][900]['edge'] = '1.000000'
        self.fx.save(rel, doc)
        self.assertFails('replica', 'stale-digest')

    def test_stale_ammfast_digest(self):
        self.fx.path('ammfast/__init__.py').write_text(AMMFAST + '\n# edited\n')
        self.assertFails('replica', 'stale-digest')

    def test_unbound_ammfast_file(self):
        self.fx.path('ammfast/kernel.py').write_text(HEADER + 'X = 1\n')
        self.assertFails('replica', 'bad-bindings')

    def test_bindings_must_cover_inventory(self):
        del self.fx.validation['bindings']
        self.fx.validation['bindings'] = {}
        doc = self.fx.validation
        doc['bindings'] = {r: sha(self.fx.path(r)) for r in vk.REPLICA_BANKS}
        doc['bindings']['ammfast/__init__.py'] = sha(self.fx.path('ammfast/__init__.py'))
        self.fx.save(vk.REPLICA_VALIDATION, doc)
        self.assertFails('replica', 'bad-bindings')

    def test_path_escapes(self):
        outside = SCRATCH / 'outside.txt'
        outside.write_text('x')
        link = self.fx.path('ammfast/link.py')
        link.symlink_to(outside)
        for bad in ('../outside.txt', '/etc/hostname', 'ammfast/../score.py', './score.py',
                    'ammfast\\x.py', 'ammfast/link.py', ''):
            with self.subTest(path=bad):
                self.fx.rebind({bad: '0' * 64})
                self.assertFails('replica', 'path-escape')
        link.unlink()

    def test_self_binding_rejected(self):
        self.fx.rebind({vk.REPLICA_VALIDATION: '0' * 64})
        self.assertFails('replica', 'self-binding')

    def test_bad_digest_format(self):
        self.fx.rebind({'score.py': 'ABC'})
        self.assertFails('replica', 'bad-bindings')

    def test_complete_false(self):
        self.fx.validation['complete'] = False
        self.fx.rebind()
        self.assertFails('replica', 'incomplete')

    def test_old_evidence_names_fail(self):
        for renamed in (('gates384',), ('spots576',), ('gates384', 'spots576')):
            with self.subTest(renamed=renamed):
                self.fx.build_replica()
                for name in renamed:
                    old_name = {'gates384': 'gates', 'spots576': 'spots'}[name]
                    self.fx.validation[old_name] = self.fx.validation.pop(name)
                self.fx.rebind()
                r = self.assertFails('replica', 'bad-validation')
                self.assertEqual(r['parts']['replica']['status'], 'fail')
                self.assertEqual(r['coverage']['verified_rows'], 0)
                self.assertIn(f'{renamed[0]} must be a list',
                              [finding['detail'] for finding in r['findings']])

    def set_gate(self, index, **values):
        self.fx.validation['gates384'][index].update(values)
        self.fx.rebind()

    def test_error_boundary(self):
        g = self.fx.validation['gates384'][0]
        exact = 2.0 ** -30  # 9.31e-10: representable, just under 1e-9
        self.set_gate(0, cpython_edge=g['ammfast_edge'] + exact,
                      abs_error=abs(g['ammfast_edge'] + exact - g['ammfast_edge']))
        self.assertEqual(self.fx.run(('replica',))['status'], 'PASS')
        over = g['ammfast_edge'] + 2e-9
        self.set_gate(0, cpython_edge=over, abs_error=abs(over - g['ammfast_edge']))
        self.assertFails('replica', 'parity-error')

    def test_reported_error_must_match_recomputed(self):
        self.set_gate(1, abs_error=0.0)
        self.assertFails('replica', 'bad-validation')

    def test_gate_missing_extra_and_duplicate(self):
        gates = self.fx.validation['gates384']
        removed = gates.pop()
        self.fx.rebind()
        self.assertFails('replica', 'bad-validation')
        e = synthetic_edge(removed['policy'], 10064)  # in-bank value, so only coverage is wrong
        gates.append(dict(removed, seed=10064, ammfast_edge=e, cpython_edge=e, abs_error=0.0))
        self.fx.rebind()
        self.assertFails('replica', 'bad-validation')
        gates[-1] = dict(gates[0])
        self.fx.rebind()
        self.assertFails('replica', 'bad-validation')

    def test_record_types(self):
        for field, bad in (('seed', '10000'), ('cpython_edge', '1.0'), ('ammfast_edge', True),
                           ('policy', 'HA')):
            with self.subTest(field=field):
                self.fx.build_replica()
                self.set_gate(2, **{field: bad})
                self.assertFails('replica', 'bad-validation')
        self.fx.build_replica()
        self.fx.validation['spots576'][0]['extra'] = 1
        self.fx.rebind()
        self.assertFails('replica', 'bad-validation')

    def test_spot_formatting_must_match_bank(self):
        s = self.fx.validation['spots576'][-1]
        new = s['ammfast_edge'] + 0.01
        s.update(ammfast_edge=new, cpython_edge=new, abs_error=0.0)
        self.fx.rebind()
        self.assertFails('replica', 'spot-format')

    def test_spot_window_coverage(self):
        spots = self.fx.validation['spots576']
        idx = next(i for i, s in enumerate(spots) if s['seed'] == 60063 and s['policy'] == 'TwoComp')
        a = synthetic_edge('TwoComp', 60064)
        spots[idx] = {'policy': 'TwoComp', 'seed': 60064, 'cpython_edge': a, 'ammfast_edge': a,
                      'abs_error': 0.0}
        self.fx.rebind()
        self.assertFails('replica', 'bad-validation')

    def test_timings(self):
        rates = {p: 10.0 for p in vk.POLICIES}
        cases = [{'runs_per_second_per_core': rates, 'cold_jit_seconds': -0.1},
                 {'runs_per_second_per_core': dict(rates, COMBO=0), 'cold_jit_seconds': 1},
                 {'runs_per_second_per_core': {'qt1-ref': 1, 'TwoComp': 1}, 'cold_jit_seconds': 1},
                 {'runs_per_second_per_core': dict(rates, COMBO=True), 'cold_jit_seconds': 1},
                 {'runs_per_second_per_core': dict(rates, COMBO='9'), 'cold_jit_seconds': 1},
                 {'runs_per_second_per_core': rates}]
        for t in cases:
            with self.subTest(timings=t):
                self.fx.validation['timings'] = t
                self.fx.rebind()
                self.assertFails('replica', 'bad-timings')


class Ammfast(Base):
    def test_missing_run_batch(self):
        self.fx.path('ammfast/__init__.py').write_text(AMMFAST.split('def run_batch')[0])
        self.assertFails('ammfast', 'bad-api')

    def test_wrong_signature(self):
        self.fx.path('ammfast/__init__.py').write_text(AMMFAST.replace('head_params=None)', 'head=None)', 1))
        self.assertFails('ammfast', 'bad-api')

    def test_reexport_accepted(self):
        self.fx.path('ammfast/api.py').write_text(AMMFAST)
        self.fx.path('ammfast/__init__.py').write_text(HEADER + 'from .api import run, run_batch\n')
        self.assertEqual(self.fx.run(('ammfast',))['status'], 'PASS')

    def test_header_credit_required(self):
        self.fx.path('ammfast/__init__.py').write_text(AMMFAST.replace(HEADER, '# no credit\n'))
        self.assertFails('ammfast', 'bad-credit')

    def test_syntax_error(self):
        self.fx.path('ammfast/__init__.py').write_text(AMMFAST + '\ndef broken(:\n')
        self.assertFails('ammfast', 'syntax-error')

    def test_kit_file_missing_or_uncredited(self):
        self.fx.path('gatep.py').unlink()
        self.assertFails('kit', 'missing-file')
        self.fx.path('gatep.py').write_text('print(1)\n')
        self.assertFails('kit', 'bad-credit')


class Engine(Base):
    BANK, AUDIT, VAL = vk.ENGINE_BANK, vk.ENGINE_AUDIT, vk.ENGINE_VALIDATION

    def edit(self, rel, fn):
        doc = self.fx.load(rel)
        fn(doc)
        self.fx.save(rel, doc)

    def test_engine_seed_outside_range(self):
        self.edit(self.BANK, lambda d: d['rows'][-1].__setitem__('seed', 9700512))
        self.assertFails('engine', 'seed-out-of-range')

    def test_gas_types(self):
        for bad in (-1, 1.5, True, '200000', None):
            with self.subTest(gas=bad):
                self.edit(self.BANK, lambda d: d['rows'][7].__setitem__('after_swap_peak_gas', bad))
                self.assertFails('engine', 'bad-gas')

    def test_audit_error_counter(self):
        self.edit(self.AUDIT, lambda d: d['audits'][3]['gas_stats'][0].__setitem__('revert', 1))
        self.assertFails('engine', 'engine-error')

    def test_audit_gas_mismatch(self):
        self.edit(self.AUDIT, lambda d: d['audits'][3]['gas_stats'][4].__setitem__('max_ok_gas', 1))
        self.assertFails('engine', 'gas-mismatch')

    def test_rounding_mismatch(self):
        self.edit(self.BANK, lambda d: d['rows'][0].__setitem__('edge', '505.956845'))
        self.assertFails('engine', 'rounding-mismatch')

    def test_qt1_digest_changed(self):
        self.edit(self.BANK, lambda d: d['provenance'].__setitem__('qt1_source_sha256', 'f' * 64))
        self.assertFails('engine', 'bad-provenance')

    def test_wheel_digest_changed(self):
        def fn(d):
            name = next(n for n in d['provenance']['downloads'] if n.startswith('amm_sim_rs-'))
            d['provenance']['downloads'][name]['sha256'] = 'a' * 64
        self.edit(self.BANK, fn)
        self.assertFails('engine', 'stale-digest')

    def test_config_digest_and_seed(self):
        self.edit(self.BANK, lambda d: d['provenance']['configs'][9].__setitem__('gbm_sigma', 0.5))
        self.assertFails('engine', 'stale-digest')
        self.edit(self.BANK, lambda d: d['provenance'].__setitem__('workers', 4))
        self.assertFails('engine', 'bad-provenance')

    def test_validation_digest_stale_after_bank_change(self):
        # Consistent bank+audit change still fails: validation.json bound the old bank bytes.
        self.edit(self.BANK, lambda d: d.__setitem__('simulation_seconds', 1.0))
        self.assertFails('engine', 'stale-digest')

    def test_engine_source_digest(self):
        with open(self.fx.path('engine_eval.py'), 'a') as fh:
            fh.write('\n# edited\n')
        self.assertFails('engine', 'stale-digest')

    def test_missing_audit(self):
        self.fx.path(self.AUDIT).unlink()
        r = self.assertFails('engine', 'missing-file')
        self.assertEqual(r['parts']['engine']['status'], 'missing')


class HAandReference(Base):
    def test_ha_edge_must_map_to_kind_ha(self):
        doc = self.fx.load(vk.HA_BANK)
        doc['rows'][10]['edge'] = format(float(doc['rows'][10]['edge']) + 1e-6, '.6f')
        self.fx.save(vk.HA_BANK, doc)
        self.assertFails('ha', 'ha-mapping')

    def test_ha_selection_kind(self):
        doc = self.fx.load(vk.HA_BANK)
        doc['provenance']['selection']['equals'] = 'T'
        self.fx.save(vk.HA_BANK, doc)
        self.assertFails('ha', 'bad-provenance')

    def test_ha_missing_row(self):
        doc = self.fx.load(vk.HA_BANK)
        doc['rows'].pop()
        self.fx.save(vk.HA_BANK, doc)
        self.assertFails('ha', 'missing-rows')

    def test_inventory_tamper(self):
        p = self.fx.path(vk.SOURCES)
        p.write_bytes(p.read_bytes() + b'\n')
        self.assertFails('reference', 'stale-digest')

    def test_envelope_tamper(self):
        p = self.fx.path('reference/envelopes/taylor__gates.py.json')
        p.write_bytes(p.read_bytes().replace(b'"base64+zlib"', b'"base64+zlib" '))
        self.assertFails('reference', 'stale-digest')


class RealTrunkAndCli(unittest.TestCase):
    def cli(self, *args):
        return subprocess.run([sys.executable, '-B', str(ROOT / 'validate_kit.py'), *args],
                              capture_output=True, text=True, cwd=ROOT, timeout=300)

    def test_real_trunk_reports_honestly(self):
        p = self.cli()
        report = json.loads(p.stdout)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(report['findings'], [])
        self.assertEqual({part: data['status'] for part, data in report['parts'].items()},
                         {part: 'pass' for part in
                          ('reference', 'kit', 'ammfast', 'engine', 'ha', 'replica')})
        coverage = report['coverage']
        self.assertEqual(coverage['expected_rows'], 14848)
        self.assertEqual(coverage['verified_rows'], 14848)
        for part, expected in (('replica', 12288), ('ha', 2048), ('engine', 512)):
            with self.subTest(part=part):
                banks = [bank for path, bank in coverage['banks'].items()
                         if path.startswith(f'baselines/{part}/')]
                self.assertTrue(banks)
                self.assertTrue(all(bank['part_status'] == 'pass' for bank in banks))
                self.assertEqual(sum(bank['expected'] for bank in banks), expected)
                self.assertEqual(sum(bank['rows_well_formed'] for bank in banks), expected)
        self.assertEqual(report['parts']['replica']['summary']['gates'], 384)
        self.assertEqual(report['parts']['replica']['summary']['spots'], 576)

    def test_existing_parts_pass_on_real_trunk(self):
        p = self.cli('--parts', 'reference,kit,engine,ha')
        self.assertEqual(p.returncode, 0, p.stdout[-2000:])
        self.assertEqual(json.loads(p.stdout)['coverage']['verified_rows'], 2560)

    def test_usage_errors(self):
        self.assertEqual(self.cli('--parts', 'nope').returncode, 2)
        self.assertEqual(self.cli('--parts', 'ha', '--out', 'docs/x.json').returncode != 0, True)
        self.assertFalse((ROOT / 'docs/x.json').exists())

    def test_out_under_scratch(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        p = self.cli('--parts', 'kit', '--out', 'scratch/kit-validator/cli-report.json')
        self.assertEqual(p.returncode, 0)
        self.assertEqual((SCRATCH / 'cli-report.json').read_text(), p.stdout)


if __name__ == '__main__':
    unittest.main()
