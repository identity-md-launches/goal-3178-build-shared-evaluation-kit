#!/usr/bin/env python3
# Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9,
# research/solved, solved-policy research. Every check below exists to keep the
# kit built on his solved-policy research reproducible and correctly attributed.
"""Whole-kit structural/provenance validator for the Paradigm AMM evaluation kit.

Standard library only. Offline: no downloads, no simulation, no import of kit
code (ammfast is parsed with ``ast``, never executed). Emits deterministic JSON
findings and exits nonzero when required evidence is missing or invalid.

This is a STRUCTURAL check. It recomputes digests, ranges, formatting and the
|cpython - ammfast| errors that the replica worker *reports*; it does not rerun
CPython replica2, ammfast or the engine, so it is not independent parity proof.
See docs/validation.md.

Exit codes: 0 all requested parts pass, 1 a requested part is missing or
invalid, 2 usage error.
"""
import argparse
import ast
import base64
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import time
import zlib

TAYLOR_COMMIT = '4812a6382311624e1506aca131ca35c378f930d9'
CREDIT = ('Taylor (gtmcknight), amm-sim@' + TAYLOR_COMMIT +
          ', research/solved, solved-policy research. Structural/provenance validation only.')
VERSION = 1

SOURCES_SHA256 = '62147ff112987f666e2c1ad5041012276a66033baec543c2b014f766cd8f01f5'
QT1_SHA256 = 'c68887cff61180e12ba2b1ceb333edf76a830f91f63b5d775a8806fb9ec08aca'
CALIB_SHA256 = 'fdf24c7aa2b604d770336a5783276da8ed4d50bca9dbcc02e98b4da58f608d26'
ENGINE_COMMIT = '6b6921f8d463314aa7c96234e1be25a25245f704'
ENGINE_FIRST, ENGINE_LAST = 9700000, 9700511
ENGINE_ERRORS = ('oog', 'revert', 'other_halt', 'tx_error', 'bad_return')
ENGINE_COUNTERS = ('calls', 'ok', 'max_ok_gas', 'sum_ok_gas') + ENGINE_ERRORS

POLICIES = ('qt1-ref', 'TwoComp', 'COMBO')
WINDOWS = ((60000, 61023), (64000, 65023), (10000, 12047))
GATE_SEEDS = tuple(range(10000, 10064)) + tuple(range(64000, 64064))
HA_RANGE = (10000, 12047)
TOLERANCE = 1e-9
EDGE_RE = re.compile(r'-?[0-9]+\.[0-9]{6}')
HEX64 = re.compile(r'[0-9a-f]{64}')

ENGINE_BANK = 'baselines/engine/qt1-9700000-9700511.json'
ENGINE_AUDIT = 'baselines/engine/qt1-audit.json'
ENGINE_VALIDATION = 'baselines/engine/validation.json'
HA_BANK = 'baselines/ha/HA-10000-12047.json'
REPLICA_VALIDATION = 'baselines/replica/validation.json'
SOURCES = 'reference/sources.json'
KIT_FILES = ('score.py', 'setup_fast.sh', 'gatep.py')
AMMFAST_INIT = 'ammfast/__init__.py'
PARTS = ('reference', 'kit', 'ammfast', 'engine', 'ha', 'replica')


def replica_bank_path(policy, start, end):
    return f'baselines/replica/{policy}-{start}-{end}.json'


REPLICA_BANKS = tuple(replica_bank_path(p, a, b) for p in POLICIES for a, b in WINDOWS)
EXPECTED_ROWS = {**{replica_bank_path(p, a, b): b - a + 1 for p in POLICIES for a, b in WINDOWS},
                 HA_BANK: HA_RANGE[1] - HA_RANGE[0] + 1, ENGINE_BANK: ENGINE_LAST - ENGINE_FIRST + 1}


class Finding(Exception):
    """One validation failure; ``kind`` is 'missing' or 'invalid'."""

    def __init__(self, code, path, detail, kind='invalid'):
        super().__init__(detail)
        self.code, self.path, self.detail, self.kind = code, path, detail, kind


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


