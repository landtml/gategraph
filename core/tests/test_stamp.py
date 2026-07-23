"""Stamp integrity: every core file matches its sha256 recorded at stamp time.

This is the harness proving itself alive. If this fails, either core files
were hand-edited (run `gatebench diff` / `gatebench upstream`) or the stamp
is corrupt.
"""
import hashlib
import json
import unittest
from pathlib import Path

# .../<root>/harness/core/tests/test_stamp.py -> parents[3] == <root>
ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "harness" / ".fh" / "MANIFEST.json"
# In the template checkout the suite lives at <template>/core/tests, so there
# is no stamp to verify there (gatebench.py's own tests cover stamping).
IS_STAMPED = Path(__file__).resolve().parents[2].name == "harness"


@unittest.skipUnless(IS_STAMPED, "not a stamped project (template checkout)")
class TestStampIntegrity(unittest.TestCase):
    def test_manifest_exists_and_well_formed(self):
        self.assertTrue(MANIFEST.is_file(), f"missing {MANIFEST}")
        data = json.loads(MANIFEST.read_text())
        self.assertIn("version", data)
        self.assertTrue(data.get("files"), "manifest has no files map")

    def test_every_manifest_file_exists(self):
        data = json.loads(MANIFEST.read_text())
        missing = [rel for rel in data["files"] if not (ROOT / rel).is_file()]
        self.assertEqual(missing, [], f"manifest files missing on disk: {missing}")

    def test_core_hashes_match_manifest(self):
        data = json.loads(MANIFEST.read_text())
        drifted = []
        for rel, digest in data["files"].items():
            path = ROOT / rel
            if path.is_file():
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != digest:
                    drifted.append(rel)
        self.assertEqual(
            drifted, [],
            f"core files drifted from stamp (run `gatebench diff`): {drifted}")


if __name__ == "__main__":
    unittest.main()
