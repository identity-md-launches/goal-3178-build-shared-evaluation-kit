# Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9,
# research/solved, solved-policy research.
"""Deterministic fixtures/stubs, NOT real backend integration tests."""
import contextlib
import copy
import io
import json
import math
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import score

CREDIT = 'Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9 research/solved'


def document(n=3, backend='replica', policy=None):
    start = score.ENGINE_MIN if backend == 'engine' else 10000
    rows = [{'seed': start + i, 'edge': '1.000000'} for i in range(n)]
    if backend == 'engine':
        for row in rows:
            row['after_swap_peak_gas'] = 123
    return {'_credit': CREDIT, 'backend': backend,
            'policy': policy or ('qt1' if backend == 'engine' else 'COMBO'),
            'provenance': {'source': 'deterministic test fixture; not simulation'}, 'rows': rows}


def bank(n=3, backend='replica'):
    doc = document(n, backend)
    return score.Dataset(backend, doc['policy'], {r['seed']: 1.0 for r in doc['rows']}, {})


def rows_candidate(b, diffs):
    return score.RowCandidate(score.Dataset(b.backend, 'different-policy',
                                            {s: b.edges[s] + d for s, d in zip(b.edges, diffs)}, {}))


class ScoreTests(unittest.TestCase):
    def test_analytic_mean_and_sample_se(self):
        b = bank()
        emitted = []
        status = score.evaluate(b, rows_candidate(b, [1, 2, 3]), emit=emitted.append)
        self.assertEqual(status['reason'], 'exhausted')
        self.assertEqual(emitted[0]['mean_diff'], 2)
        self.assertAlmostEqual(emitted[0]['se'], 1 / math.sqrt(3))
        self.assertEqual(emitted[0]['worst5'], [{'seed': 10000+i, 'diff': i+1} for i in range(3)])

    def test_zero_variance_worse(self):
        b = bank(256)
        out = []
        result = score.evaluate(b, rows_candidate(b, [-1]*256), emit=out.append)
        self.assertEqual(result, {'n': 128, 'available': 256, 'reason': 'worse'})
        self.assertEqual(out[0]['se'], 0)

    def test_above_bar(self):
        b = bank(256)
        self.assertEqual(score.evaluate(b, rows_candidate(b, [2]*256), bar=1)['reason'], 'above_bar')

    def test_equal_zero_does_not_stop(self):
        b = bank(257)
        out = []
        score.evaluate(b, rows_candidate(b, [0]*257), emit=out.append)
        self.assertEqual([r['n'] for r in out], [128, 256, 257])

    def test_equal_bar_does_not_stop(self):
        b = bank(129)
        self.assertEqual(score.evaluate(b, rows_candidate(b, [2]*129), bar=2)['n'], 129)

    def test_nonzero_se_equal_thresholds(self):
        # n=2, [-4,-2]: mean=-3, sample SE=1, upper=0 exactly.
        b = bank(2)
        self.assertEqual(score.evaluate(b, rows_candidate(b, [-4, -2]))['reason'], 'exhausted')
        # [2,4]: mean=3, SE=1, lower=0 exactly equals bar.
        self.assertEqual(score.evaluate(b, rows_candidate(b, [2, 4]), bar=0)['reason'], 'exhausted')

    def test_empty(self):
        out = []
        b = bank(0)
        self.assertEqual(score.evaluate(b, rows_candidate(b, []), emit=out.append)['n'], 0)
        self.assertEqual(out, [{'n': 0, 'mean_diff': None, 'se': None, 'worst5': []}])

    def test_singleton_no_stop(self):
        for diff, bar_value in [(-20, None), (20, 0)]:
            with self.subTest(diff=diff):
                out = []
                b = bank(1)
                self.assertEqual(score.evaluate(b, rows_candidate(b, [diff]), bar=bar_value, emit=out.append)['reason'], 'exhausted')
                self.assertIsNone(out[0]['se'])
                self.assertEqual(out[0]['mean_diff'], diff)

    def test_partial_round_is_cumulative(self):
        b = bank(129)
        out = []
        score.evaluate(b, rows_candidate(b, [0]*128 + [129]), emit=out.append)
        self.assertEqual([r['n'] for r in out], [128, 129])
        self.assertEqual(out[-1]['mean_diff'], 1)
        self.assertAlmostEqual(out[-1]['se'], 1)

    def test_worst_five_ties(self):
        pairs = [(8, 2), (7, -1), (6, -1), (5, 0), (4, -1), (3, 0), (2, -1)]
        self.assertEqual(score.summarize(pairs)['worst5'],
                         [{'seed': s, 'diff': d} for s, d in [(2,-1),(4,-1),(6,-1),(7,-1),(3,0)]])

    def test_nonfinite_bar_before_calls(self):
        for bar_value in [float('nan'), float('inf'), -float('inf'), True]:
            with self.subTest(bar=bar_value), patch('score.importlib.import_module') as imp:
                with self.assertRaises(ValueError):
                    score.evaluate(bank(), score.LiveCandidate('replica', 'COMBO'), bar=bar_value)
                imp.assert_not_called()

    def test_full_precision_difference(self):
        b = bank(256)
        result = score.evaluate(b, rows_candidate(b, [-1e-10]*256))
        self.assertEqual(result['reason'], 'worse')
        self.assertEqual(result['n'], 128)

    def test_overflow_difference_rejected(self):
        b = score.Dataset('replica', 'COMBO', {1: -1e308}, {})
        c = score.RowCandidate(score.Dataset('replica', 'COMBO', {1: 1e308}, {}))
        with self.assertRaisesRegex(ValueError, 'paired difference'):
            score.evaluate(b, c)

    def test_row_candidate_missing_and_extra(self):
        for edges in [{10000: 1.0}, {10000: 1., 10001: 1., 10002: 1., 42: 1.}]:
            with self.subTest(edges=edges), self.assertRaises(ValueError):
                score.evaluate(bank(), score.RowCandidate(score.Dataset('replica', 'COMBO', edges, {})))

    def test_backend_mismatch_before_import(self):
        with patch('score.importlib.import_module') as imp, self.assertRaises(ValueError):
            score.evaluate(bank(), score.LiveCandidate('engine', 'new-strategy', strategy_path='fixture.sol'))
        imp.assert_not_called()


