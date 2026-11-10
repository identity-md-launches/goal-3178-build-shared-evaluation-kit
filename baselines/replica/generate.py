#!/usr/bin/env python3
# Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9,
# research/solved, solved-policy research.
"""Benchmark, gate, generate and spot-check the replica banks.  See docs/replica-banks.md.

    python -B baselines/replica/generate.py [--stage all] [--workers 4] [--budget 420]

Stages run in this order and each one resumes from its checkpoint:
  bench  CPython seconds/run, cold and warm first runs, single-worker runs/second, for all three
  gate   384 pairs (3 policies x 10000..10063 + 64000..64063): ammfast.run vs CPython replica2
  banks  only after the gate passes: 3 policies x 60000..61023, 64000..65023, 10000..12047
  spot   first 64 seeds of each of the nine banks vs CPython (576 pairs, 384 shared with the gate)
  write  the nine bank files and validation.json

Checkpoints live in <state>/<binding>/, where <binding> digests the ammfast sources, the reference
inventory and the runtime versions.  A different binding gets a new, empty directory, and a row
whose recorded binding differs is refused, so stale state can never reach a bank.  When the budget
runs out the command exits 75; run the same command again to continue.
"""
import argparse
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CREDIT = ('Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9, '
          'research/solved, solved-policy research.')
TAYLOR_COMMIT = '4812a6382311624e1506aca131ca35c378f930d9'
POLICIES = ('qt1-ref', 'TwoComp', 'COMBO')
BANK_RANGES = ((60000, 61023), (64000, 65023), (10000, 12047))
GATE_SEEDS = tuple(range(10000, 10064)) + tuple(range(64000, 64064))
SPOT_COUNT = 64
TOLERANCE = 1e-9
N_STEPS = 10000
CHUNK = 128
EXIT_RESUME = 75
OUT = 'baselines/replica'
KERNEL_ORIGIN = 'reference/envelopes/numba__fastsim.py.json'
SCHEMA = 'imd-amm/replica-validation/1'


class StaleState(ValueError):
    """A checkpoint row does not belong to the current code/reference/runtime binding."""


class OutOfBudget(Exception):
    """The time box was reached; everything finished so far is checkpointed."""


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def bank_name(policy, start, end):
    return f'{OUT}/{policy}-{start}-{end}.json'


def gate_pairs():
    return [(policy, seed) for policy in POLICIES for seed in GATE_SEEDS]


def spot_pairs():
    return [(policy, seed) for policy in POLICIES for start, _ in BANK_RANGES
            for seed in range(start, start + SPOT_COUNT)]


def bank_pairs():
    return [(policy, seed) for policy in POLICIES for start, end in BANK_RANGES
            for seed in range(start, end + 1)]


def binding_document():
    """What decides every checkpointed edge: ammfast sources, reference files, runtime versions."""
    import ammfast
    from ammfast import _reference
    return {'implementation': ammfast.implementation_inventory(),
            'reference': _reference.reference_inventory(),
            'runtime': ammfast.runtime_versions(),
            'n_steps': N_STEPS}


