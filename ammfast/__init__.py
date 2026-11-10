# Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9,
# research/solved, solved-policy research: replica2.run, the filter, the fee rule and the tables.
"""ammfast: one compiled call per replica2 run, and an ordered batch call.  See docs/ammfast.md.

    ammfast.run(seed, policy, head_params=None)        -> {"seed": int, "edge": float}
    ammfast.run_batch(seeds, policy, head_params=None) -> [row, ...] in input order

Policy IDs are exactly ``qt1-ref``, ``TwoComp`` and ``COMBO``.  ``head_params`` is the COMBO
residual head: 18 finite weights (or ``{"weights": [...]}``); ``None`` is the unmodified policy.
"""
import hashlib
import math
import operator
from pathlib import Path
import threading

from ._head import HEAD_PARAM_COUNT

__all__ = ['run', 'run_batch', 'reference_run', 'validate_head_params', 'implementation_inventory',
           'runtime_versions', 'POLICIES', 'HEAD_PARAM_COUNT', 'DEFAULT_STEPS', 'SEED_MIN', 'SEED_MAX']

POLICIES = ('qt1-ref', 'TwoComp', 'COMBO')
_KERNEL_ID = {'qt1-ref': 0, 'TwoComp': 1, 'COMBO': 2}     # archived Numba ids qmref / twocomp / combo
DEFAULT_STEPS = 10000
SEED_MIN = 0
# replica2 seeds random.Random with seed, seed + 1 and 10_000_000 + seed; the compiled seeding is
# CPython's for one 32-bit word, so all three must stay below 2**32.
SEED_MAX = 2 ** 32 - 1 - 10_000_000
_LOCK = threading.Lock()
_READY = {}


def _policy(policy):
    if not isinstance(policy, str) or policy not in _KERNEL_ID:
        raise ValueError(f'unknown policy {policy!r}; expected one of {", ".join(POLICIES)}')
    return _KERNEL_ID[policy]


def _seed(seed):
    if isinstance(seed, bool):
        raise ValueError('seed must be an integer, not a bool')
    try:
        value = operator.index(seed)
    except TypeError:
        raise ValueError(f'seed must be an integer, got {type(seed).__name__}') from None
    if not SEED_MIN <= value <= SEED_MAX:
        raise ValueError(f'seed {value} outside {SEED_MIN}..{SEED_MAX}')
    return value


def _steps(n_steps):
    if isinstance(n_steps, bool) or not isinstance(n_steps, int) or n_steps < 0:
        raise ValueError('n_steps must be a nonnegative integer')
    return n_steps


def validate_head_params(head_params):
    """Return the 18 head weights as a tuple of floats, or None.  Never mutates its argument.

    Accepted: None; a sequence (list, tuple, 1-D array) of exactly 18 finite real numbers; or a
    mapping with ``weights`` holding such a sequence and nothing else but an optional ``_credit``.
    Anything else raises ValueError."""
    if head_params is None:
        return None
    if isinstance(head_params, dict):
        extra = set(head_params) - {'_credit', 'weights'}
        if extra or 'weights' not in head_params:
            raise ValueError('head_params mapping must have "weights" (optionally "_credit") and nothing else')
        head_params = head_params['weights']
    if isinstance(head_params, (str, bytes)) or not hasattr(head_params, '__len__'):
        raise ValueError('head_params must be a sequence of 18 finite numbers')
    if len(head_params) != HEAD_PARAM_COUNT:
        raise ValueError(f'head_params needs exactly {HEAD_PARAM_COUNT} weights, got {len(head_params)}')
    weights = []
    for index, value in enumerate(list(head_params)):
        if isinstance(value, (bool, str, bytes)) or not hasattr(value, '__float__') or isinstance(value, complex):
            raise ValueError(f'head weight {index} must be a real number')
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f'head weight {index} must be a real number') from None
        if not math.isfinite(value):
            raise ValueError(f'head weight {index} must be finite')
        weights.append(value)
    return tuple(weights)


