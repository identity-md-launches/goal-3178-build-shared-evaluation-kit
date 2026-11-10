"""Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9 research/solved."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import engine_eval as e


def stat(role='Submission', callback='afterSwap', gas=100):
    return dict(role=role, callback=callback, calls=2, ok=2, max_ok_gas=gas,
                **{k: 0 for k in e.ERRORS})


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.resets = 0
        self.stats = []
        self.edit = lambda stats: stats
        self.result_edit = lambda rows: rows

    def gas_stats_reset(self):
        self.resets += 1
        self.stats = []

    def run_batch(self, code, vanilla, configs, workers):
        assert workers == 8
        assert len(configs) == 1
        seed = configs[0].seed
        self.calls.append(seed)
        self.stats.extend([stat(gas=100 + seed - e.FIRST),
                           stat(callback='afterInitialize', gas=999999),
                           stat(role='Baseline', gas=888888)])
        return SimpleNamespace(results=self.result_edit([
            SimpleNamespace(seed=seed, edges={'submission': seed / 7})]))

    def gas_stats(self):
        return self.edit(self.stats)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = FakeEngine()
        self.patches = [patch.object(e, '_runtime', return_value=self.engine),
                        patch.object(e, '_compile', return_value=(b'a', b'b')),
                        patch.object(e, '_config', side_effect=lambda s: SimpleNamespace(seed=s))]
        self.mocks = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)

    def test_all_seeds_validated_before_execution(self):
        for invalid in (e.FIRST-1, e.LAST+1, True, 9700000.0, '9700000', None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                e.run_batch(iter([e.FIRST, invalid]), 'absent.sol')
        self.assertEqual(self.engine.calls, [])
        for mock in self.mocks:
            mock.assert_not_called()

    def test_empty_is_lazy(self):
        self.assertEqual(e.run_batch([], 'absent.sol'), [])
        for mock in self.mocks:
            mock.assert_not_called()

    def test_order_duplicates_and_gas_isolation(self):
        seeds = [e.LAST, e.FIRST, e.FIRST+7, e.FIRST]
        saved = seeds[:]
        rows = e.run_batch(seeds, 'candidate.sol')
        self.assertEqual(seeds, saved)
        self.assertEqual([r['seed'] for r in rows], seeds)
        self.assertEqual([r['after_swap_peak_gas'] for r in rows], [611, 100, 107, 100])
        self.assertEqual(rows[0]['edge'], e.LAST / 7)
        self.assertEqual(self.engine.resets, len(seeds))
        self.assertEqual(self.engine.calls, seeds)
        self.assertEqual([c.args[0] for c in self.mocks[2].call_args_list], seeds)

    def test_every_error_rejected_any_callback_or_role(self):
        for key in e.ERRORS:
            for role in ('Submission', 'Baseline'):
                for callback in ('afterInitialize', 'afterSwap'):
                    with self.subTest(key=key, role=role, callback=callback):
                        def edit(stats):
                            broken = stat(role, callback)
                            broken[key] = 1
                            return stats + [broken]
                        self.engine.edit = edit
                        with self.assertRaises(RuntimeError):
                            e.run_batch([e.FIRST], 'candidate.sol')

    def test_missing_swap_rejected(self):
        self.engine.edit = lambda stats: [s for s in stats if s['role'] != 'Submission']
        with self.assertRaises(RuntimeError):
            e.run_batch([e.FIRST], 'candidate.sol')

    def test_unsuccessful_swap_rejected(self):
        self.engine.edit = lambda stats: [dict(stats[0], ok=1)]
        with self.assertRaises(RuntimeError):
            e.run_batch([e.FIRST], 'candidate.sol')

    def test_result_alignment(self):
        for edit in (lambda r: [], lambda r: r*2,
                     lambda r: [SimpleNamespace(seed=e.FIRST+1, edges={'submission': 1})]):
            self.engine.result_edit = edit
            with self.assertRaises(RuntimeError):
                e.run_batch([e.FIRST], 'candidate.sol')

    def test_nonfinite_edge(self):
        for value in (float('nan'), float('inf'), -float('inf')):
            self.engine.result_edit = lambda rows: [SimpleNamespace(seed=e.FIRST, edges={'submission': value})]
            with self.assertRaises(RuntimeError):
                e.run_batch([e.FIRST], 'candidate.sol')

    def test_invalid_gas(self):
        for value in (-1, 1.2, True):
            self.engine.edit = lambda stats: [dict(stats[0], max_ok_gas=value)]
            with self.assertRaises(RuntimeError):
                e.run_batch([e.FIRST], 'candidate.sol')

    def test_engine_exception_propagates(self):
        with patch.object(self.engine, 'run_batch', side_effect=RuntimeError('initialization failed')):
            with self.assertRaisesRegex(RuntimeError, 'initialization failed'):
                e.run_batch([e.FIRST], 'candidate.sol')


class ResumeTests(unittest.TestCase):
    def setUp(self):
        EngineTests.setUp(self)
        Path('scratch').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='scratch')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binding = dict(source='one', wheel='two', configs='three')
        self.prov = patch.object(e, 'provenance', return_value=self.binding)
        self.prov.start()
        self.addCleanup(self.prov.stop)

    def gen(self, name, n):
        return e.generate('strategy', self.root / (name+'.json'),
                          self.root / name / 'bank.json', 'cache', n, 400)

    def test_resume_equivalence(self):
        whole = self.gen('whole', 4)
        self.gen('split', 2)
        self.engine.calls.clear()
        split = self.gen('split', 2)
        self.assertEqual(self.engine.calls, [e.FIRST+2, e.FIRST+3])
        self.assertEqual(whole['rows'], split['rows'])
        bank = json.loads((self.root / 'split/bank.json').read_text())
        self.assertEqual(bank['remaining_seeds'], list(range(e.FIRST+4, e.LAST+1)))
        self.assertEqual(bank['status'], 'incomplete')
        self.assertTrue(bank['screening_only'])
        self.assertEqual(bank['rows'][0]['edge'], format(whole['rows'][0]['edge'], '.6f'))

    def test_stale_state_rejected_before_execution(self):
        self.gen('split', 1)
        self.engine.calls.clear()
        self.binding['wheel'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'stale checkpoint'):
            self.gen('split', 1)
        self.assertEqual(self.engine.calls, [])

    def test_interrupted_seed_resumes(self):
        self.gen('split', 1)
        with patch.object(self.engine, 'run_batch', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):
                self.gen('split', 2)
        state = e.load_state(self.root / 'split.json', self.binding)
        self.assertEqual(len(state['rows']), 1)
        self.assertEqual(len(state['failures']), 1)
        self.engine.calls.clear()
        state = self.gen('split', 2)
        self.assertEqual(self.engine.calls, [e.FIRST+1, e.FIRST+2])
        self.assertEqual(len(state['rows']), 3)

    def test_corrupt_checkpoint_rejected(self):
        original = self.gen('split', 2)
        for alter in (lambda s: s['rows'].reverse(),
                      lambda s: s['audits'].pop(),
                      lambda s: s['audits'][0]['gas_stats'][0].update(oog=1),
                      lambda s: s['rows'][0].update(after_swap_peak_gas=999999)):
            state = copy.deepcopy(original)
            alter(state)
            e.atomic_json(self.root/'split.json', state)
            with self.assertRaises(ValueError):
                e.load_state(self.root/'split.json', self.binding)

    def test_atomic_write_preserves_previous_on_serialization_error(self):
        path = self.root/'atomic.json'
        e.atomic_json(path, {'ok': 1})
        with self.assertRaises(ValueError):
            e.atomic_json(path, {'bad': float('nan')})
        self.assertEqual(json.loads(path.read_text()), {'ok': 1})


class FakeCompiler:
    CONTRACTS_SRC_DIR = None
    def __init__(self):
        self.calls = []
    def compile(self, source, contract_name='Strategy'):
        self.calls.append((source, contract_name))
        return SimpleNamespace(success=True, bytecode=source.encode())


class CompileTests(unittest.TestCase):
    def test_cache_reuses_compilation_and_invalidates_changed_source(self):
        Path('scratch').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir='scratch') as tmp:
            root = Path(tmp)
            (root/'VanillaStrategy.sol').write_text('vanilla')
            source = root/'candidate.sol'
            source.write_text('candidate')
            compiler = FakeCompiler()
            compiler.CONTRACTS_SRC_DIR = root
            with patch.object(e, '_compiler', return_value=compiler), patch.dict(e._COMPILED, {}, clear=True):
                first = e._compile(source)
                self.assertEqual(e._compile(source), first)
                self.assertEqual(len(compiler.calls), 2)
                source.write_text('candidate changed')
                self.assertNotEqual(e._compile(source), first)
                self.assertEqual(len(compiler.calls), 4)


if __name__ == '__main__':
    unittest.main()
