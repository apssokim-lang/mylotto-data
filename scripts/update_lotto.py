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
OFFICIAL_STORES_API = "https://www.dhlottery.co.kr/wnprchsplcsrch/selectLtWnShp.do"
COLLECTOR_VERSION = "8.4.0-official-store-json-api-direct"
STORE_PARSER_VERSION = "8.4.0-direct-selectLtWnShp-json"
RESULT_SOURCE = "dhlottery-official-internal-json"
STORE_SOURCE = "dhlottery-official-winning-store-json-api"
REQUEST_TIMEOUT = 25
RECENT_RECONCILE_COUNT = 60
STORE_RETRY_ROUNDS = 4
STORE_BACKFILL_BATCH = max(1, int(os.getenv("STORE_BACKFILL_BATCH", "1")))
STORE_BACKFILL_MIN_ROUND = max(1, int(os.getenv("STORE_BACKFILL_MIN_ROUND", "1")))


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
    "서울", "서울특별시", "부산", "부산광역시", "대구", "대구광역시",
    "인천", "인천광역시", "광주", "광주광역시", "대전", "대전광역시",
    "울산", "울산광역시", "세종", "세종특별자치시", "경기", "경기도",
    "강원", "강원특별자치도", "충북", "충청북도", "충남", "충청남도",
    "전북", "전북특별자치도", "전남", "전라남도", "경북", "경상북도",
    "경남", "경상남도", "제주", "제주특별자치도",
}
GENERIC_STORE_ADDRESSES = {
    "전국", "지도", "로또6/45", "로또 6/45", "주소", "소재지",
}

def is_real_store_name(value: Any) -> bool:
    text = clean_text(value)
    if not text or text in GENERIC_STORE_NAMES:
        return False
    if re.fullmatch(r"[0-9,.-]+", text):
        return False
    if re.fullmatch(r"[123]등", text):
        return False
    if re.fullmatch(r"로또\s*6/?45", text, re.I):
        return False
    # 주소를 상호명으로 오인한 값도 배제합니다.
    if re.match(r"^(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주)(특별시|광역시|특별자치시|특별자치도|도)?\s", text):
        return False
    return len(text) >= 2


def is_real_store_address(value: Any) -> bool:
    text = clean_text(value)
    if not text or text in GENERIC_STORE_ADDRESSES:
        return False
    if re.fullmatch(r"로또\s*6/?45", text, re.I):
        return False
    # 공식 판매점 주소는 광역 시·도명으로 시작하는 국내 주소만 허용합니다.
    region = r"(?:서울(?:특별시)?|부산(?:광역시)?|대구(?:광역시)?|인천(?:광역시)?|광주(?:광역시)?|대전(?:광역시)?|울산(?:광역시)?|세종(?:특별자치시)?|경기(?:도)?|강원(?:특별자치도|도)?|충북|충청북도|충남|충청남도|전북|전북특별자치도|전라북도|전남|전라남도|경북|경상북도|경남|경상남도|제주|제주특별자치도)"
    if not re.match(rf"^{region}(?:\s|$)", text):
        return False
    has_detail = bool(re.search(r"(?:시|군|구|읍|면|동|로|길)\s*[^ ]*|\d", text))
    return has_detail and len(text) >= 8


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


