#!/usr/bin/env python3
"""당첨번호와 1등 판매점을 data/lotto_results.json에 자동 반영합니다."""
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
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36 MyLottoNoteUpdater/2.0",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
}


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


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


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


def fetch_result_page(round_no: int) -> dict[str, Any] | None:
    candidates = [
        ("https://www.dhlottery.co.kr/lt645/result", {"result": "byWin", "drwNo": round_no}),
        ("https://www.dhlottery.co.kr/gameResult.do", {"method": "byWin", "drwNo": round_no}),
    ]
    for url, params in candidates:
        try:
            res = requests.get(url, params=params, headers=HEADERS, timeout=25)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, "html.parser")
            page_text = soup.get_text(" ", strip=True)
            if not re.search(rf"(?<!\d){round_no}\s*회", page_text):
                continue
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
            item = {"round": round_no, "date": date, "numbers": found[0], "bonus": found[1]}
            if valid_result(item, round_no):
                return item
        except Exception as exc:
            print(f"[안내] 공식 결과 페이지 조회 실패({url}): {exc}")
    return None


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
            # 보통: 번호 / 상호명 / 구분 / 소재지
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
    """공식 페이지를 먼저 시도하고, 표 구조가 없으면 공개 회차 페이지를 보조로 사용합니다."""
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

    # 당첨번호보다 판매점 공개가 늦을 수 있으므로, 최신 회차 판매점부터 매번 재확인합니다.
    if not latest_item.get("firstPrizeStores"):
        stores = fetch_first_prize_stores(latest)
        if stores:
            latest_item["firstPrizeStores"] = stores
            changed = True

    target = latest + 1
    print(f"현재 JSON 최신 회차: {latest}회")
    print(f"조회할 다음 회차: {target}회")
    item = fetch_legacy_json(target) or fetch_result_page(target)
    if item is not None:
        item["firstPrizeStores"] = fetch_first_prize_stores(target)
        by_round[target] = item
        latest = target
        changed = True
        print(f"[성공] {target}회 당첨번호 추가: {item['numbers']} + 보너스 {item['bonus']}")
    else:
        print(f"[안내] {target}회 결과는 아직 확인되지 않았습니다.")

    if not changed:
        print("[정상 종료] 새로 저장할 당첨번호나 판매점 정보가 없습니다.")
        return 0

    payload["schemaVersion"] = 2
    payload["latestRound"] = latest
    payload["updatedAt"] = datetime.now(KST).isoformat(timespec="seconds")
    payload["results"] = [by_round[key] for key in sorted(by_round)]
    DATA_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("[완료] lotto_results.json을 갱신했습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
