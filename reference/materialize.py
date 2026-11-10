#!/usr/bin/env python3
# Credit: Taylor (gtmcknight), solved-policy research,
# amm-sim@4812a6382311624e1506aca131ca35c378f930d9.
"""Offline, byte-preserving extraction of the pinned reference envelopes."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import zlib

ROOT = Path(__file__).resolve().parent
SOURCES_SHA256 = '62147ff112987f666e2c1ad5041012276a66033baec543c2b014f766cd8f01f5'
CREDIT = ('Taylor (gtmcknight), solved-policy research, '
          'amm-sim@4812a6382311624e1506aca131ca35c378f930d9')


class IntegrityError(ValueError):
    """The supplied bytes do not match the pinned inventory."""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or '..' in p.parts or str(p) != name or '\\' in name:
        raise IntegrityError(f'unsafe path: {name!r}')
    return p


def inventory():
    raw = (ROOT / 'sources.json').read_bytes()
    if sha256(raw) != SOURCES_SHA256:
        raise IntegrityError('sources.json SHA256 mismatch')
    return json.loads(raw)


def originals():
    """Validate every envelope before returning any original bytes."""
    result = {}
    for entry in inventory()['files']:
        name = str(safe_path(entry['path']))
        raw = (ROOT / str(safe_path(entry['envelope']))).read_bytes()
        if sha256(raw) != entry['envelope_sha256']:
            raise IntegrityError(f'envelope SHA256 mismatch: {name}')
        box = json.loads(raw)
        if (box['encoding'] != 'base64+zlib' or box['path'] != name
                or box['size'] != entry['size'] or box['sha256'] != entry['sha256']):
            raise IntegrityError(f'envelope metadata mismatch: {name}')
        compressed = base64.b64decode(box['data'], validate=True)
        decoder = zlib.decompressobj()
        data = decoder.decompress(compressed, entry['size'] + 1)
        if (len(data) != entry['size'] or not decoder.eof or decoder.unused_data
                or decoder.unconsumed_tail or sha256(data) != entry['sha256']):
            raise IntegrityError(f'original SHA256/size mismatch: {name}')
        if name in result:
            raise IntegrityError(f'duplicate member: {name}')
        result[name] = data
    bundle = result['numba/source_bundle.json']
    if sha256(bundle) != inventory()['numba_decoded_json_sha256']:
        raise IntegrityError('decoded Numba JSON SHA256 mismatch')
    for name, source in json.loads(bundle).items():
        if result.get('numba/' + str(safe_path(name))) != source.encode('utf-8'):
            raise IntegrityError(f'Numba member mismatch: {name}')
    return result


def reject_symlinks(path):
    for p in (path, *path.parents):
        if p.is_symlink():
            raise IntegrityError(f'symlink refused: {p}')


def atomic_write(path, data):
    reject_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def materialize(out):
    data = originals()
    out = Path(out).absolute()
    reject_symlinks(out)
    # Preflight existing files before writing. Repeated extraction is idempotent;
    # a corrupt destination must be explicitly removed, never silently repaired.
    directories = {str(parent) for name in data for parent in PurePosixPath(name).parents
                   if str(parent) != '.'}
    if out.exists():
        for path in out.rglob('*'):
            if path.is_symlink():
                raise IntegrityError(f'symlink refused: {path}')
            name = path.relative_to(out).as_posix()
            if path.is_dir():
                if name not in directories:
                    raise IntegrityError(f'unexpected destination directory: {name}')
            else:
                if name not in data or not path.is_file() or path.read_bytes() != data[name]:
                    raise IntegrityError(f'unexpected or changed destination: {name}')
    for name, content in data.items():
        path = out / name
        if not path.exists():
            atomic_write(path, content)
    return len(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        count = materialize(args.out)
    except (OSError, ValueError, KeyError, zlib.error) as exc:
        parser.exit(1, f'materialize: {exc}\n')
    print(f'materialized {count} verified originals into {args.out}')


if __name__ == '__main__':
    main()
