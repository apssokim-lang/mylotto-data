from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "lotto_results.json"
KST = timezone(timedelta(hours=9))
OFFICIAL_API = "https://www.dhlottery.co.kr/common.do?method=getLottoNumber&drwNo={round_no}"
PYONY_URL = "https://pyony.com/lotto/rounds/{round_no}/"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
})

# 실제 공개 결과로 교차 검증한 최근 회차. 파서가 틀리면 저장하지 않는다.
REGRESSION = {
    1228: (2_698_334_421, 11, 83),
    1229: (3_519_759_000, 8, 89),
    1230: (1_771_357_196, 16, 90),
    1231: (1_652_990_074, 17, 92),
}


def as_int(value: Any) -> int | None:
    if value is None:
        return None
    digits = re.sub(r"[^0-9]", "", str(value))
    return int(digits) if digits else None


def valid_first_amount(v: Any) -> bool:
    n = as_int(v)
    return n is not None and 100_000_000 <= n <= 100_000_000_000


def valid_first_winners(v: Any) -> bool:
    n = as_int(v)
    return n is not None and 1 <= n <= 1_000


def valid_second_winners(v: Any) -> bool:
    n = as_int(v)
    return n is not None and 1 <= n <= 100_000


def valid_total_sales(v: Any) -> bool:
    n = as_int(v)
    return n is not None and 1_000_000_000 <= n <= 10_000_000_000_000


def load_data() -> dict[str, Any]:
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    data.setdefault("schemaVersion", 2)
    data.setdefault("results", [])
    data.setdefault("service", {})
    return data


def normalize_round(item: dict[str, Any]) -> dict[str, Any]:
    if "winning" not in item:
        item["winning"] = {"numbers": item.pop("numbers", []), "bonus": item.pop("bonus", None)}
    item.setdefault("prize", {})
    item["prize"].setdefault("first", {})
    item["prize"]["first"].setdefault("perGameAmount", None)
    item["prize"]["first"].setdefault("winnerCount", None)
    item["prize"].setdefault("second", {})
    item["prize"]["second"].setdefault("winnerCount", None)
    item["prize"].setdefault("totalSalesAmount", None)
    if "stores" not in item:
        item["stores"] = item.pop("firstPrizeStores", []) or []
    item.setdefault("dataSource", {})
    return item


def save_data(data: dict[str, Any]) -> None:
    data["latestRound"] = max((int(r.get("round", 0)) for r in data["results"]), default=0)
    data["updatedAt"] = datetime.now(KST).isoformat(timespec="seconds")
    temp = DATA_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(DATA_PATH)


def fetch_official(round_no: int) -> dict[str, Any] | None:
    try:
        response = SESSION.get(OFFICIAL_API.format(round_no=round_no), timeout=25)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        print(f"[{round_no}] 공식 JSON 조회 실패: {exc}")
        return None
    if payload.get("returnValue") != "success":
        return None
    numbers = [payload.get(f"drwtNo{i}") for i in range(1, 7)]
    if any(not isinstance(v, int) for v in numbers):
        return None
    return {
        "round": round_no,
        "date": payload.get("drwNoDate"),
        "winning": {"numbers": numbers, "bonus": payload.get("bnusNo")},
        "firstAmount": as_int(payload.get("firstWinamnt")),
        "firstWinners": as_int(payload.get("firstPrzwnerCo")),
        "totalSales": as_int(payload.get("totSellamnt")),
    }


