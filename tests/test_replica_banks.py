# Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9,
# research/solved, solved-policy research.
"""Replica bank tests: contract schema, validation evidence, checkpoint binding, and the real
score.py replica adapter driven with bank data.

Run from the repository root:
    python -B -m unittest discover -s tests -p test_replica_banks.py -v
The bank, validator and generator-checkpoint tests need only the standard library.  The live
scorer tests need the setup_fast.sh interpreter (numpy/numba) and are skipped without it."""
import importlib.util
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OUT = ROOT / 'baselines/replica'
COMMIT = '4812a6382311624e1506aca131ca35c378f930d9'
POLICIES = ('qt1-ref', 'TwoComp', 'COMBO')
RANGES = ((60000, 61023), (64000, 65023), (10000, 12047))


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    obj = importlib.util.module_from_spec(spec)
    sys.modules[name] = obj
    spec.loader.exec_module(obj)
    return obj


score = module('replica_score', ROOT / 'score.py')
validate = module('replica_validate', OUT / 'validate.py')
generate = module('replica_generate', OUT / 'generate.py')

try:
    import numba  # noqa: F401
    import numpy  # noqa: F401
    HAVE_NUMBA = True
except ImportError:
    HAVE_NUMBA = False
needs_numba = unittest.skipUnless(
    HAVE_NUMBA, 'numpy/numba missing: run setup_fast.sh --replica-only and source scratch/env.sh')


def bank_path(policy, start, end):
    return OUT / f'{policy}-{start}-{end}.json'


def paired(baseline, candidate):
    """Final cumulative summary of the real scorer, candidate rows taken from another bank."""
    rounds = []
    status = score.evaluate(score.load_dataset(baseline, bank=True),
                            score.RowCandidate(score.load_dataset(candidate, bank=True)), emit=rounds.append)
    return status, rounds


class BankTests(unittest.TestCase):
    def test_nine_banks_follow_the_contract(self):
        total = 0
        for policy in POLICIES:
            for start, end in RANGES:
                path = bank_path(policy, start, end)
                raw = json.loads(path.read_text())
                self.assertEqual(list(raw), ['_credit', 'backend', 'policy', 'provenance', 'rows'])
                for needle in ('Taylor', 'gtmcknight', COMMIT, 'research/solved'):
                    self.assertIn(needle, raw['_credit'])
                self.assertEqual((raw['backend'], raw['policy']), ('replica', policy))
                self.assertEqual([row['seed'] for row in raw['rows']], list(range(start, end + 1)))
                for row in raw['rows']:
                    self.assertEqual(list(row), ['seed', 'edge'])
                    self.assertRegex(row['edge'], r'^-?[0-9]+\.[0-9]{6}$')
                provenance = raw['provenance']
                self.assertEqual(provenance['source_commit'], COMMIT)
                self.assertEqual(provenance['seed_range'], [start, end])
                self.assertEqual(provenance['validation']['spot_check']['pairs'], 64)
                self.assertLessEqual(provenance['validation']['spot_check']['max_abs_error'], 1e-9)
                self.assertNotIn('validation.json', json.dumps(provenance['implementation']))
                dataset = score.load_dataset(path, bank=True)       # the scorer's own bank loader
                self.assertEqual((dataset.backend, dataset.policy, len(dataset.edges)), ('replica', policy, end - start + 1))
                total += len(raw['rows'])
        self.assertEqual(total, 12288)

    def test_published_paired_figures_reproduce_through_the_scorer(self):
        # reference/combo/README.md (combo-offsets column).  The choose-window qt1-ref base there
        # differs from a recomputation on 17 seeds (< 4e-6 on the mean), hence the looser bound.
        cases = [('qt1-ref', 'COMBO', (10000, 12047), 0.597391, 0.013388, 2e-6),
                 ('qt1-ref', 'TwoComp', (10000, 12047), 0.424803, 0.012202, 2e-6),
                 ('TwoComp', 'COMBO', (10000, 12047), 0.172588, 0.010763, 2e-6),
                 ('TwoComp', 'COMBO', (60000, 61023), 0.192478, 0.014753, 2e-6),
                 ('qt1-ref', 'COMBO', (60000, 61023), 0.637899, 0.019626, 1e-5),
                 ('qt1-ref', 'TwoComp', (60000, 61023), 0.445421, 0.017637, 1e-5)]
        for base, candidate, (start, end), mean, se, tolerance in cases:
            status, rounds = paired(bank_path(base, start, end), bank_path(candidate, start, end))
            self.assertEqual(status, {'reason': 'exhausted', 'n': end - start + 1, 'available': end - start + 1})
            self.assertEqual([r['n'] for r in rounds], list(range(128, end - start + 2, 128)))
            self.assertAlmostEqual(rounds[-1]['mean_diff'], mean, delta=tolerance)
            self.assertAlmostEqual(rounds[-1]['se'], se, delta=tolerance)
        means = {'COMBO': 533.860222, 'TwoComp': 533.687634, 'qt1-ref': 533.262831}
        for policy, expected in means.items():
            edges = score.load_dataset(bank_path(policy, 10000, 12047), bank=True).edges
            self.assertAlmostEqual(sum(edges.values()) / len(edges), expected, delta=2e-6)

    def test_scorer_rejects_mismatched_seed_windows(self):
        with self.assertRaises(ValueError):
            paired(bank_path('qt1-ref', 60000, 61023), bank_path('COMBO', 64000, 65023))


