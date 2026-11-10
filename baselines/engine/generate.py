"""Resumable qt1 bank, isolated singleton calls in separate worker processes.

Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9
research/solved. Each native call still uses worker argument 8.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import fcntl
import json
import multiprocessing
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import engine_eval as e


def worker(codes, seed):
    # Spawned process: native counters cannot be shared with another seed worker.
    return e._one(e._runtime(), codes, seed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strategy', default='scratch/reference/qt1/Strategy.sol')
    parser.add_argument('--checkpoint', default='scratch/engine-bank/progress.json')
    parser.add_argument('--output', default='baselines/engine/qt1-9700000-9700511.json')
    parser.add_argument('--cache', default='scratch/cache')
    parser.add_argument('--max-seeds', type=int, default=32)
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--budget', type=int, default=400)
    args = parser.parse_args()
    if not 1 <= args.max_seeds <= 512 or not 1 <= args.jobs <= 8 or not 1 <= args.budget <= 420:
        parser.error('max-seeds 1..512, jobs 1..8, budget 1..420 required')
    started = time.perf_counter()
    binding = e.provenance(args.strategy, args.cache)
    checkpoint = Path(args.checkpoint)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    with open(str(checkpoint) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = e.load_state(checkpoint, binding)
        state['commands'].append(dict(argv=sys.argv, driver_sha256=e.digest(__file__),
                                      jobs=args.jobs, budget=args.budget))
        codes = e._compile(args.strategy)
        seeds = e.validate_seeds(range(e.FIRST + len(state['rows']),
                                      min(e.LAST + 1, e.FIRST + len(state['rows']) + args.max_seeds)))
        with ProcessPoolExecutor(max_workers=args.jobs,
                                 mp_context=multiprocessing.get_context('spawn')) as pool:
            for offset in range(0, len(seeds), args.jobs):
                reserve = max(45, 3 * max((a['seconds'] for a in state['audits']), default=10))
                if time.perf_counter() - started + reserve >= args.budget:
                    break
                group = seeds[offset:offset + args.jobs]
                futures = [pool.submit(worker, codes, seed) for seed in group]
                for seed, future in zip(group, futures):
                    try:
                        row, audit = future.result()
                        if row['seed'] != seed:
                            raise RuntimeError('worker seed mismatch')
                    except Exception as exc:
                        state['failures'].append(dict(seed=seed, error=f'{type(exc).__name__}: {exc}'))
                        e.atomic_json(checkpoint, state)
                        e.publish(state, args.output)
                        raise
                    state['rows'].append(row)
                    state['audits'].append(dict(seed=seed, **audit))
                    e.atomic_json(checkpoint, state)
                    print(json.dumps(dict(n=len(state['rows']), **row, seconds=audit['seconds'])), flush=True)
        e.atomic_json(checkpoint, state)
        e.publish(state, args.output)
        print(json.dumps(dict(command_seconds=time.perf_counter()-started, n=len(state['rows']))), flush=True)


if __name__ == '__main__':
    main()
