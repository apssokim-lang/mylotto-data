from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "lotto_results.json"
KST = timezone(timedelta(hours=9))

PYONY_URL = "https://pyony.com/lotto/rounds/{round_no}/"
OFFICIAL_API = (
    "https://www.dhlottery.co.kr/common.do"
    "?method=getLottoNumber&drwNo={round_no}"
)
NEWSBRUNCH_LISTS = [
    "https://newsbrunch.net/news/list.php?mcode=m346wre",
    "https://newsbrunch.net/m/listkey.php?idx=8435&key_idx=44",
]

SESSION = requests.Session()
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
        nums = [int(x) for x in numbers]
        b = int(bonus)
    except (TypeError, ValueError):
        return False
    return (
        len(set(nums)) == 6
        and all(1 <= x <= 45 for x in nums)
        and 1 <= b <= 45
        and b not in nums
    )


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
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    data.setdefault("schemaVersion", 2)
    data.setdefault("results", [])
    data.setdefault("service", {})
    data["results"] = [normalize(x) for x in data["results"] if isinstance(x, dict)]
    return data


def save_data(data: dict[str, Any]) -> None:
    data["results"].sort(key=lambda x: int(x.get("round", 0)))
    data["latestRound"] = max((int(x["round"]) for x in data["results"]), default=0)
    data["updatedAt"] = datetime.now(KST).isoformat(timespec="seconds")
    tmp = DATA_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(DATA_PATH)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_rank_table(soup: BeautifulSoup) -> tuple[int | None, int | None, int | None]:
    first_count = first_amount = second_count = None
    for row in soup.select("table tr"):
        cells = [clean_text(c.get_text(" ", strip=True)) for c in row.select("th, td")]
        if not cells:
            continue
        rank = cells[0].replace(" ", "")
        nums = [to_int(x) for x in cells[1:] if re.fullmatch(r"[0-9][0-9,]*\s*(?:원)?", x)]
        nums = [x for x in nums if x is not None]
        if rank == "1등" and len(nums) >= 2:
            first_count, first_amount = nums[0], nums[1]
        elif rank == "2등" and len(nums) >= 1:
            second_count = nums[0]
    return first_count, first_amount, second_count


def parse_stores(soup: BeautifulSoup, round_no: int) -> list[dict[str, str]]:
    heading = soup.find(
        lambda tag: (
            tag.name in {"h2", "h3", "h4"}
            and f"{round_no}회" in clean_text(tag.get_text(" ", strip=True))
            and "1등 당첨지역 판매점" in clean_text(tag.get_text(" ", strip=True))
        )
    )
    table = heading.find_next("table") if heading else None
    if table is None:
        return []

    stores: list[dict[str, str]] = []
    for row in table.select("tr"):
        cells = [clean_text(c.get_text(" ", strip=True)) for c in row.select("td")]
        if len(cells) < 4 or to_int(cells[0]) is None:
            continue
        stores.append({"name": cells[1], "method": cells[2], "address": cells[3]})
    return stores