class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = json.loads((OUT / 'validation.json').read_text())

    def test_committed_evidence_validates(self):
        errors, summary = validate.check(ROOT)
        self.assertEqual(errors, [])
        self.assertEqual((summary['complete'], summary['banks'], summary['bank_rows']), (True, 9, 12288))
        self.assertEqual((summary['gate_pairs'], summary['spot_pairs'], summary['distinct_comparisons']), (384, 576, 576))
        self.assertLessEqual(summary['gate_max_abs_error'], 1e-9)
        self.assertLessEqual(summary['spot_max_abs_error'], 1e-9)

    def test_document_shape(self):
        doc = self.doc
        self.assertEqual(next(iter(doc)), '_credit')
        self.assertIs(doc['complete'], True)
        self.assertNotIn('baselines/replica/validation.json', doc['bindings'])
        self.assertEqual(list(doc['timings']), ['runs_per_second_per_core', 'cold_jit_seconds'])
        self.assertEqual(sorted(doc['timings']['runs_per_second_per_core']), sorted(POLICIES))
        self.assertEqual(len(doc['gates384']), 384)
        self.assertEqual(len(doc['spots576']), 576)
        self.assertEqual(list(doc['gates384'][0]), ['policy', 'seed', 'cpython_edge', 'ammfast_edge', 'abs_error'])
        self.assertEqual({(r['policy'], r['seed']) for r in doc['gates384']}, set(generate.gate_pairs()))
        self.assertEqual({(r['policy'], r['seed']) for r in doc['spots576']}, set(generate.spot_pairs()))
        for name in [f'baselines/replica/{p}-{s}-{e}.json' for p in POLICIES for s, e in RANGES]:
            self.assertIn(name, doc['bindings'])
        for path in (ROOT / 'ammfast').glob('*.py'):
            self.assertIn('ammfast/' + path.name, doc['bindings'])

    def test_tampering_is_detected(self):
        (ROOT / 'scratch').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / 'scratch') as tmp:
            root = Path(tmp)
            names = list(self.doc['bindings']) + ['baselines/replica/validation.json']
            for name in names:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, root / name)
            self.assertEqual(validate.check(root)[0], [])
            target = root / 'baselines/replica/validation.json'
            original = target.read_text()

            def broken(change):
                doc = json.loads(original)
                change(doc)
                target.write_text(json.dumps(doc))
                errors = validate.check(root)[0]
                target.write_text(original)
                return errors

            self.assertTrue(broken(lambda d: d.update(complete=False)))
            self.assertTrue(broken(lambda d: d['gates384'].pop()))
            self.assertTrue(broken(lambda d: d['gates384'][5].update(abs_error=0.0, ammfast_edge=d['gates384'][5]['ammfast_edge'] + 1e-6)))
            self.assertTrue(broken(lambda d: d['spots576'][7].update(cpython_edge=d['spots576'][7]['cpython_edge'] + 1e-3)))
            self.assertTrue(broken(lambda d: d['spots576'].reverse()))
            self.assertTrue(broken(lambda d: d['bindings'].update({'baselines/replica/validation.json': '0' * 64})))
            self.assertTrue(broken(lambda d: d['bindings'].pop('ammfast/_kernel.py')))
            self.assertTrue(broken(lambda d: d['bindings'].pop('reference/sources.json')))
            self.assertTrue(broken(lambda d: d['timings'].pop('cold_jit_seconds')))
            self.assertTrue(broken(lambda d: d['timings']['runs_per_second_per_core'].update(COMBO=0.0)))
            bank = root / 'baselines/replica/COMBO-64000-65023.json'
            text = bank.read_text()
            bank.write_text(text.replace('"seed": 64500, "edge": "', '"seed": 64500, "edge": "9', 1))
            self.assertTrue(any('COMBO-64000-65023' in e for e in validate.check(root)[0]))
            bank.write_text(text)
            kernel = root / 'ammfast/_kernel.py'
            kernel.write_text(kernel.read_text() + '# changed\n')
            self.assertTrue(any('ammfast/_kernel.py' in e for e in validate.check(root)[0]))


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'scratch').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'scratch')
        self.path = Path(self.tmp.name) / 'rows.jsonl'

    def tearDown(self):
        self.tmp.cleanup()

    def test_work_lists(self):
        self.assertEqual(len(generate.gate_pairs()), 384)
        self.assertEqual(len(generate.spot_pairs()), 576)
        self.assertEqual(len(set(generate.gate_pairs()) | set(generate.spot_pairs())), 576)
        self.assertEqual(len(generate.bank_pairs()), 12288)
        self.assertEqual(generate.TOLERANCE, 1e-9)

    def test_round_trip_keeps_unrounded_floats(self):
        rows = [('COMBO', 60000, 444.1953300340616), ('qt1-ref', 60001, -0.1 + 0.3)]
        generate.append_rows(self.path, 'a' * 64, rows)
        self.assertEqual(generate.load_rows(self.path, 'a' * 64), {(p, s): e for p, s, e in rows})

    def test_stale_binding_is_rejected(self):
        generate.append_rows(self.path, 'a' * 64, [('COMBO', 60000, 1.5)])
        with self.assertRaises(generate.StaleState):
            generate.load_rows(self.path, 'b' * 64)

    def test_bad_rows_are_rejected_and_torn_tail_is_dropped(self):
        generate.append_rows(self.path, 'a' * 64, [('COMBO', 60000, 1.5)])
        with open(self.path, 'a') as handle:
            handle.write('{"binding": "' + 'a' * 64 + '", "policy": "COMBO", "se')      # killed mid-write
        self.assertEqual(generate.load_rows(self.path, 'a' * 64), {('COMBO', 60000): 1.5})
        self.assertTrue(self.path.read_text().endswith('\n'))
        generate.append_rows(self.path, 'a' * 64, [('COMBO', 60000, 1.5)])               # duplicate
        with self.assertRaises(generate.StaleState):
            generate.load_rows(self.path, 'a' * 64)
        for bad in ('{"binding": "%s", "policy": "nope", "seed": 1, "edge": 1.0}' % ('a' * 64),
                    '{"binding": "%s", "policy": "COMBO", "seed": 1, "edge": "1.0"}' % ('a' * 64),
                    'not json'):
            self.path.write_text(bad + '\n')
            with self.assertRaises(generate.StaleState):
                generate.load_rows(self.path, 'a' * 64)

    def test_binding_follows_code_reference_and_runtime(self):
        document = generate.binding_document()
        self.assertEqual(sorted(document), ['implementation', 'n_steps', 'reference', 'runtime'])
        digest = generate.binding_digest(document)
        for key, change in (('implementation', {'ammfast/_kernel.py': '0' * 64}),
                            ('reference', {'reference/sources.json': '0' * 64}),
                            ('runtime', {'numba': 'other'})):
            changed = dict(document, **{key: dict(document[key], **change)})
            self.assertNotEqual(generate.binding_digest(changed), digest)


