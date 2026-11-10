# Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9,
# research/solved, solved-policy research: replica2.py and the filter/fee rule/tables are his.
"""The pinned CPython references, loaded from this repository's credited envelopes.

Standard library only.  Every original is read from ``reference/envelopes/``, checked against the
SHA256 pinned below (the same digests as ``reference/sources.json``), and executed unmodified:
``replica2.run`` is Taylor's simulator, ``qmref.QMRef`` is qt1-ref, ``twocomp.TwoComp`` is TwoComp and
``combo.Combo`` (combo-offsets) is COMBO.  ``head_policy`` adds the transparent pure-Python
residual head used to test the compiled hook.
"""
import base64
import hashlib
import json
import math
from pathlib import Path
import sys
import threading
import types
import zlib

from ._head import CAP, HEAD_PARAM_COUNT, MU, NH, SD, XCLIP, hidden_weights, nsum

ROOT = Path(__file__).resolve().parent.parent
TAYLOR_COMMIT = '4812a6382311624e1506aca131ca35c378f930d9'
SOURCES = 'reference/sources.json'
SOURCES_SHA256 = '62147ff112987f666e2c1ad5041012276a66033baec543c2b014f766cd8f01f5'
# original path -> (envelope path relative to the repository, original size, original sha256)
PINS = {
    'taylor/replica2.py': ('reference/envelopes/taylor__replica2.py.json', 7503,
                           '6e37b4e61e1ffa5ffb1e43912ddb8659105989aa986eb48fcd1d7608f8dca685'),
    'qt1/qmref.py': ('reference/envelopes/qt1__qmref.py.json', 7739,
                     '1dfc9b7a3c2237ff7a93dd9f0af54b9bdab16f2ce0a7eb00784cb4432598db92'),
    'qt1/blob.bin': ('reference/envelopes/qt1__blob.bin.json', 10167,
                     '3ac7cb8811ecfb6a17f499aa4e9b70dd55f7fb6432b3aec3b904e11a3dece38e'),
    'twocomp/twocomp.py': ('reference/envelopes/twocomp__twocomp.py.json', 11202,
                           '9d3c1833321493fb51bbde4e1b4dabc2f8978bd6d774ee2af8dc8aed13f0f0af'),
    'combo/combo.py': ('reference/envelopes/combo__combo.py.json', 5929,
                       'c5b711a0b4fb4783e8879a484bf87a427171f4a11aa5eb5ec3513c24ead1a796'),
}
BLOB_KEY = 'ammfast-pinned:qt1/blob.bin'   # QMRef._cache key; no file of this name is ever opened
_LOCK = threading.RLock()
_STATE = {}


class ReferenceIntegrityError(ValueError):
    """A reference envelope is missing or does not match its pinned digest."""


def original(name):
    """Verified original bytes of one pinned reference file."""
    envelope, size, digest = PINS[name]
    try:
        box = json.loads((ROOT / envelope).read_bytes())
        data = zlib.decompress(base64.b64decode(box['data'], validate=True))
    except (OSError, ValueError, KeyError, TypeError, zlib.error) as exc:
        raise ReferenceIntegrityError(f'cannot read reference envelope {envelope}: {exc}') from exc
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise ReferenceIntegrityError(f'reference {name} does not match its pinned sha256')
    return data


def reference_inventory():
    """{repository-relative path: sha256 of the file as stored} for every reference file used."""
    paths = [SOURCES] + [PINS[name][0] for name in sorted(PINS)]
    return {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}


def _execute(name, label, extra_modules=None):
    module = types.ModuleType('ammfast_reference_' + label)
    module.__file__ = 'pinned:' + name
    code = compile(original(name).decode('utf-8'), module.__file__, 'exec')
    saved = {}
    extra_modules = extra_modules or {}
    for key, value in extra_modules.items():      # make "from twocomp import ..." resolve, then restore
        saved[key] = sys.modules.get(key)
        sys.modules[key] = value
    try:
        exec(code, module.__dict__)
    finally:
        for key, previous in saved.items():
            if previous is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = previous
    return module


