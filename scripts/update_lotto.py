from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "lotto_results.json"
BACKUP_PATH = ROOT / "data" / "lotto_results_backup.json"
KST = timezone(timedelta(hours=9))

# 동행복권 공식 페이지 외의 제3자 사이트는 사용하지 않습니다.
OFFICIAL_RESULT_URL = "https://www.dhlottery.co.kr/lt645/result"
OFFICIAL_STORE_URL = "https://www.dhlottery.co.kr/wnprchsplcsrch/home"
COLLECTOR_VERSION = "5.0-official-only-playwright"

RECENT_REFRESH_COUNT = 4
OLD_BACKFILL_BATCH = 2
PAGE_TIMEOUT_MS = 60_000


def to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    digits = re.sub(r"[^0-9]", "", str(value))
    return int(digits) if digits else None


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def valid_numbers(numbers: Any, bonus: Any) -> bool:
    if not isinstance(numbers, list) or len(numbers) != 6:
        return False
    try:
        values = [int(v) for v in numbers]
        bonus_value = int(bonus)
    except (TypeError, ValueError):
        return False
    return (
        len(set(values)) == 6
        and all(1 <= v <= 45 for v in values)
        and 1 <= bonus_value <= 45
        and bonus_value not in values
    )


def valid_amount(value: Any) -> bool:
    number = to_int(value)
    return number is not None and 10_000_000 <= number <= 100_000_000_000


def valid_count(value: Any) -> bool:
    number = to_int(value)
    return number is not None and 0 <= number <= 100_000


def valid_sales(value: Any) -> bool:
    number = to_int(value)
    return number is not None and 1_000_000_000 <= number <= 10_000_000_000_000


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
    item.setdefault("date", None)
    item.setdefault("dataSource", {})
    return item


def build_empty(round_no: int) -> dict[str, Any]:
    return normalize({
        "round": round_no,
        "date": None,
        "winning": {"numbers": [], "bonus": None},
        "prize": {
            "first": {"perGameAmount": None, "winnerCount": None},
            "second": {"winnerCount": None},
            "totalSalesAmount": None,
        },
        "stores": [],
        "dataSource": {},
    })


def load_data() -> dict[str, Any]:
    if not DATA_PATH.exists():
        raise FileNotFoundError(f"데이터 파일이 없습니다: {DATA_PATH}")
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    data.setdefault("schemaVersion", 2)
    data.setdefault("results", [])
    data.setdefault("service", {})
    data["results"] = [normalize(x) for x in data["results"] if isinstance(x, dict)]
    return data


