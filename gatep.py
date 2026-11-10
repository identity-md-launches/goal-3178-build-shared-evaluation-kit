#!/usr/bin/env python3
# Credit: Taylor (gtmcknight), solved-policy research,
# amm-sim@4812a6382311624e1506aca131ca35c378f930d9.
"""Verify Taylor's inputs and run/cache unmodified Gate P (offline, stdlib only)."""
import argparse
from contextlib import contextmanager
from decimal import Decimal
import fcntl
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

# Works with python -I as well as normal invocation; never load materialized code
# into the verifier's interpreter. Do not produce __pycache__ outside scratch.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from reference.materialize import (CREDIT, IntegrityError, atomic_write, inventory,
                                   reject_symlinks, sha256)

MAX_TIMEOUT = 450.0


def taylor_entries():
    manifest = inventory()
    return manifest, {e['path'][7:]: e for e in manifest['files']
                      if e['path'].startswith('taylor/')}


def verify_inputs(reference):
    """Hash ALL bundled Taylor files, reject extra inputs, return verified bytes."""
    reference = Path(reference).absolute()
    reject_symlinks(reference)
    manifest, entries = taylor_entries()
    actual = {}
    for p in reference.rglob('*'):
        if p.is_symlink() or not p.is_file():
            raise IntegrityError(f'unexpected Taylor entry: {p.name}')
        actual[p.relative_to(reference).as_posix()] = p
    if set(actual) != set(entries):
        raise IntegrityError('Taylor file set mismatch: missing=' +
                             repr(sorted(set(entries) - set(actual))) + ' extra=' +
                             repr(sorted(set(actual) - set(entries))))
    contents = {}
    for name, e in entries.items():
        data = actual[name].read_bytes()
        if len(data) != e['size'] or sha256(data) != e['sha256']:
            raise IntegrityError(f'Taylor SHA256 mismatch: {name}')
        contents[name] = data
    digest = sha256(json.dumps({n: sha256(b) for n, b in contents.items()},
                              sort_keys=True, separators=(',', ':')).encode())
    return manifest['taylor_commit'], digest, contents


def expected_output(readme):
    lines = readme.decode('utf-8').splitlines()
    header = 'seed   perfect-info      gaussian+plain    gaussian+qm       lattice+qm'
    starts = [i for i, line in enumerate(lines) if line == header]
    if len(starts) != 1:
        raise IntegrityError('upstream README gate header missing or ambiguous')
    return '\n'.join(lines[starts[0]:starts[0] + 4]) + '\n'


def validate_stdout(stdout, expected):
    if not isinstance(stdout, str) or len(stdout) > 4096:
        raise IntegrityError('invalid gate stdout')
    lines, wanted = stdout.splitlines(), expected.splitlines()
    if len(lines) != 4 or len(wanted) != 4 or lines[0] != wanted[0]:
        raise IntegrityError('gate output header/row count mismatch')
    for seed, line, target in zip(range(10000, 10003), lines[1:], wanted[1:]):
        fields, values = line.split(), target.split()
        if len(fields) != 5 or fields[0] != str(seed):
            raise IntegrityError(f'gate output seed/column mismatch: {seed}')
        for value, wanted_value in zip(fields[1:], values[1:]):
            if (not re.fullmatch(r'-?\d+\.\d{6}', value)
                    or Decimal(value) != Decimal(wanted_value)):
                raise IntegrityError(f'gate output value mismatch: seed {seed}')
    # Cache bytes must be the original stdout, including its trailing newline.
    if stdout != expected:
        raise IntegrityError('gate output formatting mismatch')


