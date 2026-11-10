# Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9,
# research/solved, solved-policy research: replica2.run, the Gaussian filter, the fee rule and the
# qm tables this kernel ports are his.
"""Numba kernel of replica2.run plus the qt1-ref (QMRef), TwoComp and COMBO policies and the COMBO
residual-head hook.

Everything from the "CPython-compatible Mersenne Twister" banner down to the end of ``sim`` is the
archived, parity-checked kernel ``reference/envelopes/numba__fastsim.py.json`` (original sha256
4d3d2187b5dcbcd979a0775e431d30421b0e376d3c582afc60fbb4d6a052596b) with these mechanical deltas only:
the per-callback logging (``lg_row``, ``ncb``) and the ``cfg`` override were removed, and ``sim``
returns just the policy edge.  No arithmetic statement was changed; there is no fastmath.
``sim_batch`` is new: one compiled call for any number of runs.

This module is private.  The public API is ``ammfast.run`` / ``ammfast.run_batch``.
"""
import math

import numpy as np
from numba import njit

from ._head import CAP, MU as _MU, NH, SD as _SD, XCLIP

# ---------------------------------------------------------------- constants (same values as the references)
K = 100.0 * 10000.0
MAXF = 0.1
NF = 0.003
SIG2 = .000945 ** 2
LD_M, LD_S = math.log(11.12e-4), 1.176
INIT = 0.002823606104386589
TWO = np.array([2.0])           # runtime exponent: keeps LLVM from folding pow(x, 2) into x*x (CPython calls libm pow)
MU = np.array(_MU, dtype=np.float64)
SD = np.array(_SD, dtype=np.float64)


# ---------------------------------------------------------------- CPython-compatible Mersenne Twister
@njit(cache=True)
def mt_seed(mt, seed):
    """random.Random(int seed), 0 < seed < 2**32: init_by_array([seed]).  mt: int64[625] (last = index)."""
    mt[0] = 19650218
    for i in range(1, 624):
        x = mt[i - 1] ^ (mt[i - 1] >> 30)
        mt[i] = (1812433253 * x + i) & 0xFFFFFFFF
    i = 1; j = 0
    for _ in range(624):
        x = mt[i - 1] ^ (mt[i - 1] >> 30)
        mt[i] = ((mt[i] ^ (x * 1664525)) + seed + j) & 0xFFFFFFFF
        i += 1; j += 1
        if i >= 624:
            mt[0] = mt[623]; i = 1
        if j >= 1:
            j = 0
    for _ in range(623):
        x = mt[i - 1] ^ (mt[i - 1] >> 30)
        mt[i] = ((mt[i] ^ (x * 1566083941)) - i) & 0xFFFFFFFF
        i += 1
        if i >= 624:
            mt[0] = mt[623]; i = 1
    mt[0] = 0x80000000
    mt[624] = 624


@njit(cache=True)
def mt_u32(mt):
    if mt[624] >= 624:
        for kk in range(227):
            y = (mt[kk] & 0x80000000) | (mt[kk + 1] & 0x7FFFFFFF)
            mt[kk] = mt[kk + 397] ^ (y >> 1) ^ (0x9908B0DF if (y & 1) else 0)
        for kk in range(227, 623):
            y = (mt[kk] & 0x80000000) | (mt[kk + 1] & 0x7FFFFFFF)
            mt[kk] = mt[kk - 227] ^ (y >> 1) ^ (0x9908B0DF if (y & 1) else 0)
        y = (mt[623] & 0x80000000) | (mt[0] & 0x7FFFFFFF)
        mt[623] = mt[396] ^ (y >> 1) ^ (0x9908B0DF if (y & 1) else 0)
        mt[624] = 0
    y = mt[mt[624]]
    mt[624] += 1
    y ^= y >> 11
    y ^= (y << 7) & 0x9D2C5680
    y ^= (y << 15) & 0xEFC60000
    y ^= y >> 18
    return y


@njit(cache=True)
def mt_random(mt):
    a = mt_u32(mt) >> 5
    b = mt_u32(mt) >> 6
    return (a * 67108864.0 + b) * (1.0 / 9007199254740992.0)


@njit(cache=True)
def mt_gauss(mt, gc):
    """random.Random.gauss(0.0, 1.0) incl. cached second variate gc = [flag, value]."""
    if gc[0] != 0.0:
        z = gc[1]; gc[0] = 0.0
    else:
        x2pi = mt_random(mt) * 6.283185307179586
        g2rad = math.sqrt(-2.0 * math.log(1.0 - mt_random(mt)))
        z = math.cos(x2pi) * g2rad
        gc[1] = math.sin(x2pi) * g2rad; gc[0] = 1.0
    return 0.0 + z * 1.0


