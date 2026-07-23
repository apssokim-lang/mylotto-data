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
        "stores": stores if stores is not None else [{"name": "행운로또", "method": "자동", "address": "서울특별시 강남구 테헤란로 123"}],
        "dataSource": {"winning": mod.RESULT_SOURCE, "prize": mod.RESULT_SOURCE},
    }


class DataEngineTest(unittest.TestCase):
    def test_new_round_only_added(self):
        old = official(1, [1,2,3,4,5,6], 7)
        data = {"schemaVersion": 2, "latestRound": 1, "results": [old], "service": {}}
        new = official(2, [8,9,10,11,12,13], 14)
        out, changed = mod.update_dataset(data, [new, old], collect_stores=False)
        self.assertEqual(out["latestRound"], 2)
        self.assertIn(2, changed)
        self.assertEqual(next(x for x in out["results"] if x["round"] == 1)["winning"], old["winning"])

    def test_recent_corruption_repaired(self):
        good1 = official(1, [1,2,3,4,5,6], 7)
        corrupted2 = official(2, [1,2,3,4,5,6], 7)
        data = {"schemaVersion": 2, "latestRound": 2, "results": [corrupted2, good1], "service": {}}
        good2 = official(2, [8,9,10,11,12,13], 14)
        out, changed = mod.update_dataset(data, [good2, good1], collect_stores=False)
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
        payload = {"data": {"list": [{"prchSplcNm": "행운복권", "prchSplcAdr": "광주광역시 광산구 무진대로 123", "ltWnTyNm": "자동", "rnk": "1"}]}}
        self.assertEqual(mod.stores_from_json_payload(payload), [{"name": "행운복권", "method": "자동", "address": "광주광역시 광산구 무진대로 123"}])

    def test_store_html_parser(self):
        html = """
        <table><thead><tr><th>순번</th><th>상호명</th><th>구분</th><th>소재지</th></tr></thead>
        <tbody><tr><td>1</td><td>행운복권</td><td>자동</td><td>광주광역시 광산구 무진대로 123</td></tr></tbody></table>
        """
        self.assertEqual(mod.stores_from_html(html), [{"name": "행운복권", "method": "자동", "address": "광주광역시 광산구 무진대로 123"}])

    def test_empty_store_is_never_written(self):
        data = {"schemaVersion": 2, "latestRound": 1, "results": [official(1, [1,2,3,4,5,6], 7, stores=[])], "service": {}}
        with patch.object(mod, "fetch_official_stores", return_value=([], "pending")):
            out, _ = mod.update_dataset(data, [official(1, [1,2,3,4,5,6], 7, stores=[])])
        self.assertEqual(out["results"][0]["stores"], [])
        self.assertEqual(out["results"][0]["dataSource"]["storesStatus"], "pending-official-page")

    def test_numeric_store_name_rejected(self):
        payload = {"data": {"list": [{"prchSplcNm": "104", "prchSplcAdr": "부산 북구 만덕대로 166", "ltWnTyNm": "자동", "rnk": "1"}]}}
        self.assertEqual(mod.stores_from_json_payload(payload), [])

    def test_shop_name_is_not_accepted_as_address(self):
        self.assertFalse(mod.is_real_store_address("대륭로또판매점"))

    def test_backfill_cursor_advances(self):
        by_round = {}
        for r in range(1, 1234):
            item = official(r, [1,2,3,4,5,6], 7, stores=[])
            by_round[r] = item
        targets, cursor = mod.choose_backfill_targets(by_round, 1233, 2, {"storeBackfillCursorRound": 1229})
        self.assertEqual(targets, [1229, 1228])
        self.assertEqual(cursor, 1227)

    def test_legacy_recent_store_data_is_cleared_before_retry(self):
        item = official(1233, [2,7,20,25,37,40], 29, stores=[
            {"name": "진양호", "method": "반자동", "address": "경상남도 진주시 남강로 58 1층"}
        ])
        item["dataSource"]["stores"] = mod.STORE_SOURCE
        data = {"schemaVersion": 2, "latestRound": 1233, "results": [item], "service": {}}
        with patch.object(mod, "fetch_official_stores", return_value=([], "pending-no-correlated-response")):
            out, changed = mod.update_dataset(data, [official(1233, [2,7,20,25,37,40], 29, stores=[])])
        self.assertEqual(out["results"][0]["stores"], [])
        self.assertIn(1233, changed)

    def test_store_data_trusted_only_with_current_parser_version(self):
        item = official(1233, [2,7,20,25,37,40], 29)
        item["dataSource"]["stores"] = mod.STORE_SOURCE
        self.assertFalse(mod.store_data_is_trusted(item))
        item["dataSource"]["storesParserVersion"] = mod.STORE_PARSER_VERSION
        self.assertTrue(mod.store_data_is_trusted(item))

    def test_backfill_can_continue_below_recent_60_rounds(self):
        by_round = {}
        for r in range(1, 1234):
            item = official(r, [1,2,3,4,5,6], 7, stores=[])
            by_round[r] = item
        targets, cursor = mod.choose_backfill_targets(
            by_round, 1233, 2, {"storeBackfillCursorRound": 1173}
        )
        self.assertEqual(targets, [1173, 1172])
        self.assertEqual(cursor, 1171)

    def test_unchanged_official_data_does_not_change_verified_timestamp(self):
        old = official(1233, [2,7,20,25,37,40], 29, stores=[])
        old["dataSource"]["verifiedAt"] = "2026-07-01T00:00:00+09:00"
        incoming = official(1233, [2,7,20,25,37,40], 29, stores=[])
        merged = mod.merge_official(old, incoming)
        self.assertEqual(merged["dataSource"]["verifiedAt"], "2026-07-01T00:00:00+09:00")


if __name__ == "__main__":
    unittest.main()
