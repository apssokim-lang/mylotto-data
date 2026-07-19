from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "lotto_results.json"
BACKUP_PATH = ROOT / "data" / "lotto_results_backup.json"
KST = timezone(timedelta(hours=9))

PYONY_URL = "https://pyony.com/lotto/rounds/{round_no}/"
OFFICIAL_API = (
    "https://www.dhlottery.co.kr/common.do"
    "?method=getLottoNumber&drwNo={round_no}"
)

RECENT_PRIORITY_COUNT = 20
CORE_BACKFILL_BATCH = 30
STORE_BACKFILL_BATCH = 5

SESSION = requests.Session()
RETRY = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=1.0,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET"}),
)
SESSION.mount("https://", HTTPAdapter(max_retries=RETRY))
SESSION.mount("http://", HTTPAdapter(max_retries=RETRY))

SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
})


def to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    digits = re.sub(r"[^0-9]", "", str(value))
    return int(digits) if digits else None


def valid_numbers(numbers: Any, bonus: Any) -> bool:
    if not isinstance(numbers, list) or len(numbers) != 6:
        return False
    try:
        normalized = [int(value) for value in numbers]
        normalized_bonus = int(bonus)
    except (TypeError, ValueError):
        return False

    return (
        len(set(normalized)) == 6
        and all(1 <= value <= 45 for value in normalized)
        and 1 <= normalized_bonus <= 45
        and normalized_bonus not in normalized
    )


def valid_first_amount(value: Any) -> bool:
    number = to_int(value)
    return number is not None and 10_000_000 <= number <= 100_000_000_000


def valid_first_count(value: Any) -> bool:
    number = to_int(value)
    return number is not None and 0 <= number <= 1_000


def valid_second_count(value: Any) -> bool:
    number = to_int(value)
    return number is not None and 0 <= number <= 100_000


def valid_total_sales(value: Any) -> bool:
    number = to_int(value)
    return number is not None and 1_000_000_000 <= number <= 10_000_000_000_000


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize(item: dict[str, Any]) -> dict[str, Any]:
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


def load_data() -> dict[str, Any]:
    if not DATA_PATH.exists():
        raise FileNotFoundError(
            f"데이터 파일을 찾을 수 없습니다: {DATA_PATH}"
        )

    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    data.setdefault("schemaVersion", 2)
    data.setdefault("results", [])
    data.setdefault("service", {})
    data["results"] = [
        normalize(item)
        for item in data["results"]
        if isinstance(item, dict)
    ]
    return data


def save_data(data: dict[str, Any]) -> bool:
    data["results"].sort(key=lambda item: int(item.get("round", 0)))
    data["latestRound"] = max(
        (int(item["round"]) for item in data["results"]),
        default=0,
    )
    data["updatedAt"] = datetime.now(KST).isoformat(timespec="seconds")

    new_text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    old_text = (
        DATA_PATH.read_text(encoding="utf-8")
        if DATA_PATH.exists()
        else ""
    )

    if new_text == old_text:
        print("저장할 변경사항이 없습니다.")
        return False

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)

    # 새 파일을 덮어쓰기 직전에 직전 정상본을 백업합니다.
    if old_text:
        BACKUP_PATH.write_text(old_text, encoding="utf-8")

    temporary = DATA_PATH.with_suffix(".json.tmp")
    temporary.write_text(new_text, encoding="utf-8")
    temporary.replace(DATA_PATH)
    return True


def core_complete(item: dict[str, Any]) -> bool:
    item = normalize(item)
    prize = item["prize"]

    return (
        valid_numbers(
            item["winning"].get("numbers"),
            item["winning"].get("bonus"),
        )
        and valid_first_amount(prize["first"].get("perGameAmount"))
        and valid_first_count(prize["first"].get("winnerCount"))
        and valid_second_count(prize["second"].get("winnerCount"))
        and valid_total_sales(prize.get("totalSalesAmount"))
    )


def stores_complete(item: dict[str, Any]) -> bool:
    return bool(normalize(item).get("stores"))


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

    numbers = [
        to_int(payload.get(f"drwtNo{index}"))
        for index in range(1, 7)
    ]
    bonus = to_int(payload.get("bnusNo"))

    if not valid_numbers(numbers, bonus):
        print(f"[{round_no}] 공식 JSON 당첨번호 검증 실패")
        return None

    return {
        "round": round_no,
        "date": payload.get("drwNoDate"),
        "winning": {
            "numbers": numbers,
            "bonus": bonus,
        },
        "firstAmount": to_int(payload.get("firstWinamnt")),
        "firstCount": to_int(payload.get("firstPrzwnerCo")),
        "totalSales": to_int(payload.get("totSellamnt")),
    }


