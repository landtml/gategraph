"""Evidence chain: record validation (B33), digest links, flipped bytes, optional MAC.

Runs on the system python3 (kernel rules): standard library only.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import copy
import unittest

from core.kernel.types import EvidenceRecord
from core.kernel.evidence import ChainError, make_record, verify_chain

HEX_A = "a" * 64
HEX_B = "b" * 64
CONFIG = "c" * 64
ACTOR = {"kind": "tool", "id": "test-tool", "channel": None}


def _chain(n=4, mac_key=None):
    chain = []
    for i in range(n):
        surface = HEX_A if i % 2 == 0 else HEX_B
        chain.append(make_record(
            chain, kind="gate", gate=f"g{i}", at=f"2026-01-01T00:00:{i:02d}+00:00",
            surface_digest=surface, producers=(), config_digest=CONFIG,
            actor=ACTOR, mac_key=mac_key))
    return chain


def _dicts(chain):
    return [rec.to_dict() if isinstance(rec, EvidenceRecord) else copy.deepcopy(rec)
            for rec in chain]


class TestMakeRecord(unittest.TestCase):

    def test_links_seq_and_prev(self):
        chain = _chain(3)
        self.assertEqual([r.seq for r in chain], [0, 1, 2])
        self.assertEqual(chain[0].prev, "")
        self.assertEqual(chain[1].prev, chain[0].digest)
        self.assertEqual(chain[2].prev, chain[1].digest)
        for rec in chain:
            self.assertEqual(rec.digest, rec.compute_digest())
        self.assertIsNone(chain[0].mac)

    def test_takes_surface_digest_never_the_map(self):
        # section 1.8, Q9: the record carries only the digest of the surface.
        surface_map = {"main.tex": HEX_A}
        with self.assertRaises(TypeError):
            make_record([], kind="gate", gate="g0", at="2026-01-01T00:00:00+00:00",
                        surface_digest=surface_map, producers=(), config_digest=CONFIG,
                        actor=ACTOR)
        with self.assertRaises(ValueError):
            make_record([], kind="gate", gate="g0", at="2026-01-01T00:00:00+00:00",
                        surface_digest="not-a-digest", producers=(), config_digest=CONFIG,
                        actor=ACTOR)

    def test_empty_chain_has_empty_head(self):
        self.assertEqual(verify_chain([]), "")


class TestB33Validation(unittest.TestCase):
    """B33: gate records were accepted unvalidated (a bare string passed a gate)."""

    def test_B33_malformed_record_rejected(self):
        good = _dicts(_chain(2))
        self.assertEqual(verify_chain(good), good[1]["digest"])

        cases = {
            "bare string record": ["gate:g0:passed", good[1]],
            "missing field": [{k: v for k, v in good[0].items() if k != "prev"}],
            "unknown field": [dict(good[0], passed=True)],
            "unknown kind": [dict(good[0], kind="bogus")],
            "wrong version": [dict(good[0], v=2)],
            "naive timestamp": [dict(good[0], at="2026-01-01T00:00:00")],
            "digest not hex": [dict(good[0], digest="z" * 64)],
            "surface digest not hex": [dict(good[0], surface_digest="../../x")],
            "producers not a list": [dict(good[0], producers="check")],
            "gate record without gate id": [dict(good[0], gate=None)],
            "bool seq": [dict(good[0], seq=True)],
            "not a mapping": [None],
        }
        for label, chain in cases.items():
            with self.subTest(label):
                with self.assertRaises(ChainError):
                    verify_chain(chain)

    def test_malformed_record_reports_its_seq(self):
        chain = _dicts(_chain(3))
        chain[2] = "bare string"
        with self.assertRaises(ChainError) as ctx:
            verify_chain(chain)
        self.assertEqual(ctx.exception.seq, 2)


class TestFlippedByte(unittest.TestCase):

    def _flip(self, text):
        # Change the first character that is a digit or hex letter to another one.
        for i, ch in enumerate(text):
            if ch in "0123456789abcdef":
                other = "1" if ch != "1" else "2"
                return text[:i] + other + text[i + 1:]
        raise AssertionError("no hex character to flip")

    def test_flipped_at_fails_at_that_seq(self):
        chain = _dicts(_chain(4))
        chain[2]["at"] = "2026-01-02T00:00:00+00:00"
        with self.assertRaises(ChainError) as ctx:
            verify_chain(chain)
        self.assertEqual(ctx.exception.seq, 2)

    def test_flipped_digest_fails_at_that_seq(self):
        chain = _dicts(_chain(4))
        chain[3]["digest"] = self._flip(chain[3]["digest"])
        with self.assertRaises(ChainError) as ctx:
            verify_chain(chain)
        self.assertEqual(ctx.exception.seq, 3)

    def test_flipped_prev_fails_at_that_seq(self):
        chain = _dicts(_chain(4))
        chain[1]["prev"] = self._flip(chain[1]["prev"])
        with self.assertRaises(ChainError) as ctx:
            verify_chain(chain)
        self.assertEqual(ctx.exception.seq, 1)

    def test_flipped_surface_digest_fails_at_that_seq(self):
        chain = _dicts(_chain(3))
        chain[1]["surface_digest"] = self._flip(chain[1]["surface_digest"])
        with self.assertRaises(ChainError) as ctx:
            verify_chain(chain)
        self.assertEqual(ctx.exception.seq, 1)

    def test_untouched_chain_verifies(self):
        chain = _chain(4)
        self.assertEqual(verify_chain(chain), chain[-1].digest)


class TestOptionalMac(unittest.TestCase):

    def test_mac_is_added_and_checked_with_key(self):
        key = b"key-kept-outside-the-workspace"
        chain = _chain(3, mac_key=key)
        self.assertTrue(all(rec.mac for rec in chain))
        self.assertEqual(verify_chain(chain, mac_key=key), chain[-1].digest)

    def test_wrong_key_fails(self):
        chain = _chain(3, mac_key=b"right")
        with self.assertRaises(ChainError) as ctx:
            verify_chain(chain, mac_key=b"wrong")
        self.assertEqual(ctx.exception.seq, 0)

    def test_missing_mac_fails_when_key_is_given(self):
        chain = _dicts(_chain(3))
        with self.assertRaises(ChainError):
            verify_chain(chain, mac_key=b"key")

    def test_flipped_mac_fails_at_that_seq(self):
        key = b"key"
        chain = _dicts(_chain(3, mac_key=key))
        chain[1]["mac"] = "0" * 64 if chain[1]["mac"][0] != "0" else "1" * 64
        with self.assertRaises(ChainError) as ctx:
            verify_chain(chain, mac_key=key)
        self.assertEqual(ctx.exception.seq, 1)

    def test_without_key_mac_is_not_checked(self):
        chain = _dicts(_chain(3, mac_key=b"key"))
        chain[1]["mac"] = "0" * 64
        self.assertEqual(verify_chain(chain), chain[-1]["digest"])


class TestImportGuard(unittest.TestCase):
    """R1: only migration writes an import record. The guard is the actor id, checked when a
    record is made and when a chain is parsed, so a forged import line is refused too."""

    MIGRATE = {"kind": "tool", "id": "migrate", "channel": "legacy"}

    def test_make_record_refuses_import_from_any_other_actor(self):
        with self.assertRaises(ValueError):
            make_record([], kind="import", gate="g0", at="2026-01-01T00:00:00+00:00",
                        surface_digest=HEX_A, producers=(), config_digest=CONFIG, actor=ACTOR)

    def test_make_record_accepts_import_from_migration(self):
        rec = make_record([], kind="import", gate="g0", at="2026-01-01T00:00:00+00:00",
                          surface_digest=HEX_A, producers=(), config_digest=CONFIG,
                          actor=self.MIGRATE)
        self.assertEqual(rec.kind.value, "import")
        self.assertEqual(verify_chain([rec]), rec.digest)

    def test_parse_refuses_a_forged_import_line(self):
        rec = make_record([], kind="import", gate="g0", at="2026-01-01T00:00:00+00:00",
                          surface_digest=HEX_A, producers=(), config_digest=CONFIG,
                          actor=self.MIGRATE)
        forged = rec.to_dict()
        forged["actor"] = dict(ACTOR)  # the same record, signed by an agent instead
        forged["digest"] = EvidenceRecord.from_dict(forged).compute_digest()
        with self.assertRaises(ChainError):
            verify_chain([forged])


if __name__ == "__main__":
    unittest.main()