def modules():
    """The four unmodified reference modules: replica2, qmref, twocomp, combo (loaded once)."""
    with _LOCK:
        if 'modules' not in _STATE:
            replica2 = _execute('taylor/replica2.py', 'replica2')
            qmref = _execute('qt1/qmref.py', 'qmref')
            twocomp = _execute('twocomp/twocomp.py', 'twocomp')
            combo = _execute('combo/combo.py', 'combo', {'twocomp': twocomp})
            _STATE['modules'] = types.SimpleNamespace(replica2=replica2, qmref=qmref, twocomp=twocomp, combo=combo)
        return _STATE['modules']


def _decoded(module):
    """Decode the pinned blob with this module's own Tables (about 1.5 s, once per process); its
    QMRef constructor then finds the tables under BLOB_KEY and never opens a file."""
    with _LOCK:
        if BLOB_KEY not in module.QMRef._cache:
            module.QMRef._cache[BLOB_KEY] = module.Tables(original('qt1/blob.bin'))
        return module.QMRef._cache[BLOB_KEY]


def tables():
    """qt1's decoded fee tables (qmref.Tables): the arrays the compiled kernel interpolates."""
    return _decoded(modules().qmref)


def base_policy(policy):
    """A fresh, unmodified reference policy object for a public policy ID."""
    ref = modules()
    if policy == 'qt1-ref':
        _decoded(ref.qmref)
        return ref.qmref.QMRef(BLOB_KEY)
    if policy == 'TwoComp':
        _decoded(ref.twocomp)
        return ref.twocomp.TwoComp(BLOB_KEY)
    if policy == 'COMBO':
        _decoded(ref.twocomp)
        return ref.combo.Combo(BLOB_KEY)
    raise ValueError(f'unknown policy {policy!r}')