def save_data(data: dict[str, Any]) -> bool:
    data["results"].sort(key=lambda x: int(x.get("round", 0)))
    data["latestRound"] = max((int(x["round"]) for x in data["results"]), default=0)
    data["updatedAt"] = datetime.now(KST).isoformat(timespec="seconds")
    new_text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    old_text = DATA_PATH.read_text(encoding="utf-8") if DATA_PATH.exists() else ""
    if new_text == old_text:
        print("저장할 변경사항이 없습니다.")
        return False
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    if old_text:
        BACKUP_PATH.write_text(old_text, encoding="utf-8")
    tmp = DATA_PATH.with_suffix(".json.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(DATA_PATH)
    return True


def core_complete(item: dict[str, Any]) -> bool:
    item = normalize(item)
    prize = item["prize"]
    return (
        valid_numbers(item["winning"].get("numbers"), item["winning"].get("bonus"))
        and valid_amount(prize["first"].get("perGameAmount"))
        and valid_count(prize["first"].get("winnerCount"))
        and valid_count(prize["second"].get("winnerCount"))
    )


def candidate_result_urls(round_no: int | None = None) -> list[str]:
    if round_no is None:
        return [OFFICIAL_RESULT_URL]
    return [
        f"{OFFICIAL_RESULT_URL}?result=byWin&lottoId=LO40&drwNo={round_no}",
        f"{OFFICIAL_RESULT_URL}?drwNo={round_no}",
        f"https://www.dhlottery.co.kr/gameResult.do?method=byWin&drwNo={round_no}",
    ]


def goto_round(page: Page, round_no: int) -> str:
    last_error: Exception | None = None
    for url in candidate_result_urls(round_no):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            page.wait_for_timeout(1800)
            body = page.locator("body").inner_text(timeout=20_000)
            compact = body.replace(" ", "")
            if f"{round_no}회" in compact or f"제{round_no}회" in compact:
                return body
        except Exception as exc:
            last_error = exc

    # URL 인자가 무시되는 경우 회차 선택 상자를 직접 조작합니다.
    page.goto(OFFICIAL_RESULT_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_timeout(1500)
    for select in page.locator("select").all():
        try:
            options = select.locator("option").all_text_contents()
            values = select.locator("option").evaluate_all("els => els.map(e => e.value)")
            for index, text in enumerate(options):
                if to_int(text) == round_no:
                    select.select_option(values[index])
                    page.wait_for_timeout(2000)
                    body = page.locator("body").inner_text(timeout=20_000)
                    compact = body.replace(" ", "")
                    if f"{round_no}회" in compact or f"제{round_no}회" in compact:
                        return body
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"공식 페이지에서 {round_no}회를 열지 못했습니다: {last_error}")


def detect_latest_round(page: Page) -> int:
    page.goto(OFFICIAL_RESULT_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_timeout(1800)
    candidates: list[int] = []
    for select in page.locator("select").all():
        try:
            for text in select.locator("option").all_text_contents():
                value = to_int(text)
                if value and value >= 1:
                    candidates.append(value)
        except Exception:
            continue
    body = page.locator("body").inner_text(timeout=20_000)
    candidates.extend(int(v) for v in re.findall(r"(?:제\s*)?(\d{1,4})\s*회", body))
    if not candidates:
        raise RuntimeError("공식 당첨결과 페이지에서 최신 회차를 찾지 못했습니다.")
    latest = max(candidates)
    print(f"공식 페이지 최신 회차: {latest}")
    return latest


def extract_date(text: str) -> str | None:
    patterns = [
        r"(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일",
        r"(20\d{2})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    return None


def extract_balls_from_dom(page: Page) -> tuple[list[int], int | None]:
    selectors = [
        "[class*='ball']",
        "[class*='lotto'] [class*='num']",
        "[class*='win'] [class*='num']",
    ]
    for selector in selectors:
        try:
            texts = page.locator(selector).all_text_contents()
        except Exception:
            continue
        values = [to_int(t) for t in texts]
        values = [v for v in values if v is not None and 1 <= v <= 45]
        for start in range(max(1, len(values) - 12)):
            seq = values[start:start + 7]
            if len(seq) == 7 and valid_numbers(seq[:6], seq[6]):
                return seq[:6], seq[6]
    return [], None


def extract_balls_from_text(text: str, round_no: int) -> tuple[list[int], int | None]:
    compact = clean_text(text)
    starts = [compact.find("당첨번호"), compact.find(f"{round_no}회")]
    for start in starts:
        if start < 0:
            continue
        section = compact[start:start + 500]
        # 회차·연도·당첨금 숫자를 피하기 위해 1~45의 독립 숫자만 찾습니다.
        values = [int(v) for v in re.findall(r"(?<![\d,])([1-9]|[1-3]\d|4[0-5])(?![\d,])", section)]
        for index in range(max(1, len(values) - 6)):
            seq = values[index:index + 7]
            if len(seq) == 7 and valid_numbers(seq[:6], seq[6]):
                return seq[:6], seq[6]
    return [], None


def parse_prize_table(page: Page, body_text: str) -> dict[str, int | None]:
    result = {"firstAmount": None, "firstCount": None, "secondCount": None, "totalSales": None}

    # DOM 표를 우선 사용합니다.
    for row in page.locator("table tr").all():
        try:
            cells = [clean_text(v) for v in row.locator("th,td").all_text_contents()]
        except Exception:
            continue
        if not cells:
            continue
        rank = cells[0].replace(" ", "")
        numbers = [to_int(v) for v in cells[1:]]
        numbers = [v for v in numbers if v is not None]
        if rank == "1등" and len(numbers) >= 3:
            # 공식 표: 등위별 총 당첨금 / 당첨게임 수 / 1게임당 당첨금
            result["firstCount"] = numbers[1]
            result["firstAmount"] = numbers[2]
        elif rank == "2등" and len(numbers) >= 2:
            result["secondCount"] = numbers[1]

    patterns = {
        "firstCount": [r"1등[^\n]{0,120}?당첨게임\s*수\s*([0-9,]+)", r"1등[^\n]{0,80}?([0-9,]+)\s*게임"],
        "secondCount": [r"2등[^\n]{0,120}?당첨게임\s*수\s*([0-9,]+)", r"2등[^\n]{0,80}?([0-9,]+)\s*게임"],
        "firstAmount": [r"1등[^\n]{0,180}?1게임당\s*당첨금\s*([0-9,]+)", r"1등[^\n]{0,180}?([0-9,]+)\s*원"],
        "totalSales": [r"총\s*판매\s*금액\s*[:：]?\s*([0-9,]+)", r"총판매금액\s*[:：]?\s*([0-9,]+)"],
    }
    for key, regexes in patterns.items():
        if result[key] is not None:
            continue
        for pattern in regexes:
            match = re.search(pattern, body_text, re.S)
            if match:
                result[key] = to_int(match.group(1))
                break
    return result


def parse_store_text(text: str) -> list[dict[str, str]]:
    lines = [clean_text(x) for x in text.splitlines() if clean_text(x)]
    methods = {"자동", "수동", "반자동"}
    stores: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, line in enumerate(lines):
        if line not in methods:
            continue
        name = lines[index - 1] if index >= 1 else ""
        address = lines[index + 1] if index + 1 < len(lines) else ""
        if name in {"구분", "번호", "상호명", "상호", "선택"} or len(name) < 2:
            continue
        if address in methods or len(address) < 4:
            address = ""
        key = (name, line, address)
        if key in seen:
            continue
        seen.add(key)
        stores.append({"name": name, "method": line, "address": address})
    return stores


def fetch_stores_from_result(page: Page, round_no: int) -> list[dict[str, str]]:
    targets = page.get_by_text(re.compile(r"당첨\s*판매점|1등\s*판매점"))
    for index in range(min(targets.count(), 4)):
        target = targets.nth(index)
        try:
            with page.context.expect_page(timeout=5_000) as popup_info:
                target.click(timeout=5_000)
            popup = popup_info.value
            popup.wait_for_load_state("domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            popup.wait_for_timeout(1500)
            stores = parse_store_text(popup.locator("body").inner_text(timeout=20_000))
            popup.close()
            if stores:
                return stores
        except Exception:
            try:
                target.click(timeout=5_000)
                page.wait_for_timeout(1500)
                stores = parse_store_text(page.locator("body").inner_text(timeout=20_000))
                if stores:
                    return stores
                goto_round(page, round_no)
            except Exception:
                continue
    return []


def fetch_official_round(page: Page, round_no: int) -> dict[str, Any]:
    print(f"[{round_no}] 동행복권 공식 페이지 조회")
    body = goto_round(page, round_no)
    numbers, bonus = extract_balls_from_dom(page)
    if not valid_numbers(numbers, bonus):
        numbers, bonus = extract_balls_from_text(body, round_no)
    if not valid_numbers(numbers, bonus):
        raise RuntimeError(f"당첨번호 파싱 실패: numbers={numbers}, bonus={bonus}")

    prize = parse_prize_table(page, body)
    stores = fetch_stores_from_result(page, round_no)
    return {
        "round": round_no,
        "date": extract_date(body),
        "winning": {"numbers": numbers, "bonus": bonus},
        "firstAmount": prize["firstAmount"],
        "firstCount": prize["firstCount"],
        "secondCount": prize["secondCount"],
        "totalSales": prize["totalSales"],
        "stores": stores,
    }


def merge_official(item: dict[str, Any], incoming: dict[str, Any]) -> bool:
    item = normalize(item)
    before = json.dumps(item, ensure_ascii=False, sort_keys=True)
    source = "dhlottery-official-page"

    if incoming.get("date"):
        item["date"] = incoming["date"]
    winning = incoming.get("winning") or {}
    if valid_numbers(winning.get("numbers"), winning.get("bonus")):
        item["winning"] = winning
        item["dataSource"]["winning"] = source
    if valid_amount(incoming.get("firstAmount")):
        item["prize"]["first"]["perGameAmount"] = to_int(incoming["firstAmount"])
        item["dataSource"]["firstPrize"] = source
    if valid_count(incoming.get("firstCount")):
        item["prize"]["first"]["winnerCount"] = to_int(incoming["firstCount"])
        item["dataSource"]["firstWinnerCount"] = source
    if valid_count(incoming.get("secondCount")):
        item["prize"]["second"]["winnerCount"] = to_int(incoming["secondCount"])
        item["dataSource"]["secondWinnerCount"] = source
    if valid_sales(incoming.get("totalSales")):
        item["prize"]["totalSalesAmount"] = to_int(incoming["totalSales"])
        item["dataSource"]["totalSales"] = source
    if incoming.get("stores"):
        item["stores"] = incoming["stores"]
        item["dataSource"]["stores"] = source

    after = json.dumps(item, ensure_ascii=False, sort_keys=True)
    return before != after


def descending_batch(start: int, maximum: int, size: int) -> tuple[list[int], int]:
    if maximum < 1 or size < 1:
        return [], 0
    current = min(max(start, 1), maximum)
    values: list[int] = []
    while len(values) < size:
        values.append(current)
        current -= 1
        if current < 1:
            current = maximum
        if current == start:
            break
    return values, current


def main() -> int:
    data = load_data()
    by_round = {int(x["round"]): x for x in data["results"] if to_int(x.get("round"))}
    changed = False
    errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context: BrowserContext = browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1440, "height": 1800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            official_latest = detect_latest_round(page)
        except Exception as exc:
            context.close()
            browser.close()
            raise RuntimeError(f"공식 최신 회차 확인 실패: {exc}") from exc

        current_latest = max(by_round, default=0)
        targets: list[int] = []

        # 공식 최신 회차까지 누락된 회차를 먼저 추가합니다.
        for round_no in range(current_latest + 1, official_latest + 1):
            if round_no not in by_round:
                by_round[round_no] = build_empty(round_no)
                changed = True
            targets.append(round_no)

        # 최신 회차 및 최근 회차는 매번 다시 검증합니다.
        for round_no in range(official_latest, max(0, official_latest - RECENT_REFRESH_COUNT), -1):
            if round_no in by_round and round_no not in targets:
                targets.append(round_no)

        # 과거 미완성 회차는 소량씩 공식 페이지로 보완합니다.
        incomplete_old = [r for r in sorted(by_round, reverse=True) if r <= official_latest - RECENT_REFRESH_COUNT and not core_complete(by_round[r])]
        cursor = to_int(data["service"].get("officialBackfillCursorRound")) or (incomplete_old[0] if incomplete_old else official_latest)
        old_batch, next_cursor = descending_batch(cursor, official_latest, OLD_BACKFILL_BATCH)
        for round_no in old_batch:
            if round_no in by_round and not core_complete(by_round[round_no]) and round_no not in targets:
                targets.append(round_no)

        for round_no in targets:
            try:
                incoming = fetch_official_round(page, round_no)
                changed |= merge_official(by_round[round_no], incoming)
                prize = by_round[round_no]["prize"]
                print(
                    f"[{round_no}] 번호={by_round[round_no]['winning'].get('numbers')}, "
                    f"1등금={prize['first'].get('perGameAmount')}, "
                    f"1등수={prize['first'].get('winnerCount')}, "
                    f"2등수={prize['second'].get('winnerCount')}, "
                    f"판매액={prize.get('totalSalesAmount')}, "
                    f"판매점={len(by_round[round_no].get('stores') or [])}"
                )
            except (PlaywrightTimeoutError, Exception) as exc:
                message = f"[{round_no}] 공식 페이지 수집 실패: {exc}"
                print(message)
                errors.append(message)

        context.close()
        browser.close()

    data["results"] = list(by_round.values())
    service = data["service"]
    service.update({
        "collectorVersion": COLLECTOR_VERSION,
        "sourcePolicy": "official-dhlottery-only",
        "thirdPartySourceUsed": False,
        "officialBackfillCursorRound": next_cursor if 'next_cursor' in locals() else official_latest,
        "lastRunAt": datetime.now(KST).isoformat(timespec="seconds"),
        "lastErrors": errors[-30:],
        "coreCompleteCount": sum(1 for x in by_round.values() if core_complete(x)),
        "coreIncompleteCount": sum(1 for x in by_round.values() if not core_complete(x)),
    })

    # 서비스 상태가 바뀌어도 저장합니다.
    changed = True
    saved = save_data(data)
    print(
        f"완료: 공식 최신 {official_latest}회 / "
        f"핵심완성 {service['coreCompleteCount']}회 / "
        f"미완성 {service['coreIncompleteCount']}회 / "
        f"오류 {len(errors)}건 / 저장={'예' if saved else '아니오'}"
    )

    # 최신 회차 자체를 못 가져온 경우에는 성공으로 위장하지 않습니다.
    if not core_complete(by_round.get(official_latest, {})):
        print(f"최신 {official_latest}회 핵심정보가 완성되지 않았습니다.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
