#!/usr/bin/env python3
"""마이로또노트 통합 JSON 자동 갱신기.

당첨번호, 1등 판매점, 1등 1게임당 당첨금, 1등/2등 당첨게임 수,
총 판매금액을 data/lotto_results.json에 함께 저장합니다.
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
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36 MyLottoNoteUpdater/3.0",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def only_int(value: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", value)
    return int(digits) if digits else None


def valid_result(item: dict[str, Any], expected_round: int) -> bool:
    nums = item.get("numbers")
    return (
        item.get("round") == expected_round
        and isinstance(item.get("date"), str)
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["date"]) is not None
        and isinstance(nums, list)
        and len(nums) == 6
        and len(set(nums)) == 6
        and all(isinstance(n, int) and 1 <= n <= 45 for n in nums)
        and isinstance(item.get("bonus"), int)
        and 1 <= item["bonus"] <= 45
        and item["bonus"] not in nums
    )


def normalize_method(value: str) -> str:
    text = clean_text(value)
    for method in ("반자동", "자동", "수동"):
        if method in text:
            return method
    return text or "확인"


def fetch_legacy_json(round_no: int) -> dict[str, Any] | None:
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
            return None
        item = {
            "round": int(data["drwNo"]),
            "date": str(data["drwNoDate"]),
            "numbers": [int(data[f"drwtNo{i}"]) for i in range(1, 7)],
            "bonus": int(data["bnusNo"]),
        }
        if data.get("firstWinamnt") is not None:
            item["firstPrizeAmount"] = int(data["firstWinamnt"])
        return item if valid_result(item, round_no) else None
    except Exception as exc:
        print(f"[안내] 구형 JSON 조회 실패: {exc}")
        return None


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
    """공식 결과 표에서 통합 회차 정보를 추출합니다."""
    summary: dict[str, int] = {}

    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [clean_text(cell.get_text(" ", strip=True)) for cell in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        if not rows:
            continue

        header_index = next(
            (i for i, row in enumerate(rows) if any("당첨게임" in c for c in row) and any("당첨금" in c for c in row)),
            -1,
        )
        if header_index < 0:
            continue
        headers = rows[header_index]

        def column_index(*keywords: str) -> int:
            return next((i for i, h in enumerate(headers) if all(k in h for k in keywords)), -1)

        count_idx = column_index("당첨게임")
        per_game_idx = column_index("1게임당", "당첨금")
        if per_game_idx < 0:
            per_game_idx = column_index("게임당", "당첨금")

        for row in rows[header_index + 1:]:
            joined = " ".join(row)
            rank = None
            if re.search(r"(^|\s)1등($|\s)", joined):
                rank = 1
            elif re.search(r"(^|\s)2등($|\s)", joined):
                rank = 2
            if rank is None:
                continue

            if count_idx >= 0 and count_idx < len(row):
                count = only_int(row[count_idx])
                if count is not None:
                    summary["firstPrizeWinnerCount" if rank == 1 else "secondPrizeWinnerCount"] = count
            if rank == 1 and per_game_idx >= 0 and per_game_idx < len(row):
                amount = only_int(row[per_game_idx])
                if amount is not None:
                    summary["firstPrizeAmount"] = amount

    page_text = clean_text(soup.get_text(" ", strip=True))
    sales_patterns = [
        r"총\s*판매금액\s*[:：]?\s*([0-9,]+)\s*원",
        r"총판매금액\s*[:：]?\s*([0-9,]+)\s*원",
        r"판매금액\s*[:：]?\s*([0-9,]+)\s*원",
    ]
    for pattern in sales_patterns:
        match = re.search(pattern, page_text)
        if match:
            summary["totalSalesAmount"] = int(match.group(1).replace(",", ""))
            break

    # 표의 열 인식이 실패했을 때를 위한 보조 정규식
    if "firstPrizeWinnerCount" not in summary:
        match = re.search(r"1등.{0,120}?([0-9,]+)\s*(?:게임|명)", page_text)
        if match:
            summary["firstPrizeWinnerCount"] = int(match.group(1).replace(",", ""))
    if "secondPrizeWinnerCount" not in summary:
        match = re.search(r"2등.{0,120}?([0-9,]+)\s*(?:게임|명)", page_text)
        if match:
            summary["secondPrizeWinnerCount"] = int(match.group(1).replace(",", ""))
    if "firstPrizeAmount" not in summary:
        match = re.search(r"1등.{0,180}?1게임당\s*당첨금.{0,30}?([0-9,]+)\s*원", page_text)
        if match:
            summary["firstPrizeAmount"] = int(match.group(1).replace(",", ""))

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
                section = re.search(r"당첨번호(.{0,500}?)보너스(.{0,100}?)", page_text, flags=re.S)
                if section:
                    winning = [int(x) for x in re.findall(r"(?<!\d)([1-9]|[1-3]\d|4[0-5])(?!\d)", section.group(1))]
                    bonus_values = [int(x) for x in re.findall(r"(?<!\d)([1-9]|[1-3]\d|4[0-5])(?!\d)", section.group(2))]
                    if len(winning) >= 6 and bonus_values:
                        found = (winning[-6:], bonus_values[0])
            if found is None:
                continue
            date_match = re.search(r"(20\d{2})[.년\-/]\s*(\d{1,2})[.월\-/]\s*(\d{1,2})", page_text)
            if not date_match:
                continue
            date = f"{int(date_match.group(1)):04d}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
            item: dict[str, Any] = {"round": round_no, "date": date, "numbers": found[0], "bonus": found[1]}
            item.update(summary)
            if valid_result(item, round_no):
                return item, summary
        except Exception as exc:
            print(f"[안내] 공식 결과 페이지 조회 실패({url}): {exc}")
    return None, best_summary


def fetch_prize_summary(round_no: int) -> dict[str, int]:
    _, summary = fetch_result_page(round_no)
    return summary


def parse_store_table(soup: BeautifulSoup) -> list[dict[str, str]]:
    stores: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for table in soup.find_all("table"):
        headers = [clean_text(th.get_text(" ", strip=True)) for th in table.find_all("th")]
        header_text = " ".join(headers)
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
                name = cells[method_index - 1]
                method = cells[method_index]
                address = " ".join(cells[method_index + 1:])
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
            text = soup.get_text(" ", strip=True)
            if str(round_no) not in text:
                continue
            stores = parse_store_table(soup)
            if stores:
                print(f"[성공] {round_no}회 1등 판매점 {len(stores)}건 수집: {url}")
                return stores
        except Exception as exc:
            print(f"[안내] 판매점 조회 실패({url}): {exc}")
    return []


def missing_summary(item: dict[str, Any]) -> bool:
    return any(item.get(key) is None for key in (
        "firstPrizeAmount", "firstPrizeWinnerCount",
        "secondPrizeWinnerCount", "totalSalesAmount",
    ))


def main() -> int:
    payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    results = payload.get("results", [])
    if not results:
        print("[오류] JSON에 기존 당첨 결과가 없습니다.")
        return 1

    changed = False
    by_round = {int(row["round"]): row for row in results}
    latest = max(by_round)
    latest_item = by_round[latest]

    # 판매점과 당첨 상세 정보가 번호보다 늦게 공개될 수 있어 최신 회차는 매번 보완합니다.
    if not latest_item.get("firstPrizeStores"):
        stores = fetch_first_prize_stores(latest)
        if stores:
            latest_item["firstPrizeStores"] = stores
            changed = True

    if missing_summary(latest_item):
        summary = fetch_prize_summary(latest)
        for key, value in summary.items():
            if latest_item.get(key) != value:
                latest_item[key] = value
                changed = True
        if summary:
            print(f"[성공] {latest}회 당첨 상세정보 보완: {summary}")

    target = latest + 1
    print(f"현재 JSON 최신 회차: {latest}회")
    print(f"조회할 다음 회차: {target}회")
    legacy = fetch_legacy_json(target)
    page_item, page_summary = fetch_result_page(target)
    item = legacy or page_item
    if item is not None:
        if page_item is not None:
            for key in ("firstPrizeAmount", "firstPrizeWinnerCount", "secondPrizeWinnerCount", "totalSalesAmount"):
                if page_item.get(key) is not None:
                    item[key] = page_item[key]
        else:
            item.update(page_summary)
        item["firstPrizeStores"] = fetch_first_prize_stores(target)
        by_round[target] = item
        latest = target
        changed = True
        print(f"[성공] {target}회 통합 데이터 추가: {item['numbers']} + 보너스 {item['bonus']}")
    else:
        print(f"[안내] {target}회 결과는 아직 확인되지 않았습니다.")

    if not changed:
        print("[정상 종료] 새로 저장할 통합 데이터가 없습니다.")
        return 0

    payload["schemaVersion"] = 3
    payload["latestRound"] = latest
    payload["updatedAt"] = datetime.now(KST).isoformat(timespec="seconds")
    payload["results"] = [by_round[key] for key in sorted(by_round)]
    DATA_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("[완료] lotto_results.json 통합 데이터를 갱신했습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
