# Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9,
# research/solved, solved-policy research.
"""ammfast API tests: contract, parity with CPython replica2, batches, isolation, residual head.

Run from the repository root with the interpreter from setup_fast.sh (``. scratch/env.sh``):
    python -B -m unittest discover -s tests -p test_ammfast.py -v
Without numpy/numba the compiled-kernel tests are skipped and say so; the rest still run."""
import ast
import base64
import copy
import difflib
import hashlib
import json
import math
from pathlib import Path
import random
import sys
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ammfast  # noqa: E402
from ammfast import _head, _reference  # noqa: E402

try:
    import numba  # noqa: F401
    import numpy as np
    HAVE_NUMBA = True
except ImportError:
    np = None
    HAVE_NUMBA = False
needs_numba = unittest.skipUnless(
    HAVE_NUMBA, 'numpy/numba missing: run setup_fast.sh --replica-only and source scratch/env.sh')

TOL = 1e-9
COMMIT = '4812a6382311624e1506aca131ca35c378f930d9'
# Deterministic, hand-written test vectors.  They are not trained heads.
W_SMALL = [0.03 * (i - 8.5) for i in range(18)]
W_LARGE = [((-1) ** i) * (2.0 + i) for i in range(18)]      # saturates tanh: corrections near +-8 bps


class ValidationTests(unittest.TestCase):
    """Input validation happens before any compilation, so these need no numba."""

    def test_unknown_policy_ids_raise_value_error(self):
        for policy in ('qmref', 'twocomp', 'combo', 'QT1-REF', 'qt1', 'HA', 'Combo', '', ' COMBO', None, 0, b'COMBO'):
            with self.subTest(policy=policy):
                with self.assertRaises(ValueError):
                    ammfast.run(10000, policy)
                with self.assertRaises(ValueError):
                    ammfast.run_batch([], policy)
        self.assertEqual(ammfast.POLICIES, ('qt1-ref', 'TwoComp', 'COMBO'))

    def test_seed_validation(self):
        for seed in (True, False, 1.0, '10000', None, -1, ammfast.SEED_MAX + 1, 2 ** 40):
            with self.subTest(seed=seed):
                with self.assertRaises(ValueError):
                    ammfast.run(seed, 'COMBO')
                with self.assertRaises(ValueError):
                    ammfast.run_batch([10000, seed], 'COMBO')
        with self.assertRaises(ValueError):
            ammfast.run_batch('10000', 'COMBO')

    def test_empty_batch_is_empty_list(self):
        for policy in ammfast.POLICIES:
            self.assertEqual(ammfast.run_batch([], policy), [])
        self.assertEqual(ammfast.run_batch(iter(()), 'COMBO', [0.0] * 18), [])

    def test_head_params_shape_and_values(self):
        good = [0.25] * 18
        self.assertEqual(ammfast.validate_head_params(good), tuple(good))
        self.assertEqual(ammfast.validate_head_params(tuple(good)), tuple(good))
        self.assertEqual(ammfast.validate_head_params({'_credit': 'x', 'weights': good}), tuple(good))
        self.assertEqual(ammfast.validate_head_params([1] * 18), (1.0,) * 18)
        self.assertIsNone(ammfast.validate_head_params(None))
        bad = [[0.0] * 17, [0.0] * 19, [], [0.0] * 17 + [math.nan], [0.0] * 17 + [math.inf],
               [0.0] * 17 + [True], [0.0] * 17 + ['1'], [0.0] * 17 + [None], [0.0] * 17 + [1j],
               '0' * 18, b'0' * 18, 3.0, {'weights': good, 'bias': 1}, {'w': good}, {}, {'weights': [0.0] * 3},
               [[0.0] * 18], iter(good), [0.0] * 17 + [10 ** 400]]
        for value in bad:
            with self.subTest(value=repr(value)[:40]):
                with self.assertRaises(ValueError):
                    ammfast.validate_head_params(value)
                with self.assertRaises(ValueError):
                    ammfast.run(10000, 'COMBO', value)

    def test_head_is_combo_only(self):
        for policy in ('qt1-ref', 'TwoComp'):
            with self.assertRaises(ValueError):
                ammfast.run(10000, policy, [0.0] * 18)
            with self.assertRaises(ValueError):
                ammfast.run_batch([10000], policy, head_params=[0.0] * 18)

    def test_n_steps_validation(self):
        for steps in (-1, 1.5, True, '10', None):
            with self.assertRaises(ValueError):
                ammfast.run(10000, 'COMBO', n_steps=steps)
        self.assertEqual(ammfast.DEFAULT_STEPS, 10000)


