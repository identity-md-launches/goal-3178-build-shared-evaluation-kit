#!/usr/bin/env python3
# Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9,
# research/solved, solved-policy research.
"""Offline check of the replica banks and baselines/replica/validation.json.

    python3 -B baselines/replica/validate.py                 # standard library only, no simulation
    python  -B baselines/replica/validate.py --recompute 8   # also re-run 8 seeds per bank with ammfast

It verifies, without trusting generate.py: every bound file's SHA256; that the bindings cover the
ammfast sources, the generator and this validator, the reference inventory and all nine banks, and
not validation.json itself; each bank's contract schema, seed range, order and six-decimal edges;
the 384 gate pairs and 576 spot pairs (exact pair sets, recomputed abs_error <= 1e-9, spot edges
equal to the bank rows once rounded, gate and spot agreeing where they overlap); and the timings
shape.  Exit 0 only when everything holds and ``complete`` is true.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
TAYLOR_COMMIT = '4812a6382311624e1506aca131ca35c378f930d9'
POLICIES = ('qt1-ref', 'TwoComp', 'COMBO')
BANK_RANGES = ((60000, 61023), (64000, 65023), (10000, 12047))
GATE_SEEDS = tuple(range(10000, 10064)) + tuple(range(64000, 64064))
SPOT_COUNT = 64
TOLERANCE = 1e-9
OUT = 'baselines/replica'
VALIDATION = OUT + '/validation.json'
FIXED_EDGE = re.compile(r'-?[0-9]+\.[0-9]{6}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')
REFERENCE_REQUIRED = (
    'reference/sources.json',
    'reference/envelopes/taylor__replica2.py.json',
    'reference/envelopes/qt1__qmref.py.json',
    'reference/envelopes/qt1__blob.bin.json',
    'reference/envelopes/twocomp__twocomp.py.json',
    'reference/envelopes/combo__combo.py.json',
    'reference/envelopes/numba__fastsim.py.json',
)
PAIR_KEYS = ['policy', 'seed', 'cpython_edge', 'ammfast_edge', 'abs_error']
BANK_KEYS = ['_credit', 'backend', 'policy', 'provenance', 'rows']


def bank_names():
    return [(policy, start, end, f'{OUT}/{policy}-{start}-{end}.json')
            for policy in POLICIES for start, end in BANK_RANGES]


def gate_pairs():
    return [(policy, seed) for policy in POLICIES for seed in GATE_SEEDS]


def spot_pairs():
    return [(policy, seed) for policy in POLICIES for start, _ in BANK_RANGES
            for seed in range(start, start + SPOT_COUNT)]


def _unique(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f'duplicate JSON key {key}')
        obj[key] = value
    return obj


def _constant(value):
    raise ValueError(f'non-finite JSON constant {value}')


def read_json(path):
    return json.loads(Path(path).read_bytes(), object_pairs_hook=_unique, parse_constant=_constant)


def credit_ok(text):
    return (isinstance(text, str) and 'Taylor' in text and 'gtmcknight' in text
            and TAYLOR_COMMIT in text and 'research/solved' in text)


def check_bank(root, policy, start, end, name, errors):
    """Contract checks for one bank; returns {seed: edge string} (empty when unreadable)."""
    try:
        doc = read_json(root / name)
    except (OSError, ValueError) as exc:
        errors.append(f'{name}: unreadable ({exc})')
        return {}
    if not isinstance(doc, dict) or list(doc) != BANK_KEYS:
        errors.append(f'{name}: fields must be exactly {BANK_KEYS} with _credit first')
        return {}
    if not credit_ok(doc['_credit']):
        errors.append(f'{name}: _credit must name Taylor (gtmcknight), the commit and research/solved')
    if doc['backend'] != 'replica' or doc['policy'] != policy:
        errors.append(f'{name}: backend/policy mismatch')
    provenance = doc['provenance']
    if not isinstance(provenance, dict):
        errors.append(f'{name}: provenance must be an object')
        provenance = {}
    if provenance.get('seed_range') != [start, end] or provenance.get('row_count') != end - start + 1:
        errors.append(f'{name}: provenance seed_range/row_count mismatch')
    if provenance.get('source_commit') != TAYLOR_COMMIT:
        errors.append(f'{name}: provenance source_commit mismatch')
    for key in ('sources', 'implementation', 'generation_command', 'backend_runtime', 'rounding', 'validation'):
        if not provenance.get(key):
            errors.append(f'{name}: provenance lacks {key}')
    rows = doc['rows']
    edges = {}
    if not isinstance(rows, list):
        errors.append(f'{name}: rows must be a list')
        return {}
    for index, row in enumerate(rows):
        if (not isinstance(row, dict) or list(row) != ['seed', 'edge'] or type(row['seed']) is not int
                or not isinstance(row['edge'], str) or not FIXED_EDGE.match(row['edge'])
                or not math.isfinite(float(row['edge']))):
            errors.append(f'{name}: row {index} is not {{"seed": int, "edge": six-decimal string}}')
            return {}
        edges[row['seed']] = row['edge']
    if [row['seed'] for row in rows] != list(range(start, end + 1)):
        errors.append(f'{name}: seeds must be exactly {start}..{end}, ascending and unique')
    return edges


def check_pairs(rows, expected, label, errors):
    """Exact pair set and order, finite floats, honest abs_error within tolerance."""
    result = {}
    if not isinstance(rows, list) or len(rows) != len(expected):
        errors.append(f'{label}: needs exactly {len(expected)} rows, has {len(rows) if isinstance(rows, list) else "none"}')
        return result
    for row, (policy, seed) in zip(rows, expected):
        if not isinstance(row, dict) or list(row) != PAIR_KEYS or (row['policy'], row['seed']) != (policy, seed):
            errors.append(f'{label}: row for {policy} {seed} missing, misordered or with other fields')
            return {}
        values = [row['cpython_edge'], row['ammfast_edge'], row['abs_error']]
        if any(type(value) is not float or not math.isfinite(value) for value in values):
            errors.append(f'{label}: non-finite or non-float value for {policy} {seed}')
            continue
        if row['abs_error'] != abs(row['cpython_edge'] - row['ammfast_edge']):
            errors.append(f'{label}: abs_error for {policy} {seed} is not |cpython_edge - ammfast_edge|')
        if not row['abs_error'] <= TOLERANCE:
            errors.append(f'{label}: {policy} {seed} abs_error {row["abs_error"]!r} exceeds {TOLERANCE}')
        result[(policy, seed)] = row
    return result


def check(root=ROOT, recompute=0):
    """Return (errors, summary).  No error and summary['complete'] is the pass condition."""
    root = Path(root)
    errors = []
    summary = {'complete': False}
    try:
        doc = read_json(root / VALIDATION)
    except (OSError, ValueError) as exc:
        return [f'{VALIDATION}: unreadable ({exc})'], summary
    if not isinstance(doc, dict) or not doc or next(iter(doc)) != '_credit' or not credit_ok(doc['_credit']):
        errors.append(f'{VALIDATION}: _credit must come first and name Taylor (gtmcknight), the commit and research/solved')
    if type(doc.get('complete')) is not bool:
        errors.append(f'{VALIDATION}: complete must be a boolean')
    summary['complete'] = doc.get('complete') is True

    # ---- bindings: every bound file matches; required coverage; no self-hash
    bindings = doc.get('bindings')
    if not isinstance(bindings, dict) or not bindings:
        errors.append(f'{VALIDATION}: bindings must be a nonempty object')
        bindings = {}
    if VALIDATION in bindings:
        errors.append(f'{VALIDATION}: must not bind its own hash')
    for name, digest in bindings.items():
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name or not isinstance(digest, str) or not SHA.match(digest):
            errors.append(f'bindings: {name!r} is not a repository-relative path with a sha256')
            continue
        target = root / name
        if not target.is_file():
            errors.append(f'bindings: {name} is missing')
        elif hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            errors.append(f'bindings: {name} does not match its recorded sha256')
    required = sorted('ammfast/' + p.name for p in (root / 'ammfast').glob('*.py'))
    required += [OUT + '/generate.py', OUT + '/validate.py', *REFERENCE_REQUIRED]
    if summary['complete']:
        required += [name for _, _, _, name in bank_names()]
    for name in required:
        if name not in bindings:
            errors.append(f'bindings: {name} is not covered')
    summary['bound_files'] = len(bindings)

    # ---- banks
    banks = {}
    rows_total = 0
    for policy, start, end, name in bank_names():
        if name not in bindings and not summary['complete']:
            continue
        edges = check_bank(root, policy, start, end, name, errors)
        banks[(policy, start)] = edges
        rows_total += len(edges)
        try:
            implementation = read_json(root / name)['provenance']['implementation']
            if any(bindings.get(k) != v for k, v in implementation.items()) or not implementation:
                errors.append(f'{name}: provenance implementation digests differ from validation bindings')
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    summary['banks'] = len(banks)
    summary['bank_rows'] = rows_total
    if summary['complete'] and rows_total != 12288:
        errors.append(f'banks: {rows_total} rows, expected 12288')

    # ---- gate and spot evidence
    gates = check_pairs(doc.get('gates384'), gate_pairs(), 'gates384', errors)
    spots = check_pairs(doc.get('spots576'), spot_pairs(), 'spots576', errors)
    for (policy, seed), row in spots.items():
        start = next(s for s, e in BANK_RANGES if s <= seed <= e)
        edges = banks.get((policy, start), {})
        if edges and edges.get(seed) != format(row['ammfast_edge'], '.6f'):
            errors.append(f'spots576: {policy} {seed} ammfast_edge does not round to the bank row')
        gate = gates.get((policy, seed))
        if gate and (gate['cpython_edge'] != row['cpython_edge'] or gate['ammfast_edge'] != row['ammfast_edge']):
            errors.append(f'spots576: {policy} {seed} disagrees with the gate row (run vs run_batch)')
    summary['gate_pairs'] = len(gates)
    summary['spot_pairs'] = len(spots)
    summary['distinct_comparisons'] = len(set(gates) | set(spots))
    summary['gate_max_abs_error'] = max((row['abs_error'] for row in gates.values()), default=None)
    summary['spot_max_abs_error'] = max((row['abs_error'] for row in spots.values()), default=None)

    # ---- timings
    timings = doc.get('timings')
    ok = isinstance(timings, dict) and list(timings) == ['runs_per_second_per_core', 'cold_jit_seconds']
    if ok:
        rates = timings['runs_per_second_per_core']
        ok = (isinstance(rates, dict) and sorted(rates) == sorted(POLICIES)
              and all(type(v) is float and math.isfinite(v) and v > 0 for v in rates.values())
              and type(timings['cold_jit_seconds']) is float and math.isfinite(timings['cold_jit_seconds'])
              and timings['cold_jit_seconds'] > 0)
    if not ok:
        errors.append('timings: need runs_per_second_per_core for the three policies and cold_jit_seconds, positive floats')
    if not summary['complete']:
        errors.append(f'{VALIDATION}: complete is false; see its coverage for what is missing')

    # ---- optional recomputation with the installed ammfast
    if recompute and not errors:
        sys.path.insert(0, str(root))
        import ammfast
        if ammfast.implementation_inventory() != {k: v for k, v in bindings.items() if k.startswith('ammfast/')}:
            errors.append('recompute: imported ammfast is not the bound implementation')
        checked = 0
        for policy, start, end, name in bank_names():
            step = max(1, (end - start + 1) // recompute)
            seeds = list(range(start, end + 1, step))[:recompute]
            for row in ammfast.run_batch(seeds, policy):
                checked += 1
                if format(row['edge'], '.6f') != banks[(policy, start)][row['seed']]:
                    errors.append(f'recompute: {name} seed {row["seed"]} differs from ammfast now')
        summary['recomputed_rows'] = checked
    summary['ok'] = not errors
    return errors, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=ROOT, help='repository root to check')
    parser.add_argument('--recompute', type=int, default=0, metavar='N',
                        help='also re-run N evenly spaced seeds per bank with ammfast (needs numba)')
    args = parser.parse_args(argv)
    if args.recompute < 0:
        parser.error('--recompute must be >= 0')
    errors, summary = check(args.root, args.recompute)
    for error in errors:
        print(f'validate: {error}', file=sys.stderr)
    print(json.dumps(summary))
    return 0 if not errors else 1


if __name__ == '__main__':
    sys.exit(main())
