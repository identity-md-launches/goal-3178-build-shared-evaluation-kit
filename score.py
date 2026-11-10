#!/usr/bin/env python3
# Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9,
# research/solved, solved-policy research.
"""Paired 128-seed screening; stdlib-only offline scoring. See docs/score.md."""
import argparse
from dataclasses import dataclass
import hashlib
import importlib
import json
import math
from pathlib import Path
import re
import statistics
import sys

ROUND_SIZE = 128
ENGINE_MIN, ENGINE_MAX = 9700000, 9700511
BANK_POLICIES = {'replica': {'qt1-ref', 'TwoComp', 'COMBO', 'HA'}, 'engine': {'qt1'}}
FIXED_EDGE = re.compile(r'-?[0-9]+\.[0-9]{6}\Z')


def finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f'{label} must be a finite number')
    try:
        value = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f'{label} must be finite') from exc
    if not math.isfinite(value):
        raise ValueError(f'{label} must be finite')
    return value


def backend_ok(backend):
    if not isinstance(backend, str) or backend not in BANK_POLICIES:
        raise ValueError('backend must be replica or engine')


def seed_ok(seed, backend):
    if type(seed) is not int:
        raise ValueError('seed must be an integer (not bool)')
    if backend == 'engine' and not ENGINE_MIN <= seed <= ENGINE_MAX:
        raise ValueError(f'engine seed {seed} outside {ENGINE_MIN}..{ENGINE_MAX}')


def validate_rows(rows, backend, *, bank=False, requested=None):
    """Consume a finite row iterable and return an exact-seed edge mapping."""
    backend_ok(backend)
    result = {}
    previous = None
    for row in rows:
        if not isinstance(row, dict) or 'seed' not in row or 'edge' not in row:
            raise ValueError('each row needs seed and edge')
        seed = row['seed']
        seed_ok(seed, backend)
        if seed in result:
            raise ValueError(f'duplicate seed {seed}')
        if bank and previous is not None and seed <= previous:
            raise ValueError('bank rows must be in ascending seed order')
        previous = seed
        if bank and (not isinstance(row['edge'], str) or not FIXED_EDGE.fullmatch(row['edge'])):
            raise ValueError(f'bank edge for {seed} must be a six-decimal string')
        if backend == 'engine':
            gas = row.get('after_swap_peak_gas')
            if type(gas) is not int or gas < 0:
                raise ValueError(f'engine row {seed} needs nonnegative integer after_swap_peak_gas')
        # Explicit row metadata may not contradict the selected source.
        if row.get('backend', backend) != backend or row.get('metric', 'edge') != 'edge':
            raise ValueError(f'incompatible row backend/metric for {seed}')
        result[seed] = finite(row['edge'], f'edge for {seed}')
    if requested is not None and set(result) != set(requested):
        missing = sorted(set(requested) - set(result))
        extra = sorted(set(result) - set(requested))
        raise ValueError(f'requested seed mismatch: missing={missing}, extra={extra}')
    return result


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f'duplicate JSON key {key}')
        obj[key] = value
    return obj


def read_json(path):
    raw = Path(path).read_bytes()
    def reject_constant(value):
        raise ValueError(f'nonfinite JSON constant {value}')
    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant), hashlib.sha256(raw).hexdigest()


@dataclass
class Dataset:
    backend: str
    policy: str
    edges: dict
    provenance: dict


def load_dataset(path, *, bank=False):
    obj, digest = read_json(path)
    if not isinstance(obj, dict) or not obj or next(iter(obj)) != '_credit':
        raise ValueError('row document must be an object with _credit first')
    if not isinstance(obj['_credit'], str) or not obj['_credit'].strip():
        raise ValueError('_credit must be nonempty text')
    backend = obj.get('backend')
    backend_ok(backend)
    policy = obj.get('policy')
    if not isinstance(policy, str) or not policy.strip():
        raise ValueError('policy identity must be nonempty text')
    if bank and policy not in BANK_POLICIES[backend]:
        raise ValueError('incompatible baseline backend/policy')
    provenance = obj.get('provenance')
    if not isinstance(provenance, dict) or not provenance:
        raise ValueError('provenance must be a nonempty object')
    for metadata in (obj, provenance):
        if metadata.get('metric', 'edge') != 'edge' or metadata.get('backend', backend) != backend:
            raise ValueError('incompatible source backend/metric; only edge is supported')
    if not isinstance(obj.get('rows'), list):
        raise ValueError('rows must be a list')
    edges = validate_rows(obj['rows'], backend, bank=bank)
    return Dataset(backend, policy, edges, {
        'path': str(path), 'sha256': digest, 'policy': policy, 'backend': backend,
        'metric': 'edge', 'source': provenance,
        'precision': 'six-decimal bank' if bank else 'as supplied in candidate file',
    })


class RowCandidate:
    def __init__(self, dataset):
        self.backend = dataset.backend
        self.edges = dataset.edges
        self.provenance = dataset.provenance

    def preflight(self, seeds):
        if set(self.edges) != set(seeds):
            raise ValueError('candidate file seeds must exactly match baseline seeds (missing/extra)')

    def fetch(self, seeds):
        return {seed: self.edges[seed] for seed in seeds}