def parse_rank_table(
    soup: BeautifulSoup,
) -> tuple[int | None, int | None, int | None]:
    first_count = None
    first_amount = None
    second_count = None

    for row in soup.select("table tr"):
        cells = [
            clean_text(cell.get_text(" ", strip=True))
            for cell in row.select("th, td")
        ]
        if not cells:
            continue

        rank = cells[0].replace(" ", "")
        values: list[int] = []

        for cell in cells[1:]:
            if re.fullmatch(r"[0-9][0-9,]*\s*(?:원)?", cell):
                number = to_int(cell)
                if number is not None:
                    values.append(number)

        if rank == "1등" and len(values) >= 2:
            first_count = values[0]
            first_amount = values[1]
        elif rank == "2등" and values:
            second_count = values[0]

    return first_count, first_amount, second_count


def parse_stores(
    soup: BeautifulSoup,
    round_no: int,
) -> list[dict[str, str]]:
    heading = soup.find(
        lambda tag: (
            tag.name in {"h2", "h3", "h4"}
            and f"{round_no}회"
            in clean_text(tag.get_text(" ", strip=True))
            and "1등 당첨지역 판매점"
            in clean_text(tag.get_text(" ", strip=True))
        )
    )
    table = heading.find_next("table") if heading else None

    if table is None:
        return []

    stores: list[dict[str, str]] = []

    for row in table.select("tr"):
        cells = [
            clean_text(cell.get_text(" ", strip=True))
            for cell in row.select("td")
        ]

        if len(cells) < 4 or to_int(cells[0]) is None:
            continue

        stores.append({
            "name": cells[1],
            "method": cells[2],
            "address": cells[3],
        })

    return stores


