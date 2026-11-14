import unittest

from variant import clamp


class ClampTest(unittest.TestCase):
    def test_inside_interval_is_unchanged(self):
        self.assertEqual(clamp(5, 0, 10), 5)

    def test_below_is_raised_to_low(self):
        self.assertEqual(clamp(-3, 0, 10), 0)

    def test_above_is_lowered_to_high(self):
        self.assertEqual(clamp(42, 0, 10), 10)

    def test_inverted_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            clamp(1, 10, 0)


if __name__ == "__main__":
    unittest.main()
