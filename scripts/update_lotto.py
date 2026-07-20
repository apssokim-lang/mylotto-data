from __future__ import annotations

import copy
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "lotto_results.json"
BACKUP_PATH = ROOT / "data" / "lotto_results_backup.json"
DEBUG_DIR = ROOT / "artifacts" / "store_debug"
KST = timezone(timedelta(hours=9))

OFFICIAL_RESULTS_API = "https://www.dhlottery.co.kr/lt645/selectPstLt645Info.do"
OFFICIAL_RESULTS_PAGE = "https://www.dhlottery.co.kr/lt645/result"
OFFICIAL_STORES_PAGE = "https://www.dhlottery.co.kr/wnprchsplcsrch/home"
COLLECTOR_VERSION = "8.0.3-official-store-parser-safety-fix"
RESULT_SOURCE = "dhlottery-official-internal-json"
STORE_SOURCE = "dhlottery-official-winning-store-page"
REQUEST_TIMEOUT = 25
RECENT_RECONCILE_COUNT = 60
STORE_RETRY_ROUNDS = 4


def now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    digits = re.sub(r"[^0-9]", "", str(value))
    return int(digits) if digits else None


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()



GENERIC_STORE_NAMES = {
    "1등", "2등", "3등", "판매점", "상호명", "조회된 내역이 없습니다.",
    "자동", "수동", "반자동", "전국", "지도", "로또6/45", "로또 6/45",
}
GENERIC_STORE_ADDRESSES = {
    "전국", "지도", "로또6/45", "로또 6/45", "주소", "소재지",
}

def is_real_store_name(value: Any) -> bool:
    text = clean_text(value)
    if not text or text in GENERIC_STORE_NAMES:
        return False
    if re.fullmatch(r"[123]등", text):
        return False
    if re.fullmatch(r"로또\s*6/?45", text, re.I):
        return False
    return len(text) >= 2

def is_real_store_address(value: Any) -> bool:
    text = clean_text(value)
    if not text or text in GENERIC_STORE_ADDRESSES:
        return False
    if re.fullmatch(r"로또\s*6/?45", text, re.I):
        return False
    # 실제 국내 주소는 최소한 행정구역/도로명 단서와 숫자 또는 상세 지명을 포함합니다.
    has_region = bool(re.search(r"(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주|특별시|광역시|특별자치시|특별자치도|\S+[시도군구읍면동]|\S+(?:로|길))", text))
    return has_region and len(text) >= 6

def sanitize_stores(stores: Any) -> list[dict[str, str]]:
    if not isinstance(stores, list):
        return []
    cleaned: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in stores:
        if not isinstance(raw, dict):
            continue
        name = clean_text(raw.get("name"))
        address = clean_text(raw.get("address"))
        method = clean_text(raw.get("method"))
        if not is_real_store_name(name) or not is_real_store_address(address):
            continue
        if method not in {"자동", "수동", "반자동"}:
            method = ""
        key = (name, address, method)
        if key not in seen:
            seen.add(key)
            cleaned.append({"name": name, "method": method, "address": address})
    return cleaned

def valid_numbers(numbers: Any, bonus: Any) -> bool:
    if not isinstance(numbers, list) or len(numbers) != 6:
        return False
    try:
        nums = [int(v) for v in numbers]
        bns = int(bonus)
    except (TypeError, ValueError):
        return False
    return len(set(nums)) == 6 and all(1 <= v <= 45 for v in nums) and 1 <= bns <= 45 and bns not in nums


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": OFFICIAL_RESULTS_PAGE,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    return session


