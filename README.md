<!-- Credit: Taylor (gtmcknight), solved-policy research, amm-sim@4812a6382311624e1506aca131ca35c378f930d9. -->
# Shared AMM evaluation kit

Built on Taylor (gtmcknight)'s solved-policy research at
`amm-sim@4812a6382311624e1506aca131ca35c378f930d9` for the
[Paradigm AMM puzzle](https://paradigm.xyz/puzzles/amm).

The reference and Gate P foundation is implemented. The table below describes
this item's delivered scope; later items must update pending entries with their
own implementation and validation evidence.

| Piece | Purpose | Status in this item |
|---|---|---|
| `reference/` | Credited byte-preserving source envelopes, URL/SHA256 inventory, offline materializer | Implemented; includes Taylor tables/calibration, starter policies/tools, Numba source bundle |
| `gatep.py` | Verify all Taylor inputs and cache unmodified Gate P by commit, full Python version, input digest | Implemented; offline tests include actual execution and concurrent calls |
| `docs/contracts.md` | Replica API, policy IDs, bank row format, scorer boundary | Defined |
| `ammfast/` | One-call replica runs, ordered batches, residual-head hook | Pending subsequent item; archived kernel available under reference |
| `baselines/` | Replica/HA banks and screening-only engine bank | Pending subsequent item |
| `score.py` | Paired sequential scoring in rounds of 128 | Pending subsequent item |
| `setup_fast.sh` | SHA256-pinned cached installation, no Rust, timing | Pending subsequent item; archived starter setup is not this installer |
| Final pinned commit / raw goal URLs | Publish all kit pieces after merge | Pending; no final commit has been assigned to this item |

Run from the repository root (Python 3.9+, standard library only):

```sh
python3 -B reference/materialize.py --out scratch/reference
python3 -B gatep.py --reference scratch/reference/taylor --cache scratch/cache/gatep
python3 -B -m unittest discover -s tests -p test_gatep.py -v
```

Gate P prints the twelve upstream values. Repeat the same gate command to verify
the inputs and reuse successful cached stdout. Without `--cache`, the default is
`~/.cache/imd-amm/gatep`. Tests explicitly use scratch. See
[reference documentation](docs/reference.md), [contracts](docs/contracts.md),
[license inventory](reference/LICENSES.md), and [source inventory](reference/sources.json).
No engine simulation is required here. Engine work in subsequent items is limited
to screening seeds `9700000..9700511`.

Pinned upstream raw inputs (every bundled member has its own URL and digest in
`sources.json`):

- [Taylor solved README](https://raw.githubusercontent.com/gtmcknight/amm-sim/4812a6382311624e1506aca131ca35c378f930d9/research/solved/README.md)
- [Taylor gates.py](https://raw.githubusercontent.com/gtmcknight/amm-sim/4812a6382311624e1506aca131ca35c378f930d9/research/solved/gates.py)
- [Taylor QM table](https://raw.githubusercontent.com/gtmcknight/amm-sim/4812a6382311624e1506aca131ca35c378f930d9/research/solved/qm_k1e-5.json)
- [Taylor HA calibration](https://raw.githubusercontent.com/gtmcknight/amm-sim/4812a6382311624e1506aca131ca35c378f930d9/research/solved/calib_out_2048.json)
- [Starter qt1 Solidity](https://raw.githubusercontent.com/identity-md-launches/goal-3170-beat-network-s-amm-entry/4eb3e985fd31e3cd17832501b95b6d2491aca73d/reference/qt1/Strategy.sol)
- [Starter TwoComp](https://raw.githubusercontent.com/identity-md-launches/goal-3170-beat-network-s-amm-entry/4eb3e985fd31e3cd17832501b95b6d2491aca73d/reference/twocomp/twocomp.py)
- [Starter COMBO](https://raw.githubusercontent.com/identity-md-launches/goal-3170-beat-network-s-amm-entry/4eb3e985fd31e3cd17832501b95b6d2491aca73d/reference/combo/combo.py)
- [Starter setup](https://raw.githubusercontent.com/identity-md-launches/goal-3170-beat-network-s-amm-entry/4eb3e985fd31e3cd17832501b95b6d2491aca73d/tools/setup.sh)
- [Starter check](https://raw.githubusercontent.com/identity-md-launches/goal-3170-beat-network-s-amm-entry/4eb3e985fd31e3cd17832501b95b6d2491aca73d/tools/check.py)
- [Starter engine helper](https://raw.githubusercontent.com/identity-md-launches/goal-3170-beat-network-s-amm-entry/4eb3e985fd31e3cd17832501b95b6d2491aca73d/tools/engine.py)
- [Numba source artifact](https://api.imd.fun/artifacts/b9c917ebb99152aafd12993b327f0a82529efe414a4a8fea8f4a0e3493cc1f8f)

Required verified SHA256s: decoded Numba JSON
`a5d32e4c06dc93881821d568d8d89ca50adfdba56d5dccd92afef6c6c2157952`;
qt1 `Strategy.sol`
`c68887cff61180e12ba2b1ceb333edf76a830f91f63b5d775a8806fb9ec08aca`.
Final goal raw URLs will be listed here only after the merged commit is known.