@njit(cache=True)
def mt_lognorm(mt, mu, sigma):
    while True:
        u1 = mt_random(mt)
        u2 = 1.0 - mt_random(mt)
        z = 1.7155277699214135 * (u1 - 0.5) / u2
        zz = z * z / 4.0
        if zz <= -math.log(u2):
            break
    return math.exp(mu + z * sigma)


# ---------------------------------------------------------------- python-semantics helpers
@njit(cache=True)
def pmax(a, b):
    return b if b > a else a


@njit(cache=True)
def pmin(a, b):
    return b if b < a else a


@njit(cache=True)
def sq(x):
    return x ** TWO[0]


@njit(cache=True)
def phi(x):
    return math.exp(-.5 * x * x) / 2.5066282746310002


@njit(cache=True)
def Phi(x):
    return .5 * (1 + math.erf(x / 1.4142135623730951))


@njit(cache=True)
def trunc(m, v, lo, hi):
    if v <= 0:
        return (1.0 if (lo <= m and m <= hi) else 0.0), m, 0.0
    sd = math.sqrt(v); a = (lo - m) / sd; b = (hi - m) / sd; z = Phi(b) - Phi(a)
    if z < 1e-12:
        return z, (lo if m < lo else hi), v * .25
    pa = phi(a); pb = phi(b); r = (pa - pb) / z
    t = v * (1 + (a * pa - b * pb) / z - r * r)
    return z, m + sd * r, (t if t > 0 else 0.0)


@njit(cache=True)
def moments(w, x, u, n):
    m = 0.0
    for q in range(n):
        m = m + w[q] * x[q]
    v = 0.0
    for q in range(n):
        v = v + w[q] * (u[q] + sq(x[q] - m))
    return m, (v if v > 0.0 else 0.0)


@njit(cache=True)
def nsum8(t):
    """CPython >= 3.12 sum() of floats: Neumaier compensated summation, start 0.0 (head hidden pre-activation)."""
    f = 0.0; c = 0.0
    for i in range(8):
        x = t[i]
        tt = f + x
        if abs(f) >= abs(x): c += (f - tt) + x
        else: c += (x - tt) + f
        f = tt
    if c != 0.0 and abs(c) < math.inf: f += c
    return f


@njit(cache=True)
def pos2(W, v):
    n = len(W)
    if v <= W[0]: return 0.0
    if v >= W[n - 1]: return float(n - 1)
    lo = 0; hi = n - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if W[mid] <= v: lo = mid
        else: hi = mid
    return lo + (v - W[lo]) / (W[hi] - W[lo])


@njit(cache=True)
def lookup_off(A, B, S9, W2L, I, w, w2, phs, sd, synced):
    """QMRef.lookup up to (excluding) the fee conversion -> offsets (a, b) in bps"""
    NW = I[0]; W0 = I[1]; FB = I[2]; ns = len(S9) - 1; h = S9[ns]
    k = 0
    while k < ns - 2 and sd >= S9[k + 1]: k += 1
    tt = pmin(1.0, pmax(0.0, (sd - S9[k]) / (S9[k + 1] - S9[k])))
    if phs == 0: tag = 0
    else: tag = (1 if phs == 1 else 3) + (0 if synced else 1)
    pw = pmin(NW - 1.0, pmax(0.0, w / h + W0))
    l2 = w2 / h
    if phs == 0:
        l2 = pmin(float(FB), pmax(-float(FB), l2)); p2 = l2 + FB; n2 = 2 * FB + 1
    else:
        p2 = pos2(W2L, l2); n2 = I[3]
    i = min(int(pw), NW - 2); j = min(int(p2), n2 - 2); f = pw - i; g = p2 - j
    Ta = A[tag]; Tb = B[tag]
    a0 = (Ta[k, i, j] * (1 - f) + Ta[k, i + 1, j] * f) * (1 - g) + (Ta[k, i, j + 1] * (1 - f) + Ta[k, i + 1, j + 1] * f) * g
    a1 = (Ta[k + 1, i, j] * (1 - f) + Ta[k + 1, i + 1, j] * f) * (1 - g) + (Ta[k + 1, i, j + 1] * (1 - f) + Ta[k + 1, i + 1, j + 1] * f) * g
    b0 = (Tb[k, i, j] * (1 - f) + Tb[k, i + 1, j] * f) * (1 - g) + (Tb[k, i, j + 1] * (1 - f) + Tb[k, i + 1, j + 1] * f) * g
    b1 = (Tb[k + 1, i, j] * (1 - f) + Tb[k + 1, i + 1, j] * f) * (1 - g) + (Tb[k + 1, i, j + 1] * (1 - f) + Tb[k + 1, i + 1, j + 1] * f) * g
    return a0 * (1 - tt) + a1 * tt, b0 * (1 - tt) + b1 * tt