class LiveCandidate:
    """Lazy import and exactly one backend call per requested round."""
    def __init__(self, backend, policy, *, strategy_path=None, head_params=None, adapter=None):
        backend_ok(backend)
        if not isinstance(policy, str) or not policy.strip():
            raise ValueError('candidate policy identity is required')
        if backend == 'engine' and head_params is not None:
            raise ValueError('head_params is only valid for replica')
        if backend == 'replica' and strategy_path is not None:
            raise ValueError('strategy_path is only valid for engine')
        if backend == 'engine' and not strategy_path:
            raise ValueError('engine requires strategy_path')
        if adapter and not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*', adapter):
            raise ValueError('adapter must be module:function')
        self.backend, self.policy = backend, policy
        self.strategy_path, self.head_params, self.adapter = strategy_path, head_params, adapter
        self.provenance = {'backend': backend, 'policy': policy, 'metric': 'edge',
                           'adapter': adapter or ('ammfast:run_batch' if backend == 'replica' else 'engine_eval:run_batch'),
                           'head_params': head_params, 'strategy_path': strategy_path,
                           'precision': 'unrounded live values'}

    def preflight(self, seeds):
        for seed in seeds:
            seed_ok(seed, self.backend)

    def fetch(self, seeds):
        # Validate before import/call as well as before the entire evaluation.
        self.preflight(seeds)
        target = self.provenance['adapter']
        module, function = target.split(':')
        runner = getattr(importlib.import_module(module), function)
        if self.backend == 'replica':
            rows = runner(list(seeds), self.policy, head_params=self.head_params)
        else:
            rows = runner(list(seeds), self.strategy_path)
        return validate_rows(rows, self.backend, requested=seeds)


def summarize(pairs):
    if not pairs:
        return {'n': 0, 'mean_diff': None, 'se': None, 'worst5': []}
    diffs = [diff for seed, diff in pairs]
    mean = finite(statistics.mean(diffs), 'mean difference')
    se = finite(statistics.stdev(diffs) / math.sqrt(len(diffs)), 'standard error') if len(diffs) > 1 else None
    worst = sorted(pairs, key=lambda pair: (pair[1], pair[0]))[:5]
    return {'n': len(pairs), 'mean_diff': mean, 'se': se,
            'worst5': [{'seed': seed, 'diff': diff} for seed, diff in worst]}


def evaluate(bank, candidate, *, bar=None, emit=None):
    """Return status; emit only cumulative round summaries. Never prefetch."""
    if bar is not None:
        bar = finite(bar, 'bar')
    backend_ok(bank.backend)
    if candidate.backend != bank.backend:
        raise ValueError('candidate and baseline backends differ')
    seeds = sorted(bank.edges)
    for seed in seeds:
        seed_ok(seed, bank.backend)
        finite(bank.edges[seed], 'baseline edge')
    candidate.preflight(seeds)
    pairs = []
    summary = summarize(pairs)
    reason = 'exhausted'
    if not seeds and emit:
        emit(summary)
    for start in range(0, len(seeds), ROUND_SIZE):
        requested = seeds[start:start + ROUND_SIZE]
        edges = candidate.fetch(requested)
        if set(edges) != set(requested):
            raise ValueError('candidate returned missing/extra seeds')
        pairs.extend((seed, finite(finite(edges[seed], 'candidate edge') - bank.edges[seed], 'paired difference'))
                     for seed in requested)
        summary = summarize(pairs)
        if emit:
            emit(summary)
        if summary['se'] is not None:
            if summary['mean_diff'] + 3 * summary['se'] < 0:
                reason = 'worse'
                break
            if bar is not None and summary['mean_diff'] - 3 * summary['se'] > bar:
                reason = 'above_bar'
                break
    return {'reason': reason, 'n': summary['n'], 'available': len(seeds)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True, help='contract bank JSON')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--rows', help='candidate row document JSON')
    mode.add_argument('--replica', action='store_true')
    mode.add_argument('--engine', action='store_true')
    parser.add_argument('--policy', help='candidate identity (required for live mode)')
    parser.add_argument('--strategy-path', help='engine candidate strategy path')
    parser.add_argument('--head-params', help='JSON file passed to replica runner')
    parser.add_argument('--adapter', help='trusted module:function, with the selected backend signature')
    parser.add_argument('--bar', type=float, help='optional strict above-bar stopping')
    args = parser.parse_args(argv)
    try:
        if args.bar is not None:
            finite(args.bar, 'bar')
        bank = load_dataset(args.baseline, bank=True)
        if args.rows:
            if any((args.policy, args.strategy_path, args.head_params, args.adapter)):
                raise ValueError('live-only options cannot accompany --rows')
            candidate = RowCandidate(load_dataset(args.rows))
        else:
            if args.engine and args.head_params:
                raise ValueError('--head-params is replica-only')
            head = read_json(args.head_params)[0] if args.head_params else None
            candidate = LiveCandidate('replica' if args.replica else 'engine', args.policy,
                                      strategy_path=args.strategy_path, head_params=head, adapter=args.adapter)
        # No optional module imports or simulation until all known inputs pass.
        if candidate.backend != bank.backend:
            raise ValueError('candidate and baseline backends differ')
        candidate.preflight(sorted(bank.edges))
        print(json.dumps({'baseline': bank.provenance, 'candidate': candidate.provenance}, allow_nan=False), file=sys.stderr, flush=True)
        status = evaluate(bank, candidate, bar=args.bar,
                          emit=lambda row: print(json.dumps(row, allow_nan=False), flush=True))
        print(json.dumps({'status': status}), file=sys.stderr, flush=True)
        return 0
    except KeyboardInterrupt:
        print('score: interrupted; output may be partial', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'score: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