def binding_digest(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


# ---------------------------------------------------------------- checkpoints
def load_rows(path, binding):
    """{(policy, seed): edge} from a JSONL checkpoint.  A half-written last line is dropped;
    any row bound to another digest, malformed, duplicated or non-finite is refused."""
    path = Path(path)
    if not path.exists():
        return {}
    raw = path.read_bytes()
    if raw and not raw.endswith(b'\n'):
        raw = raw[:raw.rfind(b'\n') + 1]
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_bytes(raw)
        os.replace(tmp, path)
    rows = {}
    for number, line in enumerate(raw.decode('utf-8').splitlines(), 1):
        try:
            row = json.loads(line)
            key = (row['policy'], row['seed'])
            edge = row['edge']
            bound = row['binding']
        except (ValueError, KeyError, TypeError) as exc:
            raise StaleState(f'{path}:{number}: unreadable checkpoint row ({exc})') from exc
        if bound != binding:
            raise StaleState(f'{path}:{number}: row bound to {str(bound)[:16]}, current binding is {binding[:16]}')
        if (key[0] not in POLICIES or type(key[1]) is not int or type(edge) is not float
                or not math.isfinite(edge) or key in rows):
            raise StaleState(f'{path}:{number}: invalid or duplicate checkpoint row')
        rows[key] = edge
    return rows


def append_rows(path, binding, items):
    """Append [(policy, seed, edge)] and fsync, so a kill loses at most the line being written."""
    with open(path, 'a', encoding='utf-8') as handle:
        for policy, seed, edge in items:
            handle.write(json.dumps({'binding': binding, 'policy': policy, 'seed': seed, 'edge': edge},
                                    allow_nan=False) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(document, indent=1, allow_nan=False) + '\n', encoding='utf-8')
    os.replace(tmp, path)


# ---------------------------------------------------------------- workers (fresh spawned processes)
def cpython_task(task):
    """One actual CPython replica2.run with a fresh policy object."""
    import ammfast
    policy, seed = task
    started = time.perf_counter()
    edge = ammfast.reference_run(seed, policy)['edge']
    return policy, seed, edge, time.perf_counter() - started


def ammfast_task(task):
    """One compiled batch call for a chunk of seeds of one policy."""
    import ammfast
    policy, seeds = task
    started = time.perf_counter()
    rows = ammfast.run_batch(seeds, policy)
    return policy, [(row['seed'], row['edge']) for row in rows], time.perf_counter() - started


class Run:
    def __init__(self, args):
        self.args = args
        self.started = time.monotonic()
        self.document = binding_document()
        self.binding = binding_digest(self.document)
        self.state_root = Path(args.state).absolute()
        self.dir = self.state_root / self.binding[:16]
        self.dir.mkdir(parents=True, exist_ok=True)
        marker = self.dir / 'binding.json'
        if marker.exists():
            if json.loads(marker.read_text())['digest'] != self.binding:
                raise StaleState(f'{marker} does not match the current binding')
        else:
            write_json(marker, {'_credit': CREDIT, 'digest': self.binding, 'document': self.document})
        stale = sorted(p.name for p in self.state_root.iterdir() if p.is_dir() and p.name != self.dir.name)
        self.say(f'binding {self.binding[:16]} state {self.dir}' + (f' (ignoring stale state: {", ".join(stale)})' if stale else ''))

    def say(self, text):
        print(f'generate: {text}', file=sys.stderr, flush=True)

    def left(self):
        return self.args.budget - (time.monotonic() - self.started)

    def log(self, stage, seconds, rows, workers):
        with open(self.dir / 'log.jsonl', 'a', encoding='utf-8') as handle:
            handle.write(json.dumps({'stage': stage, 'seconds': seconds, 'rows': rows, 'workers': workers}) + '\n')

    def stage_log(self):
        path = self.dir / 'log.jsonl'
        totals = {}
        if path.exists():
            for line in path.read_text().splitlines():
                row = json.loads(line)
                total = totals.setdefault(row['stage'], {'seconds': 0.0, 'rows': 0, 'invocations': 0, 'workers': row['workers']})
                total['seconds'] += row['seconds']
                total['rows'] += row['rows']
                total['invocations'] += 1
        return totals

    # ------------------------------------------------------------ bench
    def bench(self):
        path = self.dir / 'bench.json'
        if path.exists():
            return json.loads(path.read_text())
        import ammfast
        self.say('bench: CPython seconds per run')
        result = {'_credit': CREDIT, 'binding': self.binding, 'bench_seed': 10000, 'cpython_seconds_per_run': {},
                  'cold': {}, 'warm_start': {}, 'runs_per_second': {}}
        for policy in POLICIES:
            ammfast.reference_run(10000, policy, n_steps=10)      # table decode is not part of a run
            times = []
            for seed in (10000, 10001):
                t = time.perf_counter()
                ammfast.reference_run(seed, policy)
                times.append(time.perf_counter() - t)
            result['cpython_seconds_per_run'][policy] = statistics.mean(times)
        child = ('import json,sys,time;t0=time.perf_counter();import ammfast;'
                 'row=ammfast.run(10000,sys.argv[1]);t1=time.perf_counter();'
                 'ammfast.run(10000,sys.argv[1]);t2=time.perf_counter();'
                 'print(json.dumps({"first_call_seconds":t1-t0,"second_call_seconds":t2-t1,"edge":row["edge"]}))')
        for policy in POLICIES:
            cache = self.dir / 'cold_numba_cache'
            shutil.rmtree(cache, ignore_errors=True)
            env = dict(os.environ, NUMBA_CACHE_DIR=str(cache), PYTHONPATH=str(ROOT))
            for label in ('cold', 'warm_start'):
                self.say(f'bench: {label} fresh process, {policy}')
                done = subprocess.run([sys.executable, '-B', '-c', child, policy], env=env, capture_output=True,
                                      text=True, timeout=300, cwd=str(ROOT))
                if done.returncode != 0:
                    raise RuntimeError(f'bench child failed: {done.stderr[-800:]}')
                result[label][policy] = json.loads(done.stdout.strip().splitlines()[-1])
            shutil.rmtree(cache, ignore_errors=True)
        seeds = list(range(10000, 10064))
        for policy in POLICIES:
            ammfast.run(10000, policy)
            rates = []
            for _ in range(3):
                t = time.perf_counter()
                ammfast.run_batch(seeds, policy)
                rates.append(len(seeds) / (time.perf_counter() - t))
            single = []
            t = time.perf_counter()
            for seed in seeds[:16]:
                ammfast.run(seed, policy)
            single = 16 / (time.perf_counter() - t)
            result['runs_per_second'][policy] = {'run_batch_64_seeds_median_of_3': statistics.median(rates),
                                                 'run_batch_all': rates, 'run_single_calls_16_seeds': single}
        workers = self.args.workers
        bank_runs = len(bank_pairs())
        slowest = min(v['run_batch_64_seeds_median_of_3'] for v in result['runs_per_second'].values())
        cpy = max(result['cpython_seconds_per_run'].values())
        result['plan'] = {'workers': workers, 'chunk_seeds': CHUNK, 'budget_seconds_per_command': self.args.budget,
                          'bank_runs': bank_runs,
                          'bank_seconds_estimate': bank_runs / slowest / workers,
                          'cpython_runs': len(set(gate_pairs()) | set(spot_pairs())),
                          'cpython_seconds_estimate': len(set(gate_pairs()) | set(spot_pairs())) * cpy / workers}
        write_json(path, result)
        self.say('bench: ' + json.dumps(result['plan']))
        return result

    # ------------------------------------------------------------ CPython rows
    def cpython(self, wanted, stage):
        path = self.dir / 'cpython.jsonl'
        have = load_rows(path, self.binding)
        todo = [pair for pair in wanted if pair not in have]
        if todo:
            self.say(f'{stage}: {len(todo)} CPython replica2 runs on {self.args.workers} workers')
            started = time.monotonic()
            count = 0
            context = multiprocessing.get_context('spawn')
            with context.Pool(self.args.workers) as pool:
                for policy, seed, edge, _ in pool.imap_unordered(cpython_task, todo):
                    append_rows(path, self.binding, [(policy, seed, edge)])
                    have[(policy, seed)] = edge
                    count += 1
                    if self.left() <= 0:
                        pool.terminate()
                        break
            self.log(stage + '_cpython', time.monotonic() - started, count, self.args.workers)
            if count < len(todo):
                raise OutOfBudget(f'{stage}: {count} of {len(todo)} CPython runs done')
        return have

    # ------------------------------------------------------------ gate
    def gate(self):
        import ammfast
        pairs = gate_pairs()
        reference = self.cpython(pairs, 'gate')
        started = time.monotonic()
        rows = []
        for policy, seed in pairs:
            edge = ammfast.run(seed, policy)['edge']          # one compiled call per run
            rows.append({'policy': policy, 'seed': seed, 'cpython_edge': reference[(policy, seed)],
                         'ammfast_edge': edge, 'abs_error': abs(reference[(policy, seed)] - edge)})
        worst = max(row['abs_error'] for row in rows)
        passed = len(rows) == 384 and all(row['abs_error'] <= TOLERANCE for row in rows)
        result = {'_credit': CREDIT, 'binding': self.binding, 'pass': passed, 'pairs': len(rows),
                  'tolerance': TOLERANCE, 'max_abs_error': worst, 'rows': rows}
        write_json(self.dir / 'gate.json', result)
        self.log('gate_ammfast', time.monotonic() - started, len(rows), 1)
        self.say(f'gate: {"PASS" if passed else "FAIL"} pairs={len(rows)} max_abs_error={worst!r}')
        if not passed:
            raise SystemExit('generate: gate FAILED; no bank will be generated')
        return result

    # ------------------------------------------------------------ banks
    def banks(self, need_all=True):
        path = self.dir / 'bank.jsonl'
        have = load_rows(path, self.binding)
        tasks = []
        for policy in POLICIES:
            for start, end in BANK_RANGES:
                missing = [seed for seed in range(start, end + 1) if (policy, seed) not in have]
                tasks += [(policy, missing[i:i + CHUNK]) for i in range(0, len(missing), CHUNK)]
        if tasks and need_all:
            total = sum(len(seeds) for _, seeds in tasks)
            self.say(f'banks: {total} ammfast runs in {len(tasks)} chunks on {self.args.workers} workers')
            started = time.monotonic()
            count = 0
            context = multiprocessing.get_context('spawn')
            with context.Pool(self.args.workers) as pool:
                for policy, rows, _ in pool.imap_unordered(ammfast_task, tasks):
                    append_rows(path, self.binding, [(policy, seed, edge) for seed, edge in rows])
                    for seed, edge in rows:
                        have[(policy, seed)] = edge
                    count += len(rows)
                    if self.left() <= 0:
                        pool.terminate()
                        break
            self.log('banks_ammfast', time.monotonic() - started, count, self.args.workers)
            if count < total:
                raise OutOfBudget(f'banks: {count} of {total} runs done')
        return have

    # ------------------------------------------------------------ spot
    def spot(self, bank):
        pairs = spot_pairs()
        reference = self.cpython(pairs, 'spot')
        rows = [{'policy': policy, 'seed': seed, 'cpython_edge': reference[(policy, seed)],
                 'ammfast_edge': bank[(policy, seed)], 'abs_error': abs(reference[(policy, seed)] - bank[(policy, seed)])}
                for policy, seed in pairs]
        worst = max(row['abs_error'] for row in rows)
        self.say(f'spot: pairs={len(rows)} max_abs_error={worst!r}')
        return rows

    # ------------------------------------------------------------ write
    def write(self, bench, gate, bank, spots):
        import ammfast
        from ammfast import _reference
        out = ROOT / OUT
        out.mkdir(parents=True, exist_ok=True)
        sources = json.loads((ROOT / _reference.SOURCES).read_text())
        urls = {entry['path']: entry['url'] for entry in sources['files']}
        command = (f'python -B {OUT}/generate.py --stage all --workers {self.args.workers} '
                   f'--budget {self.args.budget:g}')
        runtime = {k: v for k, v in self.document['runtime'].items() if k != 'python_build'}
        gate_ok = bool(gate and gate['pass'])
        spots_ok = len(spots) == 576 and all(row['abs_error'] <= TOLERANCE for row in spots)
        coverage = []
        written = {}
        for policy in POLICIES:
            for start, end in BANK_RANGES:
                seeds = range(start, end + 1)
                done = sum((policy, seed) in bank for seed in seeds)
                name = bank_name(policy, start, end)
                item = {'bank': name, 'policy': policy, 'seed_range': [start, end], 'rows_expected': len(seeds),
                        'rows_generated': done, 'written': False}
                coverage.append(item)
                if done != len(seeds) or not gate_ok or not spots_ok:
                    continue
                spot = [row for row in spots if row['policy'] == policy and start <= row['seed'] < start + SPOT_COUNT]
                header = {
                    '_credit': CREDIT, 'backend': 'replica', 'policy': policy,
                    'provenance': {
                        'origin': 'ammfast.run_batch (Numba port of Taylor\'s replica2.run); not a CPython rerun',
                        'source_commit': TAYLOR_COMMIT,
                        'sources': [{'path': 'reference/' + path, 'url': urls[path], 'sha256': pin[2]}
                                    for path, pin in sorted(_reference.PINS.items())],
                        'source_inventory': {'path': _reference.SOURCES, 'sha256': _reference.SOURCES_SHA256},
                        'kernel_origin': {'path': KERNEL_ORIGIN, 'sha256': sha256_file(ROOT / KERNEL_ORIGIN)},
                        'implementation': self.document['implementation'],
                        'checkpoint_binding': self.binding,
                        'generation_command': command,
                        'backend_runtime': runtime,
                        'n_steps': N_STEPS,
                        'seed_range': [start, end],
                        'row_count': len(seeds),
                        'edge_semantics': 'policy pool edge e1 of replica2.run (dict key "edge")',
                        'rounding': 'unrounded binary64 edge; format(edge, ".6f") once at serialization',
                        'validation': {
                            'evidence': f'{OUT}/validation.json',
                            'tolerance_abs': TOLERANCE,
                            'gate': {'pairs': gate['pairs'], 'max_abs_error': gate['max_abs_error'], 'pass': True},
                            'spot_check': {'seeds': [start, start + SPOT_COUNT - 1], 'pairs': len(spot),
                                           'reference': 'CPython replica2.run, fresh policy object per run',
                                           'max_abs_error': max(row['abs_error'] for row in spot)},
                        },
                    },
                }
                text = json.dumps(header, indent=1, allow_nan=False)
                rows = ',\n'.join('  {"seed": %d, "edge": "%s"}' % (seed, format(bank[(policy, seed)], '.6f'))
                                  for seed in seeds)
                text = text[:-2] + ',\n "rows": [\n' + rows + '\n ]\n}\n'
                json.loads(text)
                tmp = ROOT / (name + '.tmp')
                tmp.write_text(text, encoding='utf-8')
                os.replace(tmp, ROOT / name)
                item['written'] = True
                written[name] = sha256_file(ROOT / name)
        complete = gate_ok and spots_ok and len(written) == 9
        bindings = dict(self.document['implementation'])
        for name in (f'{OUT}/generate.py', f'{OUT}/validate.py'):
            bindings[name] = sha256_file(ROOT / name)
        bindings.update(self.document['reference'])
        bindings[KERNEL_ORIGIN] = sha256_file(ROOT / KERNEL_ORIGIN)
        bindings.update(written)
        timings = {'runs_per_second_per_core': {policy: bench['runs_per_second'][policy]['run_batch_64_seeds_median_of_3']
                                                for policy in POLICIES},
                   'cold_jit_seconds': max(bench['cold'][policy]['first_call_seconds'] for policy in POLICIES)}
        document = {
            '_credit': CREDIT,
            'schema': SCHEMA,
            'complete': complete,
            'tolerance_abs': TOLERANCE,
            'bindings': dict(sorted(bindings.items())),
            'checkpoint_binding': self.binding,
            'runtime': self.document['runtime'],
            'coverage': {'gate_pairs': len(gate['rows']) if gate else 0, 'gate_pass': gate_ok,
                         'spot_pairs': len(spots), 'spot_pass': spots_ok, 'banks': coverage,
                         'rows_generated': sum(item['rows_generated'] for item in coverage),
                         'rows_expected': sum(item['rows_expected'] for item in coverage)},
            'gates384': gate['rows'] if gate else [],
            'spots576': spots,
            'timings': timings,
            'timing_notes': {
                'runs_per_second_per_core': 'one worker process, warm, wall time of ammfast.run_batch on seeds '
                                            '10000..10063 (Python wrapper included), median of 3',
                'cold_jit_seconds': 'slowest of the three policies: fresh interpreter, empty Numba cache, wall time '
                                    'of "import ammfast" plus the first ammfast.run(10000, policy) (table decode included)',
            },
            'benchmark': {key: bench[key] for key in ('cpython_seconds_per_run', 'cold', 'warm_start', 'runs_per_second', 'plan')},
            'stage_wall_seconds': self.stage_log(),
            'generation_command': command,
            'limitations': [
                'Linux x86_64 only was executed; macOS arm64 is untested.',
                'Rates and cold times were measured on a shared machine and vary with its load.',
                'Banks are bound to the runtime versions above; another libm/LLVM may differ in the last bits.',
            ],
        }
        write_json(out / 'validation.json', document)
        self.say(f'write: complete={complete} banks_written={len(written)}/9 validation={OUT}/validation.json')
        return complete


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--stage', default='all', choices=('all', 'bench', 'gate', 'banks', 'spot', 'write', 'status'))
    parser.add_argument('--state', default=str(ROOT / 'scratch' / 'replica-kit'), help='checkpoint directory (under scratch/)')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--budget', type=float, default=420.0, help='seconds before a clean exit 75 (max 450)')
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 64 or not 0 < args.budget <= 450:
        parser.error('--workers must be 1..64 and --budget in (0, 450]')
    sys.dont_write_bytecode = True
    run = Run(args)
    try:
        if args.stage == 'status':
            bank = load_rows(run.dir / 'bank.jsonl', run.binding)
            cpy = load_rows(run.dir / 'cpython.jsonl', run.binding)
            print(json.dumps({'binding': run.binding, 'bank_rows': len(bank), 'bank_rows_expected': len(bank_pairs()),
                              'cpython_rows': len(cpy), 'bench': (run.dir / 'bench.json').exists(),
                              'gate': (run.dir / 'gate.json').exists()}))
            return 0
        bench = run.bench()
        if args.stage == 'bench':
            print(json.dumps(bench['plan']))
            return 0
        gate = run.gate()
        if args.stage == 'gate':
            return 0
        bank = run.banks()
        if args.stage == 'banks':
            return 0
        spots = run.spot(bank)
        if args.stage == 'spot':
            return 0
        return 0 if run.write(bench, gate, bank, spots) else 1
    except OutOfBudget as exc:
        run.say(f'time box reached ({exc}); run the same command again to resume')
        return EXIT_RESUME


if __name__ == '__main__':
    sys.exit(main())