@njit(cache=True)
def to_fees(a, b, w):
    fa = 1.0 - math.exp(-(a - w) * 1e-4) if a > w else 0.0
    fb = 1.0 - math.exp(-(b + w) * 1e-4) if b > -w else 0.0
    return fb, fa


# ---------------------------------------------------------------- policy
# sf (float64[8]): 0 m, 1 v, 2 s2h, 3 fb, 4 fa, 5 sp, 6 pi    si (int64[8]): 0 tlast, 1 cnt, 2 synced, 3 silent, 4 ncomp, 5 new_path
# cw/cm/cv (float64[2]): mixture components of TwoComp
@njit(cache=True)
def policy_init(sf, si, cw, cm, cv):
    sf[0] = math.log(100); sf[1] = 0.0; sf[2] = 100.0; sf[3] = INIT; sf[4] = INIT; sf[5] = 100.0; sf[6] = 1.0
    si[0] = 0; si[1] = 0; si[2] = 0; si[3] = 0; si[4] = 1; si[5] = 0
    cw[0] = 1.0; cm[0] = sf[0]; cv[0] = 0.0; cw[1] = 0.0; cm[1] = 0.0; cv[1] = 0.0


@njit(cache=True)
def propagate(dt, lo, hi, cw, cm, cv, n, scr):
    steps = min(dt - 1, 12)
    tw = scr[0:2]; tm = scr[2:4]; tv = scr[4:6]
    for step in range(steps):
        if step == 2 and n > 1:
            m, v = moments(cw, cm, cv, n)
            cw[0] = 1.0; cm[0] = m; cv[0] = v; n = 1
        for q in range(n):
            z, mt, vt = trunc(cm[q], cv[q] + SIG2, lo, hi)
            tw[q] = cw[q] * z; tm[q] = mt; tv[q] = vt
        total = 0.0
        for q in range(n):
            total = total + tw[q]
        if total <= 1e-300:
            i = 0
            if n > 1 and cw[1] > cw[0]: i = 1
            cw[0] = 1.0; cm[0] = tm[i]; cv[0] = tv[i]; n = 1
        else:
            kk = 0
            for q in range(n):
                if tw[q] > 1e-300:
                    cw[kk] = tw[q] / total; cm[kk] = tm[q]; cv[kk] = tv[q]; kk += 1
            n = kk
    for q in range(n):
        cv[q] = cv[q] + SIG2
    return n