def _head_class():
    with _LOCK:
        if 'head' in _STATE:
            return _STATE['head']
        ref = modules()
        tc = ref.twocomp
        phi, trunc, LD_M, LD_S, MAXF = tc.phi, tc.trunc, tc.LD_M, tc.LD_S, tc.MAXF
        HU, HC = hidden_weights()

        class ComboHead(ref.combo.Combo):
            """COMBO plus the residual head, in plain Python.

            ``on_trade`` is combo.Combo.on_trade restated statement for statement; the lines marked
            HEAD are the only additions.  With 18 zero weights it returns COMBO's fees exactly.
            """

            def __init__(self, blob_path, weights):
                super().__init__(blob_path)
                if len(weights) != HEAD_PARAM_COUNT:
                    raise ValueError('head needs 18 weights')
                self.w = [float(v) for v in weights]
                self.pi = 1.0                                                   # HEAD

            def on_trade(self, is_buy, ax, ay, t, x, y, arb=False, p=100., s2=100., j=0):
                s = y / x; dt = t - self.tlast; is_arb = False; mix = None
                sp_prev = self.sp; silent = 0                                   # HEAD
                if dt >= 1:
                    self.cnt = 0
                    lo = math.log(self.sp * (1 - self.fb)); hi = math.log(self.sp / (1 - self.fa))
                    prior = self.propagate(dt, lo, hi)
                    lp = math.log(s * (1 - self.fb)) if is_buy else math.log(s / (1 - self.fa))
                    la = 0.0; Z = 0.0; retail = []
                    for w, m, v in prior:
                        sd = math.sqrt(v)
                        la += w * phi((lp - m) / sd) / sd
                        z, mt, vt = trunc(m, v, lo, hi)
                        Z += w * z
                        retail.append((w * z, mt, vt))
                    if Z > 1e-300:
                        ret = [(w / Z, m, v) for w, m, v in retail]
                        mt, vt = self.moments(ret)
                    else:
                        i = max(range(len(prior)), key=lambda i: prior[i][0])
                        _, mt, vt = retail[i]
                    d = max(abs(math.log(s / self.sp)), 1e-9)
                    lr = Z * .26 * phi((math.log(d) - LD_M) / LD_S) / (d * LD_S)
                    pi = la / (la + lr) if la + lr > 0 else 0.
                    is_arb = pi > .5
                    self.pi = pi; silent = min(dt - 1, 12)                      # HEAD
                    if .005 < pi < .995:
                        self.comp = [(pi, lp, 0.), (1 - pi, mt, vt)]
                        pa = math.exp(lp); pr = math.exp(mt); old_s2 = self.s2h
                        s2_arb = max(pa * .997, min(pa / .997, old_s2))
                        s2_ret = max(pr * .997, min(pr / .997, old_s2))
                        cand_ret = s * (1 - self.fb) / .997 if is_buy else s * .997 / (1 - self.fa)
                        synced_ret = (cand_ret < s2_ret) if is_buy else (cand_ret > s2_ret)
                        if synced_ret: s2_ret = cand_ret
                        mix = (pi, lp, mt, vt, s2_arb, s2_ret, synced_ret)
                    elif is_arb:
                        self.comp = [(1.0, lp, 0.)]
                    else:
                        self.comp = [(1.0, mt, vt)]
                    self.m, self.v = self.moments(self.comp)
                    pe = math.exp(self.m)
                    self.s2h = max(pe * .997, min(pe / .997, self.s2h))
                    self.synced = False
                if not is_arb:
                    cand = s * (1 - self.fb) / .997 if is_buy else s * .997 / (1 - self.fa)
                    if (cand < self.s2h) if is_buy else (cand > self.s2h):
                        self.s2h = cand; self.synced = True
                    self.cnt += 1
                    ph = 1 if self.cnt == 1 else 2
                else:
                    ph = 0
                self.tlast = t; self.sp = s
                ls = math.log(s)
                if mix is None:
                    fb, fa = self.lookup((ls - self.m) * 1e4, (math.log(self.s2h) - self.m) * 1e4, ph, math.sqrt(self.v) * 1e4)
                else:
                    pi, lp, mt, vt, s2_arb, s2_ret, synced_ret = mix
                    aa, ba = self.lookup_offsets((ls - lp) * 1e4, (math.log(s2_arb) - lp) * 1e4, 0, 0., False)
                    ar, br = self.lookup_offsets((ls - mt) * 1e4, (math.log(s2_ret) - mt) * 1e4, 1, math.sqrt(vt) * 1e4, synced_ret)
                    a = pi * aa + (1 - pi) * ar; b = pi * ba + (1 - pi) * br
                    w = (ls - self.m) * 1e4
                    fa = 1.0 - math.exp(-(a - w) * 1e-4) if a > w else 0.0
                    fb = 1.0 - math.exp(-(b + w) * 1e-4) if b > -w else 0.0
                    self.new_path_calls += 1
                # ---- HEAD: bounded correction added to the UNCLAMPED base fees, then one clamp ----
                sgn = 1.0 if is_buy else -1.0
                feat = (sgn * ((self.m - math.log(sp_prev)) * 1e4),
                        math.sqrt(self.v) * 1e4,
                        self.pi,
                        sgn * ((ls - self.m) * 1e4),
                        sgn * ((math.log(self.s2h) - self.m) * 1e4),
                        float(ph),
                        math.log1p(abs(math.log(s / sp_prev)) * 1e4),
                        math.log1p(float(silent)))
                xn = [min(XCLIP, max(-XCLIP, (feat[i] - MU[i]) / SD[i])) for i in range(8)]
                zb = self.w[NH]; za = self.w[2 * NH + 1]
                for k in range(NH):
                    h = math.tanh(HC[k] + nsum(HU[k][i] * xn[i] for i in range(8)))
                    zb += self.w[k] * h; za += self.w[NH + 1 + k] * h
                d1 = CAP * math.tanh(zb); d2 = CAP * math.tanh(za)
                db, da = (d1, d2) if is_buy else (d2, d1)
                fb = min(MAXF, max(0.0, fb + db * 1e-4)); fa = min(MAXF, max(0.0, fa + da * 1e-4))
                self.fb, self.fa = fb, fa
                return fb, fa

        _STATE['head'] = ComboHead
        return ComboHead


def head_policy(weights):
    """A fresh pure-Python COMBO + residual-head policy (weights: 18 finite floats)."""
    _decoded(modules().twocomp)
    return _head_class()(BLOB_KEY, list(weights))


def run(seed, policy, weights=None, n_steps=10000):
    """Edge of Taylor's replica2.run for one fresh policy object (CPython; about a second per run)."""
    pol = base_policy(policy) if weights is None else head_policy(weights)
    return modules().replica2.run(seed, pol, n_steps=n_steps)['edge']