class ReferenceTests(unittest.TestCase):
    """The pinned CPython oracle and the pure-Python head; standard library only."""

    def test_pins_match_the_source_inventory(self):
        self.assertEqual(_reference.TAYLOR_COMMIT, COMMIT)
        inventory = json.loads((ROOT / 'reference/sources.json').read_text())
        files = {entry['path']: entry for entry in inventory['files']}
        for name, (envelope, size, digest) in _reference.PINS.items():
            self.assertEqual(('reference/' + files[name]['envelope'], files[name]['size'], files[name]['sha256']),
                             (envelope, size, digest))
            self.assertEqual(len(_reference.original(name)), size)
        self.assertEqual(files['combo/twocomp.py']['sha256'], _reference.PINS['twocomp/twocomp.py'][2])
        self.assertEqual(inventory['taylor_commit'], COMMIT)

    def test_loading_does_not_leave_modules_behind(self):
        before = sys.modules.get('twocomp')
        ref = _reference.modules()
        self.assertIs(sys.modules.get('twocomp'), before)
        self.assertTrue(issubclass(ref.combo.Combo, ref.twocomp.TwoComp))

    def test_python_head_with_zero_weights_is_combo_exactly(self):
        for seed in (10000, 64000):
            plain = ammfast.reference_run(seed, 'COMBO', n_steps=400)
            zero = ammfast.reference_run(seed, 'COMBO', [0.0] * 18, n_steps=400)
            self.assertEqual(plain, zero)

    def test_reference_runs_use_fresh_policy_state(self):
        first = ammfast.reference_run(10001, 'TwoComp', n_steps=300)
        ammfast.reference_run(64001, 'TwoComp', n_steps=300)
        self.assertEqual(ammfast.reference_run(10001, 'TwoComp', n_steps=300), first)

    def test_hidden_projection_is_fixed(self):
        U, c = _head.hidden_weights()
        self.assertEqual((len(U), len(U[0]), len(c)), (8, 8, 8))
        self.assertEqual(_head.hidden_weights(), (U, c))
        self.assertEqual(_head.HEAD_PARAM_COUNT, 2 * _head.NH + 2)

    def test_compensated_sum(self):
        self.assertEqual(_head.nsum([1e16, 1.0, -1e16, 1.0]), 2.0)
        if sys.version_info >= (3, 12):
            rng = random.Random(7)
            for _ in range(200):
                values = [rng.gauss(0, 1) * 10 ** rng.randint(-8, 8) for _ in range(8)]
                self.assertEqual(_head.nsum(values), sum(values))


