#!/usr/bin/env python3
# Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9,
# research/solved, solved-policy research.
"""Deterministic, offline import of the pinned Taylor HA calibration."""
import argparse
import hashlib
import json
import math
from pathlib import Path

COMMIT = "4812a6382311624e1506aca131ca35c378f930d9"
CREDIT = f"Taylor (gtmcknight), amm-sim@{COMMIT}, research/solved, solved-policy research."
SOURCE_SHA256 = "fdf24c7aa2b604d770336a5783276da8ed4d50bca9dbcc02e98b4da58f608d26"
BASE_URL = f"https://raw.githubusercontent.com/gtmcknight/amm-sim/{COMMIT}/research/solved/"
SEEDS = range(10000, 12048)
KINDS = ("oracle", "T", "HA", "fixed30")
EVIDENCE = {
    "evalpol.py": "6e67667bfed7798bdd3384652a6efc6ba0b1fce653740d0f9af2bcbcb8e6a27c",
    "replica2.py": "6e37b4e61e1ffa5ffb1e43912ddb8659105989aa986eb48fcd1d7608f8dca685",
}


class ValidationError(ValueError):
    """Invalid source or bank; no best-effort export is permitted."""


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise ValidationError(f"nonfinite JSON constant: {value}")


def parse_source(raw):
    """Strict structural validation, also exposed for offline malformed-input tests.

    Production entry point export_bytes additionally enforces the immutable digest.
    No position-based inference or aggregation is used.
    """
    try:
        rows = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (ValueError, UnicodeError) as exc:
        raise ValidationError(f"invalid calibration JSON: {exc}") from exc
    if not isinstance(rows, list) or len(rows) != 8192:
        raise ValidationError("calibration must contain exactly 8192 rows")
    by_kind = {kind: {} for kind in KINDS}
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or not {"kind", "seed", "edge"} <= row.keys():
            raise ValidationError(f"row {i}: missing kind, seed or edge")
        kind, seed, edge = row["kind"], row["seed"], row["edge"]
        if not isinstance(kind, str) or kind not in by_kind:
            raise ValidationError(f"row {i}: unknown kind")
        if type(seed) is not int or seed not in SEEDS:
            raise ValidationError(f"row {i}: invalid seed")
        if type(edge) not in (int, float):
            raise ValidationError(f"row {i}: edge must be a finite number")
        try:
            finite = math.isfinite(edge)
        except OverflowError:
            finite = False
        if not finite:
            raise ValidationError(f"row {i}: nonfinite edge")
        if seed in by_kind[kind]:
            raise ValidationError(f"row {i}: duplicate {kind} seed {seed}")
        by_kind[kind][seed] = edge
    for kind, values in by_kind.items():
        if set(values) != set(SEEDS):
            raise ValidationError(f"{kind}: missing seeds")
    return [{"seed": seed, "edge": format(by_kind["HA"][seed], ".6f")} for seed in SEEDS]


def export_bytes(raw):
    """Verify source SHA256, validate all rows, then return canonical bank bytes."""
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise ValidationError("calibration SHA256 mismatch")
    rows = parse_source(raw)
    bank = {
        "_credit": CREDIT,
        "backend": "replica",
        "policy": "HA",
        "provenance": {
            "origin": "Imported Taylor calibration; not ammfast output or a simulation rerun",
            "source_commit": COMMIT,
            "source_url": BASE_URL + "calib_out_2048.json",
            "source_sha256": SOURCE_SHA256,
            "source_rows": 8192,
            "selection": {"field": "kind", "equals": "HA"},
            "edge_field": "edge",
            "edge_semantics": "Policy pool trade edge e1; not rival norm=e2, fees, or edge minus norm",
            "seed_mapping": "Explicit integer source row.seed; select kind=HA, pair by seed, sort ascending",
            "seed_range": [10000, 12047],
            "row_count": 2048,
            "rounding": "Python json.loads binary64 float; format(edge, \".6f\") once at serialization",
            "generation_command": "python3 -B baselines/ha/export.py --source scratch/ha/reference/taylor/calib_out_2048.json --out baselines/ha/HA-10000-12047.json",
            "backend_runtime": "Archived CPython replica calibration; original Python version and generator invocation not recorded in pinned sources",
            "export_runtime": "Python 3 standard library; tested versions and timings in baselines/ha/audit.md",
            "evidence": [{"url": BASE_URL + name, "sha256": digest} for name, digest in EVIDENCE.items()],
            "validation": "Pinned source digest; all 8192 rows checked for required fields, finite edge, valid kind/seed and unique (kind,seed); exact 2048 seeds per kind; offline all-row bank comparison and repeat export tests in tests/test_ha.py",
            "mapping_evidence": "evalpol.py:11,92-100 explicitly reads this calibration and compares ref[k][s][edge], including HA; replica2.py:134 returns edge=e1, norm=e2",
        },
        "rows": rows,
    }
    return (json.dumps(bank, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")


def validate_bank(raw, bank_bytes):
    """Check every value and provenance field, ordering and canonical serialization."""
    if bank_bytes != export_bytes(raw):
        raise ValidationError("bank does not match canonical pinned-source export")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--out", type=Path)
    action.add_argument("--check", type=Path)
    args = parser.parse_args()
    try:
        raw = args.source.read_bytes()
        if args.check:
            validate_bank(raw, args.check.read_bytes())
            result = args.check.read_bytes()
        else:
            if args.out.resolve() == args.source.resolve():
                raise ValidationError("output must not overwrite source")
            result = export_bytes(raw)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_bytes(result)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"ha export: {exc}\n")
    print(f"HA: 2048 rows, seeds 10000..12047, sha256={hashlib.sha256(result).hexdigest()}")


if __name__ == "__main__":
    main()