def fetch_pyony(round_no: int) -> dict[str, Any] | None:
    try:
        response = SESSION.get(
            PYONY_URL.format(round_no=round_no),
            timeout=30,
        )
        response.raise_for_status()
    except Exception as exc:
        print(f"[{round_no}] Pyony 조회 실패: {exc}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    page_text = clean_text(soup.get_text(" ", strip=True))

    if f"로또 {round_no}회 당첨번호" not in page_text:
        return None

    date_match = re.search(
        rf"{round_no}회\s*\((\d{{4}})년\s*"
        rf"(\d{{1,2}})월\s*(\d{{1,2}})일\s*추첨\)",
        page_text,
    )
    if date_match is None:
        return None

    date = (
        f"{int(date_match.group(1)):04d}-"
        f"{int(date_match.group(2)):02d}-"
        f"{int(date_match.group(3)):02d}"
    )

    start = page_text.find(date_match.group(0)) + len(date_match.group(0))
    end = page_text.find("본 정보에는 오류가 있을 수", start)
    number_section = page_text[
        start:end if end > start else start + 250
    ]

    balls = [
        int(value)
        for value in re.findall(
            r"(?<![\d,])([1-9]|[1-3]\d|4[0-5])(?![\d,])",
            number_section,
        )
    ]

    if len(balls) < 7:
        print(f"[{round_no}] Pyony 당첨번호 파싱 실패: {balls}")
        return None

    numbers = balls[:6]
    bonus = balls[6]

    if not valid_numbers(numbers, bonus):
        print(
            f"[{round_no}] Pyony 번호 검증 실패: "
            f"{numbers}, bonus={bonus}"
        )
        return None

    first_count, first_amount, second_count = parse_rank_table(soup)

    return {
        "round": round_no,
        "date": date,
        "winning": {
            "numbers": numbers,
            "bonus": bonus,
        },
        "firstAmount": first_amount,
        "firstCount": first_count,
        "secondCount": second_count,
        "stores": parse_stores(soup, round_no),
    }


def build_empty_item(round_no: int) -> dict[str, Any]:
    return normalize({
        "round": round_no,
        "date": None,
        "winning": {
            "numbers": [],
            "bonus": None,
        },
        "prize": {
            "first": {
                "perGameAmount": None,
                "winnerCount": None,
            },
            "second": {
                "winnerCount": None,
            },
            "totalSalesAmount": None,
        },
        "stores": [],
        "dataSource": {},
    })


def merge_official(
    item: dict[str, Any],
    official: dict[str, Any] | None,
) -> bool:
    if official is None:
        return False

    before = json.dumps(item, ensure_ascii=False, sort_keys=True)

    if official.get("date"):
        item["date"] = official["date"]

    winning = official.get("winning") or {}
    if valid_numbers(winning.get("numbers"), winning.get("bonus")):
        item["winning"] = winning
        item["dataSource"]["winning"] = "dhlottery-official-json"

    if valid_first_amount(official.get("firstAmount")):
        item["prize"]["first"]["perGameAmount"] = official["firstAmount"]
        item["dataSource"]["firstPrize"] = "dhlottery-official-json"

    if valid_first_count(official.get("firstCount")):
        item["prize"]["first"]["winnerCount"] = official["firstCount"]
        item["dataSource"]["firstWinnerCount"] = (
            "dhlottery-official-json"
        )

    if valid_total_sales(official.get("totalSales")):
        item["prize"]["totalSalesAmount"] = official["totalSales"]
        item["dataSource"]["totalSales"] = "dhlottery-official-json"

    after = json.dumps(item, ensure_ascii=False, sort_keys=True)
    return before != after


def merge_pyony(
    item: dict[str, Any],
    page: dict[str, Any] | None,
) -> bool:
    if page is None:
        return False

    before = json.dumps(item, ensure_ascii=False, sort_keys=True)

    if page.get("date"):
        item["date"] = page["date"]

    winning = page.get("winning") or {}
    if valid_numbers(winning.get("numbers"), winning.get("bonus")):
        item["winning"] = winning
        item["dataSource"].setdefault(
            "winning",
            "pyony-round-page",
        )

    if valid_first_amount(page.get("firstAmount")):
        item["prize"]["first"]["perGameAmount"] = page["firstAmount"]
        item["dataSource"].setdefault(
            "firstPrize",
            "pyony-round-page",
        )

    if valid_first_count(page.get("firstCount")):
        item["prize"]["first"]["winnerCount"] = page["firstCount"]
        item["dataSource"].setdefault(
            "firstWinnerCount",
            "pyony-round-page",
        )

    if valid_second_count(page.get("secondCount")):
        item["prize"]["second"]["winnerCount"] = page["secondCount"]
        item["dataSource"]["secondWinnerCount"] = "pyony-round-page"

    stores = page.get("stores") or []
    if stores:
        item["stores"] = stores
        item["dataSource"]["stores"] = "pyony-round-page"

    after = json.dumps(item, ensure_ascii=False, sort_keys=True)
    return before != after


def refresh_round(
    item: dict[str, Any],
    *,
    use_official: bool = True,
    use_pyony: bool = True,
) -> bool:
    round_no = int(item["round"])
    changed = False

    if use_official:
        changed |= merge_official(item, fetch_official(round_no))

    if use_pyony:
        changed |= merge_pyony(item, fetch_pyony(round_no))

    prize = item["prize"]
    print(
        f"[{round_no}] "
        f"번호={item['winning'].get('numbers')}, "
        f"1등금={prize['first'].get('perGameAmount')}, "
        f"1등수={prize['first'].get('winnerCount')}, "
        f"2등수={prize['second'].get('winnerCount')}, "
        f"판매액={prize.get('totalSalesAmount')}, "
        f"판매점={len(item.get('stores') or [])}"
    )

    return changed


def descending_batch(
    start_round: int,
    maximum_round: int,
    batch_size: int,
) -> tuple[list[int], int]:
    if maximum_round <= 0 or batch_size <= 0:
        return [], 0

    current = min(max(start_round, 1), maximum_round)
    rounds: list[int] = []

    while len(rounds) < batch_size:
        rounds.append(current)
        current -= 1

        if current < 1:
            current = maximum_round

        if current == start_round:
            break

    return rounds, current


def main() -> int:
    data = load_data()
    by_round = {
        int(item["round"]): item
        for item in data["results"]
        if to_int(item.get("round")) is not None
    }

    latest = max(by_round, default=0)
    changed = False
    errors: list[str] = []
    original_core_cursor = to_int(
        data["service"].get("coreBackfillCursorRound")
    )
    original_store_cursor = to_int(
        data["service"].get("storeBackfillCursorRound")
    )
    original_errors = list(data["service"].get("lastErrors") or [])

    # 1) 중간에 빠진 회차 복구
    missing = [
        round_no
        for round_no in range(1, latest + 1)
        if round_no not in by_round
    ]

    for round_no in sorted(missing, reverse=True)[:30]:
        item = build_empty_item(round_no)
        refreshed = refresh_round(item)

        if not valid_numbers(
            item["winning"].get("numbers"),
            item["winning"].get("bonus"),
        ):
            errors.append(f"{round_no}회 누락 복구 실패")
            continue

        data["results"].append(item)
        by_round[round_no] = item
        changed = True
        changed |= refreshed

    # 2) 새 회차: 공식 JSON에서 번호가 나오면 즉시 추가
    for _ in range(3):
        candidate = max(by_round, default=0) + 1
        official = fetch_official(candidate)
        page = fetch_pyony(candidate)

        if official is None and page is None:
            break

        item = build_empty_item(candidate)
        merge_official(item, official)
        merge_pyony(item, page)

        if not valid_numbers(
            item["winning"].get("numbers"),
            item["winning"].get("bonus"),
        ):
            break

        data["results"].append(item)
        by_round[candidate] = item
        changed = True
        print(f"[{candidate}] 새 회차 추가 완료")

    latest = max(by_round, default=0)

    # 3) 최근 20회는 미완성 정보 또는 판매점 누락 시 매번 우선 확인
    recent_rounds = range(
        latest,
        max(0, latest - RECENT_PRIORITY_COUNT),
        -1,
    )

    for round_no in recent_rounds:
        item = by_round.get(round_no)
        if item is None:
            continue

        if not core_complete(item) or not stores_complete(item):
            changed |= refresh_round(item)

        if not core_complete(item):
            errors.append(f"{round_no}회 회차 당첨 정보 미완성")

    # 4) 1215회 이전을 포함한 과거 회차 핵심정보를 30회씩 순환 보완
    historical_max = max(0, latest - RECENT_PRIORITY_COUNT)
    core_cursor = (
        to_int(data["service"].get("coreBackfillCursorRound"))
        or historical_max
    )
    core_batch, next_core_cursor = descending_batch(
        core_cursor,
        historical_max,
        CORE_BACKFILL_BATCH,
    )

    for round_no in core_batch:
        item = by_round.get(round_no)
        if item is None or core_complete(item):
            continue
        changed |= refresh_round(item)

    # 5) 판매점은 과거 회차를 5회씩 별도 순환 보완
    store_cursor = (
        to_int(data["service"].get("storeBackfillCursorRound"))
        or historical_max
    )
    store_batch, next_store_cursor = descending_batch(
        store_cursor,
        historical_max,
        STORE_BACKFILL_BATCH,
    )

    for round_no in store_batch:
        item = by_round.get(round_no)
        if item is None or stores_complete(item):
            continue
        changed |= refresh_round(
            item,
            use_official=False,
            use_pyony=True,
        )

    completed_core = sum(
        1 for item in data["results"] if core_complete(item)
    )
    completed_stores = sum(
        1 for item in data["results"] if stores_complete(item)
    )

    state_changed = (
        data["service"].get("collectorVersion")
        != "4.0-fully-automatic-scheduled-backfill"
        or original_core_cursor != next_core_cursor
        or original_store_cursor != next_store_cursor
        or original_errors != errors[-30:]
    )

    data["service"].update({
        "collectorVersion": "4.0-fully-automatic-scheduled-backfill",
        "mode": "fully-automatic-official-plus-pyony",
        "newRoundUpdate": "scheduled-auto-detect-and-commit",
        "recentPriorityCount": RECENT_PRIORITY_COUNT,
        "coreBackfillBatchSize": CORE_BACKFILL_BATCH,
        "coreBackfillCursorRound": next_core_cursor,
        "storeBackfillBatchSize": STORE_BACKFILL_BATCH,
        "storeBackfillCursorRound": next_store_cursor,
        "completedCoreRoundCount": completed_core,
        "incompleteCoreRoundCount": (
            len(data["results"]) - completed_core
        ),
        "completedStoreRoundCount": completed_stores,
        "lastRunAt": datetime.now(KST).isoformat(timespec="seconds"),
        "lastErrors": errors[-30:],
    })

    if changed or state_changed:
        saved = save_data(data)
    else:
        saved = False
        print("새 회차·보완 데이터·진행 상태 변경이 없어 저장하지 않습니다.")

    print(
        f"완료: 최신 {data['latestRound']}회 / "
        f"회차정보 완성 {completed_core}회 / "
        f"회차정보 미완성 {len(data['results']) - completed_core}회 / "
        f"판매점 완성 {completed_stores}회 / "
        f"파일 저장 {'완료' if saved else '없음'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
