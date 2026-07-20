from __future__ import annotations

import copy
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

OFFICIAL_RESULT_URL = "https://www.dhlottery.co.kr/lt645/result"
COLLECTOR_VERSION = "6.0-official-immutable-round-engine"
RECENT_VERIFY_COUNT = 12
OLD_BACKFILL_BATCH = 3
PAGE_TIMEOUT_MS = 60_000
SOURCE = "dhlottery-official-page"


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
    return len(set(values)) == 6 and all(1 <= v <= 45 for v in values) and 1 <= bonus_value <= 45 and bonus_value not in values


def valid_amount(value: Any) -> bool:
    number = to_int(value)
    return number is not None and 10_000_000 <= number <= 100_000_000_000


def valid_count(value: Any) -> bool:
    number = to_int(value)
    return number is not None and 0 <= number <= 100_000


def valid_sales(value: Any) -> bool:
    number = to_int(value)
    return number is not None and 1_000_000_000 <= number <= 10_000_000_000_000


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    item = copy.deepcopy(raw)
    if not isinstance(item.get("winning"), dict):
        item["winning"] = {"numbers": item.pop("numbers", []), "bonus": item.pop("bonus", None)}
    item.setdefault("winning", {}).setdefault("numbers", [])
    item["winning"].setdefault("bonus", None)
    prize = item.setdefault("prize", {})
    prize.setdefault("first", {}).setdefault("perGameAmount", None)
    prize["first"].setdefault("winnerCount", None)
    prize.setdefault("second", {}).setdefault("winnerCount", None)
    prize.setdefault("totalSalesAmount", None)
    item["stores"] = item.get("stores", item.pop("firstPrizeStores", [])) or []
    item.setdefault("date", None)
    item.setdefault("dataSource", {})
    return item


def build_item(incoming: dict[str, Any]) -> dict[str, Any]:
    item = normalize({"round": int(incoming["round"])})
    return apply_incoming(item, incoming, allow_winning_replace=True)


def load_data() -> dict[str, Any]:
    if not DATA_PATH.exists():
        raise FileNotFoundError(f"데이터 파일이 없습니다: {DATA_PATH}")
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    data.setdefault("schemaVersion", 2)
    data.setdefault("results", [])
    data.setdefault("service", {})
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in data["results"]:
        if not isinstance(raw, dict):
            continue
        round_no = to_int(raw.get("round"))
        if not round_no or round_no in seen:
            raise ValueError(f"회차 중복 또는 잘못된 회차: {raw.get('round')}")
        seen.add(round_no)
        item = normalize(raw)
        item["round"] = round_no
        normalized.append(item)
    data["results"] = normalized
    return data


def core_complete(item: dict[str, Any]) -> bool:
    item = normalize(item)
    prize = item["prize"]
    return valid_numbers(item["winning"].get("numbers"), item["winning"].get("bonus")) and valid_amount(prize["first"].get("perGameAmount")) and valid_count(prize["first"].get("winnerCount")) and valid_count(prize["second"].get("winnerCount"))


def winning_signature(item: dict[str, Any]) -> tuple[int, ...] | None:
    item = normalize(item)
    nums = item["winning"].get("numbers")
    bonus = item["winning"].get("bonus")
    if not valid_numbers(nums, bonus):
        return None
    return tuple(int(v) for v in nums) + (int(bonus),)


def consecutive_duplicate_suspects(by_round: dict[int, dict[str, Any]]) -> set[int]:
    suspects: set[int] = set()
    rounds = sorted(by_round)
    for left, right in zip(rounds, rounds[1:]):
        if right != left + 1:
            continue
        sig_l = winning_signature(by_round[left])
        sig_r = winning_signature(by_round[right])
        if sig_l is not None and sig_l == sig_r:
            suspects.update({left, right})
    return suspects


def body_without_controls(page: Page) -> str:
    return page.evaluate("""
        () => {
          const clone = document.body.cloneNode(true);
          clone.querySelectorAll('select, option, script, style, noscript').forEach(e => e.remove());
          return clone.innerText || '';
        }
    """)