@contextmanager
def cache_lock(path, deadline):
    reject_symlinks(path)
    with path.open('a+b') as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('timed out waiting for gate cache lock')
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def validate_record(record, identity, expected):
    if not isinstance(record, dict) or record.get('identity') != identity:
        raise IntegrityError('cache identity mismatch')
    if (type(record.get('returncode')) is not int or record['returncode'] != 0
            or record.get('stderr') != '' or record.get('status') != 'passed'):
        raise IntegrityError('cache contains failed output')
    elapsed = record.get('elapsed_seconds')
    if (type(elapsed) not in (int, float) or not math.isfinite(elapsed)
            or not 0 < elapsed <= MAX_TIMEOUT):
        raise IntegrityError('cache contains invalid elapsed time')
    stdout = record.get('stdout')
    validate_stdout(stdout, expected)
    if record.get('stdout_sha256') != sha256(stdout.encode()):
        raise IntegrityError('cached stdout SHA256 mismatch')
    return stdout


def run_gate(reference, cache, timeout=MAX_TIMEOUT):
    if not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError(f'timeout must be > 0 and <= {MAX_TIMEOUT:g} seconds')
    deadline = time.monotonic() + timeout
    commit, digest, contents = verify_inputs(reference)
    expected = expected_output(contents['README.md'])
    validate_stdout(expected, expected)
    identity = {'format': 1, 'commit': commit, 'python': sys.version,
                'implementation': sys.implementation.name, 'input_digest': digest}
    key = sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode())
    cache = Path(cache).absolute()
    reject_symlinks(cache)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (key + '.json')
    with cache_lock(cache / (key + '.lock'), deadline):
        # Also verify after waiting: another process may have changed the input.
        if verify_inputs(reference)[:2] != (commit, digest):
            raise IntegrityError('Taylor inputs changed while waiting')
        if time.monotonic() >= deadline:
            raise TimeoutError('gate deadline exceeded')
        reject_symlinks(target)
        if target.exists():
            try:
                record = json.loads(target.read_text(encoding='utf-8'))
            except (ValueError, UnicodeError) as exc:
                raise IntegrityError('malformed gate cache') from exc
            return validate_record(record, identity, expected), True, target
        # Execute only the verified snapshot; arbitrary neighboring modules and
        # stale bytecode in the caller's directory cannot affect the child.
        with tempfile.TemporaryDirectory(prefix='gatep-', dir=cache) as tmp:
            stage = Path(tmp)
            for name, data in contents.items():
                (stage / name).write_bytes(data)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('gate deadline exceeded before execution')
            start = time.monotonic()
            try:
                proc = subprocess.run([sys.executable, '-I', '-B', str(stage / 'gates.py')],
                                      cwd=stage, capture_output=True, text=True,
                                      encoding='utf-8', timeout=remaining, check=False)
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError('gates.py timed out; no success cached') from exc
            elapsed = time.monotonic() - start
        if time.monotonic() >= deadline:
            raise TimeoutError('gate deadline exceeded; no success cached')
        if proc.returncode != 0 or proc.stderr:
            raise IntegrityError(f'gates.py failed: exit={proc.returncode}; stderr={proc.stderr[:2000]!r}')
        validate_stdout(proc.stdout, expected)
        verify_inputs(reference)
        record = {'_credit': CREDIT, 'identity': identity, 'status': 'passed',
                  'returncode': proc.returncode, 'stdout': proc.stdout,
                  'stdout_sha256': sha256(proc.stdout.encode()), 'stderr': proc.stderr,
                  'elapsed_seconds': elapsed}
        validate_record(record, identity, expected)
        atomic_write(target, (json.dumps(record, indent=2) + '\n').encode())
        return proc.stdout, False, target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--cache', type=Path, default=Path('~/.cache/imd-amm/gatep'))
    parser.add_argument('--timeout', type=float, default=MAX_TIMEOUT,
                        help='total lock/execution budget in seconds (max 450)')
    args = parser.parse_args()
    try:
        stdout, hit, _ = run_gate(args.reference, args.cache.expanduser(), args.timeout)
    except (OSError, ValueError, TimeoutError) as exc:
        parser.exit(1, f'gatep: {exc}\n')
    print(stdout, end='')
    print('gatep: PASS (verified cache hit)' if hit else 'gatep: PASS (executed gates.py)',
          file=sys.stderr)


if __name__ == '__main__':
    main()