def fetch_pyony(round_no: int) -> dict[str, Any] | None:
    try:
        response = SESSION.get(PYONY_URL.format(round_no=round_no), timeout=30)
        response.raise_for_status()
    except Exception as exc:
        print(f"[{round_no}] pyony 조회 실패: {exc}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    page_text = clean_text(soup.get_text(" ", strip=True))
    if f"로또 {round_no}회 당첨번호" not in page_text:
        return None

    date_match = re.search(
        rf"{round_no}회\s*\((\d{{4}})년\s*(\d{{1,2}})월\s*(\d{{1,2}})일\s*추첨\)",
        page_text,
    )
    if not date_match:
        return None
    date = f"{int(date_match.group(1)):04d}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"

    # 회차 날짜 문구 다음부터 안내 문구 전까지 숫자 7개(당첨 6 + 보너스)를 읽는다.
    start = page_text.find(date_match.group(0)) + len(date_match.group(0))
    end = page_text.find("본 정보에는 오류가 있을 수", start)
    number_section = page_text[start:end if end > start else start + 250]
    balls = [int(x) for x in re.findall(r"(?<![\d,])([1-9]|[1-3]\d|4[0-5])(?![\d,])", number_section)]
    if len(balls) < 7:
        print(f"[{round_no}] 당첨번호 파싱 실패: {balls}")
        return None
    numbers, bonus = balls[:6], balls[6]
    if not valid_numbers(numbers, bonus):
        print(f"[{round_no}] 당첨번호 검증 실패: {numbers}, bonus={bonus}")
        return None

    first_count, first_amount, second_count = parse_rank_table(soup)
    if not (
        first_count and 1 <= first_count <= 1000
        and first_amount and 10_000_000 <= first_amount <= 100_000_000_000
        and second_count and 1 <= second_count <= 100000
    ):
        print(
            f"[{round_no}] 당첨금 표 파싱 실패: "
            f"1등금={first_amount}, 1등수={first_count}, 2등수={second_count}"
        )
        return None

    return {
        "round": round_no,
        "date": date,
        "winning": {"numbers": numbers, "bonus": bonus},
        "firstAmount": first_amount,
        "firstCount": first_count,
        "secondCount": second_count,
        "stores": parse_stores(soup, round_no),
    }


def fetch_official_total(round_no: int) -> int | None:
    try:
        response = SESSION.get(OFFICIAL_API.format(round_no=round_no), timeout=20)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None
    if payload.get("returnValue") != "success":
        return None
    value = to_int(payload.get("totSellamnt"))
    return value if value and 1_000_000_000 <= value <= 10_000_000_000_000 else None


def fetch_newsbrunch_total(round_no: int) -> int | None:
    patterns = [
        rf"로또\s*{round_no}회의\s*총판매금액은\s*([0-9,]+)원",
        rf"제?\s*{round_no}회(?:차)?\s*로또\s*총\s*판매금액은\s*([0-9,]+)원",
    ]
    for url in NEWSBRUNCH_LISTS:
        try:
            response = SESSION.get(url, timeout=20)
            response.raise_for_status()
            text = clean_text(BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True))
        except Exception:
            continue
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                value = to_int(match.group(1))
                if value and 1_000_000_000 <= value <= 10_000_000_000_000:
                    return value
    return None


def build_item(page: dict[str, Any], total_sales: int | None) -> dict[str, Any]:
    return normalize({
        "round": page["round"],
        "date": page["date"],
        "winning": page["winning"],
        "prize": {
            "first": {
                "perGameAmount": page["firstAmount"],
                "winnerCount": page["firstCount"],
            },
            "second": {"winnerCount": page["secondCount"]},
            "totalSalesAmount": total_sales,
        },
        "stores": page["stores"],
        "dataSource": {
            "winning": "pyony-round-page",
            "prize": "pyony-round-page",
            "stores": "pyony-round-page",
            "totalSales": (
                "dhlottery-official-json" if total_sales is not None else "pending"
            ),
        },
    })


def merge(item: dict[str, Any], page: dict[str, Any], total_sales: int | None) -> bool:
    before = json.dumps(item, ensure_ascii=False, sort_keys=True)
    item["date"] = page["date"]
    item["winning"] = page["winning"]
    item["prize"]["first"]["perGameAmount"] = page["firstAmount"]
    item["prize"]["first"]["winnerCount"] = page["firstCount"]
    item["prize"]["second"]["winnerCount"] = page["secondCount"]
    if total_sales is not None:
        item["prize"]["totalSalesAmount"] = total_sales
    if page["stores"]:
        item["stores"] = page["stores"]
    item["dataSource"].update({
        "winning": "pyony-round-page",
        "prize": "pyony-round-page",
        "stores": "pyony-round-page" if page["stores"] else item["dataSource"].get("stores", "pending"),
    })
    return before != json.dumps(item, ensure_ascii=False, sort_keys=True)