NAME_KEYS = ("storeName", "shopName", "stNm", "bsshNm", "prchSplcNm", "storeNm", "상호명")
ADDRESS_KEYS = ("roadAddress", "rdnmAdr", "bsshLctn", "prchSplcAdr", "storeAddr", "소재지", "주소")
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
    """공식 결과 표만 엄격하게 읽습니다. 일반 카드/필터 UI는 절대 파싱하지 않습니다."""
    soup = BeautifulSoup(html, "html.parser")
    stores: list[dict[str, str]] = []
    for table in soup.select("table"):
        rows = table.select("tr")
        if not rows:
            continue
        header_cells = [clean_text(x.get_text(" ", strip=True)) for x in rows[0].select("th,td")]
        header_joined = " | ".join(header_cells)
        if not re.search(r"상호|판매점|복권방", header_joined) or not re.search(r"소재지|주소", header_joined):
            continue
        name_idx = next((i for i, x in enumerate(header_cells) if re.search(r"상호|판매점", x)), None)
        method_idx = next((i for i, x in enumerate(header_cells) if re.search(r"구분|구매|자동|수동", x)), None)
        addr_idx = next((i for i, x in enumerate(header_cells) if re.search(r"소재지|주소", x)), None)
        rank_idx = next((i for i, x in enumerate(header_cells) if re.search(r"등위|등수|순위", x)), None)
        if name_idx is None or addr_idx is None:
            continue
        for row in rows[1:]:
            cells = [clean_text(x.get_text(" ", strip=True)) for x in row.select("th,td")]
            if max(name_idx, addr_idx) >= len(cells):
                continue
            record = {
                "prchSplcNm": cells[name_idx],
                "prchSplcAdr": cells[addr_idx],
                "ltWnTyNm": cells[method_idx] if method_idx is not None and method_idx < len(cells) else "",
                "rnk": cells[rank_idx] if rank_idx is not None and rank_idx < len(cells) else "1",
            }
            parsed = normalize_store_record(record)
            if parsed:
                stores.append(parsed)
    return dedupe_stores(stores)


def _option_snapshot(page: Any) -> list[dict[str, Any]]:
    return page.locator("select").evaluate_all(
        """els => els.map((el, index) => ({
            index, name: el.name || '', id: el.id || '',
            options: Array.from(el.options).map(o => ({text: (o.textContent || '').trim(), value: o.value, selected: o.selected}))
        }))"""
    )


def _select_exact_option(page: Any, matcher: re.Pattern[str]) -> tuple[bool, str]:
    for info in _option_snapshot(page):
        matches = [o for o in info["options"] if matcher.search(clean_text(o.get("text")))]
        if len(matches) != 1:
            continue
        option = matches[0]
        select = page.locator("select").nth(info["index"])
        select.select_option(value=str(option.get("value", "")))
        selected = clean_text(select.locator("option:checked").inner_text())
        return bool(matcher.search(selected)), selected
    return False, ""


def _request_has_round(request: Any, round_no: int) -> bool:
    haystack = " ".join([str(getattr(request, "url", "") or ""), str(getattr(request, "post_data", "") or "")])
    return bool(re.search(rf"(?<!\d){round_no}(?!\d)", haystack))


def _payload_has_round(value: Any, round_no: int) -> bool:
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except Exception:
        return False
    return bool(re.search(rf"(?<!\d){round_no}(?!\d)", text))


def _response_to_stores_and_text(response: Any) -> tuple[list[dict[str, str]], str]:
    try:
        content_type = (response.headers.get("content-type") or "").lower()
    except Exception:
        content_type = ""
    try:
        if "json" in content_type:
            payload = response.json()
            return stores_from_json_payload(payload), json.dumps(payload, ensure_ascii=False)
        text = response.text()
        if text.lstrip().startswith(("{", "[")):
            try:
                payload = json.loads(text)
                return stores_from_json_payload(payload), text
            except Exception:
                pass
        if "<table" in text.lower() or "상호명" in text or "소재지" in text:
            return stores_from_html(text), text
        return [], text
    except Exception:
        return [], ""


def _click_first_visible(page: Any, selectors: Iterable[str]) -> bool:
    for selector in selectors:
        loc = page.locator(selector)
        try:
            count = loc.count()
        except Exception:
            continue
        for i in range(count):
            item = loc.nth(i)
            try:
                if item.is_visible():
                    item.click(timeout=4000)
                    return True
            except Exception:
                continue
    return False