class KernelProvenanceTests(unittest.TestCase):
    """ammfast/_kernel.py is the archived parity-checked kernel plus the documented deltas only."""
    BANNER = '# ---------------------------------------------------------------- CPython-compatible Mersenne Twister'

    def archived(self):
        box = json.loads((ROOT / 'reference/envelopes/numba__fastsim.py.json').read_text())
        data = zlib.decompress(base64.b64decode(box['data']))
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         '4d3d2187b5dcbcd979a0775e431d30421b0e376d3c582afc60fbb4d6a052596b')
        return data.decode('utf-8')

    def test_simulation_code_differs_only_by_the_documented_deltas(self):
        source = self.archived()
        old = source[source.index(self.BANNER):source.index('@njit(cache=True)\ndef lg_row')].rstrip().split('\n')
        kernel = (ROOT / 'ammfast/_kernel.py').read_text()
        new = kernel[kernel.index(self.BANNER):kernel.index('@njit(cache=True)\ndef sim_batch')].rstrip().split('\n')
        removed = [line for line in difflib.ndiff(old, new) if line.startswith('- ')]
        added = [line[2:] for line in difflib.ndiff(old, new) if line.startswith('+ ')]
        logging = [line for line in removed if 'lg_row(' in line or line[2:].strip() in ('ncb += 1', 'ncb = 0')]
        self.assertEqual(len(logging), 4 + 4 + 1)                       # four callback sites logged and counted
        rest = [line[2:] for line in removed if line not in logging]
        self.assertEqual(rest, [
            '# ---------------------------------------------------------------- replica2.run',
            'def sim(seed, n_steps, pol, hp, hon, A, B, S9, W2L, I, HU, HC, lg, nlog_max, cfgo, usecfg):',
            '    """returns (edge, norm edge, number of callbacks, new-path count)"""',
            '    if usecfg:',
            '        size_mean = cfgo[0]; lam = cfgo[1]; sigma = cfgo[2]',
            '    return e1, e2, ncb, si[5]'])
        self.assertEqual(added[0], '# ---------------------------------------------------------------- replica2.run (Taylor, gtmcknight)')
        self.assertEqual(added[1], 'def sim(seed, n_steps, pol, hp, hon, A, B, S9, W2L, I, HU, HC):')
        self.assertEqual(added[-1], '    return e1')
        self.assertEqual(len(added), 5)                                 # banner, signature, two docstring lines, return
        self.assertNotIn('fastmath', kernel.split(self.BANNER)[1])

    def test_constants_are_the_archived_ones(self):
        source = self.archived()

        def constant(name):
            line = next(l for l in source.split('\n') if l.startswith(name + ' = '))
            text = line.split(' = ', 1)[1].split('#')[0].strip()
            if text.startswith('np.array('):
                text = text[len('np.array('):-1]
            return ast.literal_eval(text)

        for name in ('HSEED', 'NH', 'CAP', 'XCLIP'):
            self.assertEqual(constant(name), getattr(_head, name))
        self.assertEqual(tuple(constant('MU')), _head.MU)
        self.assertEqual(tuple(constant('SD')), _head.SD)
        kernel = (ROOT / 'ammfast/_kernel.py').read_text()
        for name in ('K', 'MAXF', 'NF', 'SIG2', 'LD_M, LD_S', 'INIT', 'TWO'):
            line = next(l for l in source.split('\n') if l.startswith(name + ' = '))
            self.assertIn(line + '\n', kernel)