@needs_numba
class LiveScorerTests(unittest.TestCase):
    """score.py's default replica adapter (ammfast:run_batch), unmodified, against bank data."""

    def cli(self, *args):
        done = subprocess.run([sys.executable, '-B', str(ROOT / 'score.py'), *args], cwd=ROOT,
                              capture_output=True, text=True, timeout=300)
        rounds = [json.loads(line) for line in done.stdout.splitlines()]
        return done, rounds

    def test_cli_live_candidate_reproduces_published_pairing(self):
        done, rounds = self.cli('--baseline', str(bank_path('qt1-ref', 60000, 61023)), '--replica', '--policy', 'TwoComp')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([r['n'] for r in rounds], [128 * k for k in range(1, 9)])
        self.assertEqual(sorted(rounds[-1]), ['mean_diff', 'n', 'se', 'worst5'])
        self.assertAlmostEqual(rounds[-1]['mean_diff'], 0.445421, delta=1e-5)
        self.assertAlmostEqual(rounds[-1]['se'], 0.017637, delta=1e-5)
        self.assertEqual(len(rounds[-1]['worst5']), 5)
        header = json.loads(done.stderr.splitlines()[0])
        self.assertEqual(header['candidate']['adapter'], 'ammfast:run_batch')
        self.assertIn('"reason": "exhausted"', done.stderr)

    def test_cli_zero_head_file_matches_the_combo_bank_to_rounding(self):
        (ROOT / 'scratch').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / 'scratch') as tmp:
            head = Path(tmp) / 'head.json'
            head.write_text(json.dumps({'_credit': f'Taylor (gtmcknight), amm-sim@{COMMIT}, research/solved', 'weights': [0.0] * 18}))
            done, rounds = self.cli('--baseline', str(bank_path('COMBO', 64000, 65023)), '--replica',
                                    '--policy', 'COMBO', '--head-params', str(head))
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertGreaterEqual(rounds[-1]['n'], 128)
            for row in rounds:                                  # only six-decimal rounding separates them
                self.assertLessEqual(abs(row['mean_diff']), 5.0001e-7)
                self.assertTrue(all(abs(w['diff']) <= 5.0001e-7 for w in row['worst5']))
            head.write_text(json.dumps({'weights': [0.0] * 17}))
            done, rounds = self.cli('--baseline', str(bank_path('COMBO', 64000, 65023)), '--replica',
                                    '--policy', 'COMBO', '--head-params', str(head))
            self.assertEqual((done.returncode, rounds), (2, []))
            self.assertIn('18', done.stderr)

    def test_in_process_live_candidate_and_unknown_policy(self):
        bank = score.load_dataset(bank_path('COMBO', 60000, 61023), bank=True)
        seeds = sorted(bank.edges)[:128]
        edges = score.LiveCandidate('replica', 'COMBO').fetch(seeds)
        self.assertEqual(sorted(edges), seeds)
        for seed in seeds:
            self.assertEqual(format(edges[seed], '.6f'), format(bank.edges[seed], '.6f'))
        with self.assertRaises(ValueError):
            score.LiveCandidate('replica', 'combo').fetch(seeds[:2])


if __name__ == '__main__':
    unittest.main()
