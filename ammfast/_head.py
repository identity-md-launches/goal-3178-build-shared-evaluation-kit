# Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9,
# research/solved, solved-policy research (the filter state the head reads is his).
"""Fixed constants of the COMBO residual-head hook.  Standard library only.

These are the archived kernel's values (reference/envelopes/numba__fastsim.py.json), copied
unchanged.  They are the hook's frozen feature map, not a trained head: the 18 output weights are
always supplied by the caller and this package ships none.
"""
import math
import random

HEAD_PARAM_COUNT = 18
NH = 8              # hidden tanh units
NFEAT = 8           # live features per callback
CAP = 8.0           # each side's correction is CAP * tanh(z) basis points, so |correction| < 8 bps
XCLIP = 3.0         # standardised features are clipped to [-3, 3]
HSEED = 20261109    # seed of the frozen hidden projection
MU = (-36.3, 11.6, 0.57, 17.7, -3.6, 0.83, 2.49, 0.14)
SD = (77.2, 6.9, 0.37, 83.0, 44.0, 0.73, 0.96, 0.36)


def hidden_weights():
    """(U, c): the frozen 8x8 hidden projection and 8 biases, drawn exactly as the archive does."""
    r = random.Random(HSEED)
    U = [[r.gauss(0, 1) / math.sqrt(8) for _ in range(8)] for _ in range(NH)]
    c = [r.gauss(0, 1) * 0.5 for _ in range(NH)]
    return U, c


def nsum(values):
    """Neumaier compensated sum, start 0.0: what CPython >= 3.12's builtin sum() does for floats."""
    f = 0.0
    c = 0.0
    for x in values:
        t = f + x
        if abs(f) >= abs(x):
            c += (f - t) + x
        else:
            c += (x - t) + f
        f = t
    if c != 0.0 and math.isfinite(c):
        f += c
    return f