def _ready():
    """Decode the tables and import the compiled kernel once per process."""
    with _LOCK:
        if not _READY:
            try:
                import numpy as np
                from . import _kernel as kernel
            except ImportError as exc:
                raise ImportError('ammfast needs numpy and numba; run setup_fast.sh --replica-only and '
                                  f'source scratch/env.sh ({exc})') from exc
            from . import _reference
            from ._head import hidden_weights
            T = _reference.tables()
            P = T.P
            tags = ['0c', '1s', '1c', '2s', '2c']
            ns, nw = len(P['S']), P['NW']
            nc = max(len(T.A[t][0][0]) for t in tags)
            A = np.zeros((5, ns, nw, nc))
            B = np.zeros((5, ns, nw, nc))
            for ti, t in enumerate(tags):
                a = np.array(T.A[t]); b = np.array(T.B[t])
                A[ti, :, :, :a.shape[2]] = a
                B[ti, :, :, :b.shape[2]] = b
            S9 = np.concatenate([np.array(P['S'], dtype=np.float64), [P['H']]])
            W2L = np.array(P['W2L'], dtype=np.float64)
            I = np.array([nw, P['W0'], P['FB'], len(W2L)], dtype=np.int64)
            U, c = hidden_weights()
            arrays = (A, B, S9, W2L, I, np.array(U), np.array(c))
            for array in arrays:
                array.setflags(write=False)
            _READY.update(np=np, kernel=kernel, arrays=arrays)
        return _READY


def _simulate(seed_values, policy_id, weights, n_steps):
    ready = _ready()
    np = ready['np']
    hon = weights is not None
    hp = np.array(weights if hon else (0.0,) * HEAD_PARAM_COUNT, dtype=np.float64)
    seeds = np.array(seed_values, dtype=np.int64)
    out = np.empty(len(seed_values), dtype=np.float64)
    ready['kernel'].sim_batch(seeds, n_steps, policy_id, hp, hon, *ready['arrays'], out)   # the one compiled call
    edges = [float(v) for v in out]
    for seed, edge in zip(seed_values, edges):
        if not math.isfinite(edge):
            raise ArithmeticError(f'non-finite edge for seed {seed}')
    return edges


def _prepare(policy, head_params, n_steps):
    policy_id = _policy(policy)
    weights = validate_head_params(head_params)
    if weights is not None and policy != 'COMBO':
        raise ValueError('head_params is the COMBO residual head; it is not supported for ' + policy)
    return policy_id, weights, _steps(n_steps)


def run(seed, policy, head_params=None, *, n_steps=DEFAULT_STEPS):
    """One whole replica2 run (RNG, market, every callback) in one compiled call.

    Returns ``{"seed": int, "edge": float}`` with the finite, unrounded policy edge."""
    policy_id, weights, n_steps = _prepare(policy, head_params, n_steps)
    value = _seed(seed)
    return {'seed': value, 'edge': _simulate([value], policy_id, weights, n_steps)[0]}


def run_batch(seeds, policy, head_params=None, *, n_steps=DEFAULT_STEPS):
    """The rows ``run`` would return for each seed, in input order, duplicates kept, in one compiled call."""
    policy_id, weights, n_steps = _prepare(policy, head_params, n_steps)
    if isinstance(seeds, (str, bytes)):
        raise ValueError('seeds must be an iterable of integers')
    values = [_seed(seed) for seed in seeds]
    if not values:
        return []
    edges = _simulate(values, policy_id, weights, n_steps)
    return [{'seed': seed, 'edge': edge} for seed, edge in zip(values, edges)]


def reference_run(seed, policy, head_params=None, *, n_steps=DEFAULT_STEPS):
    """The same row from CPython: Taylor's unmodified replica2.run with a fresh reference policy
    (or the pure-Python head).  Slow; this is the parity oracle, not a backend."""
    _, weights, n_steps = _prepare(policy, head_params, n_steps)
    value = _seed(seed)
    from . import _reference
    edge = float(_reference.run(value, policy, weights, n_steps))
    if not math.isfinite(edge):
        raise ArithmeticError(f'non-finite reference edge for seed {value}')
    return {'seed': value, 'edge': edge}


def implementation_inventory():
    """{repository-relative path: sha256} of every source file of this package."""
    here = Path(__file__).resolve().parent
    return {'ammfast/' + path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(here.glob('*.py'))}


def runtime_versions():
    """Interpreter and library versions that decide the compiled code (no host identifiers)."""
    import platform
    import sys
    versions = {'python': sys.version.split()[0], 'python_build': sys.version, 'implementation': sys.implementation.name,
                'platform': sys.platform, 'machine': platform.machine()}
    for name in ('numpy', 'numba', 'llvmlite'):
        try:
            versions[name] = __import__(name).__version__
        except ImportError:
            versions[name] = None
    return versions
