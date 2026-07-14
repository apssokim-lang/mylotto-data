#!/usr/bin/env python3
"""마이로또노트 통합 데이터 서버 v2 자동 갱신기.

JSON v2 구조를 항상 유지하며 다음 데이터를 한 회차 객체에 통합합니다.
- 당첨번호 / 보너스 번호
- 1등 1게임당 당첨금
- 1등 당첨게임 수
- 2등 당첨게임 수
- 총 판매금액
- 1등 판매점

상세정보 수집에 실패해도 필드는 삭제하지 않고 null/빈 배열로 유지합니다.
따라서 앱과 JSON 소비자는 매번 동일한 구조를 안전하게 사용할 수 있습니다.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "lotto_results.json"
KST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36 MyLottoNoteUpdater/4.0",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def only_int(value: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", value)
    return int(digits) if digits else None


def normalize_method(value: str) -> str:
    text = clean_text(value)
    for method in ("반자동", "자동", "수동"):
        if method in text:
            return method
    return text or "확인"


def blank_prize() -> dict[str, Any]:
    return {
        "first": {"perGameAmount": None, "winnerCount": None},
        "second": {"winnerCount": None},
        "totalSalesAmount": None,
    }


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """구버전/신버전 행을 JSON v2 표준 구조로 변환합니다."""
    winning = row.get("winning") if isinstance(row.get("winning"), dict) else {}
    numbers = winning.get("numbers", row.get("numbers", []))
    bonus = winning.get("bonus", row.get("bonus"))

    prize = row.get("prize") if isinstance(row.get("prize"), dict) else {}
    first = prize.get("first") if isinstance(prize.get("first"), dict) else {}
    second = prize.get("second") if isinstance(prize.get("second"), dict) else {}

    stores = row.get("stores", row.get("firstPrizeStores", []))
    if not isinstance(stores, list):
        stores = []

    return {
        "round": int(row["round"]),
        "date": str(row["date"]),
        "winning": {
            "numbers": [int(n) for n in numbers],
            "bonus": int(bonus),
        },
        "prize": {
            "first": {
                "perGameAmount": first.get("perGameAmount", row.get("firstPrizeAmount")),
                "winnerCount": first.get("winnerCount", row.get("firstPrizeWinnerCount")),
            },
            "second": {
                "winnerCount": second.get("winnerCount", row.get("secondPrizeWinnerCount")),
            },
            "totalSalesAmount": prize.get("totalSalesAmount", row.get("totalSalesAmount")),
        },
        "stores": stores,
    }


def valid_row(row: dict[str, Any], expected_round: int) -> bool:
    try:
        winning = row["winning"]
        nums = winning["numbers"]
        bonus = winning["bonus"]
        return (
            row["round"] == expected_round
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["date"]) is not None
            and isinstance(nums, list)
            and len(nums) == 6
            and len(set(nums)) == 6
            and all(isinstance(n, int) and 1 <= n <= 45 for n in nums)
            and isinstance(bonus, int)
            and 1 <= bonus <= 45
            and bonus not in nums
            and isinstance(row.get("prize"), dict)
            and isinstance(row.get("stores"), list)
        )
    except Exception:
        return False


def apply_summary(row: dict[str, Any], summary: dict[str, int]) -> bool:
    changed = False
    prize = row["prize"]
    mapping = {
        "firstPrizeAmount": (prize["first"], "perGameAmount"),
        "firstPrizeWinnerCount": (prize["first"], "winnerCount"),
        "secondPrizeWinnerCount": (prize["second"], "winnerCount"),
        "totalSalesAmount": (prize, "totalSalesAmount"),
    }
    for source_key, (target, target_key) in mapping.items():
        value = summary.get(source_key)
        if value is not None and target.get(target_key) != value:
            target[target_key] = value
            changed = True
    return changed


def summary_missing(row: dict[str, Any]) -> bool:
    prize = row["prize"]
    return any(value is None for value in (
        prize["first"].get("perGameAmount"),
        prize["first"].get("winnerCount"),
        prize["second"].get("winnerCount"),
        prize.get("totalSalesAmount"),
    ))


def fetch_legacy_json(round_no: int) -> tuple[dict[str, Any] | None, dict[str, int]]:
    try:
        res = requests.get(
            "https://www.dhlottery.co.kr/common.do",
            params={"method": "getLottoNumber", "drwNo": round_no},
            headers=HEADERS,
            timeout=20,
        )
        res.raise_for_status()
        data = res.json()
        if data.get("returnValue") != "success":
            return None, {}
        row = normalize_row({
            "round": int(data["drwNo"]),
            "date": str(data["drwNoDate"]),
            "numbers": [int(data[f"drwtNo{i}"]) for i in range(1, 7)],
            "bonus": int(data["bnusNo"]),
        })
        summary: dict[str, int] = {}
        if data.get("firstWinamnt") is not None:
            summary["firstPrizeAmount"] = int(data["firstWinamnt"])
        if data.get("firstPrzwnerCo") is not None:
            summary["firstPrizeWinnerCount"] = int(data["firstPrzwnerCo"])
        if data.get("totSellamnt") is not None:
            summary["totalSalesAmount"] = int(data["totSellamnt"])
        apply_summary(row, summary)
        return (row, summary) if valid_row(row, round_no) else (None, summary)
    except Exception as exc:
        print(f"[안내] 구형 JSON 조회 실패: {exc}")
        return None, {}


def _numbers_from_selectors(soup: BeautifulSoup) -> tuple[list[int], int] | None:
    selectors = [
        ".win_num .ball_645", ".lotto645_prizerank .ball",
        ".lotto645_prizerank .ball_645", ".result-ball",
        "[class*='win'] [class*='ball']",
    ]
    for selector in selectors:
        values: list[int] = []
        for node in soup.select(selector):
            match = re.search(r"(?<!\d)([1-9]|[1-3]\d|4[0-5])(?!\d)", node.get_text(" ", strip=True))
            if match:
                values.append(int(match.group(1)))
        for i in range(max(1, len(values) - 6)):
            chunk = values[i:i + 7]
            if len(chunk) == 7 and len(set(chunk[:6])) == 6 and chunk[6] not in chunk[:6]:
                return chunk[:6], chunk[6]
    return None


def parse_prize_summary(soup: BeautifulSoup) -> dict[str, int]:
    summary: dict[str, int] = {}
    for table in soup.find_all("table"):
        rows: list[list[str]] = []
        for tr in table.find_all("tr"):
            cells = [clean_text(cell.get_text(" ", strip=True)) for cell in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        if not rows:
            continue

        header_index = next((i for i, row in enumerate(rows)
            if any("당첨게임" in c for c in row) and any("당첨금" in c for c in row)), -1)
        if header_index < 0:
            continue
        headers = rows[header_index]

        def col(*keywords: str) -> int:
            return next((i for i, h in enumerate(headers) if all(k in h for k in keywords)), -1)

        count_idx = col("당첨게임")
        per_game_idx = col("1게임당", "당첨금")
        if per_game_idx < 0:
            per_game_idx = col("게임당", "당첨금")

        for row in rows[header_index + 1:]:
            joined = " ".join(row)
            rank = 1 if re.search(r"(^|\s)1등($|\s)", joined) else 2 if re.search(r"(^|\s)2등($|\s)", joined) else None
            if rank is None:
                continue
            if 0 <= count_idx < len(row):
                count = only_int(row[count_idx])
                if count is not None:
                    summary["firstPrizeWinnerCount" if rank == 1 else "secondPrizeWinnerCount"] = count
            if rank == 1 and 0 <= per_game_idx < len(row):
                amount = only_int(row[per_game_idx])
                if amount is not None:
                    summary["firstPrizeAmount"] = amount

    text = clean_text(soup.get_text(" ", strip=True))
    for pattern in (
        r"총\s*판매금액\s*[:：]?\s*([0-9,]+)\s*원",
        r"총판매금액\s*[:：]?\s*([0-9,]+)\s*원",
    ):
        match = re.search(pattern, text)
        if match:
            summary["totalSalesAmount"] = int(match.group(1).replace(",", ""))
            break
    return summary


def fetch_result_page(round_no: int) -> tuple[dict[str, Any] | None, dict[str, int]]:
    candidates = [
        ("https://www.dhlottery.co.kr/lt645/result", {"result": "byWin", "drwNo": round_no}),
        ("https://www.dhlottery.co.kr/gameResult.do", {"method": "byWin", "drwNo": round_no}),
    ]
    best_summary: dict[str, int] = {}
    for url, params in candidates:
        try:
            res = requests.get(url, params=params, headers=HEADERS, timeout=25)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, "html.parser")
            page_text = soup.get_text(" ", strip=True)
            if not re.search(rf"(?<!\d){round_no}\s*회", page_text):
                continue
            summary = parse_prize_summary(soup)
            if len(summary) > len(best_summary):
                best_summary = summary
            found = _numbers_from_selectors(soup)
            if found is None:
                continue
            date_match = re.search(r"(20\d{2})[.년\-/]\s*(\d{1,2})[.월\-/]\s*(\d{1,2})", page_text)
            if not date_match:
                continue
            row = normalize_row({
                "round": round_no,
                "date": f"{int(date_match.group(1)):04d}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}",
                "numbers": found[0],
                "bonus": found[1],
            })
            apply_summary(row, summary)
            if valid_row(row, round_no):
                return row, summary
        except Exception as exc:
            print(f"[안내] 공식 결과 페이지 조회 실패({url}): {exc}")
    return None, best_summary


def parse_store_table(soup: BeautifulSoup) -> list[dict[str, str]]:
    stores: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for table in soup.find_all("table"):
        header_text = " ".join(clean_text(th.get_text(" ", strip=True)) for th in table.find_all("th"))
        if not ("상호" in header_text and ("소재지" in header_text or "주소" in header_text)):
            continue
        for tr in table.find_all("tr"):
            cells = [clean_text(td.get_text(" ", strip=True)) for td in tr.find_all("td")]
            if len(cells) < 3:
                continue
            if cells[0].isdigit() and len(cells) >= 4:
                name, method, address = cells[1], cells[2], " ".join(cells[3:])
            else:
                method_index = next((i for i, c in enumerate(cells) if normalize_method(c) in {"자동", "수동", "반자동"}), -1)
                if method_index <= 0 or method_index >= len(cells) - 1:
                    continue
                name, method, address = cells[method_index - 1], cells[method_index], " ".join(cells[method_index + 1:])
            name, method, address = clean_text(name), normalize_method(method), clean_text(address)
            if not name or not address:
                continue
            key = (name, method, address)
            if key not in seen:
                stores.append({"name": name, "method": method, "address": address})
                seen.add(key)
    return stores


def fetch_first_prize_stores(round_no: int) -> list[dict[str, str]]:
    candidates = [
        ("https://www.dhlottery.co.kr/lt645/result", {"result": "byWin", "drwNo": round_no}),
        ("https://www.dhlottery.co.kr/gameResult.do", {"method": "byWin", "drwNo": round_no}),
        (f"https://pyony.com/lotto/rounds/{round_no}/", None),
    ]
    for url, params in candidates:
        try:
            res = requests.get(url, params=params, headers=HEADERS, timeout=25)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, "html.parser")
            if str(round_no) not in soup.get_text(" ", strip=True):
                continue
            stores = parse_store_table(soup)
            if stores:
                print(f"[성공] {round_no}회 1등 판매점 {len(stores)}건 수집")
                return stores
        except Exception as exc:
            print(f"[안내] 판매점 조회 실패({url}): {exc}")
    return []


def main() -> int:
    raw_payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    raw_results = raw_payload.get("results", [])
    if not raw_results:
        print("[오류] JSON에 기존 당첨 결과가 없습니다.")
        return 1

    rows = [normalize_row(dict(row)) for row in raw_results]
    by_round = {row["round"]: row for row in rows if valid_row(row, row["round"])}
    if not by_round:
        print("[오류] 유효한 당첨 결과가 없습니다.")
        return 1

    # 스키마 마이그레이션 자체도 변경으로 인식합니다.
    changed = raw_payload.get("schemaVersion") != 2 or raw_results != rows
    latest = max(by_round)
    latest_row = by_round[latest]

    if not latest_row["stores"]:
        stores = fetch_first_prize_stores(latest)
        if stores:
            latest_row["stores"] = stores
            changed = True

    if summary_missing(latest_row):
        legacy_row, legacy_summary = fetch_legacy_json(latest)
        _, page_summary = fetch_result_page(latest)
        summary = {**legacy_summary, **page_summary}
        if apply_summary(latest_row, summary):
            changed = True
            print(f"[성공] {latest}회 당첨 상세정보 보완: {summary}")

    target = latest + 1
    print(f"현재 JSON 최신 회차: {latest}회")
    print(f"조회할 다음 회차: {target}회")
    legacy_row, legacy_summary = fetch_legacy_json(target)
    page_row, page_summary = fetch_result_page(target)
    new_row = legacy_row or page_row
    if new_row is not None:
        apply_summary(new_row, {**legacy_summary, **page_summary})
        new_row["stores"] = fetch_first_prize_stores(target)
        by_round[target] = new_row
        latest = target
        changed = True
        print(f"[성공] {target}회 통합 데이터 추가")
    else:
        print(f"[안내] {target}회 결과는 아직 확인되지 않았습니다.")

    if not changed:
        print("[정상 종료] 새로 저장할 통합 데이터가 없습니다.")
        return 0

    now = datetime.now(KST).isoformat(timespec="seconds")
    payload = {
        "schemaVersion": 2,
        "service": {
            "name": "MyLottoNote Data Server",
            "description": "당첨번호, 당첨금, 당첨게임 수, 총 판매금액, 1등 판매점 통합 데이터",
            "generatedAt": now,
        },
        "latestRound": latest,
        "updatedAt": now,
        "results": [by_round[key] for key in sorted(by_round)],
    }

    output = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    DATA_FILE.write_text(output, encoding="utf-8")
    print("[완료] lotto_results.json을 통합 데이터 서버 v2 구조로 저장했습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
