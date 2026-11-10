"""Screening-only singleton engine evaluation and resumable qt1 bank generation.

Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9
research/solved. Config transcription reused from bootstrap/smoke_engine.py.
"""
from pathlib import Path
import argparse
import hashlib
import inspect
import json
import math
import numbers
import os
import platform
import tempfile
import threading
import time

CREDIT = ('Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9 '
          'research/solved, solved-policy research')
FIRST, LAST = 9700000, 9700511
QT1_SHA256 = 'c68887cff61180e12ba2b1ceb333edf76a830f91f63b5d775a8806fb9ec08aca'
ENGINE_COMMIT = '6b6921f8d463314aa7c96234e1be25a25245f704'
ERRORS = ('oog', 'revert', 'other_halt', 'tx_error', 'bad_return')
ROOT = Path(__file__).resolve().parent
_LOCK = threading.RLock()  # Native gas counters are process-global.
_COMPILED = {}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def digest(path):
    return sha256(Path(path).read_bytes())


def validate_seeds(seeds):
    """Consume and validate the entire iterable before importing or invoking the engine."""
    seeds = list(seeds)
    if any(isinstance(s, bool) or not isinstance(s, numbers.Integral)
           or not FIRST <= s <= LAST for s in seeds):
        raise ValueError(f'engine screening seeds must be integers in {FIRST}..{LAST}')
    return [int(s) for s in seeds]


def _runtime():
    import amm_sim_rs
    return amm_sim_rs


def _compiler():
    # Never allow SolidityCompiler to download an unpinned compiler implicitly.
    import solcx
    from amm_competition.evm.compiler import SolidityCompiler
    exe = solcx.install.get_executable('0.8.24')
    expected = next(r[2] for r in _manifest() if r[1] == 'solc' and r[0] == 'linux-x86_64:engine')
    if digest(exe) != expected:
        raise ValueError('pinned solc SHA256 mismatch')
    return SolidityCompiler()


def _compile(strategy_path):
    source = Path(strategy_path).read_bytes()
    compiler = _compiler()
    dependencies = [(p.name, digest(p)) for p in sorted(compiler.CONTRACTS_SRC_DIR.glob('*.sol'))]
    key = (sha256(source), tuple(dependencies), digest(inspect.getfile(compiler.__class__)))
    if key not in _COMPILED:
        result = compiler.compile(source.decode('utf-8'))
        if not result.success:
            raise RuntimeError(f'Strategy compilation failed: {result.errors}')
        vanilla_source = (compiler.CONTRACTS_SRC_DIR / 'VanillaStrategy.sol').read_text()
        vanilla = compiler.compile(vanilla_source, contract_name='VanillaStrategy')
        if not vanilla.success:
            raise RuntimeError(f'VanillaStrategy compilation failed: {vanilla.errors}')
        _COMPILED[key] = (bytes(result.bytecode), bytes(vanilla.bytecode))
    return _COMPILED[key]


def _config(seed):
    from bootstrap.smoke_engine import config_for
    return config_for(seed)


def config_equivalence():
    """Construction only at indices 0..3: never simulate those indices."""
    from bootstrap.smoke_engine import FIELDS
    from amm_competition.competition.config import BASELINE_VARIANCE, build_base_config
    from amm_competition.competition.match import MatchRunner
    refs = MatchRunner(n_simulations=4, config=build_base_config(seed=None),
                       n_workers=8, variance=BASELINE_VARIANCE)._build_configs()
    count = 0
    for seed, ref in enumerate(refs):
        mine = _config(seed)
        for field in FIELDS:
            if getattr(mine, field) != getattr(ref, field):
                raise ValueError(f'config differs at index {seed}: {field}')
            count += 1
    return count


def _one(engine, codes, seed):
    cfg = _config(seed)
    engine.gas_stats_reset()
    started = time.perf_counter()
    result = engine.run_batch(list(codes[0]), list(codes[1]), [cfg], 8)
    seconds = time.perf_counter() - started
    stats = engine.gas_stats()
    # Reject errors in either participant, on any callback, including initialization.
    for stat in stats:
        if any(stat[key] != 0 for key in ERRORS):
            raise RuntimeError(f'engine callback failure at seed {seed}: {stat}')
    swap = [s for s in stats if s['role'] == 'Submission' and s['callback'] == 'afterSwap']
    if not swap or sum(s['calls'] for s in swap) <= 0:
        raise RuntimeError(f'missing Submission/afterSwap gas at seed {seed}')
    if any(s['calls'] != s['ok'] for s in swap):
        raise RuntimeError(f'unsuccessful afterSwap at seed {seed}')
    gas = max(s['max_ok_gas'] for s in swap)
    rows = list(result.results)
    if len(rows) != 1 or rows[0].seed != seed:
        raise RuntimeError(f'engine seed/result mismatch at seed {seed}')
    edge = float(rows[0].edges['submission'])
    if not math.isfinite(edge) or type(gas) is not int or gas < 0:
        raise RuntimeError(f'invalid edge/gas at seed {seed}')
    return dict(seed=seed, edge=edge, after_swap_peak_gas=gas), dict(seconds=seconds, gas_stats=stats)