def fetch_pyony(round_no: int) -> dict[str, Any] | None:
    """서버 렌더링된 회차별 표에서 정확히 1등/2등 행을 읽는다."""
    try:
        response = SESSION.get(PYONY_URL.format(round_no=round_no), timeout=25)
        response.raise_for_status()
    except Exception as exc:
        print(f"[{round_no}] 대체 데이터 조회 실패: {exc}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    first_amount = first_winners = second_winners = None

    for row in soup.select("table tr"):
        cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip() for c in row.select("th,td")]
        if len(cells) < 3:
            continue
        rank = cells[0].replace(" ", "")
        if rank == "1등":
            first_winners = as_int(cells[1])
            first_amount = as_int(cells[2])
        elif rank == "2등":
            second_winners = as_int(cells[1])

    # 표 마크업이 바뀌었을 때 본문 텍스트를 보조 파싱한다.
    if not (valid_first_amount(first_amount) and valid_first_winners(first_winners) and valid_second_winners(second_winners)):
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        m1 = re.search(r"1등\s+([0-9,]+)\s+([0-9,]+)\s*원", text)
        m2 = re.search(r"2등\s+([0-9,]+)\s+([0-9,]+)\s*원", text)
        if m1:
            first_winners = as_int(m1.group(1))
            first_amount = as_int(m1.group(2))
        if m2:
            second_winners = as_int(m2.group(1))

    if not (valid_first_amount(first_amount) and valid_first_winners(first_winners) and valid_second_winners(second_winners)):
        print(f"[{round_no}] 대체 표 파싱 실패: 금액={first_amount}, 1등수={first_winners}, 2등수={second_winners}")
        return None

    # 알려진 회차는 실제 공개값과 반드시 일치해야 한다.
    if round_no in REGRESSION:
        expected = REGRESSION[round_no]
        actual = (first_amount, first_winners, second_winners)
        if actual != expected:
            raise RuntimeError(f"{round_no}회 회귀 검증 실패: actual={actual}, expected={expected}")

    stores: list[dict[str, Any]] = []
    heading = soup.find(lambda tag: tag.name in {"h2", "h3", "h4"} and "1등 당첨지역 판매점" in tag.get_text(" ", strip=True))
    if heading:
        table = heading.find_next("table")
        if table:
            for row in table.select("tr"):
                cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip() for c in row.select("td")]
                if len(cells) >= 4 and as_int(cells[0]) is not None:
                    stores.append({"name": cells[1], "method": cells[2], "address": cells[3]})

    return {
        "firstAmount": first_amount,
        "firstWinners": first_winners,
        "secondWinners": second_winners,
        "stores": stores,
    }


def clear_contaminated_values(results: list[dict[str, Any]], old_collector: str) -> bool:
    """2.6/2.7 수집기가 잘못 복사한 최근 회차 값은 재수집 전 제거한다."""
    if not (old_collector.startswith("2.6") or old_collector.startswith("2.7")):
        return False
    changed = False
    latest = max((int(x.get("round", 0)) for x in results), default=0)
    for item in results:
        item = normalize_round(item)
        round_no = int(item.get("round", 0))
        if round_no < latest - 30:
            continue
        first = item["prize"]["first"]
        second = item["prize"]["second"]
        # 이전 고장 난 수집기가 기록한 필드는 모두 검증된 소스로 다시 채운다.
        for obj, key in ((first, "perGameAmount"), (first, "winnerCount"), (second, "winnerCount")):
            if obj.get(key) is not None:
                obj[key] = None
                changed = True
        if item["prize"].get("totalSalesAmount") is not None:
            item["prize"]["totalSalesAmount"] = None
            changed = True
        item["dataSource"].pop("prize", None)
    return changed


def merge_item(item: dict[str, Any], official: dict[str, Any] | None, pyony: dict[str, Any] | None) -> bool:
    changed = False
    item = normalize_round(item)
    if official:
        if official.get("date") and item.get("date") != official["date"]:
            item["date"] = official["date"]
            changed = True
        winning = official.get("winning", {})
        if winning.get("numbers") and item["winning"].get("numbers") != winning["numbers"]:
            item["winning"]["numbers"] = winning["numbers"]
            changed = True
        if winning.get("bonus") is not None and item["winning"].get("bonus") != winning["bonus"]:
            item["winning"]["bonus"] = winning["bonus"]
            changed = True

    # 금액·게임 수는 회귀 검증된 회차별 표를 우선한다.
    if pyony:
        first = item["prize"]["first"]
        second = item["prize"]["second"]
        values = {
            ("first", "perGameAmount"): pyony["firstAmount"],
            ("first", "winnerCount"): pyony["firstWinners"],
            ("second", "winnerCount"): pyony["secondWinners"],
        }
        for (group, key), value in values.items():
            if item["prize"][group].get(key) != value:
                item["prize"][group][key] = value
                changed = True
        if pyony.get("stores") and item.get("stores") != pyony["stores"]:
            item["stores"] = pyony["stores"]
            changed = True
        item["dataSource"]["prize"] = "pyony-round-table-verified"

    # 총판매금액은 공식 JSON 값만 허용한다. 다른 회차 값 복사를 절대 허용하지 않는다.
    if official and valid_total_sales(official.get("totalSales")):
        total = official["totalSales"]
        if item["prize"].get("totalSalesAmount") != total:
            item["prize"]["totalSalesAmount"] = total
            changed = True
        item["dataSource"]["totalSales"] = "dhlottery-official-json"

    return changed


