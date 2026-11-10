# Credit: Taylor (gtmcknight), amm-sim@4812a6382311624e1506aca131ca35c378f930d9,
# research/solved, solved-policy research.
"""Offline HA import tests: only committed envelopes, no network or simulation."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


ha = module("ha_export", ROOT / "baselines/ha/export.py")
materialize = module("ha_materialize", ROOT / "reference/materialize.py")


class HATests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = ROOT / "scratch/ha"
        scratch.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix="tests-", dir=scratch)
        cls.addClassCleanup(cls.temp.cleanup)
        cls.work = Path(cls.temp.name)
        materialize.materialize(cls.work / "reference")
        cls.source = cls.work / "reference/taylor/calib_out_2048.json"
        cls.raw = cls.source.read_bytes()
        cls.rows = json.loads(cls.raw)
        cls.bank = (ROOT / "baselines/ha/HA-10000-12047.json").read_bytes()

    def test_source_and_evidence_hashes(self):
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(), ha.SOURCE_SHA256)
        for name, digest in ha.EVIDENCE.items():
            self.assertEqual(hashlib.sha256((self.source.parent / name).read_bytes()).hexdigest(), digest)

    def test_every_exported_value_independently(self):
        bank = json.loads(self.bank)
        self.assertEqual(next(iter(bank)), "_credit")
        self.assertEqual((bank["backend"], bank["policy"]), ("replica", "HA"))
        selected = {r["seed"]: r for r in self.rows if r["kind"] == "HA"}
        self.assertEqual(len(selected), 2048)
        self.assertEqual([r["seed"] for r in bank["rows"]], list(range(10000, 12048)))
        for row in bank["rows"]:
            with self.subTest(seed=row["seed"]):
                self.assertRegex(row["edge"], r"^-?[0-9]+\.[0-9]{6}$")
                self.assertEqual(row["edge"], format(selected[row["seed"]]["edge"], ".6f"))
        self.assertEqual(bank["rows"][0]["edge"], "440.443361")
        self.assertEqual(bank["rows"][-1]["edge"], "409.823332")

    def test_deterministic_exports_and_committed_bank(self):
        self.assertEqual(ha.export_bytes(self.raw), ha.export_bytes(self.raw))
        ha.validate_bank(self.raw, self.bank)

    def test_order_is_not_seed_mapping(self):
        self.assertEqual(ha.parse_source(json.dumps(self.rows[::-1])), json.loads(self.bank)["rows"])

    def test_digest_mismatch(self):
        for raw in (self.raw + b" ", b"[]", self.raw.replace(b"440.443", b"441.443", 1)):
            with self.subTest(raw_length=len(raw)), self.assertRaisesRegex(ha.ValidationError, "SHA256"):
                ha.export_bytes(raw)

    def test_malformed_json(self):
        for raw in (b"{", b"\xff", b"null", b"{}", b"[]"):
            with self.subTest(raw=raw), self.assertRaises(ha.ValidationError):
                ha.parse_source(raw)

    def test_duplicate_json_keys(self):
        with self.assertRaisesRegex(ha.ValidationError, "duplicate JSON key"):
            ha.parse_source(b'[{"seed":10000,"seed":10001}]')

    def test_missing_and_extra_rows(self):
        for rows in (self.rows[:-1], self.rows + [self.rows[0]]):
            with self.assertRaises(ha.ValidationError):
                ha.parse_source(json.dumps(rows))

    def test_missing_fields(self):
        for key in ("kind", "seed", "edge"):
            rows = copy.deepcopy(self.rows)
            del rows[2][key]
            with self.subTest(key=key), self.assertRaises(ha.ValidationError):
                ha.parse_source(json.dumps(rows))

    def test_duplicate_and_missing_seed_same_cardinality(self):
        rows = copy.deepcopy(self.rows)
        rows[6] = rows[2]
        with self.assertRaisesRegex(ha.ValidationError, "duplicate HA seed"):
            ha.parse_source(json.dumps(rows))

    def test_invalid_field_types_and_ranges(self):
        cases = [("seed", v) for v in (True, 10000.0, "10000", 9999, 12048, None)]
        cases += [("kind", v) for v in ("ha", None, [], 1)]
        cases += [("edge", v) for v in (True, None, "440.0", [], {})]
        for field, value in cases:
            rows = copy.deepcopy(self.rows)
            rows[2][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ha.ValidationError):
                ha.parse_source(json.dumps(rows))

    def test_nonfinite_and_overflow_edges(self):
        for value in ("NaN", "Infinity", "-Infinity", "1e999", "9" * 400):
            raw = self.raw.replace(b"440.4433605775813", value.encode(), 1)
            with self.subTest(value=value[:20]), self.assertRaises(ha.ValidationError):
                ha.parse_source(raw)

    def test_non_ha_rows_are_validated(self):
        rows = copy.deepcopy(self.rows)
        rows[0]["edge"] = "bad"
        with self.assertRaises(ha.ValidationError):
            ha.parse_source(json.dumps(rows))

    def test_bad_bank_values_metadata_order_and_duplicates(self):
        for change in ("edge", "metadata", "order", "duplicate", "missing"):
            bank = json.loads(self.bank)
            if change == "edge": bank["rows"][100]["edge"] = "NaN"
            elif change == "metadata": bank["backend"] = "engine"
            elif change == "order": bank["rows"].reverse()
            elif change == "duplicate": bank["rows"][1] = bank["rows"][0]
            else: bank["rows"].pop()
            with self.subTest(change=change), self.assertRaises(ha.ValidationError):
                ha.validate_bank(self.raw, json.dumps(bank).encode())

    def test_cli_export_check_and_fail_without_overwrite(self):
        dest = self.work / "bank.json"
        base = [sys.executable, "-B", str(ROOT / "baselines/ha/export.py"), "--source", str(self.source)]
        for action in ("--out", "--check"):
            result = subprocess.run(base + [action, str(dest)], capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(dest.read_bytes(), self.bank)
        bad = self.work / "bad.json"
        bad.write_bytes(b"[]")
        result = subprocess.run(base[:-1] + [str(bad), "--out", str(dest)], capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"SHA256 mismatch", result.stderr)
        self.assertEqual(dest.read_bytes(), self.bank)
        result = subprocess.run(base + ["--out", str(self.source)], capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.source.read_bytes(), self.raw)


if __name__ == "__main__":
    unittest.main()