def run_batch(seeds, strategy_path):
    """Ordered unrounded rows; preserves duplicates; [] requires no runtime or source.

    Candidate source must define Strategy and use the pinned engine's base contracts.
    Each supplied seed gets one singleton run_batch(X, VanillaStrategy, [config], 8).
    Callers sharing this module are serialized to isolate native gas counters. Do not
    invoke amm_sim_rs directly in other threads of the same process during these calls.
    """
    seeds = validate_seeds(seeds)
    if not seeds:
        return []
    with _LOCK:
        codes = _compile(strategy_path)
        engine = _runtime()
        return [_one(engine, codes, s)[0] for s in seeds]


def _manifest():
    return [line.split('\t') for line in (ROOT / 'bootstrap/manifest.tsv').read_text().splitlines()
            if line and not line.startswith('#')]


def provenance(strategy_path, cache):
    """Hash actual installed inputs and verified cached distribution files."""
    import amm_sim_rs
    import amm_competition
    import numpy
    import solcx
    import importlib.metadata
    from bootstrap.smoke_engine import FIELDS
    source = Path(strategy_path).read_bytes()
    if sha256(source) != QT1_SHA256:
        raise ValueError('qt1 source SHA256 mismatch')
    files = {}
    for row in _manifest():
        group, kind, expected, size, name, url = row
        if group == 'linux-x86_64:engine' and (kind in ('engine-src', 'solc') or name.startswith('amm_sim_rs-')):
            path = Path(cache) / 'sha256' / expected / name
            if digest(path) != expected or path.stat().st_size != int(size):
                raise ValueError(f'cached pinned input mismatch: {name}')
            files[name] = dict(sha256=expected, url=url)
    engine_root = Path(amm_competition.__file__).resolve().parent.parent
    installed = {str(p.relative_to(engine_root)): digest(p)
                 for top in ('amm_competition', 'contracts')
                 for p in sorted((engine_root / top).rglob('*')) if p.suffix in ('.py', '.sol')}
    native = next(Path(amm_sim_rs.__file__).parent.glob('*.so'))
    native_sha = digest(native)
    if native_sha != 'ff88b6353c2aad7342f0a26194f7347e78b9c3b38d683a7449006d423978cd14':
        raise ValueError('release wheel native SHA256 mismatch')
    configs = []
    for seed in range(FIRST, LAST + 1):
        cfg = _config(seed)
        configs.append({f: getattr(cfg, f) for f in FIELDS})
    codes = _compile(strategy_path)
    return dict(engine_commit=ENGINE_COMMIT, qt1_source_sha256=sha256(source),
                engine_eval_sha256=digest(__file__), bootstrap_smoke_sha256=digest(ROOT / 'bootstrap/smoke_engine.py'),
                downloads=files, installed_source_sha256=installed,
                native_sha256=native_sha, creation_bytecode_sha256=sha256(codes[0]),
                vanilla_creation_bytecode_sha256=sha256(codes[1]), compiler_sha256=digest(solcx.install.get_executable('0.8.24')),
                compiler_version='0.8.24', compiler_settings=dict(optimizer_runs=200, viaIR=True, evmVersion='paris'),
                wheel_version=importlib.metadata.version('amm-sim-rs'),
                python=platform.python_version(), numpy=numpy.__version__, platform='linux-x86_64',
                configs_sha256=sha256(json.dumps(configs, sort_keys=True).encode()),
                configs=configs, config_fields_compared=config_equivalence(), workers=8,
                config_construction='bootstrap.smoke_engine.config_for: default_rng(seed), size then rate then sigma',
                rounding='format(edge, ".6f") only at bank serialization', screening_only=True)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, indent=2, allow_nan=False)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_state(path, binding):
    if not Path(path).exists():
        return dict(_credit=CREDIT, binding=binding, rows=[], audits=[], failures=[], commands=[])
    state = json.loads(Path(path).read_text())
    if state['binding'] != binding:
        raise ValueError('stale checkpoint: source/wheel/compiler/config/runtime digest changed')
    rows = state['rows']
    if [r['seed'] for r in rows] != list(range(FIRST, FIRST + len(rows))) or len(rows) > 512:
        raise ValueError('checkpoint rows must be a contiguous unique sorted prefix')
    if len(state['audits']) != len(rows):
        raise ValueError('checkpoint audit count mismatch')
    for row, audit in zip(rows, state['audits']):
        if (not math.isfinite(row['edge']) or type(row['after_swap_peak_gas']) is not int
                or row['after_swap_peak_gas'] < 0 or audit['seed'] != row['seed']):
            raise ValueError('invalid checkpoint row')
        swap = [s for s in audit['gas_stats'] if s['role'] == 'Submission' and s['callback'] == 'afterSwap']
        if (not swap or sum(s['calls'] for s in swap) <= 0
                or any(s['calls'] != s['ok'] for s in swap)
                or max(s['max_ok_gas'] for s in swap) != row['after_swap_peak_gas']
                or any(s[k] for s in audit['gas_stats'] for k in ERRORS)):
            raise ValueError('invalid checkpoint gas evidence')
    return state