def _select_round_control(page: Any, round_no: int) -> tuple[bool, str]:
    # 1) 실제 <select>가 있는 경우
    ok, selected = _select_exact_option(page, re.compile(rf"^\s*{round_no}\s*회?\s*$"))
    if ok:
        return True, selected

    # 2) 커스텀 드롭다운: '선택' 버튼/combobox를 하나씩 열어 원하는 회차를 찾는다.
    triggers = page.locator('[role="combobox"], button:has-text("선택"), [aria-haspopup="listbox"]')
    try:
        trigger_count = triggers.count()
    except Exception:
        trigger_count = 0
    target_patterns = [
        re.compile(rf"^\s*{round_no}\s*회\s*$"),
        re.compile(rf"^\s*{round_no}\s*$"),
    ]
    for i in range(trigger_count):
        trigger = triggers.nth(i)
        try:
            if not trigger.is_visible():
                continue
            trigger.click(timeout=3000)
            page.wait_for_timeout(300)
            for pattern in target_patterns:
                candidate = page.get_by_text(pattern, exact=False)
                for j in range(candidate.count()):
                    item = candidate.nth(j)
                    try:
                        text = clean_text(item.inner_text())
                        if item.is_visible() and pattern.search(text):
                            item.click(timeout=3000)
                            return True, text
                    except Exception:
                        continue
            page.keyboard.press("Escape")
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
    return False, ""


def _choose_first_rank(page: Any) -> bool:
    # radio/label/button/tab 등 실제 UI 구조를 모두 지원한다.
    selectors = [
        'label:has-text("1등")',
        'button:has-text("1등")',
        '[role="tab"]:has-text("1등")',
        '[role="radio"]:has-text("1등")',
        'input[type="radio"][value="1"]',
        'input[type="radio"][value="01"]',
    ]
    if _click_first_visible(page, selectors):
        return True
    # 텍스트 노드 직접 클릭 fallback
    text = page.get_by_text(re.compile(r"^\s*1등\s*$"), exact=False)
    for i in range(text.count()):
        try:
            if text.nth(i).is_visible():
                text.nth(i).click(timeout=3000)
                return True
        except Exception:
            continue
    return False


def _choose_lotto645(page: Any) -> bool:
    selectors = [
        'label:has-text("로또6/45")',
        'button:has-text("로또6/45")',
        '[role="tab"]:has-text("로또6/45")',
        'a:has-text("로또6/45")',
    ]
    return _click_first_visible(page, selectors)