class LiveTests(unittest.TestCase):
    def test_both_backend_spies_stop_no_later_execution(self):
        for backend in ('replica', 'engine'):
            with self.subTest(backend=backend):
                b = bank(300, backend)
                calls = []
                def runner(seeds, identity, **kwargs):
                    calls.append((list(seeds), identity, kwargs))
                    return [dict(seed=s, edge=0., after_swap_peak_gas=1) for s in reversed(seeds)]
                module = types.SimpleNamespace(run_batch=runner)
                module_name = 'ammfast' if backend == 'replica' else 'engine_eval'
                c = score.LiveCandidate(backend, 'new-policy', strategy_path='fixture.sol' if backend == 'engine' else None)
                with patch.dict(sys.modules, {module_name: module}):
                    result = score.evaluate(b, c)
                self.assertEqual(result['reason'], 'worse')
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], list(b.edges)[:128])
                self.assertEqual(calls[0][1], 'new-policy' if backend == 'replica' else 'fixture.sol')
                self.assertEqual(calls[0][2], {'head_params': None} if backend == 'replica' else {})

    def test_both_above_bar_stop(self):
        for backend in ('replica', 'engine'):
            calls = []
            def runner(seeds, identity, **kwargs):
                calls.append(seeds)
                return [dict(seed=s, edge=3., after_swap_peak_gas=1) for s in seeds]
            c = score.LiveCandidate(backend, 'new-policy', strategy_path='fixture.sol' if backend == 'engine' else None,
                                    adapter='fixture:run')
            with patch.dict(sys.modules, {'fixture': types.SimpleNamespace(run=runner)}):
                result = score.evaluate(bank(300, backend), c, bar=1)
            self.assertEqual(result['reason'], 'above_bar')
            self.assertEqual(len(calls), 1)

    def test_lazy_no_import_on_empty(self):
        with patch('score.importlib.import_module') as imp:
            score.evaluate(bank(0), score.LiveCandidate('replica', 'COMBO'))
            imp.assert_not_called()

    def test_generator_adapter_and_head(self):
        calls = []
        def runner(seeds, policy, head_params=None):
            calls.append((list(seeds), policy, head_params))
            return (dict(seed=s, edge=1.) for s in reversed(seeds))
        with patch.dict(sys.modules, {'fixture': types.SimpleNamespace(run=runner)}):
            result = score.evaluate(bank(257), score.LiveCandidate('replica', 'custom', head_params={'a': 1}, adapter='fixture:run'))
        self.assertEqual(result['n'], 257)
        self.assertEqual([len(c[0]) for c in calls], [128,128,1])
        self.assertTrue(all(c[1:] == ('custom', {'a': 1}) for c in calls))

    def test_engine_bad_later_seed_blocks_all_execution(self):
        b = bank(129, 'engine')
        b.edges[9700512] = 1.
        with patch('score.importlib.import_module') as imp, self.assertRaises(ValueError):
            score.evaluate(b, score.LiveCandidate('engine', 'new', strategy_path='fixture.sol'))
        imp.assert_not_called()

    def test_engine_boundaries(self):
        rows = [dict(seed=s, edge=1, after_swap_peak_gas=0) for s in (9700000,9700511)]
        self.assertEqual(len(score.validate_rows(rows, 'engine')), 2)
        for seed in (9699999,9700512):
            with self.assertRaises(ValueError):
                score.validate_rows([dict(seed=seed, edge=1, after_swap_peak_gas=0)], 'engine')

    def test_bad_live_rows(self):
        variants = [[], [{'seed':10000,'edge':1}]*2,
                    [{'seed':9,'edge':1}], [{'seed':10000,'edge':float('nan')}],
                    [{'seed':10000,'edge':1, 'backend':'engine'}],
                    [{'seed':10000,'edge':1, 'metric':'profit'}]]
        for rows in variants:
            with self.subTest(rows=rows), patch.dict(sys.modules, {'fixture': types.SimpleNamespace(run=lambda *a, **k: rows)}):
                with self.assertRaises(ValueError):
                    score.evaluate(bank(1), score.LiveCandidate('replica', 'custom', adapter='fixture:run'))

    def test_live_options_invalid(self):
        for kwargs in [dict(backend='wrong',policy='x'),dict(backend='replica',policy=''),
                       dict(backend='engine',policy='x'),dict(backend='engine',policy='x',strategy_path='x',head_params=[]),
                       dict(backend='replica',policy='x',strategy_path='x'),dict(backend='replica',policy='x',adapter='no colon')]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                score.LiveCandidate(**kwargs)


