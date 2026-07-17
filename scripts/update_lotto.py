from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "lotto_results.json"
KST = timezone(timedelta(hours=9))

OFFICIAL_API = (
    "https://www.dhlottery.co.kr/common.do"
    "?method=getLottoNumber&drwNo={round_no}"
)
ROUND_PAGE = "https://pyony.com/lotto/rounds/{round_no}/"

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0 Safari/537.36"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    }
)


def as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    digits = re.sub(r"[^0-9]", "", str(value))
    return int(digits) if digits else None


def valid_first_amount(value: Any) -> bool:
    number = as_int(value)
    return number is not None and 10_000_000 <= number <= 100_000_000_000


def valid_first_count(value: Any) -> bool:
    number = as_int(value)
    return number is not None and 1 <= number <= 1_000


def valid_second_count(value: Any) -> bool:
    number = as_int(value)
    return number is not None and 1 <= number <= 100_000


def valid_total_sales(value: Any) -> bool:
    number = as_int(value)
    return number is not None and 1_000_000_000 <= number <= 10_000_000_000_000


def load_data() -> dict[str, Any]:
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    data.setdefault("schemaVersion", 2)
    data.setdefault("results", [])
    data.setdefault("service", {})
    return data


def normalize_round(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item.get("winning"), dict):
        item["winning"] = {
            "numbers": item.pop("numbers", []),
            "bonus": item.pop("bonus", None),
        }

    prize = item.setdefault("prize", {})
    first = prize.setdefault("first", {})
    second = prize.setdefault("second", {})

    first.setdefault("perGameAmount", None)
    first.setdefault("winnerCount", None)
    second.setdefault("winnerCount", None)
    prize.setdefault("totalSalesAmount", None)

    if "stores" not in item:
        item["stores"] = item.pop("firstPrizeStores", []) or []

    item.setdefault("dataSource", {})
    return item


def save_data(data: dict[str, Any]) -> None:
    data["latestRound"] = max(
        (int(item.get("round", 0)) for item in data["results"]),
        default=0,
    )
    data["updatedAt"] = datetime.now(KST).isoformat(timespec="seconds")

    temp_path = DATA_PATH.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(DATA_PATH)