def displayed_round(text: str) -> int | None:
    patterns = [
        r"(?:제\s*)?(\d{1,4})\s*회\s*(?:로또6/45\s*)?당첨결과",
        r"당첨결과\s*(?:제\s*)?(\d{1,4})\s*회",
        r"(?:제\s*)?(\d{1,4})\s*회\s*당첨번호",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return int(match.group(1))
    return None


def verify_round_page(page: Page, round_no: int) -> str:
    text = body_without_controls(page)
    actual = displayed_round(text)
    if actual != round_no:
        raise RuntimeError(f"요청 {round_no}회와 화면 표시 회차가 다릅니다: {actual}")
    return text


def candidate_result_urls(round_no: int) -> list[str]:
    return [
        f"{OFFICIAL_RESULT_URL}?result=byWin&lottoId=LO40&drwNo={round_no}",
        f"{OFFICIAL_RESULT_URL}?drwNo={round_no}",
        f"https://www.dhlottery.co.kr/gameResult.do?method=byWin&drwNo={round_no}",
    ]


def choose_round_from_select(page: Page, round_no: int) -> str:
    for select in page.locator("select").all():
        try:
            options = select.locator("option")
            texts = options.all_text_contents()
            values = options.evaluate_all("els => els.map(e => e.value)")
            match_index = next((i for i, text in enumerate(texts) if to_int(text) == round_no), None)
            if match_index is None:
                continue
            select.select_option(values[match_index])
            # 사이트에 따라 change 이벤트만으로 이동하거나 조회 버튼이 필요합니다.
            page.wait_for_timeout(1200)
            try:
                return verify_round_page(page, round_no)
            except Exception:
                pass
            form = select.locator("xpath=ancestor::form[1]")
            if form.count():
                buttons = form.locator("button, input[type=submit], a").all()
                for button in buttons:
                    label = clean_text(button.inner_text() if button.evaluate("e => e.tagName") != "INPUT" else button.get_attribute("value") or "")
                    if re.search(r"조회|검색|확인", label):
                        button.click()
                        page.wait_for_timeout(1800)
                        return verify_round_page(page, round_no)
        except Exception:
            continue
    raise RuntimeError("회차 선택 상자를 통한 조회에 실패했습니다.")


def goto_round(page: Page, round_no: int) -> str:
    errors: list[str] = []
    for url in candidate_result_urls(round_no):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            page.wait_for_timeout(1600)
            return verify_round_page(page, round_no)
        except Exception as exc:
            errors.append(str(exc))
    page.goto(OFFICIAL_RESULT_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_timeout(1400)
    try:
        return choose_round_from_select(page, round_no)
    except Exception as exc:
        errors.append(str(exc))
    raise RuntimeError(" / ".join(errors[-4:]))


def detect_latest_round(page: Page) -> int:
    page.goto(OFFICIAL_RESULT_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_timeout(1600)
    candidates: list[int] = []
    for select in page.locator("select").all():
        try:
            candidates.extend(v for v in (to_int(t) for t in select.locator("option").all_text_contents()) if v)
        except Exception:
            pass
    try:
        actual = displayed_round(body_without_controls(page))
        if actual:
            candidates.append(actual)
    except Exception:
        pass
    if not candidates:
        raise RuntimeError("최신 회차를 찾지 못했습니다.")
    latest = max(candidates)
    print(f"공식 페이지 최신 회차: {latest}")
    return latest


def extract_date(text: str) -> str | None:
    for pattern in [r"(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", r"(20\d{2})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})"]:
        match = re.search(pattern, text)
        if match:
            return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    return None


def extract_balls(page: Page, text: str, round_no: int) -> tuple[list[int], int | None]:
    # 공식 결과 영역의 공(ball) 요소를 먼저 사용합니다.
    selectors = [
        ".lotto645 .ball_645",
        ".win_num .ball_645",
        "[class*='lotto645'] [class*='ball']",
        "[class*='win_num'] [class*='ball']",
    ]
    for selector in selectors:
        try:
            values = [to_int(v) for v in page.locator(selector).all_text_contents()]
            values = [v for v in values if v is not None and 1 <= v <= 45]
            for i in range(0, max(1, len(values) - 6)):
                seq = values[i:i + 7]
                if len(seq) == 7 and valid_numbers(seq[:6], seq[6]):
                    return seq[:6], seq[6]
        except Exception:
            pass
    # select/option이 제거되고 회차가 검증된 결과 본문에서만 찾습니다.
    anchor = re.search(r"당첨번호", text)
    if anchor:
        section = text[anchor.start():anchor.start() + 500]
        values = [int(v) for v in re.findall(r"(?<![\d,])([1-9]|[1-3]\d|4[0-5])(?![\d,])", section)]
        for i in range(0, max(1, len(values) - 6)):
            seq = values[i:i + 7]
            if len(seq) == 7 and valid_numbers(seq[:6], seq[6]):
                return seq[:6], seq[6]
    raise RuntimeError(f"{round_no}회 당첨번호를 결과 영역에서 찾지 못했습니다.")


def parse_prize_table(page: Page, body_text: str) -> dict[str, int | None]:
    result = {"firstAmount": None, "firstCount": None, "secondCount": None, "totalSales": None}
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
            result["firstCount"] = numbers[1]
            result["firstAmount"] = numbers[2]
        elif rank == "2등" and len(numbers) >= 2:
            result["secondCount"] = numbers[1]
    patterns = {
        "firstCount": [r"1등[^\n]{0,160}?([0-9,]+)\s*게임"],
        "secondCount": [r"2등[^\n]{0,160}?([0-9,]+)\s*게임"],
        "firstAmount": [r"1등[^\n]{0,240}?1게임당[^0-9]{0,30}([0-9,]+)\s*원"],
        "totalSales": [r"총\s*판매\s*금액[^0-9]{0,30}([0-9,]+)\s*원?"],
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


def parse_store_rows(page: Page) -> list[dict[str, str]]:
    stores: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in page.locator("table tr").all():
        try:
            cells = [clean_text(v) for v in row.locator("th,td").all_text_contents()]
        except Exception:
            continue
        method_index = next((i for i, value in enumerate(cells) if value in {"자동", "수동", "반자동"}), None)
        if method_index is None:
            continue
        method = cells[method_index]
        usable = [v for v in cells if v and not v.isdigit() and v not in {"자동", "수동", "반자동", "상호명", "소재지", "구분"}]
        if not usable:
            continue
        name = usable[0]
        address = usable[-1] if len(usable) > 1 else ""
        key = (name, method, address)
        if key not in seen:
            seen.add(key)
            stores.append({"name": name, "method": method, "address": address})
    return stores


def fetch_stores(page: Page, round_no: int) -> list[dict[str, str]]:
    # 결과 화면 내 공식 판매점 링크/탭만 사용합니다.
    targets = page.get_by_text(re.compile(r"당첨\s*판매점|1등\s*판매점"))
    for index in range(min(targets.count(), 6)):
        target = targets.nth(index)
        try:
            with page.context.expect_page(timeout=4_000) as popup_info:
                target.click(timeout=5_000)
            popup = popup_info.value
            popup.wait_for_load_state("domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            popup.wait_for_timeout(1000)
            stores = parse_store_rows(popup)
            popup.close()
            if stores:
                return stores
        except Exception:
            try:
                target.click(timeout=5_000)
                page.wait_for_timeout(1200)
                stores = parse_store_rows(page)
                if stores:
                    return stores
                goto_round(page, round_no)
            except Exception:
                pass
    return []


def fetch_official_round(page: Page, round_no: int) -> dict[str, Any]:
    print(f"[{round_no}] 공식 페이지 조회")
    body = goto_round(page, round_no)
    numbers, bonus = extract_balls(page, body, round_no)
    prize = parse_prize_table(page, body)
    return {
        "round": round_no,
        "date": extract_date(body),
        "winning": {"numbers": numbers, "bonus": bonus},
        "firstAmount": prize["firstAmount"],
        "firstCount": prize["firstCount"],
        "secondCount": prize["secondCount"],
        "totalSales": prize["totalSales"],
        "stores": fetch_stores(page, round_no),
    }


def incoming_core_valid(incoming: dict[str, Any]) -> bool:
    winning = incoming.get("winning") or {}
    return valid_numbers(winning.get("numbers"), winning.get("bonus")) and valid_amount(incoming.get("firstAmount")) and valid_count(incoming.get("firstCount")) and valid_count(incoming.get("secondCount"))


def apply_incoming(item: dict[str, Any], incoming: dict[str, Any], *, allow_winning_replace: bool) -> dict[str, Any]:
    result = normalize(item)
    if int(result.get("round", 0)) != int(incoming.get("round", 0)):
        raise ValueError("서로 다른 회차를 병합하려고 했습니다.")
    result["round"] = int(incoming["round"])
    if incoming.get("date"):
        result["date"] = incoming["date"]
    winning = incoming.get("winning") or {}
    if valid_numbers(winning.get("numbers"), winning.get("bonus")):
        current_valid = valid_numbers(result["winning"].get("numbers"), result["winning"].get("bonus"))
        if not current_valid or allow_winning_replace:
            result["winning"] = {"numbers": [int(v) for v in winning["numbers"]], "bonus": int(winning["bonus"])}
            result["dataSource"]["winning"] = SOURCE
    if valid_amount(incoming.get("firstAmount")):
        result["prize"]["first"]["perGameAmount"] = to_int(incoming["firstAmount"])
        result["dataSource"]["firstPrize"] = SOURCE
    if valid_count(incoming.get("firstCount")):
        result["prize"]["first"]["winnerCount"] = to_int(incoming["firstCount"])
        result["dataSource"]["firstWinnerCount"] = SOURCE
    if valid_count(incoming.get("secondCount")):
        result["prize"]["second"]["winnerCount"] = to_int(incoming["secondCount"])
        result["dataSource"]["secondWinnerCount"] = SOURCE
    if valid_sales(incoming.get("totalSales")):
        result["prize"]["totalSalesAmount"] = to_int(incoming["totalSales"])
        result["dataSource"]["totalSales"] = SOURCE
    if incoming.get("stores"):
        result["stores"] = copy.deepcopy(incoming["stores"])
        result["dataSource"]["stores"] = SOURCE
    return result


def validate_dataset(by_round: dict[int, dict[str, Any]], protected_before: dict[int, tuple[int, ...] | None], mutable_winning_rounds: set[int], official_latest: int) -> None:
    if not by_round:
        raise ValueError("결과 데이터가 비었습니다.")
    for round_no, item in by_round.items():
        if int(item.get("round", 0)) != round_no:
            raise ValueError(f"회차 키 불일치: {round_no}")
        sig = winning_signature(item)
        if round_no <= official_latest and sig is None:
            raise ValueError(f"{round_no}회 당첨번호가 유효하지 않습니다.")
        if round_no in protected_before and round_no not in mutable_winning_rounds and sig != protected_before[round_no]:
            raise ValueError(f"보호된 {round_no}회 당첨번호가 변경됐습니다.")
    suspects = consecutive_duplicate_suspects(by_round)
    recent_floor = max(1, official_latest - RECENT_VERIFY_COUNT + 1)
    recent_suspects = sorted(r for r in suspects if r >= recent_floor)
    if recent_suspects:
        raise ValueError(f"최근 회차에 연속 동일 당첨번호가 남아 있습니다: {recent_suspects}")


def save_data(data: dict[str, Any]) -> bool:
    data["results"].sort(key=lambda x: int(x["round"]))
    data["latestRound"] = max(int(x["round"]) for x in data["results"])
    data["updatedAt"] = datetime.now(KST).isoformat(timespec="seconds")
    new_text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    old_text = DATA_PATH.read_text(encoding="utf-8") if DATA_PATH.exists() else ""
    if new_text == old_text:
        print("저장할 변경사항이 없습니다.")
        return False
    if old_text:
        BACKUP_PATH.write_text(old_text, encoding="utf-8")
    tmp = DATA_PATH.with_suffix(".json.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    json.loads(tmp.read_text(encoding="utf-8"))
    tmp.replace(DATA_PATH)
    return True


def main() -> int:
    data = load_data()
    by_round = {int(x["round"]): normalize(x) for x in data["results"]}
    protected_before = {r: winning_signature(item) for r, item in by_round.items()}
    initial_suspects = consecutive_duplicate_suspects(by_round)
    errors: list[str] = []
    repaired: list[int] = []
    added: list[int] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context: BrowserContext = browser.new_context(locale="ko-KR", timezone_id="Asia/Seoul", viewport={"width": 1440, "height": 1800})
        page = context.new_page()
        official_latest = detect_latest_round(page)

        targets: list[int] = []
        # 새 회차는 빈 객체를 먼저 만들지 않고, 공식 핵심정보가 검증된 뒤에만 추가합니다.
        for round_no in range(max(by_round, default=0) + 1, official_latest + 1):
            targets.append(round_no)
        # 최근 회차와 연속 동일번호 의심 회차를 공식 페이지에서 재검증합니다.
        targets.extend(range(official_latest, max(0, official_latest - RECENT_VERIFY_COUNT), -1))
        targets.extend(sorted(initial_suspects, reverse=True))
        # 미완성 과거 회차는 소량 보완합니다.
        incomplete = [r for r in sorted(by_round, reverse=True) if r < official_latest - RECENT_VERIFY_COUNT and not core_complete(by_round[r])]
        targets.extend(incomplete[:OLD_BACKFILL_BATCH])
        targets = list(dict.fromkeys(r for r in targets if 1 <= r <= official_latest))

        mutable_winning_rounds = set(initial_suspects) | {r for r in targets if r not in by_round} | {r for r in targets if not valid_numbers(by_round.get(r, {}).get("winning", {}).get("numbers"), by_round.get(r, {}).get("winning", {}).get("bonus"))}

        for round_no in targets:
            try:
                incoming = fetch_official_round(page, round_no)
                if not incoming_core_valid(incoming):
                    raise RuntimeError("공식 핵심정보 검증 실패")
                if round_no not in by_round:
                    by_round[round_no] = build_item(incoming)
                    added.append(round_no)
                else:
                    old_sig = winning_signature(by_round[round_no])
                    by_round[round_no] = apply_incoming(by_round[round_no], incoming, allow_winning_replace=round_no in mutable_winning_rounds)
                    if round_no in initial_suspects and winning_signature(by_round[round_no]) != old_sig:
                        repaired.append(round_no)
                print(f"[{round_no}] 번호={by_round[round_no]['winning']['numbers']} 보너스={by_round[round_no]['winning']['bonus']}")
            except Exception as exc:
                message = f"[{round_no}] 수집 실패: {exc}"
                print(message)
                errors.append(message)

        context.close()
        browser.close()

    validate_dataset(by_round, protected_before, mutable_winning_rounds, official_latest)
    data["results"] = list(by_round.values())
    data["service"].update({
        "collectorVersion": COLLECTOR_VERSION,
        "sourcePolicy": "official-dhlottery-only",
        "thirdPartySourceUsed": False,
        "immutableRoundMerge": True,
        "lastRunAt": datetime.now(KST).isoformat(timespec="seconds"),
        "lastErrors": errors[-30:],
        "addedRounds": added,
        "repairedDuplicateRounds": repaired,
        "coreCompleteCount": sum(1 for x in by_round.values() if core_complete(x)),
        "coreIncompleteCount": sum(1 for x in by_round.values() if not core_complete(x)),
    })
    saved = save_data(data)
    print(f"완료: 공식 최신 {official_latest}회 / 신규 {added} / 중복복구 {repaired} / 오류 {len(errors)} / 저장={'예' if saved else '아니오'}")
    if official_latest not in by_round or not core_complete(by_round[official_latest]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