class FileTests(unittest.TestCase):
    def setUp(self):
        Path('scratch/score-tests').mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir='scratch/score-tests')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, obj, name='fixture.json'):
        path = self.root / name
        path.write_text(json.dumps(obj), encoding='utf-8')
        return str(path)

    def cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = score.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_valid_bank_and_shuffled_different_candidate(self):
        b = score.load_dataset(self.write(document()), bank=True)
        doc = document(policy='new-policy')
        doc['rows'].reverse()
        c = score.RowCandidate(score.load_dataset(self.write(doc)))
        self.assertEqual(score.evaluate(b, c)['n'], 3)
        self.assertEqual(c.provenance['policy'], 'new-policy')
        self.assertEqual(len(b.provenance['sha256']), 64)
        self.assertEqual(b.provenance['precision'], 'six-decimal bank')

    def test_invalid_banks(self):
        changes = [lambda d: d.pop('_credit'),lambda d: d.update(_credit=''),
                   lambda d: d.update(backend='unknown'),lambda d: d.update(policy='qt1'),
                   lambda d: d.update(provenance={}),lambda d: d.update(rows={}),
                   lambda d: d.update(metric='norm'),lambda d: d['provenance'].update(backend='engine'),
                   lambda d: d['provenance'].update(metric='gas'),lambda d: d['rows'].reverse(),
                   lambda d: d['rows'].append(copy.copy(d['rows'][0])),
                   lambda d: d['rows'][0].update(seed=True),lambda d: d['rows'][0].update(seed=1.0),
                   lambda d: d['rows'][0].update(edge=1.),lambda d: d['rows'][0].update(edge='1.0'),
                   lambda d: d['rows'][0].update(edge='NaN'),lambda d: d['rows'][0].pop('seed'),
                   lambda d: d['rows'][0].update(edge='9'*400+'.000000')]
        for change in changes:
            doc = document(); change(doc)
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                score.load_dataset(self.write(doc), bank=True)

    def test_credit_must_be_first(self):
        doc = document(); credit = doc.pop('_credit'); doc['_credit'] = credit
        with self.assertRaises(ValueError):
            score.load_dataset(self.write(doc), bank=True)

    def test_invalid_engine_gas(self):
        for gas in [None, -1, True, 1.2, '123']:
            doc = document(1, 'engine'); doc['rows'][0]['after_swap_peak_gas'] = gas
            with self.subTest(gas=gas), self.assertRaises(ValueError):
                score.load_dataset(self.write(doc), bank=True)

    def test_candidate_nonfinite_values(self):
        for value in ['NaN', 'Infinity', '-Infinity', 1e309, True, None, []]:
            doc = document(); doc['rows'][0]['edge'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                score.load_dataset(self.write(doc))

    def test_duplicate_json_keys(self):
        p = self.root/'duplicate.json'; p.write_text('{"rows":[],"rows":[]}')
        with self.assertRaisesRegex(ValueError, 'duplicate JSON key'):
            score.read_json(p)

    def test_cli_offline_no_optional_import(self):
        baseline = self.write(document(129), 'bank.json')
        candidate = self.write(document(129, policy='new'), 'candidate.json')
        with patch('score.importlib.import_module', side_effect=AssertionError('optional import')):
            code, out, err = self.cli(['--baseline', baseline, '--rows', candidate])
        self.assertEqual(code, 0, err)
        summaries = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([r['n'] for r in summaries], [128,129])
        self.assertEqual(set(summaries[0]), {'n','mean_diff','se','worst5'})
        metadata, status = map(json.loads, err.splitlines())
        self.assertEqual(metadata['baseline']['policy'], 'COMBO')
        self.assertEqual(metadata['candidate']['policy'], 'new')
        self.assertEqual(status['status']['reason'], 'exhausted')

    def test_cli_live_both_modes(self):
        for backend in ('replica', 'engine'):
            with self.subTest(backend=backend):
                path = self.write(document(1, backend))
                def runner(seeds, identity, **kw):
                    return [dict(seed=s, edge=1, after_swap_peak_gas=1) for s in seeds]
                args = ['--baseline',path,'--'+backend,'--policy','different','--adapter','fixture:run']
                if backend == 'engine':
                    args += ['--strategy-path','fixture.sol']
                with patch.dict(sys.modules, {'fixture': types.SimpleNamespace(run=runner)}):
                    code,out,err = self.cli(args)
                self.assertEqual(code,0,err)
                self.assertEqual(json.loads(out)['n'],1)

    def test_cli_invalid_options(self):
        baseline = self.write(document(), 'bank.json')
        candidate = self.write(document(), 'candidate.json')
        variants = [['--rows',candidate,'--policy','x'],['--replica'],
                    ['--replica','--policy','x','--bar','nan'],
                    ['--engine','--policy','x','--strategy-path','x','--head-params','absent'],
                    ['--engine','--policy','x','--strategy-path','x']]
        for args in variants:
            with self.subTest(args=args), patch('score.importlib.import_module') as imp:
                code,out,err = self.cli(['--baseline',baseline]+args)
                self.assertEqual(code,2,err)
                self.assertEqual(out,'')
                imp.assert_not_called()

    def test_cli_backend_failure(self):
        baseline = self.write(document())
        with patch('score.importlib.import_module', side_effect=ImportError('fixture backend unavailable')):
            code,out,err = self.cli(['--baseline',baseline,'--replica','--policy','COMBO'])
        self.assertEqual(code,2)
        self.assertIn('fixture backend unavailable',err)
        self.assertEqual(out,'')

    def test_cli_interrupt(self):
        baseline = self.write(document())
        with patch('score.importlib.import_module', side_effect=KeyboardInterrupt):
            code,out,err = self.cli(['--baseline',baseline,'--replica','--policy','COMBO'])
        self.assertEqual(code,130)
        self.assertIn('partial',err)

    def test_cli_missing_file(self):
        code,out,err = self.cli(['--baseline',str(self.root/'absent'),'--rows','absent'])
        self.assertEqual(code,2)
        self.assertIn('FileNotFoundError',err)


if __name__ == '__main__':
    unittest.main()