def fetch_official_rows(session: requests.Session, round_value: str | int = "all") -> list[dict[str, Any]]:
    params = {"srchLtEpsd": str(round_value), "_": str(int(time.time() * 1000))}
    response = session.get(OFFICIAL_RESULTS_API, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    content_type = (response.headers.get("content-type") or "").lower()
    if "json" not in content_type and not response.text.lstrip().startswith("{"):
        raise RuntimeError(f"공식 당첨결과 API가 JSON이 아닌 응답을 반환했습니다: {content_type or 'unknown'}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("공식 당첨결과 API JSON 해석에 실패했습니다.") from exc
    rows = payload.get("data", {}).get("list", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        raise RuntimeError("공식 당첨결과 API의 data.list 형식이 올바르지 않습니다.")
    return [row for row in rows if isinstance(row, dict)]


def parse_date(raw: Any) -> str | None:
    digits = re.sub(r"[^0-9]", "", str(raw or ""))
    if len(digits) != 8:
        return None
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def official_row_to_item(row: dict[str, Any]) -> dict[str, Any]:
    round_no = to_int(row.get("ltEpsd"))
    numbers = [to_int(row.get(f"tm{i}WnNo")) for i in range(1, 7)]
    bonus = to_int(row.get("bnsWnNo"))
    if round_no is None or any(v is None for v in numbers) or not valid_numbers(numbers, bonus):
        raise ValueError(f"공식 응답의 회차/당첨번호가 올바르지 않습니다: {round_no}")
    return {
        "round": round_no,
        "date": parse_date(row.get("ltRflYmd")),
        "winning": {"numbers": [int(v) for v in numbers], "bonus": bonus},
        "prize": {
            "first": {"perGameAmount": to_int(row.get("rnk1WnAmt")), "winnerCount": to_int(row.get("rnk1WnNope"))},
            "second": {"perGameAmount": to_int(row.get("rnk2WnAmt")), "winnerCount": to_int(row.get("rnk2WnNope"))},
            "third": {"perGameAmount": to_int(row.get("rnk3WnAmt")), "winnerCount": to_int(row.get("rnk3WnNope"))},
            "totalSalesAmount": to_int(row.get("wholEpsdSumNtslAmt")) or to_int(row.get("rlvtEpsdSumNtslAmt")),
        },
        "dataSource": {"winning": RESULT_SOURCE, "prize": RESULT_SOURCE, "verifiedAt": now_iso()},
    }


def normalize_existing(raw: dict[str, Any]) -> dict[str, Any]:
    item = copy.deepcopy(raw)
    if not isinstance(item.get("winning"), dict):
        item["winning"] = {"numbers": item.pop("numbers", []), "bonus": item.pop("bonus", None)}
    stores = item.get("stores", item.pop("firstPrizeStores", []) if "firstPrizeStores" in item else [])
    item["stores"] = sanitize_stores(stores)
    item.setdefault("prize", {})
    item.setdefault("dataSource", {})
    return item


def merge_official(existing: dict[str, Any] | None, official: dict[str, Any]) -> dict[str, Any]:
    if existing is None:
        result = normalize_existing({"round": official["round"], "stores": []})
    else:
        result = normalize_existing(existing)
    if int(result.get("round", official["round"])) != int(official["round"]):
        raise ValueError("서로 다른 회차를 병합하려고 했습니다.")
    result["round"] = int(official["round"])
    result["date"] = official.get("date") or result.get("date")
    result["winning"] = copy.deepcopy(official["winning"])
    result["prize"] = copy.deepcopy(official["prize"])
    result.setdefault("dataSource", {}).update(copy.deepcopy(official.get("dataSource", {})))
    return result


NAME_KEYS = ("storeName", "shopName", "stNm", "bsshNm", "prchSplcNm", "storeNm", "name", "상호명")
ADDRESS_KEYS = ("address", "addr", "roadAddress", "rdnmAdr", "bsshLctn", "prchSplcAdr", "storeAddr", "소재지", "주소")
METHOD_KEYS = ("method", "winType", "ltWnTyNm", "wnTyNm", "gameType", "구분")
RANK_KEYS = ("rank", "rnk", "winRank", "등위", "등수")


def first_value(record: dict[str, Any], keys: Iterable[str]) -> Any:
    lowered = {str(k).lower(): v for k, v in record.items()}
    for key in keys:
        if key in record:
            return record[key]
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


def normalize_method(value: Any) -> str:
    text = clean_text(value)
    if "반자동" in text or text in {"3", "SEMI", "S"}:
        return "반자동"
    if "수동" in text or text in {"2", "MANUAL", "M"}:
        return "수동"
    if "자동" in text or text in {"1", "AUTO", "A"}:
        return "자동"
    return text


def normalize_store_record(record: dict[str, Any]) -> dict[str, str] | None:
    name = clean_text(first_value(record, NAME_KEYS))
    address = clean_text(first_value(record, ADDRESS_KEYS))
    method = normalize_method(first_value(record, METHOD_KEYS))
    rank = clean_text(first_value(record, RANK_KEYS))
    if rank and "1" not in rank and "일" not in rank:
        return None
    if not is_real_store_name(name) or not is_real_store_address(address):
        return None
    if method not in {"자동", "수동", "반자동"}:
        method = ""
    return {"name": name, "method": method, "address": address}


def walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def stores_from_json_payload(payload: Any) -> list[dict[str, str]]:
    stores: list[dict[str, str]] = []
    for record in walk_json(payload):
        parsed = normalize_store_record(record)
        if parsed:
            stores.append(parsed)
    return dedupe_stores(stores)


def dedupe_stores(stores: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in stores:
        name = clean_text(raw.get("name"))
        address = clean_text(raw.get("address"))
        method = normalize_method(raw.get("method"))
        if not is_real_store_name(name) or not is_real_store_address(address):
            continue
        key = (name, address, method)
        if key not in seen:
            seen.add(key)
            result.append({"name": name, "method": method, "address": address})
    return result


def stores_from_html(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    stores: list[dict[str, str]] = []
    # 표 구조
    for row in soup.select("tr"):
        cells = [clean_text(cell.get_text(" ", strip=True)) for cell in row.select("th,td")]
        if len(cells) < 3:
            continue
        method_index = next((i for i, text in enumerate(cells) if normalize_method(text) in {"자동", "수동", "반자동"}), None)
        if method_index is None:
            continue
        method = normalize_method(cells[method_index])
        # 순번/등위/구분을 제외하고 가장 긴 주소성 문자열과 상호명을 선택
        address = next((x for x in reversed(cells) if re.search(r"(시|도|군|구|읍|면|동|로|길)\b|\d+-\d+", x) and len(x) >= 6), "")
        candidates = [x for x in cells if x and x != method and x != address and not re.fullmatch(r"\d+", x) and "등" not in x]
        name = candidates[0] if candidates else ""
        parsed = normalize_store_record({"name": name, "method": method, "address": address, "rank": "1등"})
        if parsed:
            stores.append(parsed)
    # 카드 구조: 자동/수동/반자동이 있는 가까운 컨테이너
    for text_node in soup.find_all(string=re.compile(r"^(자동|수동|반자동)$")):
        container = text_node.parent
        for _ in range(5):
            if container is None:
                break
            text = clean_text(container.get_text(" ", strip=True))
            if len(text) >= 15 and re.search(r"(시|도|군|구|읍|면|동|로|길)", text):
                parts = [clean_text(x) for x in container.stripped_strings]
                method = normalize_method(text_node)
                address = next((x for x in reversed(parts) if len(x) >= 6 and re.search(r"(시|도|군|구|읍|면|동|로|길)", x)), "")
                name = next((x for x in parts if x not in {method, "1등", "2등", "3등", "지도", "전국", "로또6/45", "로또 6/45"} and x != address and is_real_store_name(x)), "")
                parsed = normalize_store_record({"name": name, "method": method, "address": address, "rank": "1등"})
                if parsed:
                    stores.append(parsed)
                break
            container = container.parent
    return dedupe_stores(stores)


def _select_option_semantic(page: Any, patterns: list[re.Pattern[str]], desired: re.Pattern[str]) -> bool:
    selects = page.locator("select")
    for i in range(selects.count()):
        select = selects.nth(i)
        texts = [clean_text(x) for x in select.locator("option").all_inner_texts()]
        joined = " | ".join(texts)
        if not any(p.search(joined) for p in patterns):
            continue
        options = select.locator("option")
        for j in range(options.count()):
            option = options.nth(j)
            text = clean_text(option.inner_text())
            if desired.search(text):
                value = option.get_attribute("value")
                if value is not None:
                    select.select_option(value=value)
                else:
                    select.select_option(label=text)
                return True
    return False


def _click_text_option(page: Any, trigger_patterns: list[str], desired_pattern: re.Pattern[str]) -> bool:
    for trigger in trigger_patterns:
        loc = page.get_by_text(re.compile(trigger), exact=False)
        if loc.count() == 0:
            continue
        try:
            loc.first.click(timeout=2500)
            page.wait_for_timeout(300)
            option = page.get_by_text(desired_pattern, exact=False)
            if option.count():
                option.last.click(timeout=2500)
                return True
        except Exception:
            continue
    return False


def fetch_official_stores(round_no: int, expected_winners: int | None) -> tuple[list[dict[str, str]], str]:
    """공식 당첨 판매점 화면을 브라우저로 조작하고 XHR/DOM 양쪽에서 결과를 읽습니다.

    실패는 당첨번호 업데이트를 막지 않습니다. 다음 예약 실행에서 다시 시도합니다.
    """
    from playwright.sync_api import sync_playwright

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    captured: list[dict[str, str]] = []
    response_notes: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--no-sandbox"])
        context = browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            viewport={"width": 1440, "height": 1200},
        )
        page = context.new_page()

        def on_response(response: Any) -> None:
            try:
                content_type = (response.headers.get("content-type") or "").lower()
                if "json" not in content_type:
                    return
                payload = response.json()
                found = stores_from_json_payload(payload)
                if found:
                    captured.extend(found)
                    response_notes.append(response.url)
            except Exception:
                return

        page.on("response", on_response)
        page.goto(OFFICIAL_STORES_PAGE, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        # 기본 HTML에 대기/차단 문구가 있으면 한 번 새로고침합니다.
        body = clean_text(page.locator("body").inner_text())
        if "서비스 접근 대기" in body or "접속이 차단" in body:
            page.wait_for_timeout(4000)
            page.reload(wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)

        round_pattern = re.compile(rf"(^|\D){round_no}\s*회?(\D|$)")
        # 실제 select가 있으면 우선 사용합니다.
        _select_option_semantic(page, [re.compile(r"\d{3,4}\s*회")], round_pattern)
        _select_option_semantic(page, [re.compile(r"전체.*1등.*2등|1등.*2등")], re.compile(r"^\s*1등\s*$"))
        _select_option_semantic(page, [re.compile(r"로또\s*6/?45")], re.compile(r"로또\s*6/?45"))

        # 커스텀 셀렉트 보완
        _click_text_option(page, [r"회차", r"선택"], round_pattern)
        _click_text_option(page, [r"전체", r"등위", r"등수"], re.compile(r"^\s*1등\s*$"))

        # 검색/조회 버튼 중 화면 아래쪽의 마지막 유효 버튼을 누릅니다.
        clicked = False
        for pattern in [re.compile(r"^검색$"), re.compile(r"^조회$")]:
            buttons = page.get_by_role("button", name=pattern)
            for i in reversed(range(buttons.count())):
                try:
                    buttons.nth(i).click(timeout=3000)
                    clicked = True
                    break
                except Exception:
                    continue
            if clicked:
                break
        if not clicked:
            candidates = page.locator("button, input[type=submit], a")
            for i in range(candidates.count()):
                candidate = candidates.nth(i)
                if clean_text(candidate.inner_text() if candidate.evaluate("el => el.innerText || el.value || ''") else "") in {"검색", "조회"}:
                    try:
                        candidate.click(timeout=2500)
                        clicked = True
                        break
                    except Exception:
                        pass

        page.wait_for_timeout(4500)
        html = page.content()
        dom_stores = stores_from_html(html)
        stores = dedupe_stores([*captured, *dom_stores])

        # 공식 결과에서 인터넷 판매사이트는 앱의 '오프라인 판매점' 목록에서 제외합니다.
        stores = [s for s in stores if "동행복권" not in s["address"] and "인터넷" not in s["name"]]
        # 당첨 게임 수보다 판매점 행이 많을 수는 없습니다. 과도한 결과는 다른 표를 잘못 읽은 것입니다.
        if expected_winners and len(stores) > expected_winners:
            bad_count = len(stores)
            stores = []
            status = f"invalid-too-many:{bad_count}/{expected_winners}"
        else:
            status = "ok" if stores else "pending"

        if not stores:
            (DEBUG_DIR / f"stores_{round_no}.html").write_text(html, encoding="utf-8")
            page.screenshot(path=str(DEBUG_DIR / f"stores_{round_no}.png"), full_page=True)
            (DEBUG_DIR / f"stores_{round_no}_responses.txt").write_text("\n".join(response_notes), encoding="utf-8")
        context.close()
        browser.close()
    return stores, status


def load_dataset() -> dict[str, Any]:
    if not DATA_PATH.exists():
        raise FileNotFoundError(f"데이터 파일이 없습니다: {DATA_PATH}")
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("lotto_results.json 형식이 올바르지 않습니다.")
    return data


def validate_dataset(data: dict[str, Any]) -> None:
    seen: set[int] = set()
    for raw in data.get("results", []):
        if not isinstance(raw, dict):
            raise ValueError("results 안에 객체가 아닌 값이 있습니다.")
        round_no = to_int(raw.get("round"))
        if round_no is None or round_no < 1 or round_no in seen:
            raise ValueError(f"잘못되거나 중복된 회차: {raw.get('round')}")
        seen.add(round_no)
        item = normalize_existing(raw)
        if not valid_numbers(item["winning"].get("numbers"), item["winning"].get("bonus")):
            raise ValueError(f"{round_no}회 당첨번호가 올바르지 않습니다.")
        for store in item["stores"]:
            if not clean_text(store.get("name")) or not clean_text(store.get("address")):
                raise ValueError(f"{round_no}회에 이름/주소가 빈 판매점 객체가 있습니다.")
    latest = max(seen) if seen else 0
    if to_int(data.get("latestRound")) != latest:
        raise ValueError(f"latestRound 불일치: {data.get('latestRound')} != {latest}")


def update_dataset(data: dict[str, Any], official_items: list[dict[str, Any]]) -> tuple[dict[str, Any], list[int]]:
    result = copy.deepcopy(data)
    by_round: dict[int, dict[str, Any]] = {}
    for raw in result["results"]:
        item = normalize_existing(raw)
        round_no = to_int(item.get("round"))
        if round_no is None or round_no in by_round:
            raise ValueError(f"기존 데이터 회차가 잘못되었습니다: {item.get('round')}")
        item["round"] = round_no
        by_round[round_no] = item

    if not official_items:
        raise ValueError("공식 API에서 유효한 회차를 받지 못했습니다.")
    official_items = sorted(official_items, key=lambda x: int(x["round"]), reverse=True)
    latest_official = int(official_items[0]["round"])
    recent_items = [x for x in official_items if int(x["round"]) >= latest_official - RECENT_RECONCILE_COUNT + 1]

    changed: list[int] = []
    for official in recent_items:
        round_no = int(official["round"])
        before = by_round.get(round_no)
        after = merge_official(before, official)
        if before != after:
            by_round[round_no] = after
            changed.append(round_no)

    # 최신 회차부터, 판매점이 비어 있는 최근 회차만 공식 페이지에서 재시도합니다.
    store_targets: list[int] = []
    for round_no in range(latest_official, max(0, latest_official - STORE_RETRY_ROUNDS), -1):
        item = by_round.get(round_no)
        if item and not item.get("stores") and to_int(item.get("prize", {}).get("first", {}).get("winnerCount")):
            store_targets.append(round_no)

    store_status: dict[str, str] = {}
    for round_no in store_targets:
        item = by_round[round_no]
        winner_count = to_int(item.get("prize", {}).get("first", {}).get("winnerCount"))
        try:
            stores, status = fetch_official_stores(round_no, winner_count)
            store_status[str(round_no)] = status
            if stores:
                before_stores = item.get("stores", [])
                item["stores"] = stores
                item.setdefault("dataSource", {})["stores"] = STORE_SOURCE
                item["dataSource"]["storesVerifiedAt"] = now_iso()
                if before_stores != stores:
                    changed.append(round_no)
                print(f"[{round_no}] 공식 1등 오프라인 판매점 {len(stores)}곳 수집")
            else:
                item.setdefault("dataSource", {})["storesStatus"] = "pending-official-page"
                print(f"[{round_no}] 판매점 공식 화면 반영 대기 - 기존 데이터 유지")
        except Exception as exc:
            store_status[str(round_no)] = f"retry:{type(exc).__name__}"
            item.setdefault("dataSource", {})["storesStatus"] = "retry-next-schedule"
            print(f"[{round_no}] 판매점 수집 일시 실패 - 다음 예약 실행에서 재시도: {exc}")

    result["results"] = [by_round[r] for r in sorted(by_round, reverse=True)]
    result["latestRound"] = max(by_round)
    result["schemaVersion"] = max(2, to_int(result.get("schemaVersion")) or 2)
    result.setdefault("service", {}).update({
        "collectorVersion": COLLECTOR_VERSION,
        "sourcePolicy": "official-dhlottery-only",
        "thirdPartySourceUsed": False,
        "officialResultsApi": OFFICIAL_RESULTS_API,
        "officialWinningStoresPage": OFFICIAL_STORES_PAGE,
        "lastCheckedAt": now_iso(),
        "latestOfficialRound": latest_official,
        "recentReconcileCount": RECENT_RECONCILE_COUNT,
        "storeRetryRounds": STORE_RETRY_ROUNDS,
        "storeStatus": store_status,
        "changedRounds": sorted(set(changed), reverse=True),
    })
    return result, sorted(set(changed), reverse=True)


def main() -> int:
    data = load_dataset()
    validate_dataset(data)
    rows = fetch_official_rows(make_session(), "all")
    official_items: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    for row in rows:
        try:
            official_items.append(official_row_to_item(row))
        except Exception as exc:
            parse_errors.append(str(exc))
    if not official_items:
        raise RuntimeError("공식 API 응답에서 유효한 회차를 하나도 읽지 못했습니다.")

    updated, changed = update_dataset(data, official_items)
    validate_dataset(updated)
    BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    BACKUP_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    DATA_PATH.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    latest = updated["latestRound"]
    print(f"완료: 공식 최신 {latest}회 / 변경 회차 {changed if changed else '없음'}")
    if parse_errors:
        print(f"참고: 무시된 비정상 공식 API 행 {len(parse_errors)}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