def fetch_official(round_no: int) -> dict[str, Any] | None:
    try:
        response = SESSION.get(
            OFFICIAL_API.format(round_no=round_no),
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        print(f"[{round_no}] 공식 JSON 조회 실패: {exc}")
        return None

    if payload.get("returnValue") != "success":
        return None

    numbers = [as_int(payload.get(f"drwtNo{i}")) for i in range(1, 7)]
    bonus = as_int(payload.get("bnusNo"))

    if (
        any(number is None for number in numbers)
        or len(set(numbers)) != 6
        or bonus is None
    ):
        print(f"[{round_no}] 공식 JSON 당첨번호 형식 오류")
        return None

    return {
        "round": round_no,
        "date": payload.get("drwNoDate"),
        "winning": {
            "numbers": numbers,
            "bonus": bonus,
        },
        "firstAmount": as_int(payload.get("firstWinamnt")),
        "firstCount": as_int(payload.get("firstPrzwnerCo")),
        "totalSales": as_int(payload.get("totSellamnt")),
    }


def numeric_tokens(values: list[str]) -> list[int]:
    result: list[int] = []
    for value in values:
        stripped = value.strip()
        if re.fullmatch(r"[0-9][0-9,]*", stripped):
            parsed = as_int(stripped)
            if parsed is not None:
                result.append(parsed)
    return result


def parse_rank_from_rows(
    soup: BeautifulSoup,
    rank_name: str,
) -> tuple[int | None, int | None]:
    for row in soup.select("table tr"):
        cells = [
            re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()
            for cell in row.select("th, td")
        ]
        if not cells:
            continue

        normalized_rank = cells[0].replace(" ", "")
        if normalized_rank != rank_name:
            continue

        numbers = numeric_tokens(cells[1:])
        if len(numbers) >= 2:
            return numbers[0], numbers[1]

    return None, None


def parse_rank_from_strings(
    soup: BeautifulSoup,
    rank_name: str,
) -> tuple[int | None, int | None]:
    strings = [
        re.sub(r"\s+", " ", value).strip()
        for value in soup.stripped_strings
    ]

    for index, value in enumerate(strings):
        if value.replace(" ", "") != rank_name:
            continue

        numbers = numeric_tokens(strings[index + 1 : index + 12])
        if len(numbers) >= 2:
            return numbers[0], numbers[1]

    return None, None


def parse_stores(soup: BeautifulSoup, round_no: int) -> list[dict[str, Any]]:
    stores: list[dict[str, Any]] = []

    heading = soup.find(
        lambda tag: (
            tag.name in {"h2", "h3", "h4"}
            and f"{round_no}회" in tag.get_text(" ", strip=True)
            and "1등 당첨지역 판매점" in tag.get_text(" ", strip=True)
        )
    )

    table = heading.find_next("table") if heading else None
    if table is None:
        return stores

    for row in table.select("tr"):
        cells = [
            re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()
            for cell in row.select("td")
        ]
        if len(cells) < 4:
            continue

        if as_int(cells[0]) is None:
            continue

        stores.append(
            {
                "name": cells[1],
                "method": cells[2],
                "address": cells[3],
            }
        )

    return stores


def fetch_round_page(round_no: int) -> dict[str, Any] | None:
    try:
        response = SESSION.get(
            ROUND_PAGE.format(round_no=round_no),
            timeout=25,
        )
        response.raise_for_status()
    except Exception as exc:
        print(f"[{round_no}] 회차별 페이지 조회 실패: {exc}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")

    title_text = soup.get_text(" ", strip=True)
    if f"로또 {round_no}회 당첨번호" not in title_text:
        print(f"[{round_no}] 요청한 회차 페이지가 아님")
        return None

    first_count, first_amount = parse_rank_from_rows(soup, "1등")
    second_count, _ = parse_rank_from_rows(soup, "2등")

    if not (
        valid_first_count(first_count)
        and valid_first_amount(first_amount)
        and valid_second_count(second_count)
    ):
        first_count, first_amount = parse_rank_from_strings(soup, "1등")
        second_count, _ = parse_rank_from_strings(soup, "2등")

    if not (
        valid_first_count(first_count)
        and valid_first_amount(first_amount)
        and valid_second_count(second_count)
    ):
        print(
            f"[{round_no}] 회차 표 파싱 실패: "
            f"1등금={first_amount}, 1등수={first_count}, "
            f"2등수={second_count}"
        )
        return None

    return {
        "firstAmount": first_amount,
        "firstCount": first_count,
        "secondCount": second_count,
        "stores": parse_stores(soup, round_no),
    }


def clear_invalid_and_duplicated_values(
    results: list[dict[str, Any]],
) -> bool:
    changed = False

    for item in results:
        normalize_round(item)
        prize = item["prize"]
        first = prize["first"]
        second = prize["second"]

        fields = [
            (first, "perGameAmount", valid_first_amount),
            (first, "winnerCount", valid_first_count),
            (second, "winnerCount", valid_second_count),
            (prize, "totalSalesAmount", valid_total_sales),
        ]

        for target, key, validator in fields:
            value = target.get(key)
            if value is not None and not validator(value):
                print(
                    f"[{item.get('round')}] 비정상 값 삭제: "
                    f"{key}={value}"
                )
                target[key] = None
                changed = True

    recent = sorted(
        results,
        key=lambda item: int(item.get("round", 0)),
        reverse=True,
    )[:50]

    totals = [
        item["prize"].get("totalSalesAmount")
        for item in recent
        if valid_total_sales(item["prize"].get("totalSalesAmount"))
    ]
    duplicate_totals = {
        value for value, count in Counter(totals).items() if count >= 3
    }

    if duplicate_totals:
        for item in recent:
            value = item["prize"].get("totalSalesAmount")
            if value in duplicate_totals:
                print(
                    f"[{item.get('round')}] 복사 의심 총판매액 삭제: "
                    f"{value}"
                )
                item["prize"]["totalSalesAmount"] = None
                item["dataSource"].pop("totalSales", None)
                changed = True

    return changed


def merge_item(
    item: dict[str, Any],
    official: dict[str, Any] | None,
    round_page: dict[str, Any] | None,
) -> bool:
    changed = False
    normalize_round(item)

    if official:
        if official.get("date") and item.get("date") != official["date"]:
            item["date"] = official["date"]
            changed = True

        official_winning = official.get("winning") or {}
        if (
            official_winning.get("numbers")
            and item["winning"].get("numbers")
            != official_winning["numbers"]
        ):
            item["winning"]["numbers"] = official_winning["numbers"]
            changed = True

        if (
            official_winning.get("bonus") is not None
            and item["winning"].get("bonus")
            != official_winning["bonus"]
        ):
            item["winning"]["bonus"] = official_winning["bonus"]
            changed = True

        first_amount = official.get("firstAmount")
        first_count = official.get("firstCount")
        total_sales = official.get("totalSales")

        if valid_first_amount(first_amount):
            if item["prize"]["first"].get("perGameAmount") != first_amount:
                item["prize"]["first"]["perGameAmount"] = first_amount
                changed = True
            item["dataSource"]["firstPrize"] = "dhlottery-official-json"

        if valid_first_count(first_count):
            if item["prize"]["first"].get("winnerCount") != first_count:
                item["prize"]["first"]["winnerCount"] = first_count
                changed = True
            item["dataSource"]["firstWinnerCount"] = (
                "dhlottery-official-json"
            )

        if valid_total_sales(total_sales):
            if item["prize"].get("totalSalesAmount") != total_sales:
                item["prize"]["totalSalesAmount"] = total_sales
                changed = True
            item["dataSource"]["totalSales"] = "dhlottery-official-json"

    if round_page:
        first_amount = round_page.get("firstAmount")
        first_count = round_page.get("firstCount")
        second_count = round_page.get("secondCount")

        if (
            valid_first_amount(first_amount)
            and item["prize"]["first"].get("perGameAmount")
            != first_amount
        ):
            item["prize"]["first"]["perGameAmount"] = first_amount
            changed = True

        if (
            valid_first_count(first_count)
            and item["prize"]["first"].get("winnerCount")
            != first_count
        ):
            item["prize"]["first"]["winnerCount"] = first_count
            changed = True

        if (
            valid_second_count(second_count)
            and item["prize"]["second"].get("winnerCount")
            != second_count
        ):
            item["prize"]["second"]["winnerCount"] = second_count
            changed = True

        stores = round_page.get("stores") or []
        if stores and item.get("stores") != stores:
            item["stores"] = stores
            changed = True

        item["dataSource"]["rankTable"] = "pyony-round-page"

    return changed


def is_incomplete(item: dict[str, Any]) -> bool:
    normalize_round(item)
    prize = item["prize"]

    return not all(
        [
            valid_first_amount(prize["first"].get("perGameAmount")),
            valid_first_count(prize["first"].get("winnerCount")),
            valid_second_count(prize["second"].get("winnerCount")),
            valid_total_sales(prize.get("totalSalesAmount")),
            bool(item.get("stores")),
        ]
    )


def main() -> int:
    data = load_data()
    results = [normalize_round(item) for item in data["results"]]
    results.sort(key=lambda item: int(item.get("round", 0)))

    by_round = {
        int(item["round"]): item
        for item in results
        if as_int(item.get("round")) is not None
    }
    latest = max(by_round, default=0)

    changed = clear_invalid_and_duplicated_values(results)

    candidate = latest + 1
    while True:
        official = fetch_official(candidate)
        if official is None:
            break

        new_item = normalize_round(
            {
                "round": candidate,
                "date": official.get("date"),
                "winning": official.get("winning"),
                "prize": {
                    "first": {},
                    "second": {},
                    "totalSalesAmount": None,
                },
                "stores": [],
            }
        )
        results.append(new_item)
        by_round[candidate] = new_item
        latest = candidate
        candidate += 1
        changed = True

    recent_targets = [
        round_no
        for round_no in range(latest, max(0, latest - 20), -1)
        if round_no in by_round and is_incomplete(by_round[round_no])
    ]

    older_targets = [
        round_no
        for round_no in sorted(by_round, reverse=True)
        if round_no <= latest - 20 and is_incomplete(by_round[round_no])
    ]

    cursor = as_int(data["service"].get("olderCursor")) or 0
    older_batch: list[int] = []

    if older_targets:
        cursor %= len(older_targets)
        older_batch = (
            older_targets[cursor : cursor + 10]
            + older_targets[: max(0, cursor + 10 - len(older_targets))]
        )[:10]

    targets = list(dict.fromkeys(recent_targets + older_batch))
    errors: list[str] = []

    for round_no in targets:
        official = fetch_official(round_no)
        round_page = fetch_round_page(round_no)

        if merge_item(by_round[round_no], official, round_page):
            changed = True

        item = by_round[round_no]
        prize = item["prize"]
        print(
            f"[{round_no}] 결과: "
            f"1등금={prize['first'].get('perGameAmount')}, "
            f"1등수={prize['first'].get('winnerCount')}, "
            f"2등수={prize['second'].get('winnerCount')}, "
            f"판매액={prize.get('totalSalesAmount')}, "
            f"판매점={len(item.get('stores') or [])}"
        )

        if is_incomplete(item):
            errors.append(f"{round_no}회 일부 정보 미완성")

    results.sort(key=lambda item: int(item.get("round", 0)))
    data["results"] = results

    complete_count = sum(1 for item in results if not is_incomplete(item))

    data["service"].update(
        {
            "collectorVersion": "2.9-official-fallback-robust-rank-parser",
            "mode": "official-json-plus-round-page",
            "recentPriorityCount": 20,
            "olderBatchSize": 10,
            "olderCursor": (
                cursor + len(older_batch) if older_targets else 0
            ),
            "completedRoundCount": complete_count,
            "incompleteRoundCount": len(results) - complete_count,
            "lastRunAt": datetime.now(KST).isoformat(timespec="seconds"),
            "lastErrors": errors[-20:],
        }
    )

    save_data(data)

    print(
        f"완료: 최신 {data['latestRound']}회 / "
        f"완성 {complete_count}회 / "
        f"미완성 {len(results) - complete_count}회"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