def is_incomplete(item: dict[str, Any]) -> bool:
    item = normalize_round(item)
    return not all([
        valid_first_amount(item["prize"]["first"].get("perGameAmount")),
        valid_first_winners(item["prize"]["first"].get("winnerCount")),
        valid_second_winners(item["prize"]["second"].get("winnerCount")),
        valid_total_sales(item["prize"].get("totalSalesAmount")),
        bool(item.get("stores")),
    ])


def main() -> int:
    data = load_data()
    results = [normalize_round(x) for x in data["results"]]
    results.sort(key=lambda x: int(x.get("round", 0)))
    by_round = {int(x["round"]): x for x in results}
    latest = max(by_round, default=0)
    changed = clear_contaminated_values(results, str(data.get("service", {}).get("collectorVersion", "")))

    # 새 회차 추가: 공식 JSON이 성공한 경우만 생성한다.
    candidate = latest + 1
    while True:
        official = fetch_official(candidate)
        if not official:
            break
        new_item = normalize_round({
            "round": candidate,
            "date": official.get("date"),
            "winning": official.get("winning", {}),
            "prize": {"first": {}, "second": {}, "totalSalesAmount": None},
            "stores": [],
        })
        by_round[candidate] = new_item
        results.append(new_item)
        latest = candidate
        candidate += 1
        changed = True

    # 첫 실행에서 사용자가 확인 중인 최근 회차를 즉시 복구한다.
    priority = [r for r in range(latest, max(0, latest - 30), -1) if r in by_round and is_incomplete(by_round[r])]
    older = [r for r in sorted(by_round, reverse=True) if r <= latest - 30 and is_incomplete(by_round[r])]
    cursor = as_int(data.get("service", {}).get("incompleteCursor")) or 0
    older_batch = []
    if older:
        cursor %= len(older)
        older_batch = (older[cursor:cursor + 5] + older[:max(0, cursor + 5 - len(older))])[:5]
    targets = list(dict.fromkeys(priority[:30] + older_batch))

    errors: list[str] = []
    for round_no in targets:
        try:
            official = fetch_official(round_no)
            pyony = fetch_pyony(round_no)
            if merge_item(by_round[round_no], official, pyony):
                changed = True
            item = by_round[round_no]
            print(
                f"[{round_no}] 결과: 1등금={item['prize']['first']['perGameAmount']}, "
                f"1등수={item['prize']['first']['winnerCount']}, "
                f"2등수={item['prize']['second']['winnerCount']}, "
                f"판매액={item['prize']['totalSalesAmount']}, 판매점={len(item.get('stores', []))}"
            )
        except Exception as exc:
            message = f"[{round_no}] 갱신 실패: {exc}"
            print(message)
            errors.append(message)

    results.sort(key=lambda x: int(x.get("round", 0)))
    data["results"] = results
    complete_count = sum(1 for x in results if not is_incomplete(x))
    data.setdefault("service", {}).update({
        "collectorVersion": "2.8-verified-official-plus-round-table",
        "mode": "official-json-and-verified-server-rendered-round-table",
        "recentPriorityCount": 30,
        "olderBatchSize": 5,
        "incompleteCursor": (cursor + len(older_batch)) if older else 0,
        "completedRoundCount": complete_count,
        "incompleteRoundCount": len(results) - complete_count,
        "lastRunAt": datetime.now(KST).isoformat(timespec="seconds"),
        "lastErrors": errors[-10:],
    })
    save_data(data)
    print(f"완료: 최신 {data['latestRound']}회 / 완성 {complete_count}회 / 미완성 {len(results)-complete_count}회")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
