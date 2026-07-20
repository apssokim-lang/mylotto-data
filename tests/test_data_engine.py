import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "update_lotto.py"
spec = importlib.util.spec_from_file_location("update_lotto", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(mod)


def official(round_no, nums, bonus, stores=None):
    return {
        "round": round_no,
        "date": "2026-07-18",
        "winning": {"numbers": nums, "bonus": bonus},
        "prize": {
            "first": {"perGameAmount": 1000000000, "winnerCount": 10},
            "second": {"perGameAmount": 50000000, "winnerCount": 80},
            "third": {"perGameAmount": 1000000, "winnerCount": 3000},
            "totalSalesAmount": 120000000000,
        },
        "stores": stores if stores is not None else [{"name": "테스트복권", "method": "자동", "address": "서울시 테스트로 1"}],
        "dataSource": {"winning": mod.RESULT_SOURCE, "prize": mod.RESULT_SOURCE},
    }


class DataEngineTest(unittest.TestCase):
    def test_new_round_only_added(self):
        old = official(1, [1,2,3,4,5,6], 7)
        data = {"schemaVersion": 2, "latestRound": 1, "results": [old], "service": {}}
        new = official(2, [8,9,10,11,12,13], 14)
        out, changed = mod.update_dataset(data, [new, old])
        self.assertEqual(out["latestRound"], 2)
        self.assertIn(2, changed)
        self.assertEqual(next(x for x in out["results"] if x["round"] == 1)["winning"], old["winning"])

    def test_recent_corruption_repaired(self):
        good1 = official(1, [1,2,3,4,5,6], 7)
        corrupted2 = official(2, [1,2,3,4,5,6], 7)
        data = {"schemaVersion": 2, "latestRound": 2, "results": [corrupted2, good1], "service": {}}
        good2 = official(2, [8,9,10,11,12,13], 14)
        out, changed = mod.update_dataset(data, [good2, good1])
        self.assertIn(2, changed)
        self.assertEqual(out["results"][0]["winning"], good2["winning"])

    def test_stores_preserved(self):
        old = official(1, [1,2,3,4,5,6], 7)
        merged = mod.merge_official(old, official(1, [1,2,3,4,5,6], 7))
        self.assertEqual(merged["stores"], old["stores"])

    def test_cross_round_merge_blocked(self):
        with self.assertRaises(ValueError):
            mod.merge_official(official(1, [1,2,3,4,5,6], 7), official(2, [8,9,10,11,12,13], 14))

    def test_parse_official_row(self):
        row = {
            "ltEpsd": 1233, "tm1WnNo": 2, "tm2WnNo": 7, "tm3WnNo": 20,
            "tm4WnNo": 25, "tm5WnNo": 37, "tm6WnNo": 40, "bnsWnNo": 29,
            "ltRflYmd": "20260718", "rnk1WnNope": 31, "rnk1WnAmt": 837965396,
            "rnk2WnNope": 76, "rnk2WnAmt": 56966946, "rnk3WnNope": 4438,
            "rnk3WnAmt": 975550, "wholEpsdSumNtslAmt": 120000000000,
        }
        item = mod.official_row_to_item(row)
        self.assertEqual(item["winning"], {"numbers": [2,7,20,25,37,40], "bonus": 29})
        self.assertEqual(item["date"], "2026-07-18")

    def test_store_json_parser(self):
        payload = {"data": {"list": [{"prchSplcNm": "행운복권", "prchSplcAdr": "광주 광산구 테스트로 1", "ltWnTyNm": "자동", "rnk": "1"}]}}
        self.assertEqual(mod.stores_from_json_payload(payload), [{"name": "행운복권", "method": "자동", "address": "광주 광산구 테스트로 1"}])

    def test_store_html_parser(self):
        html = """
        <table><tbody><tr><td>1</td><td>행운복권</td><td>자동</td><td>광주 광산구 테스트로 1</td></tr></tbody></table>
        """
        self.assertEqual(mod.stores_from_html(html), [{"name": "행운복권", "method": "자동", "address": "광주 광산구 테스트로 1"}])

    def test_empty_store_is_never_written(self):
        data = {"schemaVersion": 2, "latestRound": 1, "results": [official(1, [1,2,3,4,5,6], 7, stores=[])], "service": {}}
        with patch.object(mod, "fetch_official_stores", return_value=([], "pending")):
            out, _ = mod.update_dataset(data, [official(1, [1,2,3,4,5,6], 7, stores=[])])
        self.assertEqual(out["results"][0]["stores"], [])
        self.assertEqual(out["results"][0]["dataSource"]["storesStatus"], "pending-official-page")


if __name__ == "__main__":
    unittest.main()
