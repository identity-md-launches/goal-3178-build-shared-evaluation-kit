# Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9
# research/solved, solved-policy research.
"""Optional actual engine checks. Default discovery remains stdlib-only.

IMD_SCORE_ENGINE=1 python -B -m unittest discover -s tests -p test_score_integration.py -v
Run from the checkout with scratch/env.sh sourced; see docs/score.md.
"""
import contextlib
import io
import json
import math
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import score

CREDIT = ('Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9 '
          'research/solved, solved-policy research')
BANK = Path('baselines/engine/qt1-9700000-9700511.json')
AUDIT = BANK.with_name('qt1-audit.json')
WORK = Path('scratch/score-integration')
SOURCE_SHA = '62d40ccef0de99a385e332a36ddf9cbd1da73eda06bd7954492a9880cb91a572'
CANDIDATE_SHA = '64b62042f06d3b48d61c1387c0b6f08b0c291c9d2419a0ca65a5fc1d1cc19f96'


def expected_summary(rows, bank):
    """Independent two-pass sample SE, not score.summarize/statistics.stdev."""
    pairs = [(r['seed'], r['edge'] - bank.edges[r['seed']]) for r in rows]
    n = len(pairs)
    mean = math.fsum(d for _, d in pairs) / n
    se = math.sqrt(math.fsum((d - mean)**2 for _, d in pairs) / (n - 1) / n)
    return dict(n=n, mean_diff=mean, se=se,
                worst5=[dict(seed=s, diff=d) for s, d in sorted(pairs, key=lambda p: (p[1], p[0]))[:5]])


@unittest.skipUnless(os.environ.get('IMD_SCORE_ENGINE') == '1',
                     'optional real engine: source scratch/env.sh and set IMD_SCORE_ENGINE=1')
class RealEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import engine_eval
        cls.engine = engine_eval
        WORK.mkdir(parents=True, exist_ok=True)
        cls.bank = score.load_dataset(BANK, bank=True)
        cls.document = score.read_json(BANK)[0]
        cls.audit = {r['seed']: r for r in score.read_json(AUDIT)[0]['unrounded_rows']}
        if list(cls.bank.edges) != list(range(9700000, 9700512)):
            raise AssertionError('this integration requires the complete screening bank')
        source = Path('scratch/engine/amm-challenge/contracts/src/VanillaStrategy.sol')
        raw = source.read_bytes()
        if engine_eval.digest(source) != SOURCE_SHA or raw.count(b'contract VanillaStrategy') != 1:
            raise AssertionError('pinned VanillaStrategy source mismatch')
        cls.strategy = WORK / 'Strategy.sol'
        cls.strategy.write_bytes(raw.replace(b'contract VanillaStrategy', b'contract Strategy'))
        if engine_eval.digest(cls.strategy) != CANDIDATE_SHA:
            raise AssertionError('only the required contract name may change')
        # Benchmark FIRST, before any multi-seed engine work.
        start = time.perf_counter()
        rows = engine_eval.run_batch([9700000], cls.strategy)
        elapsed = time.perf_counter() - start
        cls.benchmark = dict(_credit=CREDIT, source_sha256=SOURCE_SHA,
                             candidate_sha256=CANDIDATE_SHA, seconds=elapsed,
                             projection128_seconds=128 * elapsed, rows=rows)
        engine_eval.atomic_json(WORK / 'test-benchmark.json', cls.benchmark)
        # This fingerprint is test evidence, never a new scorer/bank requirement.
        cls.provenance = engine_eval.provenance('scratch/reference/qt1/Strategy.sol', 'scratch/cache')
        cls.provenance.update(vanilla_source_sha256=SOURCE_SHA, candidate_sha256=CANDIDATE_SHA,
                              bank_sha256=engine_eval.digest(BANK), audit_sha256=engine_eval.digest(AUDIT),
                              score_sha256=engine_eval.digest('score.py'),
                              integration_test_sha256=engine_eval.digest(__file__))
        engine_eval.atomic_json(WORK / 'runtime.json', dict(_credit=CREDIT, provenance=cls.provenance))

    def test_live128_cli_worse_and_no_later_calls(self):
        if self.benchmark['projection128_seconds'] > 400:
            self.skipTest('projection exceeds 400s; collect bounded chunks and report offline replay separately')
        # Seed 9700000 was the benchmark. Offer ALL remaining seeds so a stop
        # genuinely prevents later rounds, rather than merely exhausting 128.
        selected = dict(self.document, rows=self.document['rows'][1:])
        path = WORK / 'next-bank.json'
        self.engine.atomic_json(path, selected)
        bank = score.load_dataset(path, bank=True)
        captured, native_requests = [], []
        original_one = self.engine._one

        def observe_one(engine, codes, seed):
            native_requests.append(seed)
            row, audit = original_one(engine, codes, seed)
            captured.append(row)
            # Checkpoint after each real native singleton, without changing edges.
            self.engine.atomic_json(WORK / 'live-progress.json',
                                    dict(_credit=CREDIT, provenance=self.provenance,
                                         rows=captured, native_requests=native_requests,
                                         complete=False))
            return row, audit

        out, err = io.StringIO(), io.StringIO()
        started = time.perf_counter()
        with patch.object(self.engine, '_one', side_effect=observe_one), \
                patch.object(self.engine, 'run_batch', wraps=self.engine.run_batch) as calls, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = score.main(['--baseline', str(path), '--engine', '--policy', 'VanillaStrategy',
                             '--strategy-path', str(self.strategy)])
        seconds = time.perf_counter() - started
        (WORK / 'live-rounds.jsonl').write_text(out.getvalue())
        (WORK / 'live-status.jsonl').write_text(err.getvalue())
        self.assertEqual(rc, 0, err.getvalue())
        requested = list(range(9700001, 9700129))
        calls.assert_called_once_with(requested, str(self.strategy))
        self.assertEqual(native_requests, requested)
        summaries = [json.loads(s) for s in out.getvalue().splitlines()]
        self.assertEqual(len(summaries), 1)
        got = summaries[0]
        self.assertEqual(set(got), {'n', 'mean_diff', 'se', 'worst5'})
        want = expected_summary(captured, bank)
        self.assertEqual(got['n'], 128)
        self.assertAlmostEqual(got['mean_diff'], want['mean_diff'], places=12)
        self.assertAlmostEqual(got['se'], want['se'], places=12)
        self.assertEqual(got['worst5'], want['worst5'])
        self.assertLess(got['mean_diff'] + 3 * got['se'], 0)
        metadata, status = map(json.loads, err.getvalue().splitlines())
        self.assertEqual(metadata['candidate']['policy'], 'VanillaStrategy')
        self.assertEqual(metadata['baseline']['policy'], 'qt1')
        self.assertEqual(status['status'], dict(reason='worse', n=128, available=511))
        self.engine.atomic_json(WORK / 'live-result.json',
                                dict(_credit=CREDIT, coverage='live128 CLI; no replay',
                                     seconds=seconds, summary=got, status=status['status'],
                                     adapter_requests=[requested], native_requests=native_requests,
                                     provenance=self.provenance, rows=captured, complete=True))

    def test_qt1_three_actual_bank_seeds(self):
        seeds = [9700000, 9700128, 9700511]
        started = time.perf_counter()
        rows = self.engine.run_batch(seeds, 'scratch/reference/qt1/Strategy.sol')
        self.assertEqual([r['seed'] for r in rows], seeds)
        rounded = {r['seed']: r for r in self.document['rows']}
        for row in rows:
            self.assertEqual(row, self.audit[row['seed']])  # unrounded edge AND gas
            self.assertEqual(format(row['edge'], '.6f'), rounded[row['seed']]['edge'])
            self.assertEqual(row['after_swap_peak_gas'], rounded[row['seed']]['after_swap_peak_gas'])
        self.engine.atomic_json(WORK / 'qt1-check.json',
                                dict(_credit=CREDIT, seconds=time.perf_counter()-started,
                                     rows=rows, exact_unrounded_and_six_decimal=True))

    def test_saved_qt1_above_bar_and_exact_boundary(self):
        # OFFLINE replay of committed audit rows; no engine invocation.
        seeds = list(self.bank.edges)[:128]
        bank = score.Dataset('engine', 'qt1', {s: self.bank.edges[s] for s in seeds},
                             self.bank.provenance)
        rows = [self.audit[s] for s in seeds]
        edges = score.validate_rows(rows, 'engine', requested=seeds)
        candidate = score.RowCandidate(score.Dataset('engine', 'qt1-audit-replay', edges, {}))
        emitted = []
        with patch.object(self.engine, 'run_batch', side_effect=AssertionError('offline replay')):
            score.evaluate(bank, candidate, emit=emitted.append)
            summary = emitted[0]
            self.assertGreaterEqual(summary['mean_diff'] + 3 * summary['se'], 0)
            lower = summary['mean_diff'] - 3 * summary['se']
            boundary = score.evaluate(bank, candidate, bar=lower)
            above = score.evaluate(bank, candidate, bar=math.nextafter(lower, -math.inf))
        self.assertEqual(boundary['reason'], 'exhausted')
        self.assertEqual(above['reason'], 'above_bar')
        self.engine.atomic_json(WORK / 'replay-boundary.json',
                                dict(_credit=CREDIT, coverage='offline committed qt1 audit replay',
                                     seeds=seeds, summary=summary, exact_bar=lower,
                                     boundary=boundary, above=above))


if __name__ == '__main__':
    unittest.main()
