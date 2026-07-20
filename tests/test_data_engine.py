import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "update_lotto.py"
spec = importlib.util.spec_from_file_location("update_lotto", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def item(round_no, nums, bonus):
    return mod.normalize({"round": round_no, "winning": {"numbers": nums, "bonus": bonus}})


class EngineTests(unittest.TestCase):
    def test_protected_winning_is_not_replaced(self):
        original = item(100, [1, 2, 3, 4, 5, 6], 7)
        incoming = {"round": 100, "winning": {"numbers": [8, 9, 10, 11, 12, 13], "bonus": 14}}
        merged = mod.apply_incoming(original, incoming, allow_winning_replace=False)
        self.assertEqual(merged["winning"], original["winning"])

    def test_suspect_winning_can_be_repaired(self):
        original = item(100, [1, 2, 3, 4, 5, 6], 7)
        incoming = {"round": 100, "winning": {"numbers": [8, 9, 10, 11, 12, 13], "bonus": 14}}
        merged = mod.apply_incoming(original, incoming, allow_winning_replace=True)
        self.assertEqual(merged["winning"]["numbers"], [8, 9, 10, 11, 12, 13])

    def test_detects_consecutive_duplicate(self):
        by_round = {100: item(100, [1, 2, 3, 4, 5, 6], 7), 101: item(101, [1, 2, 3, 4, 5, 6], 7)}
        self.assertEqual(mod.consecutive_duplicate_suspects(by_round), {100, 101})

    def test_different_round_cannot_merge(self):
        with self.assertRaises(ValueError):
            mod.apply_incoming(item(100, [1, 2, 3, 4, 5, 6], 7), {"round": 101}, allow_winning_replace=False)


if __name__ == "__main__":
    unittest.main()
