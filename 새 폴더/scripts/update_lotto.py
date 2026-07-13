#!/usr/bin/env python3
"""동행복권 당첨 결과를 읽어 data/lotto_results.json에 다음 회차를 추가합니다."""
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
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "Chrome/124.0 Safari/537.36 MyLottoNoteUpdater/1.0",
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


def fetch_legacy_json(round_no: int) -> dict[str, Any] | None:
    """구형 공식 JSON이 살아 있으면 가장 먼저 사용합니다."""
    url = "https://www.dhlottery.co.kr/common.do"
    try:
        res = requests.get(
            url,
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
        ".win_num .ball_645",
        ".lotto645_prizerank .ball",
        ".lotto645_prizerank .ball_645",
        ".result-ball",
        "[class*='win'] [class*='ball']",
    ]
    for selector in selectors:
        values = []
        for node in soup.select(selector):
            match = re.search(r"(?<!\d)([1-9]|[1-3]\d|4[0-5])(?!\d)", node.get_text(" ", strip=True))
            if match:
                values.append(int(match.group(1)))
        # 중복 DOM을 고려해 연속된 유효 7개 묶음을 찾습니다.
        for i in range(max(1, len(values) - 6)):
            chunk = values[i:i + 7]
            if len(chunk) == 7 and len(set(chunk[:6])) == 6 and chunk[6] not in chunk[:6]:
                return chunk[:6], chunk[6]
    return None


def fetch_result_page(round_no: int) -> dict[str, Any] | None:
    """현재 공식 회차별 당첨번호 페이지를 파싱합니다."""
    urls = [
        "https://www.dhlottery.co.kr/lt645/result",
        "https://www.dhlottery.co.kr/gameResult.do",
    ]
    params_list = [
        {"result": "byWin", "drwNo": round_no},
        {"method": "byWin", "drwNo": round_no},
    ]
    for url, params in zip(urls, params_list):
        try:
            res = requests.get(url, params=params, headers=HEADERS, timeout=25)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, "html.parser")
            page_text = soup.get_text(" ", strip=True)
            if not re.search(rf"(?<!\d){round_no}\s*회", page_text):
                continue

            found = _numbers_from_selectors(soup)
            if found is None:
                # 페이지의 '당첨번호'와 '보너스' 사이 텍스트를 제한적으로 검사합니다.
                section = re.search(
                    r"당첨번호(.{0,500}?)보너스(.{0,100}?)",
                    page_text,
                    flags=re.S,
                )
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
            print(f"[안내] 공식 페이지 조회 실패({url}): {exc}")
    return None


def main() -> int:
    payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    results = payload.get("results", [])
    if not results:
        print("[오류] JSON에 기존 당첨 결과가 없습니다.")
        return 1

    latest = max(int(item["round"]) for item in results)
    target = latest + 1
    print(f"현재 JSON 최신 회차: {latest}회")
    print(f"조회할 회차: {target}회")

    item = fetch_legacy_json(target) or fetch_result_page(target)
    if item is None:
        print(f"[정상 종료] {target}회 결과가 아직 없거나 공식 사이트에서 확인되지 않았습니다.")
        return 0

    by_round = {int(row["round"]): row for row in results}
    by_round[target] = item
    updated = [by_round[key] for key in sorted(by_round)]
    payload["schemaVersion"] = 1
    payload["latestRound"] = target
    payload["updatedAt"] = datetime.now(KST).isoformat(timespec="seconds")
    payload["results"] = updated
    DATA_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[성공] {target}회가 JSON에 추가되었습니다: {item['numbers']} + 보너스 {item['bonus']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