def publish(state, output):
    remaining = list(range(FIRST + len(state['rows']), LAST + 1))
    bank = dict(_credit=CREDIT, backend='engine', policy='qt1',
                status='incomplete' if remaining else 'complete', screening_only=True,
                remaining_seeds=remaining, provenance=state['binding'],
                commands=state['commands'], failures=state['failures'],
                simulation_seconds=sum(a['seconds'] for a in state['audits']),
                rows=[dict(r, edge=format(r['edge'], '.6f')) for r in state['rows']])
    atomic_json(output, bank)
    atomic_json(Path(output).with_name('qt1-audit.json'),
                dict(_credit=CREDIT, unrounded_rows=state['rows'], audits=state['audits']))


def generate(strategy, checkpoint, output, cache, max_seeds=32, budget=400):
    """Checkpoint every seed. Resume repeats at most an interrupted, uncommitted seed."""
    started = time.perf_counter()
    binding = provenance(strategy, cache)
    # Lock across processes too: two resumptions must not overwrite each other's progress.
    import fcntl
    Path(checkpoint).parent.mkdir(parents=True, exist_ok=True)
    with open(str(checkpoint) + '.lock', 'a') as lock, _LOCK:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = load_state(checkpoint, binding)
        state['commands'].append(dict(argv=__import__('sys').argv, max_seeds=max_seeds, budget=budget))
        codes = _compile(strategy)
        engine = _runtime()
        for seed in range(FIRST + len(state['rows']), min(LAST + 1, FIRST + len(state['rows']) + max_seeds)):
            reserve = max(30, 2 * max((a['seconds'] for a in state['audits']), default=10))
            if time.perf_counter() - started + reserve >= budget:
                break
            try:
                row, audit = _one(engine, codes, seed)
            except Exception as exc:
                state['failures'].append(dict(seed=seed, error=f'{type(exc).__name__}: {exc}'))
                atomic_json(checkpoint, state)
                publish(state, output)
                raise
            state['rows'].append(row)
            state['audits'].append(dict(seed=seed, **audit))
            atomic_json(checkpoint, state)
            print(json.dumps(dict(n=len(state['rows']), **row, seconds=audit['seconds'])), flush=True)
        atomic_json(checkpoint, state)
        publish(state, output)
        return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strategy', default='scratch/reference/qt1/Strategy.sol')
    parser.add_argument('--checkpoint', default='scratch/engine-bank/progress.json')
    parser.add_argument('--output', default='baselines/engine/qt1-9700000-9700511.json')
    parser.add_argument('--cache', default='scratch/cache')
    parser.add_argument('--max-seeds', type=int, default=32)
    parser.add_argument('--budget', type=int, default=400)
    args = parser.parse_args()
    if not 1 <= args.max_seeds <= 512 or not 1 <= args.budget <= 420:
        parser.error('max-seeds must be 1..512; budget must be 1..420 seconds')
    generate(args.strategy, args.checkpoint, args.output, args.cache, args.max_seeds, args.budget)


if __name__ == '__main__':
    main()