@needs_numba
class KernelTests(unittest.TestCase):
    def test_row_shape_and_published_edges(self):
        # reference/combo/README.md, "Edges on 10000..10002 (6 dp)"
        published = {'qt1-ref': ('443.101741', '571.454483', '525.908441'),
                     'TwoComp': ('443.690614', '571.968071', '526.124475'),
                     'COMBO': ('444.195330', '572.172682', '526.274760')}
        for policy, edges in published.items():
            for seed, expected in zip((10000, 10001, 10002), edges):
                row = ammfast.run(seed, policy)
                self.assertEqual(list(row), ['seed', 'edge'])
                self.assertIs(type(row['seed']), int)
                self.assertIs(type(row['edge']), float)
                self.assertTrue(math.isfinite(row['edge']))
                self.assertEqual(format(row['edge'], '.6f'), expected)

    def test_parity_with_cpython_replica2_short_runs(self):
        seeds = (0, 1, 7, 10000, 64000, 2 ** 31, ammfast.SEED_MAX)     # includes both ends of the seed domain
        for policy in ammfast.POLICIES:
            rows = ammfast.run_batch(seeds, policy, n_steps=700)
            for row in rows:
                reference = ammfast.reference_run(row['seed'], policy, n_steps=700)
                self.assertLessEqual(abs(row['edge'] - reference['edge']), TOL, (policy, row['seed']))

    def test_parity_with_cpython_replica2_full_run(self):
        for policy in ammfast.POLICIES:
            fast = ammfast.run(60123, policy)
            reference = ammfast.reference_run(60123, policy)
            self.assertLessEqual(abs(fast['edge'] - reference['edge']), TOL, policy)

    def test_default_is_10000_steps(self):
        row = ammfast.run(10003, 'TwoComp')
        self.assertEqual(row, ammfast.run(10003, 'TwoComp', n_steps=10000))
        self.assertNotEqual(row, ammfast.run(10003, 'TwoComp', n_steps=9999))
        self.assertEqual(ammfast.run(10003, 'TwoComp', n_steps=0), {'seed': 10003, 'edge': 0.0})

    def test_run_batch_order_duplicates_and_inputs(self):
        seeds = [10005, 10002, 10005, 64010, 10002]
        snapshot = list(seeds)
        for policy in ammfast.POLICIES:
            rows = ammfast.run_batch(seeds, policy)
            self.assertEqual([row['seed'] for row in rows], snapshot)
            self.assertEqual(rows, [ammfast.run(seed, policy) for seed in snapshot])
            self.assertIsNot(rows[0], rows[2])
            self.assertEqual(rows[0], rows[2])
        self.assertEqual(seeds, snapshot)
        expected = ammfast.run_batch(snapshot, 'COMBO')
        self.assertEqual(ammfast.run_batch(tuple(snapshot), 'COMBO'), expected)
        self.assertEqual(ammfast.run_batch(iter(snapshot), 'COMBO'), expected)
        self.assertEqual(ammfast.run_batch(range(10000, 10003), 'COMBO'),
                         [ammfast.run(seed, 'COMBO') for seed in (10000, 10001, 10002)])
        rows = ammfast.run_batch(np.array(snapshot), 'COMBO')
        self.assertEqual(rows, expected)
        self.assertTrue(all(type(row['seed']) is int for row in rows))

    def test_no_state_survives_a_run(self):
        first = {policy: ammfast.run(64005, policy) for policy in ammfast.POLICIES}
        ammfast.run(10009, 'COMBO', W_LARGE)
        ammfast.run_batch([10010, 10011], 'TwoComp')
        for policy in ammfast.POLICIES:
            self.assertEqual(ammfast.run(64005, policy), first[policy])
        forward = ammfast.run_batch([10020, 10021], 'COMBO')
        backward = ammfast.run_batch([10021, 10020], 'COMBO')
        self.assertEqual(forward, backward[::-1])

    def test_zero_head_equals_combo_exactly(self):
        seeds = [10000, 10001, 64000]
        self.assertEqual(ammfast.run_batch(seeds, 'COMBO', [0.0] * 18), ammfast.run_batch(seeds, 'COMBO'))
        self.assertEqual(ammfast.run_batch(seeds, 'COMBO', {'weights': [0] * 18}), ammfast.run_batch(seeds, 'COMBO'))

    def test_nonzero_head_matches_python_equivalent(self):
        for weights in (W_SMALL, W_LARGE):
            snapshot = copy.deepcopy(weights)
            for seed in (10000, 64003, 60007):
                fast = ammfast.run(seed, 'COMBO', weights, n_steps=1500)
                reference = ammfast.reference_run(seed, 'COMBO', weights, n_steps=1500)
                self.assertLessEqual(abs(fast['edge'] - reference['edge']), TOL, seed)
                self.assertNotEqual(fast, ammfast.run(seed, 'COMBO', n_steps=1500))
            self.assertEqual(weights, snapshot)
        fast = ammfast.run(10000, 'COMBO', W_SMALL)
        reference = ammfast.reference_run(10000, 'COMBO', W_SMALL)
        self.assertLessEqual(abs(fast['edge'] - reference['edge']), TOL)
        self.assertEqual(ammfast.run(10000, 'COMBO', {'_credit': 'test', 'weights': tuple(W_SMALL)}), fast)
        self.assertEqual(ammfast.run(10000, 'COMBO', np.array(W_SMALL)), fast)


class HeaderTests(unittest.TestCase):
    def test_every_delivered_file_credits_taylor(self):
        paths = sorted((ROOT / 'ammfast').glob('*.py')) + sorted((ROOT / 'baselines/replica').glob('*.py'))
        paths += [ROOT / 'tests/test_ammfast.py', ROOT / 'tests/test_replica_banks.py',
                  ROOT / 'docs/ammfast.md', ROOT / 'docs/replica-banks.md']
        self.assertGreaterEqual(len(paths), 10)
        for path in paths:
            head = '\n'.join(path.read_text(encoding='utf-8').splitlines()[:6])
            for needle in ('Taylor', 'gtmcknight', COMMIT, 'research/solved'):
                self.assertIn(needle, head, f'{path.name} header lacks {needle}')


if __name__ == '__main__':
    unittest.main()