def fetch_official_stores(round_no: int, expected_winners: int | None) -> tuple[list[dict[str, str]], str]:
    """HAR에서 확인한 동행복권 공식 판매점 JSON API를 직접 호출합니다."""
    session = make_session()
    session.headers.update({
        "Referer": OFFICIAL_STORES_PAGE,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    })
    params = {
        "srchWnShpRnk": "1",
        "srchLtEpsd": str(round_no),
        "srchShpLctn": "",
        "_": str(int(time.time() * 1000)),
    }
    response = session.get(OFFICIAL_STORES_API, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    content_type = (response.headers.get("content-type") or "").lower()
    if "json" not in content_type and not response.text.lstrip().startswith("{"):
        raise RuntimeError(f"공식 판매점 API가 JSON이 아닌 응답을 반환했습니다: {content_type or 'unknown'}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("공식 판매점 API JSON 해석에 실패했습니다.") from exc

    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    rows = data.get("list", []) if isinstance(data, dict) else []
    total = to_int(data.get("total")) if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("공식 판매점 API의 data.list 형식이 올바르지 않습니다.")

    stores: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = clean_text(row.get("shpNm"))
        method = normalize_method(row.get("atmtPsvYnTxt"))
        address = clean_text(row.get("shpAddr"))
        if not address:
            address = clean_text(" ".join(str(row.get(k) or "") for k in (
                "tm1ShpLctnAddr", "tm2ShpLctnAddr", "tm3ShpLctnAddr", "tm4ShpLctnAddr"
            )))
        # 앱은 오프라인 판매점만 표시합니다.
        if "인터넷" in name or "동행복권" in name or "인터넷" in address:
            continue
        if is_real_store_name(name) and is_real_store_address(address):
            stores.append({"name": name, "method": method if method in {"자동", "수동", "반자동"} else "", "address": address})

    stores = dedupe_stores(stores)
    if total == 0:
        return [], "pending-official-api-empty"
    if not stores:
        return [], "pending-no-valid-offline-store"
    if expected_winners and len(stores) > expected_winners:
        return [], f"invalid-too-many:{len(stores)}/{expected_winners}"
    return stores, f"ok-official-json-api:{len(stores)}"

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


def store_data_is_trusted(item: dict[str, Any]) -> bool:
    source = item.get("dataSource", {}) if isinstance(item.get("dataSource"), dict) else {}
    return bool(item.get("stores")) and source.get("storesParserVersion") == STORE_PARSER_VERSION


def choose_backfill_targets(
    by_round: dict[int, dict[str, Any]],
    latest_official: int,
    batch: int,
    service: dict[str, Any],
) -> tuple[list[int], int]:
    """최근 1년 범위에서 판매점이 비어 있는 회차를 커서 기반으로 순환 선택합니다."""
    lower = max(STORE_BACKFILL_MIN_ROUND, latest_official - 52)
    upper = max(lower, latest_official - STORE_RETRY_ROUNDS)
    cursor = to_int(service.get("storeBackfillCursorRound"))
    if cursor is None or cursor > upper or cursor < lower:
        cursor = upper

    targets: list[int] = []
    checked = 0
    round_no = cursor
    span = max(1, upper - lower + 1)
    while checked < span and len(targets) < batch:
        item = by_round.get(round_no)
        if (
            item
            and not store_data_is_trusted(item)
            and to_int(item.get("prize", {}).get("first", {}).get("winnerCount"))
        ):
            targets.append(round_no)
        round_no -= 1
        if round_no < lower:
            round_no = upper
        checked += 1
    return targets, round_no


def update_dataset(
    data: dict[str, Any],
    official_items: list[dict[str, Any]],
    *,
    collect_stores: bool = True,
) -> tuple[dict[str, Any], list[int]]:
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

    # 최신 4회는 매 실행 재시도하고, 과거 미완성 회차는 별도 백필 배치로 조금씩 처리합니다.
    recent_targets: list[int] = []
    for round_no in range(latest_official, max(0, latest_official - STORE_RETRY_ROUNDS), -1):
        item = by_round.get(round_no)
        if item and not store_data_is_trusted(item) and to_int(item.get("prize", {}).get("first", {}).get("winnerCount")):
            # 이전 DOM 파서가 저장한 회차 불일치 데이터는 최신 회차에서 즉시 제거합니다.
            if item.get("stores"):
                item["stores"] = []
                item.setdefault("dataSource", {})["storesStatus"] = "cleared-untrusted-legacy-store-data"
                changed.append(round_no)
            recent_targets.append(round_no)
    backfill_targets, next_backfill_cursor = choose_backfill_targets(
        by_round, latest_official, STORE_BACKFILL_BATCH, result.get("service", {})
    )
    store_targets = list(dict.fromkeys([*recent_targets, *backfill_targets]))

    store_status: dict[str, str] = {}
    if collect_stores:
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
                    item["dataSource"]["storesParserVersion"] = STORE_PARSER_VERSION
                    item["dataSource"].pop("storesStatus", None)
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
    else:
        store_status["testMode"] = "store-network-skipped"

    result["results"] = [by_round[r] for r in sorted(by_round, reverse=True)]
    result["latestRound"] = max(by_round)
    result["schemaVersion"] = max(2, to_int(result.get("schemaVersion")) or 2)
    result.setdefault("service", {}).update({
        "collectorVersion": COLLECTOR_VERSION,
        "sourcePolicy": "official-dhlottery-only",
        "thirdPartySourceUsed": False,
        "officialResultsApi": OFFICIAL_RESULTS_API,
        "officialWinningStoresPage": OFFICIAL_STORES_PAGE,
        "officialWinningStoresApi": OFFICIAL_STORES_API,
        "storesParserVersion": STORE_PARSER_VERSION,
        "lastCheckedAt": now_iso(),
        "latestOfficialRound": latest_official,
        "recentReconcileCount": RECENT_RECONCILE_COUNT,
        "storeRetryRounds": STORE_RETRY_ROUNDS,
        "storeBackfillBatch": STORE_BACKFILL_BATCH,
        "storeBackfillTargets": backfill_targets,
        "storeBackfillCursorRound": next_backfill_cursor,
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