@njit(cache=True)
def on_trade(pol, hp, hon, A, B, S9, W2L, I, HU, HC, sf, si, cw, cm, cv, feat, scr, is_buy, t, x, y):
    """One afterSwap callback.  Inputs are ONLY (side, timestamp, reserves) -> (bid fee, ask fee), clamped.
    pol 0 QMRef, 1 TwoComp, 2 COMBO; hon: apply the residual head hp (18 weights) on top of COMBO."""
    s = y / x; dt = t - si[0]; is_arb = False; mix = False
    sp_prev = sf[5]
    pa = 0.0; pr = 0.0; mpi = 0.0; mlp = 0.0; mmt = 0.0; mvt = 0.0; s2_arb = 0.0; s2_ret = 0.0; synced_ret = False
    if dt >= 1:
        si[1] = 0
        fbq = sf[3]; faq = sf[4]
        lo = math.log(sf[5] * (1 - fbq)); hi = math.log(sf[5] / (1 - faq))
        if pol == 0:
            m = sf[0]; v = sf[1]
            for _ in range(min(dt - 1, 12)):
                v += SIG2
                _z, m, v = trunc(m, v, lo, hi)
            v += SIG2
            lp = math.log(s * (1 - fbq)) if is_buy else math.log(s / (1 - faq)); sd = math.sqrt(v)
            la = phi((lp - m) / sd) / sd
            Z, mt, vt = trunc(m, v, lo, hi); d = pmax(abs(math.log(s / sf[5])), 1e-9)
            lr = Z * .26 * phi((math.log(d) - LD_M) / LD_S) / (d * LD_S)
            pi = la / (la + lr) if la + lr > 0 else 0.0; is_arb = pi > .5
            if .005 < pi and pi < .995:
                m2 = pi * lp + (1 - pi) * mt; sf[0] = m2; sf[1] = pi * sq(lp - m2) + (1 - pi) * (vt + sq(mt - m2))
            elif is_arb:
                sf[0] = lp; sf[1] = 0.0
            else:
                sf[0] = mt; sf[1] = vt
            sf[6] = pi
        else:
            n = propagate(dt, lo, hi, cw, cm, cv, si[4], scr)
            lp = math.log(s * (1 - fbq)) if is_buy else math.log(s / (1 - faq))
            la = 0.0; Z = 0.0
            rw = scr[6:8]; rm = scr[8:10]; rv = scr[10:12]
            for q in range(n):
                sd = math.sqrt(cv[q])
                la += cw[q] * phi((lp - cm[q]) / sd) / sd
                z, mt_, vt_ = trunc(cm[q], cv[q], lo, hi)
                Z += cw[q] * z
                rw[q] = cw[q] * z; rm[q] = mt_; rv[q] = vt_
            if Z > 1e-300:
                nw = scr[12:14]
                for q in range(n): nw[q] = rw[q] / Z
                mt, vt = moments(nw, rm, rv, n)
            else:
                i = 0
                if n > 1 and cw[1] > cw[0]: i = 1
                mt = rm[i]; vt = rv[i]
            d = pmax(abs(math.log(s / sf[5])), 1e-9)
            lr = Z * .26 * phi((math.log(d) - LD_M) / LD_S) / (d * LD_S)
            pi = la / (la + lr) if la + lr > 0 else 0.0; is_arb = pi > .5
            sf[6] = pi; si[3] = min(dt - 1, 12)
            if .005 < pi and pi < .995:
                cw[0] = pi; cm[0] = lp; cv[0] = 0.0; cw[1] = 1 - pi; cm[1] = mt; cv[1] = vt; si[4] = 2
                if pol >= 2:
                    mix = True
                    pa = math.exp(lp); pr = math.exp(mt); old_s2 = sf[2]
                    s2_arb = pmax(pa * .997, pmin(pa / .997, old_s2))
                    s2_ret = pmax(pr * .997, pmin(pr / .997, old_s2))
                    cand_ret = s * (1 - fbq) / .997 if is_buy else s * .997 / (1 - faq)
                    synced_ret = (cand_ret < s2_ret) if is_buy else (cand_ret > s2_ret)
                    if synced_ret: s2_ret = cand_ret
                    mpi = pi; mlp = lp; mmt = mt; mvt = vt
            elif is_arb:
                cw[0] = 1.0; cm[0] = lp; cv[0] = 0.0; si[4] = 1
            else:
                cw[0] = 1.0; cm[0] = mt; cv[0] = vt; si[4] = 1
            sf[0], sf[1] = moments(cw, cm, cv, si[4])
        pe = math.exp(sf[0])
        sf[2] = pmax(pe * .997, pmin(pe / .997, sf[2]))
        si[2] = 0
    else:
        si[3] = 0
    if not is_arb:
        cand = s * (1 - sf[3]) / .997 if is_buy else s * .997 / (1 - sf[4])
        if (cand < sf[2]) if is_buy else (cand > sf[2]):
            sf[2] = cand; si[2] = 1
        si[1] += 1
        ph = 1 if si[1] == 1 else 2
    else:
        ph = 0
    si[0] = t; sf[5] = s
    ls = math.log(s)
    if not mix:
        a, b = lookup_off(A, B, S9, W2L, I, (ls - sf[0]) * 1e4, (math.log(sf[2]) - sf[0]) * 1e4, ph, math.sqrt(sf[1]) * 1e4, si[2] == 1)
        fb, fa = to_fees(a, b, (ls - sf[0]) * 1e4)
    else:
        aa, ba = lookup_off(A, B, S9, W2L, I, (ls - mlp) * 1e4, (math.log(s2_arb) - mlp) * 1e4, 0, 0.0, False)
        ar, br = lookup_off(A, B, S9, W2L, I, (ls - mmt) * 1e4, (math.log(s2_ret) - mmt) * 1e4, 1, math.sqrt(mvt) * 1e4, synced_ret)
        a = mpi * aa + (1 - mpi) * ar; b = mpi * ba + (1 - mpi) * br
        fb, fa = to_fees(a, b, (ls - sf[0]) * 1e4)
        si[5] += 1
    fbu = fb; fau = fa      # the head (like head.py's correct()) adds its correction to the UNCLAMPED base fee
    fb = pmin(MAXF, pmax(0.0, fb)); fa = pmin(MAXF, pmax(0.0, fa))
    sf[3] = fb; sf[4] = fa
    if pol >= 2:
        # live features (own callback / history only); logged for later heads even when hon is False
        sgn = 1.0 if is_buy else -1.0
        feat[0] = sgn * ((sf[0] - math.log(sp_prev)) * 1e4)
        feat[1] = math.sqrt(sf[1]) * 1e4
        feat[2] = sf[6]
        feat[3] = sgn * ((ls - sf[0]) * 1e4)
        feat[4] = sgn * ((math.log(sf[2]) - sf[0]) * 1e4)
        feat[5] = float(ph)
        feat[6] = math.log1p(abs(math.log(s / sp_prev)) * 1e4)
        feat[7] = math.log1p(float(si[3]))
        if hon:
            zb = hp[NH]; za = hp[2 * NH + 1]
            xn = np.empty(8)
            for i in range(8):
                xn[i] = pmin(XCLIP, pmax(-XCLIP, (feat[i] - MU[i]) / SD[i]))
            tv8 = np.empty(8)
            for k in range(NH):
                for i in range(8):
                    tv8[i] = HU[k, i] * xn[i]
                ssum = nsum8(tv8)
                h = math.tanh(HC[k] + ssum)
                zb += hp[k] * h; za += hp[NH + 1 + k] * h
            d1 = CAP * math.tanh(zb); d2 = CAP * math.tanh(za)
            if is_buy: db = d1; da = d2
            else: db = d2; da = d1
            fb = pmin(MAXF, pmax(0.0, fbu + db * 1e-4)); fa = pmin(MAXF, pmax(0.0, fau + da * 1e-4))
            sf[3] = fb; sf[4] = fa
    return fb, fa


