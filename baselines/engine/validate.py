"""Validate bank/audit provenance and independently rerun spread screening seeds.

Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9
research/solved, solved-policy research.
"""
import argparse
import json
from pathlib import Path
import re
import sys
import tarfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import engine_eval as e


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate(bank_path, strategy, cache, rerun_count=5):
    started = time.perf_counter()
    from reference.materialize import inventory
    refs = inventory()
    origin = next(row for row in refs['files'] if row['path'] == 'qt1/Strategy.sol')
    bank_path = Path(bank_path)
    bank = json.loads(bank_path.read_text())
    audit_path = bank_path.with_name('qt1-audit.json')
    audit = json.loads(audit_path.read_text())
    require(next(iter(bank)) == '_credit' and next(iter(audit)) == '_credit', 'credit must be first')
    binding = e.provenance(strategy, cache)
    require(binding == bank['provenance'], 'bank provenance differs from installed pinned runtime')
    require(bank['backend'] == 'engine' and bank['policy'] == 'qt1', 'wrong bank backend/policy')
    require(bank['screening_only'] is True, 'missing screening-only marker')
    rows = bank['rows']
    raw = audit['unrounded_rows']
    require(0 < len(rows) <= 512, 'bank count outside 1..512')
    require([r['seed'] for r in rows] == list(range(e.FIRST, e.FIRST + len(rows))), 'non-prefix bank')
    remaining = list(range(e.FIRST + len(rows), e.LAST + 1))
    require(bank['remaining_seeds'] == remaining, 'incorrect remaining seeds')
    require(bank['status'] == ('incomplete' if remaining else 'complete'), 'incorrect completeness')
    require(len(raw) == len(rows) == len(audit['audits']), 'bank/audit lengths differ')
    for row, original, evidence in zip(rows, raw, audit['audits']):
        require(re.fullmatch(r'-?[0-9]+\.[0-9]{6}', row['edge']), 'edge must be a 6dp string')
        require(row == dict(original, edge=format(original['edge'], '.6f')), 'rounded/raw row mismatch')
        require(evidence['seed'] == row['seed'], 'audit seed mismatch')
        swaps = [s for s in evidence['gas_stats'] if s['role'] == 'Submission' and s['callback'] == 'afterSwap']
        require(bool(swaps) and sum(s['calls'] for s in swaps) > 0, 'missing swap counters')
        require(all(s['calls'] == s['ok'] for s in swaps), 'failed swap calls')
        require(max(s['max_ok_gas'] for s in swaps) == row['after_swap_peak_gas'], 'gas metric mismatch')
        require(not any(s[k] for s in evidence['gas_stats'] for k in e.ERRORS), 'nonzero failure counter')
    # Compare installed source bytes to the actual hash-verified upstream archive.
    archive_name = next(n for n in binding['downloads'] if n.endswith('.tar.gz'))
    archive_sha = binding['downloads'][archive_name]['sha256']
    archive_path = Path(cache)/'sha256'/archive_sha/archive_name
    with tarfile.open(archive_path) as archive:
        prefix = 'amm-challenge-' + e.ENGINE_COMMIT + '/'
        for name, expected in binding['installed_source_sha256'].items():
            handle = archive.extractfile(prefix + name)
            require(handle is not None and e.sha256(handle.read()) == expected,
                    'installed source differs from upstream archive: ' + name)
    # Choose spread seeds only from rows actually present, never from future coverage.
    indices = sorted({round(i*(len(raw)-1)/(rerun_count-1)) for i in range(rerun_count)})
    seeds = [raw[i]['seed'] for i in indices]
    t = time.perf_counter()
    repeat = e.run_batch(seeds, strategy)
    rerun_seconds = time.perf_counter()-t
    checks = []
    for index, row in zip(indices, repeat):
        require(row == raw[index], f'independent rerun mismatch at {row["seed"]}')
        checks.append(dict(seed=row['seed'], exact_unrounded_edge_equal=True,
                           exact_after_swap_peak_gas_equal=True, **{k:v for k,v in row.items() if k!='seed'}))
    return dict(_credit=e.CREDIT, status=bank['status'], screening_only=True, count=len(rows),
                remaining_seeds=remaining, bank_sha256=e.digest(bank_path), audit_sha256=e.digest(audit_path),
                validator_sha256=e.digest(__file__), argv=sys.argv,
                reference_origin=origin, reference_goal_commit=refs['starter_commit'],
                taylor_commit=refs['taylor_commit'],
                reference_inventory_sha256=e.digest(e.ROOT / 'reference/sources.json'),
                upstream_source_files_checked=len(binding['installed_source_sha256']),
                config_fields_compared=binding['config_fields_compared'],
                singleton_worker_argument=8, all_gas_counters_valid=True,
                failed_seed_attempts=bank['failures'], reruns=checks,
                rerun_seconds=rerun_seconds, validation_seconds=time.perf_counter()-started,
                tested_platform='linux-x86_64', untested_platforms=['macos-arm64 (engine unsupported)', 'Windows', 'other platforms'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bank', default='baselines/engine/qt1-9700000-9700511.json')
    parser.add_argument('--strategy', default='scratch/reference/qt1/Strategy.sol')
    parser.add_argument('--cache', default='scratch/cache')
    parser.add_argument('--output', default='baselines/engine/validation.json')
    args = parser.parse_args()
    result = validate(args.bank, args.strategy, args.cache)
    e.atomic_json(args.output, result)
    print(json.dumps({k: v for k, v in result.items() if k != 'remaining_seeds'}))


if __name__ == '__main__':
    main()
