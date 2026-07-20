from __future__ import annotations

import copy
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "lotto_results.json"
BACKUP_PATH = ROOT / "data" / "lotto_results_backup.json"
KST = timezone(timedelta(hours=9))

OFFICIAL_API_URL = "https://www.dhlottery.co.kr/lt645/selectPstLt645Info.do"
OFFICIAL_REFERER = "https://www.dhlottery.co.kr/lt645/result"
COLLECTOR_VERSION = "7.0-official-json-api-minimal-engine"
SOURCE_NAME = "dhlottery-official-internal-json"
REQUEST_TIMEOUT = 25
RECENT_RECONCILE_COUNT = 60


def to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    digits = re.sub(r"[^0-9]", "", str(value))
    return int(digits) if digits else None


def valid_numbers(numbers: Any, bonus: Any) -> bool:
    if not isinstance(numbers, list) or len(numbers) != 6:
        return False
    try:
        nums = [int(v) for v in numbers]
        bns = int(bonus)
    except (TypeError, ValueError):
        return False
    return (
        len(set(nums)) == 6
        and all(1 <= v <= 45 for v in nums)
        and 1 <= bns <= 45
        and bns not in nums
    )


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
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": OFFICIAL_REFERER,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
    )
    return session


def fetch_official_rows(session: requests.Session, round_value: str | int = "all") -> list[dict[str, Any]]:
    params = {"srchLtEpsd": str(round_value), "_": str(int(time.time() * 1000))}
    response = session.get(OFFICIAL_API_URL, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    content_type = (response.headers.get("content-type") or "").lower()
    if "json" not in content_type and not response.text.lstrip().startswith("{"):
        raise RuntimeError(f"공식 API가 JSON이 아닌 응답을 반환했습니다: {content_type or 'unknown'}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("공식 API JSON 해석에 실패했습니다.") from exc
    rows = payload.get("data", {}).get("list", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        raise RuntimeError("공식 API 응답의 data.list 형식이 올바르지 않습니다.")
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
            "first": {
                "perGameAmount": to_int(row.get("rnk1WnAmt")),
                "winnerCount": to_int(row.get("rnk1WnNope")),
            },
            "second": {
                "perGameAmount": to_int(row.get("rnk2WnAmt")),
                "winnerCount": to_int(row.get("rnk2WnNope")),
            },
            "third": {
                "perGameAmount": to_int(row.get("rnk3WnAmt")),
                "winnerCount": to_int(row.get("rnk3WnNope")),
            },
            "totalSalesAmount": to_int(row.get("wholEpsdSumNtslAmt"))
            or to_int(row.get("rlvtEpsdSumNtslAmt")),
        },
        "dataSource": {
            "winning": SOURCE_NAME,
            "prize": SOURCE_NAME,
            "verifiedAt": datetime.now(KST).isoformat(timespec="seconds"),
        },
    }


def normalize_existing(raw: dict[str, Any]) -> dict[str, Any]:
    item = copy.deepcopy(raw)
    if not isinstance(item.get("winning"), dict):
        item["winning"] = {
            "numbers": item.pop("numbers", []),
            "bonus": item.pop("bonus", None),
        }
    item.setdefault("stores", item.pop("firstPrizeStores", []) if "firstPrizeStores" in item else [])
    item.setdefault("prize", {})
    item.setdefault("dataSource", {})
    return item


def merge_official(existing: dict[str, Any] | None, official: dict[str, Any]) -> dict[str, Any]:
    """공식 핵심 필드만 교체하고, 판매점 등 앱 전용 부가 필드는 보존합니다."""
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
    source = result.setdefault("dataSource", {})
    source.update(copy.deepcopy(official.get("dataSource", {})))
    return result


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
        winning = normalize_existing(raw).get("winning", {})
        if not valid_numbers(winning.get("numbers"), winning.get("bonus")):
            raise ValueError(f"{round_no}회 당첨번호가 올바르지 않습니다.")
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

    # API가 새 회차를 반환했을 때만 추가합니다. 미래의 빈 회차는 절대 생성하지 않습니다.
    result["results"] = [by_round[r] for r in sorted(by_round, reverse=True)]
    result["latestRound"] = max(by_round)
    result["schemaVersion"] = max(2, to_int(result.get("schemaVersion")) or 2)
    service = result.setdefault("service", {})
    service.update(
        {
            "collectorVersion": COLLECTOR_VERSION,
            "sourcePolicy": "official-dhlottery-only",
            "thirdPartySourceUsed": False,
            "officialApi": OFFICIAL_API_URL,
            "lastCheckedAt": datetime.now(KST).isoformat(timespec="seconds"),
            "latestOfficialRound": latest_official,
            "recentReconcileCount": RECENT_RECONCILE_COUNT,
            "changedRounds": sorted(set(changed), reverse=True),
        }
    )
    return result, sorted(set(changed), reverse=True)


def main() -> int:
    data = load_dataset()
    validate_dataset(data)

    session = make_session()
    rows = fetch_official_rows(session, "all")
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
    if changed:
        print(f"완료: 공식 최신 {latest}회 / 변경 회차 {changed}")
    else:
        print(f"완료: 공식 최신 {latest}회 / 변경 없음")
    if parse_errors:
        print(f"참고: 무시된 비정상 API 행 {len(parse_errors)}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