class Context:
    """Reads files under one root, refusing escapes and symlinks; records digests."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.findings = []
        self.digests = {}
        self.rows = {}

    def fail(self, part, code, path, detail, kind='invalid'):
        self.findings.append({'part': part, 'kind': kind, 'code': code, 'path': path,
                              'detail': detail})

    def safe(self, rel, part='replica'):
        if not isinstance(rel, str) or not rel or '\\' in rel or '\x00' in rel:
            raise Finding('path-escape', str(rel), 'path must be a nonempty POSIX string')
        p = PurePosixPath(rel)
        if p.is_absolute() or '..' in p.parts or '.' in p.parts or str(p) != rel:
            raise Finding('path-escape', rel, 'path must be normalized and repo-relative')
        full = self.root
        for piece in p.parts:
            full = full / piece
            if full.is_symlink():
                raise Finding('path-escape', rel, 'symlinks are refused')
        if not str(full.resolve()).startswith(str(self.root) + os.sep):
            raise Finding('path-escape', rel, 'path resolves outside the repository')
        return full

    def exists(self, rel):
        return self.safe(rel).is_file()

    def read(self, rel):
        full = self.safe(rel)
        if not full.is_file():
            raise Finding('missing-file', rel, 'required file is absent', 'missing')
        data = full.read_bytes()
        self.digests[rel] = sha256(data)
        return data

    def json(self, rel):
        return load_json_bytes(self.read(rel), rel)


def _pairs(path):
    def hook(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise Finding('duplicate-key', path, f'duplicate JSON key {key!r}')
            out[key] = value
        return out
    return hook


def _nonfinite(path):
    def hook(token):
        raise Finding('nonfinite-number', path, f'non-finite JSON number {token}')
    return hook


def _float(path):
    def hook(token):
        value = float(token)
        if not math.isfinite(value):
            raise Finding('nonfinite-number', path, f'JSON number overflows: {token}')
        return value
    return hook


def load_json_bytes(data, path):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise Finding('bad-json', path, 'not UTF-8') from None
    try:
        return json.loads(text, object_pairs_hook=_pairs(path), parse_constant=_nonfinite(path),
                          parse_float=_float(path))
    except json.JSONDecodeError as exc:
        raise Finding('bad-json', path, f'invalid JSON: {exc.msg} at line {exc.lineno}') from None


def check_credit_value(value, path):
    if not isinstance(value, str) or 'Taylor' not in value or 'gtmcknight' not in value \
            or TAYLOR_COMMIT not in value:
        raise Finding('bad-credit', path, '_credit must name Taylor (gtmcknight) and the full pin '
                      + TAYLOR_COMMIT)


def check_credit(doc, path):
    if not isinstance(doc, dict) or not doc:
        raise Finding('bad-shape', path, 'expected a nonempty JSON object')
    if next(iter(doc)) != '_credit':
        raise Finding('credit-not-first', path, 'first JSON field must be _credit')
    check_credit_value(doc['_credit'], path)


def check_header(data, path):
    head = b'\n'.join(data.splitlines()[:15]).decode('utf-8', 'replace')
    if 'gtmcknight' not in head or 'Taylor' not in head or TAYLOR_COMMIT not in head:
        raise Finding('bad-credit', path, 'file header (first 15 lines) must credit Taylor '
                      '(gtmcknight) with the full pin ' + TAYLOR_COMMIT)


def require(cond, code, path, detail):
    if not cond:
        raise Finding(code, path, detail)


def check_rows(rows, path, start, end, *, engine=False):
    """Exact sorted unique seeds start..end with fixed-6dp finite edge strings."""
    require(isinstance(rows, list), 'bad-shape', path, 'rows must be a list')
    expected = end - start + 1
    for i, row in enumerate(rows):
        require(isinstance(row, dict), 'bad-row', path, f'row {i} is not an object')
        seed, edge = row.get('seed'), row.get('edge')
        require(is_int(seed), 'bad-row', path, f'row {i} seed must be an integer')
        require(start <= seed <= end, 'seed-out-of-range', path,
                f'row {i} seed {seed} outside {start}..{end}')
        require(isinstance(edge, str) and EDGE_RE.fullmatch(edge) is not None, 'bad-edge', path,
                f'seed {seed} edge must be a fixed six-decimal string')
        require(math.isfinite(float(edge)), 'bad-edge', path, f'seed {seed} edge not finite')
        if engine:
            gas = row.get('after_swap_peak_gas')
            require(is_int(gas) and gas >= 0, 'bad-gas', path,
                    f'seed {seed} after_swap_peak_gas must be a nonnegative integer')
    seeds = [r['seed'] for r in rows]
    require(len(set(seeds)) == len(seeds), 'duplicate-seed', path, 'seeds must be unique')
    require(seeds == sorted(seeds), 'unsorted-seeds', path, 'seeds must be ascending')
    if len(rows) != expected:
        missing = sorted(set(range(start, end + 1)) - set(seeds))
        raise Finding('missing-rows', path, f'{len(rows)} of {expected} rows; first missing '
                      f'{missing[:5]}', 'missing')
    return {r['seed']: r['edge'] for r in rows}


# ---------------------------------------------------------------- reference
def decode_envelope(ctx, entry):
    name = entry.get('path')
    ctx.safe('reference/' + str(name))
    env_rel = 'reference/' + str(entry.get('envelope'))
    raw = ctx.read(env_rel)
    require(sha256(raw) == entry.get('envelope_sha256'), 'stale-digest', env_rel,
            'envelope sha256 differs from reference/sources.json')
    box = load_json_bytes(raw, env_rel)
    require(box.get('encoding') == 'base64+zlib' and box.get('path') == name
            and box.get('sha256') == entry.get('sha256') and box.get('size') == entry.get('size'),
            'bad-envelope', env_rel, 'envelope metadata differs from inventory')
    try:
        data = zlib.decompress(base64.b64decode(box['data'], validate=True))
    except Exception as exc:  # noqa: BLE001 - any decode failure is a finding
        raise Finding('bad-envelope', env_rel, f'cannot decode: {type(exc).__name__}') from None
    require(str(len(data)) == str(entry.get('size')) and sha256(data) == entry.get('sha256'),
            'stale-digest', env_rel, 'decoded bytes differ from pinned sha256/size')
    return data


def reference_inventory(ctx):
    raw = ctx.read(SOURCES)
    require(sha256(raw) == SOURCES_SHA256, 'stale-digest', SOURCES,
            'reference inventory sha256 differs from the pinned ' + SOURCES_SHA256)
    inv = load_json_bytes(raw, SOURCES)
    check_credit(inv, SOURCES)
    require(inv.get('taylor_commit') == TAYLOR_COMMIT, 'bad-pin', SOURCES, 'taylor_commit changed')
    return inv


def inventory_entry(inv, name):
    hits = [f for f in inv.get('files', []) if isinstance(f, dict) and f.get('path') == name]
    require(len(hits) == 1, 'bad-inventory', SOURCES, f'expected exactly one entry for {name}')
    return hits[0]


def part_reference(ctx):
    inv = reference_inventory(ctx)
    files = inv.get('files')
    require(isinstance(files, list) and files, 'bad-inventory', SOURCES, 'files must be a list')
    names = [f.get('path') if isinstance(f, dict) else None for f in files]
    require(len(set(names)) == len(names), 'bad-inventory', SOURCES, 'duplicate inventory path')
    for entry in files:
        decode_envelope(ctx, entry)
    require(inventory_entry(inv, 'qt1/Strategy.sol')['sha256'] == QT1_SHA256, 'bad-pin', SOURCES,
            'qt1/Strategy.sol sha256 is not the pinned qt1 digest')
    require(inventory_entry(inv, 'taylor/calib_out_2048.json')['sha256'] == CALIB_SHA256,
            'bad-pin', SOURCES, 'calib_out_2048.json sha256 changed')
    return {'envelopes_verified': len(files)}


# ---------------------------------------------------------------- kit + ammfast
def part_kit(ctx):
    for rel in KIT_FILES:
        data = ctx.read(rel)
        check_header(data, rel)
        if rel.endswith('.py'):
            try:
                ast.parse(data, rel)
            except SyntaxError as exc:
                raise Finding('syntax-error', rel, f'line {exc.lineno}: {exc.msg}') from None
    return {'files': len(KIT_FILES)}


def ammfast_sources(ctx):
    base = ctx.safe('ammfast')
    if not base.is_dir():
        raise Finding('missing-file', 'ammfast/', 'ammfast package is absent', 'missing')
    out = []
    for p in sorted(base.rglob('*.py')):
        rel = p.relative_to(ctx.root).as_posix()
        if '__pycache__' not in rel.split('/'):
            out.append(rel)
    return out


def _signature(fn):
    args = fn.args
    names = [a.arg for a in args.posonlyargs + args.args]
    defaults = [None] * (len(names) - len(args.defaults)) + list(args.defaults)
    return names, defaults


def part_ammfast(ctx):
    sources = ammfast_sources(ctx)
    require(AMMFAST_INIT in sources, 'missing-file', AMMFAST_INIT, 'ammfast/__init__.py absent')
    text = {}
    for rel in sources:
        data = ctx.read(rel)
        check_header(data, rel)
        try:
            text[rel] = ast.parse(data, rel)
        except SyntaxError as exc:
            raise Finding('syntax-error', rel, f'line {exc.lineno}: {exc.msg}') from None
    funcs = {}
    exported = set()
    for node in text[AMMFAST_INIT].body:
        if isinstance(node, ast.FunctionDef):
            funcs[node.name] = node
        elif isinstance(node, (ast.ImportFrom,)):
            exported.update(a.asname or a.name for a in node.names)
    for name, first in (('run', 'seed'), ('run_batch', 'seeds')):
        if name in funcs:
            names, defaults = _signature(funcs[name])
            require(names[:3] == [first, 'policy', 'head_params'], 'bad-api', AMMFAST_INIT,
                    f'{name} must take ({first}, policy, head_params=None)')
            require(isinstance(defaults[2], ast.Constant) and defaults[2].value is None,
                    'bad-api', AMMFAST_INIT, f'{name} head_params must default to None')
        else:
            require(name in exported, 'bad-api', AMMFAST_INIT,
                    f'ammfast.{name} is neither defined nor imported in __init__.py')
    blob = b''.join(ctx.read(r) for r in sources).decode('utf-8', 'replace')
    for policy in POLICIES:
        require(repr(policy) in blob or f'"{policy}"' in blob, 'bad-api', 'ammfast/',
                f'policy id {policy!r} not found in ammfast sources')
    return {'sources': len(sources)}


# ---------------------------------------------------------------- engine
def manifest_rows(ctx):
    out = {}
    for line in ctx.read('bootstrap/manifest.tsv').decode().splitlines():
        if line and not line.startswith('#'):
            cols = line.split('\t')
            if len(cols) >= 6 and cols[0] == 'linux-x86_64:engine':
                out[cols[4]] = {'sha256': cols[2], 'url': cols[5]}
    return out


def pin_value(ctx, key):
    m = re.search(rf'^{key}=(\S+)$', ctx.read('bootstrap/pins.sh').decode(), re.M)
    require(m is not None, 'bad-pin', 'bootstrap/pins.sh', f'{key} not found')
    return m.group(1).strip('\'"')


def part_engine(ctx):
    bank = ctx.json(ENGINE_BANK)
    check_credit(bank, ENGINE_BANK)
    require(bank.get('backend') == 'engine' and bank.get('policy') == 'qt1', 'bad-identity',
            ENGINE_BANK, 'backend/policy must be engine/qt1')
    require(bank.get('screening_only') is True, 'bad-identity', ENGINE_BANK,
            'screening_only must be true')
    require(bank.get('status') == 'complete' and bank.get('remaining_seeds') == [], 'incomplete',
            ENGINE_BANK, 'engine bank must be complete with no remaining seeds')
    require(bank.get('failures') == [], 'engine-failures', ENGINE_BANK, 'failures must be empty')
    rows = bank.get('rows')
    edges = check_rows(rows, ENGINE_BANK, ENGINE_FIRST, ENGINE_LAST, engine=True)
    prov = bank.get('provenance')
    require(isinstance(prov, dict), 'bad-provenance', ENGINE_BANK, 'provenance must be an object')
    require(prov.get('qt1_source_sha256') == QT1_SHA256, 'bad-provenance', ENGINE_BANK,
            'qt1_source_sha256 is not the pinned qt1 digest')
    inv = reference_inventory(ctx)
    qt1 = decode_envelope(ctx, inventory_entry(inv, 'qt1/Strategy.sol'))
    require(sha256(qt1) == QT1_SHA256, 'stale-digest', 'reference/envelopes', 'qt1 bytes differ')
    require(prov.get('engine_commit') == ENGINE_COMMIT == pin_value(ctx, 'IMD_ENGINE_COMMIT'),
            'bad-provenance', ENGINE_BANK, 'engine_commit differs from bootstrap/pins.sh')
    require(prov.get('native_sha256') == pin_value(ctx, 'IMD_ENGINE_SO_SHA256'), 'bad-provenance',
            ENGINE_BANK, 'native_sha256 differs from IMD_ENGINE_SO_SHA256')
    require(prov.get('wheel_version') == pin_value(ctx, 'IMD_ENGINE_WHEEL_VERSION'),
            'bad-provenance', ENGINE_BANK, 'wheel_version differs from bootstrap/pins.sh')
    for key, rel in (('engine_eval_sha256', 'engine_eval.py'),
                     ('bootstrap_smoke_sha256', 'bootstrap/smoke_engine.py')):
        require(prov.get(key) == sha256(ctx.read(rel)), 'stale-digest', ENGINE_BANK,
                f'provenance {key} differs from committed {rel}')
    manifest = manifest_rows(ctx)
    downloads = prov.get('downloads')
    require(isinstance(downloads, dict) and downloads, 'bad-provenance', ENGINE_BANK,
            'downloads must be a nonempty object')
    wheels = [n for n in downloads if n.startswith('amm_sim_rs-') and n.endswith('.whl')]
    require(len(wheels) == 1, 'bad-provenance', ENGINE_BANK, 'exactly one amm_sim_rs release wheel')
    for name, item in downloads.items():
        require(name in manifest and item == manifest[name], 'stale-digest', ENGINE_BANK,
                f'download {name} sha256/url differs from bootstrap/manifest.tsv')
    require('-' + prov.get('wheel_version', '?') + '-' in wheels[0], 'bad-provenance', ENGINE_BANK,
            'wheel file name does not carry wheel_version')
    require(prov.get('workers') == 8, 'bad-provenance', ENGINE_BANK, 'run_batch worker count must be 8')
    require(isinstance(prov.get('config_construction'), str) and prov['config_construction'],
            'bad-provenance', ENGINE_BANK, 'config_construction must be recorded')
    configs = prov.get('configs')
    require(isinstance(configs, list) and len(configs) == len(rows), 'bad-config', ENGINE_BANK,
            'one config per row is required')
    require([c.get('seed') if isinstance(c, dict) else None for c in configs] == list(edges),
            'bad-config', ENGINE_BANK, 'config seeds must equal row seeds in order')
    require(sha256(json.dumps(configs, sort_keys=True).encode()) == prov.get('configs_sha256'),
            'stale-digest', ENGINE_BANK, 'configs_sha256 does not match the committed configs')
    require(is_int(prov.get('config_fields_compared')) and prov['config_fields_compared'] > 0,
            'bad-config', ENGINE_BANK, 'config_fields_compared must be a positive integer')

    audit = ctx.json(ENGINE_AUDIT)
    check_credit(audit, ENGINE_AUDIT)
    raw, audits = audit.get('unrounded_rows'), audit.get('audits')
    require(isinstance(raw, list) and isinstance(audits, list) and len(raw) == len(audits) == len(rows),
            'bad-audit', ENGINE_AUDIT, 'audit must hold one unrounded row and one audit per bank row')
    for row, orig, ev in zip(rows, raw, audits):
        seed = row['seed']
        require(isinstance(orig, dict) and orig.get('seed') == seed and is_number(orig.get('edge'))
                and isinstance(orig.get('edge'), float), 'bad-audit', ENGINE_AUDIT,
                f'unrounded row for {seed} missing or not a finite float')
        require(format(orig['edge'], '.6f') == row['edge'], 'rounding-mismatch', ENGINE_AUDIT,
                f'seed {seed}: bank edge is not format(unrounded, ".6f")')
        require(orig.get('after_swap_peak_gas') == row['after_swap_peak_gas'], 'gas-mismatch',
                ENGINE_AUDIT, f'seed {seed}: unrounded gas differs from bank')
        require(isinstance(ev, dict) and ev.get('seed') == seed and isinstance(ev.get('gas_stats'), list),
                'bad-audit', ENGINE_AUDIT, f'audit for {seed} missing gas_stats')
        for st in ev['gas_stats']:
            require(isinstance(st, dict) and all(is_int(st.get(k)) and st[k] >= 0
                                                 for k in ENGINE_COUNTERS),
                    'bad-gas', ENGINE_AUDIT, f'seed {seed}: gas counters must be nonnegative ints')
            require(all(st[k] == 0 for k in ENGINE_ERRORS), 'engine-error', ENGINE_AUDIT,
                    f'seed {seed}: nonzero error counter in {st.get("role")}/{st.get("callback")}')
        swaps = [s for s in ev['gas_stats'] if s.get('role') == 'Submission'
                 and s.get('callback') == 'afterSwap']
        require(swaps and sum(s['calls'] for s in swaps) > 0 and all(s['calls'] == s['ok'] for s in swaps),
                'engine-error', ENGINE_AUDIT, f'seed {seed}: missing or failed afterSwap calls')
        require(max(s['max_ok_gas'] for s in swaps) == row['after_swap_peak_gas'], 'gas-mismatch',
                ENGINE_AUDIT, f'seed {seed}: afterSwap peak gas differs from bank')

    val = ctx.json(ENGINE_VALIDATION)
    check_credit(val, ENGINE_VALIDATION)
    for key, rel in (('bank_sha256', ENGINE_BANK), ('audit_sha256', ENGINE_AUDIT),
                     ('validator_sha256', 'baselines/engine/validate.py'),
                     ('reference_inventory_sha256', SOURCES)):
        require(val.get(key) == ctx.digests.get(rel) or val.get(key) == sha256(ctx.read(rel)),
                'stale-digest', ENGINE_VALIDATION, f'{key} differs from committed {rel}')
    require(val.get('status') == 'complete' and val.get('count') == len(rows)
            and val.get('remaining_seeds') == [] and val.get('screening_only') is True
            and val.get('all_gas_counters_valid') is True and val.get('failed_seed_attempts') == [],
            'bad-validation', ENGINE_VALIDATION, 'validation must record a complete, failure-free bank')
    require(val.get('taylor_commit') == TAYLOR_COMMIT, 'bad-pin', ENGINE_VALIDATION, 'taylor_commit')
    origin = val.get('reference_origin')
    require(isinstance(origin, dict) and origin.get('sha256') == QT1_SHA256, 'bad-validation',
            ENGINE_VALIDATION, 'reference_origin must be the pinned qt1 source')
    require(val.get('config_fields_compared') == prov['config_fields_compared']
            and val.get('singleton_worker_argument') == 8, 'bad-validation', ENGINE_VALIDATION,
            'config/worker evidence differs from bank provenance')
    raw_by_seed = {r['seed']: r for r in raw}
    reruns = val.get('reruns')
    require(isinstance(reruns, list) and reruns, 'bad-validation', ENGINE_VALIDATION, 'no reruns')
    for rr in reruns:
        seed = rr.get('seed') if isinstance(rr, dict) else None
        require(seed in raw_by_seed and rr.get('exact_unrounded_edge_equal') is True
                and rr.get('exact_after_swap_peak_gas_equal') is True
                and rr.get('edge') == raw_by_seed[seed]['edge']
                and rr.get('after_swap_peak_gas') == raw_by_seed[seed]['after_swap_peak_gas'],
                'bad-validation', ENGINE_VALIDATION, f'rerun {seed} does not match audit evidence')
    ctx.rows[ENGINE_BANK] = len(rows)
    return {'rows': len(rows), 'audits': len(audits), 'reruns': len(reruns)}


# ---------------------------------------------------------------- HA
def part_ha(ctx):
    bank = ctx.json(HA_BANK)
    check_credit(bank, HA_BANK)
    require(bank.get('backend') == 'replica' and bank.get('policy') == 'HA', 'bad-identity', HA_BANK,
            'backend/policy must be replica/HA')
    rows = bank.get('rows')
    edges = check_rows(rows, HA_BANK, *HA_RANGE)
    prov = bank.get('provenance')
    require(isinstance(prov, dict) and prov.get('source_sha256') == CALIB_SHA256
            and prov.get('source_commit') == TAYLOR_COMMIT
            and prov.get('selection') == {'field': 'kind', 'equals': 'HA'}
            and prov.get('edge_field') == 'edge' and prov.get('seed_range') == list(HA_RANGE)
            and prov.get('row_count') == len(rows), 'bad-provenance', HA_BANK,
            'provenance must name calib_out_2048.json, kind=HA selection, edge field and range')
    inv = reference_inventory(ctx)
    src = load_json_bytes(decode_envelope(ctx, inventory_entry(inv, 'taylor/calib_out_2048.json')),
                          'taylor/calib_out_2048.json')
    require(isinstance(src, list), 'bad-source', 'taylor/calib_out_2048.json', 'expected a list')
    require(prov.get('source_rows') == len(src), 'bad-provenance', HA_BANK, 'source_rows mismatch')
    ha = {}
    for r in src:
        require(isinstance(r, dict) and isinstance(r.get('kind'), str) and is_int(r.get('seed'))
                and is_number(r.get('edge')), 'bad-source', 'taylor/calib_out_2048.json',
                'source rows need string kind, integer seed and finite edge')
        if r['kind'] == 'HA':
            require(r['seed'] not in ha, 'bad-source', 'taylor/calib_out_2048.json',
                    f'duplicate HA seed {r["seed"]}')
            ha[r['seed']] = r['edge']
    require(sorted(ha) == list(edges), 'ha-mapping', HA_BANK, 'HA source seeds differ from bank seeds')
    for seed, edge in edges.items():
        require(format(ha[seed], '.6f') == edge, 'ha-mapping', HA_BANK,
                f'seed {seed}: edge is not format(source kind=HA edge, ".6f")')
    ctx.rows[HA_BANK] = len(rows)
    return {'rows': len(rows), 'source_rows': len(src)}


# ---------------------------------------------------------------- replica
def check_records(items, name, expected, banks, path):
    require(isinstance(items, list), 'bad-validation', path, f'{name} must be a list')
    keys = {'policy', 'seed', 'cpython_edge', 'ammfast_edge', 'abs_error'}
    seen = set()
    worst = 0.0
    for i, it in enumerate(items):
        require(isinstance(it, dict) and set(it) == keys, 'bad-validation', path,
                f'{name}[{i}] must have exactly {sorted(keys)}')
        key = (it['policy'], it['seed'])
        require(it['policy'] in POLICIES and is_int(it['seed']), 'bad-validation', path,
                f'{name}[{i}] has an unknown policy or non-integer seed')
        require(key not in seen, 'bad-validation', path, f'{name} duplicates {key}')
        seen.add(key)
        c, a, e = it['cpython_edge'], it['ammfast_edge'], it['abs_error']
        require(all(is_number(v) for v in (c, a, e)), 'bad-validation', path,
                f'{name} {key}: edges and abs_error must be finite numbers')
        err = abs(c - a)
        require(e >= 0 and abs(err - e) <= 1e-12, 'bad-validation', path,
                f'{name} {key}: reported abs_error {e!r} != |cpython-ammfast| {err!r}')
        require(err <= TOLERANCE, 'parity-error', path, f'{name} {key}: |error| {err!r} > 1e-9')
        worst = max(worst, err)
        for (pol, a0, b0), edges in banks.items():
            if pol == it['policy'] and a0 <= it['seed'] <= b0:
                require(format(a, '.6f') == edges[it['seed']], 'spot-format', path,
                        f'{name} {key}: format(ammfast_edge) != bank edge {edges[it["seed"]]}')
    missing = sorted(expected - seen)
    extra = sorted(seen - expected)
    require(not missing and not extra, 'bad-validation', path,
            f'{name} must cover exactly {len(expected)} records; missing {missing[:3]} extra {extra[:3]}')
    return worst


def part_replica(ctx):
    P = 'replica'
    banks = {}
    problems = []
    for policy in POLICIES:
        for start, end in WINDOWS:
            rel = replica_bank_path(policy, start, end)
            try:
                doc = ctx.json(rel)
                check_credit(doc, rel)
                require(doc.get('backend') == 'replica' and doc.get('policy') == policy,
                        'bad-identity', rel, f'backend/policy must be replica/{policy}')
                require(isinstance(doc.get('provenance'), dict) and doc['provenance'], 'bad-provenance',
                        rel, 'provenance must be a nonempty object')
                sr = doc['provenance'].get('seed_range')
                require(sr is None or sr == [start, end], 'bad-provenance', rel,
                        'provenance seed_range differs from file name')
                banks[(policy, start, end)] = check_rows(doc.get('rows'), rel, start, end)
                ctx.rows[rel] = len(doc['rows'])
            except Finding as f:
                problems.append(f)
    for f in problems:
        ctx.fail(P, f.code, f.path, f.detail, f.kind)
    val = ctx.json(REPLICA_VALIDATION)
    check_credit(val, REPLICA_VALIDATION)
    require(val.get('complete') is True, 'incomplete', REPLICA_VALIDATION, 'complete must be true')
    bindings = val.get('bindings')
    require(isinstance(bindings, dict) and bindings, 'bad-bindings', REPLICA_VALIDATION,
            'bindings must be a nonempty {path: sha256} object')
    for rel, digest in sorted(bindings.items()):
        require(rel != REPLICA_VALIDATION, 'self-binding', REPLICA_VALIDATION,
                'validation.json must not bind itself (self-hash cycle)')
        require(isinstance(digest, str) and HEX64.fullmatch(digest), 'bad-bindings',
                REPLICA_VALIDATION, f'{rel}: digest must be 64 lowercase hex')
        ctx.safe(rel)
        if not ctx.exists(rel):
            raise Finding('stale-digest', rel, 'bound file is absent')
        require(sha256(ctx.read(rel)) == digest, 'stale-digest', rel,
                'file changed since baselines/replica/validation.json bound it')
    need = set(REPLICA_BANKS) | {SOURCES}
    try:
        need |= set(ammfast_sources(ctx))
    except Finding:
        pass
    absent = sorted(need - set(bindings))
    require(not absent, 'bad-bindings', REPLICA_VALIDATION, f'bindings omit {absent[:4]}')
    require(any(k.startswith('ammfast/') for k in bindings), 'bad-bindings', REPLICA_VALIDATION,
            'bindings must include the ammfast implementation')
    gates = {(p, s) for p in POLICIES for s in GATE_SEEDS}
    spots = {(p, s) for p in POLICIES for a, _ in WINDOWS for s in range(a, a + 64)}
    worst_gate = check_records(val.get('gates384'), 'gates384', gates, banks, REPLICA_VALIDATION)
    worst_spot = check_records(val.get('spots576'), 'spots576', spots, banks, REPLICA_VALIDATION)
    timings = val.get('timings')
    require(isinstance(timings, dict), 'bad-timings', REPLICA_VALIDATION, 'timings must be an object')
    rates = timings.get('runs_per_second_per_core')
    require(isinstance(rates, dict) and set(rates) == set(POLICIES)
            and all(is_number(v) and v > 0 for v in rates.values()), 'bad-timings',
            REPLICA_VALIDATION, 'runs_per_second_per_core needs a finite positive rate per policy')
    jit = timings.get('cold_jit_seconds')
    require(is_number(jit) and jit >= 0, 'bad-timings', REPLICA_VALIDATION,
            'cold_jit_seconds must be a finite nonnegative number')
    if problems:
        raise _Reported()
    return {'banks': len(banks), 'gates': len(gates), 'spots': len(spots),
            'worst_gate_abs_error': worst_gate, 'worst_spot_abs_error': worst_spot}


class _Reported(Exception):
    """Findings already recorded; mark the part failed without a duplicate entry."""


RUNNERS = {'reference': part_reference, 'kit': part_kit, 'ammfast': part_ammfast,
           'engine': part_engine, 'ha': part_ha, 'replica': part_replica}


def validate(root, parts=PARTS, timing=None):
    ctx = Context(root)
    report = {}
    for part in parts:
        started = time.perf_counter()
        before = len(ctx.findings)
        try:
            summary = RUNNERS[part](ctx)
            status = 'pass' if len(ctx.findings) == before else 'fail'
        except _Reported:
            summary, status = None, 'fail'
        except Finding as f:
            ctx.fail(part, f.code, f.path, f.detail, f.kind)
            summary, status = None, 'fail'
        if status == 'fail' and all(x['kind'] == 'missing' for x in ctx.findings[before:]):
            status = 'missing'
        report[part] = {'status': status, 'summary': summary}
        if timing is not None:
            timing[part] = time.perf_counter() - started
    expected = {k: v for k, v in EXPECTED_ROWS.items()
                if (k in REPLICA_BANKS and 'replica' in parts) or (k == HA_BANK and 'ha' in parts)
                or (k == ENGINE_BANK and 'engine' in parts)}
    verified = {k: ctx.rows.get(k, 0) for k in sorted(expected)}
    result = {
        '_credit': CREDIT,
        'validator': 'validate_kit.py',
        'version': VERSION,
        'status': 'PASS' if all(r['status'] == 'pass' for r in report.values()) else 'FAIL',
        'parts': report,
        'coverage': {'expected_rows': sum(expected.values()),
                     'verified_rows': sum(v for k, v in verified.items()
                                          if report[_part_of(k)]['status'] == 'pass'),
                     'banks': {k: {'expected': expected[k], 'rows_well_formed': verified[k],
                                   'part_status': report[_part_of(k)]['status']} for k in verified}},
        'findings': sorted(ctx.findings, key=lambda x: (x['part'], x['path'], x['code'], x['detail'])),
        'inputs_sha256': dict(sorted(ctx.digests.items())),
        'limits': 'Structural/provenance checks of committed evidence only; no simulator, CPython '
                  'replica2 or engine was run, so a PASS is not independent parity proof.',
    }
    return result


def _part_of(bank):
    return 'engine' if bank == ENGINE_BANK else 'ha' if bank == HA_BANK else 'replica'


def write_out(root, rel, data):
    root = Path(root).resolve()
    target = (root / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
    scratch = root / 'scratch'
    if not str(target).startswith(str(scratch) + os.sep):
        raise SystemExit(f'--out must be under {scratch}/ (runtime writes stay in scratch/)')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.validate-', dir=target.parent)
    with os.fdopen(fd, 'w') as fh:
        fh.write(data)
    os.replace(tmp, target)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--root', default=str(Path(__file__).resolve().parent),
                    help='repository root to validate (default: this file\'s directory)')
    ap.add_argument('--parts', default=','.join(PARTS),
                    help='comma list of ' + ','.join(PARTS) + ' (default: all)')
    ap.add_argument('--out', help='also write the JSON report here (must be under <root>/scratch/)')
    ap.add_argument('--timing', action='store_true', help='print per-part seconds to stderr')
    args = ap.parse_args(argv)
    parts = [p for p in args.parts.split(',') if p]
    bad = [p for p in parts if p not in PARTS]
    if bad or not parts or len(set(parts)) != len(parts):
        ap.error(f'unknown, duplicate or empty parts {bad}; choose from {",".join(PARTS)}')
    timing = {} if args.timing else None
    result = validate(args.root, parts, timing)
    text = json.dumps(result, indent=1, sort_keys=False) + '\n'
    if args.out:
        write_out(args.root, args.out, text)
    sys.stdout.write(text)
    if timing is not None:
        sys.stderr.write(json.dumps({k: round(v, 4) for k, v in timing.items()}) + '\n')
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