# ---------------------------------------------------------------- replica2.run (Taylor, gtmcknight)
@njit(cache=True)
def clampf(f):
    return 0.0 if f < 0.0 else (MAXF if f > MAXF else f)


@njit(cache=True)
def sim(seed, n_steps, pol, hp, hon, A, B, S9, W2L, I, HU, HC):
    """One whole replica2.run: config draw, both RNG streams, market loop and every policy callback.
    All policy state is allocated here, so nothing survives the call.  Returns the policy pool edge e1."""
    mt = np.zeros(625, dtype=np.int64)
    mt_seed(mt, 10000000 + seed)
    size_mean = 19.0 + (21.0 - 19.0) * mt_random(mt)
    lam = 0.6 + (1.0 - 0.6) * mt_random(mt)
    sigma = 0.000882 + (0.001008 - 0.000882) * mt_random(mt)
    pr = np.zeros(625, dtype=np.int64); mt_seed(pr, seed); gc = np.zeros(2)
    rr = np.zeros(625, dtype=np.int64); mt_seed(rr, seed + 1)
    drift = -0.5 * sigma * sigma
    mu_ln = math.log(size_mean) - 0.5 * 1.2 * 1.2
    emlam = math.exp(-lam)
    p = 100.0
    x1 = 100.0; y1 = 10000.0; x2 = 100.0; y2 = 10000.0
    sf = np.zeros(8); si = np.zeros(8, dtype=np.int64); cw = np.zeros(2); cm = np.zeros(2); cv = np.zeros(2)
    feat = np.zeros(8); scr = np.zeros(16)
    policy_init(sf, si, cw, cm, cv)
    fb = clampf(INIT); fa = clampf(INIT)
    e1 = 0.0; e2 = 0.0
    g2 = 1.0 - NF
    sqrt = math.sqrt
    for t in range(n_steps):
        p *= math.exp(drift + sigma * mt_gauss(pr, gc))
        s2 = y2 / x2
        if s2 < p:
            nx = sqrt(K / (g2 * p)); ax = x2 - nx
            if ax > 0:
                ax = min(ax, x2 * 0.99)
                nrx = x2 - ax; nry = K / nrx; net_y = nry - y2; ty = net_y / g2
                prof = ax * p - ty
                if prof > 0:
                    x2 = nrx; y2 = nry; e2 -= prof
        elif s2 > p:
            xv = sqrt(K * g2 / p); ax = (xv - x2) / g2
            if ax > 0:
                nrx = x2 + ax * g2; nry = K / nrx; yo = y2 - nry
                prof = yo - ax * p
                if yo > 0 and prof > 0:
                    x2 = nrx; y2 = nry; e2 -= prof
        s1 = y1 / x1
        if s1 < p:
            g = 1.0 - fa
            if g > 0:
                nx = sqrt(K / (g * p)); ax = x1 - nx
                if ax > 0:
                    ax = min(ax, x1 * 0.99)
                    nrx = x1 - ax; nry = K / nrx; net_y = nry - y1; ty = net_y / g
                    prof = ax * p - ty
                    if prof > 0:
                        x1 = nrx; y1 = nry; e1 -= prof
                        fb, fa = on_trade(pol, hp, hon, A, B, S9, W2L, I, HU, HC, sf, si, cw, cm, cv, feat, scr, False, t, x1, y1)
                        fb = clampf(fb); fa = clampf(fa)
        elif s1 > p:
            g = 1.0 - fb
            if g > 0:
                xv = sqrt(K * g / p); ax = (xv - x1) / g
                if ax > 0:
                    nrx = x1 + ax * g; nry = K / nrx; yo = y1 - nry
                    prof = yo - ax * p
                    if yo > 0 and prof > 0:
                        x1 = nrx; y1 = nry; e1 -= prof
                        fb, fa = on_trade(pol, hp, hon, A, B, S9, W2L, I, HU, HC, sf, si, cw, cm, cv, feat, scr, True, t, x1, y1)
                        fb = clampf(fb); fa = clampf(fa)
        n = 0; q = mt_random(rr)
        while q > emlam:
            n += 1; q *= mt_random(rr)
        for jr in range(1, n + 1):
            size = mt_lognorm(rr, mu_ln, 1.2)
            buy = mt_random(rr) < 0.5
            if buy:
                ga = 1.0 - fa
                a1 = sqrt(x1 * ga * y1); a2 = sqrt(x2 * g2 * y2); r = a1 / a2
                d = ga + r * g2
                u1 = (r * (y2 + g2 * size) - y1) / d if d != 0 else size / 2
                u1 = 0.0 if u1 < 0 else (size if u1 > size else u1)
                u2 = size - u1
                if u2 > 1e-4:
                    nry = y2 + u2 * g2; nrx = K / nry; xo = x2 - nrx
                    if xo > 0:
                        e2 += u2 - xo * p; x2 = nrx; y2 = nry
                if u1 > 1e-4:
                    nry = y1 + u1 * ga; nrx = K / nry; xo = x1 - nrx
                    if xo > 0:
                        ed = u1 - xo * p; e1 += ed
                        x1 = nrx; y1 = nry
                        fb, fa = on_trade(pol, hp, hon, A, B, S9, W2L, I, HU, HC, sf, si, cw, cm, cv, feat, scr, False, t, x1, y1)
                        fb = clampf(fb); fa = clampf(fa)
            else:
                tx = size / p
                gb = 1.0 - fb
                b1 = sqrt(y1 * gb * x1); b2 = sqrt(y2 * g2 * x2); r = b1 / b2
                d = gb + r * g2
                v1 = (r * (x2 + g2 * tx) - x1) / d if d != 0 else tx / 2
                v1 = 0.0 if v1 < 0 else (tx if v1 > tx else v1)
                v2 = tx - v1
                if v2 > 1e-4:
                    nrx = x2 + v2 * g2; nry = K / nrx; yo = y2 - nry
                    if yo > 0:
                        e2 += v2 * p - yo; x2 = nrx; y2 = nry
                if v1 > 1e-4:
                    nrx = x1 + v1 * gb; nry = K / nrx; yo = y1 - nry
                    if yo > 0:
                        ed = v1 * p - yo; e1 += ed
                        x1 = nrx; y1 = nry
                        fb, fa = on_trade(pol, hp, hon, A, B, S9, W2L, I, HU, HC, sf, si, cw, cm, cv, feat, scr, True, t, x1, y1)
                        fb = clampf(fb); fa = clampf(fa)
    return e1


@njit(cache=True)
def sim_batch(seeds, n_steps, pol, hp, hon, A, B, S9, W2L, I, HU, HC, out):
    """out[i] = sim(seeds[i], ...) for every seed, in order, inside one compiled call."""
    for i in range(len(seeds)):
        out[i] = sim(seeds[i], n_steps, pol, hp, hon, A, B, S9, W2L, I, HU, HC)