def complete(item: dict[str, Any]) -> bool:
    p = item["prize"]
    return (
        valid_numbers(item["winning"].get("numbers"), item["winning"].get("bonus"))
        and p["first"].get("perGameAmount") is not None
        and p["first"].get("winnerCount") is not None
        and p["second"].get("winnerCount") is not None
        and p.get("totalSalesAmount") is not None
        and bool(item.get("stores"))
    )


def main() -> int:
    data = load_data()
    by_round = {int(x["round"]): x for x in data["results"] if to_int(x.get("round")) is not None}
    latest = max(by_round, default=0)
    changed = False
    errors: list[str] = []

    # 핵심 수정: latestRound 아래에 빠진 회차가 있으면 반드시 복구한다.
    missing = [r for r in range(1, latest + 1) if r not in by_round]
    # 최근 누락부터 한 번에 최대 30개. 현재 1215~1228 누락 14개는 첫 실행에 전부 복구된다.
    for round_no in sorted(missing, reverse=True)[:30]:
        page = fetch_pyony(round_no)
        if page is None:
            errors.append(f"{round_no}회 누락 복구 실패")
            continue
        total = fetch_official_total(round_no) or fetch_newsbrunch_total(round_no)
        item = build_item(page, total)
        data["results"].append(item)
        by_round[round_no] = item
        changed = True
        print(f"[{round_no}] 누락 회차 복구 완료")

    # 새 회차는 pyony 페이지에 실제 당첨금 표가 생긴 경우에만 추가한다.
    candidate = max(by_round, default=0) + 1
    while True:
        page = fetch_pyony(candidate)
        if page is None:
            break
        total = fetch_official_total(candidate) or fetch_newsbrunch_total(candidate)
        item = build_item(page, total)
        data["results"].append(item)
        by_round[candidate] = item
        changed = True
        print(f"[{candidate}] 새 회차 추가 완료")
        candidate += 1

    latest = max(by_round, default=0)

    # 최근 20회 미완성 데이터 우선 보완
    targets = [
        r for r in range(latest, max(0, latest - 20), -1)
        if r in by_round and not complete(by_round[r])
    ]

    # 오래된 미완성 데이터는 실행당 10개
    older = [
        r for r in sorted(by_round, reverse=True)
        if r <= latest - 20 and not complete(by_round[r])
    ]
    cursor = to_int(data["service"].get("olderCursor")) or 0
    older_batch: list[int] = []
    if older:
        cursor %= len(older)
        older_batch = (older[cursor:cursor + 10] + older[:max(0, cursor + 10 - len(older))])[:10]

    for round_no in list(dict.fromkeys(targets + older_batch)):
        page = fetch_pyony(round_no)
        if page is None:
            errors.append(f"{round_no}회 보완 실패")
            continue
        total = (
            by_round[round_no]["prize"].get("totalSalesAmount")
            or fetch_official_total(round_no)
            or fetch_newsbrunch_total(round_no)
        )
        if merge(by_round[round_no], page, total):
            changed = True
        if not complete(by_round[round_no]):
            errors.append(f"{round_no}회 일부 정보 미완성")

    data["service"].update({
        "collectorVersion": "3.0-gap-repair-pyony-primary",
        "mode": "pyony-primary-with-official-and-newsbrunch-fallback",
        "missingRoundRepair": "enabled",
        "recentPriorityCount": 20,
        "olderBatchSize": 10,
        "olderCursor": cursor + len(older_batch) if older else 0,
        "lastRunAt": datetime.now(KST).isoformat(timespec="seconds"),
        "lastErrors": errors[-30:],
    })

    completed = sum(1 for x in data["results"] if complete(x))
    data["service"]["completedRoundCount"] = completed
    data["service"]["incompleteRoundCount"] = len(data["results"]) - completed

    save_data(data)
    print(
        f"완료: 최신 {data['latestRound']}회 / 총 {len(data['results'])}회 / "
        f"완성 {completed}회 / 미완성 {len(data['results']) - completed}회"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
