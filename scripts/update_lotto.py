from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "lotto_results.json"
KST = timezone(timedelta(hours=9))
RAW_API = "https://www.dhlottery.co.kr/common.do?method=getLottoNumber&drwNo={round_no}"
RESULT_URL = "https://www.dhlottery.co.kr/lt645/result"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
})


def as_int(value: Any) -> int | None:
    if value is None:
        return None
    digits = re.sub(r"[^0-9]", "", str(value))
    return int(digits) if digits else None


def load_data() -> dict[str, Any]:
    with DATA_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("schemaVersion", 2)
    data.setdefault("results", [])
    data.setdefault("service", {})
    return data


def save_data(data: dict[str, Any]) -> None:
    data["latestRound"] = max((int(r.get("round", 0)) for r in data["results"]), default=0)
    data["updatedAt"] = datetime.now(KST).isoformat(timespec="seconds")
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(DATA_PATH)


def normalize_round(item: dict[str, Any]) -> dict[str, Any]:
    if "winning" not in item:
        item["winning"] = {
            "numbers": item.pop("numbers", []),
            "bonus": item.pop("bonus", None),
        }
    item.setdefault("prize", {})
    item["prize"].setdefault("first", {})
    item["prize"]["first"].setdefault("perGameAmount", None)
    item["prize"]["first"].setdefault("winnerCount", None)
    item["prize"].setdefault("second", {})
    item["prize"]["second"].setdefault("winnerCount", None)
    item["prize"].setdefault("totalSalesAmount", None)
    if "stores" not in item:
        item["stores"] = item.pop("firstPrizeStores", []) or []
    return item


def fetch_legacy_json(round_no: int) -> dict[str, Any] | None:
    try:
        r = SESSION.get(RAW_API.format(round_no=round_no), timeout=20)
        r.raise_for_status()
        payload = r.json()
    except Exception as exc:
        print(f"[{round_no}] 공식 JSON 보조조회 실패: {exc}")
        return None
    if payload.get("returnValue") != "success":
        return None
    numbers = [payload.get(f"drwtNo{i}") for i in range(1, 7)]
    if any(not isinstance(v, int) for v in numbers):
        return None
    first_total = as_int(payload.get("firstAccumamnt"))
    first_winners = as_int(payload.get("firstPrzwnerCo"))
    first_per_game = as_int(payload.get("firstWinamnt"))
    if first_per_game is None and first_total and first_winners:
        first_per_game = first_total // first_winners
    return {
        "round": round_no,
        "date": payload.get("drwNoDate"),
        "winning": {"numbers": numbers, "bonus": payload.get("bnusNo")},
        "prize": {
            "first": {"perGameAmount": first_per_game, "winnerCount": first_winners},
            "second": {"winnerCount": None},
            "totalSalesAmount": as_int(payload.get("totSellamnt")),
        },
        "stores": [],
    }


def parse_money_table_text(text: str) -> dict[str, int | None]:
    compact = re.sub(r"[\t\r]+", " ", text)
    lines = [re.sub(r"\s+", " ", x).strip() for x in compact.split("\n") if x.strip()]
    result: dict[str, int | None] = {
        "firstPerGame": None,
        "firstWinners": None,
        "secondWinners": None,
        "totalSales": None,
    }

    # Table rows commonly become one line per cell. Find row starts and inspect the next cells.
    for rank in ("1등", "2등"):
        for idx, line in enumerate(lines):
            if line == rank or line.startswith(rank + " "):
                window = lines[idx: idx + 8]
                nums = [as_int(v) for v in window]
                nums = [v for v in nums if v is not None]
                # Expected order: total prize, game count, per-game prize.
                if rank == "1등" and len(nums) >= 3:
                    result["firstWinners"] = nums[1]
                    result["firstPerGame"] = nums[2]
                elif rank == "2등" and len(nums) >= 2:
                    result["secondWinners"] = nums[1]
                break

    # More tolerant Korean sentence/label patterns.
    patterns = {
        "firstWinners": [r"1등[^\n]{0,50}?당첨게임\s*수\s*([0-9,]+)", r"1등[^\n]{0,30}?([0-9,]+)\s*게임"],
        "secondWinners": [r"2등[^\n]{0,50}?당첨게임\s*수\s*([0-9,]+)", r"2등[^\n]{0,30}?([0-9,]+)\s*게임"],
        "firstPerGame": [r"1등[^\n]{0,100}?1게임당\s*당첨금\s*([0-9,]+)", r"1등[^\n]{0,80}?([0-9,]+)\s*원"],
        "totalSales": [r"총\s*판매\s*금액\s*[:：]?\s*([0-9,]+)", r"총판매금액\s*[:：]?\s*([0-9,]+)"],
    }
    for key, regexes in patterns.items():
        if result[key] is not None:
            continue
        for pattern in regexes:
            m = re.search(pattern, text, re.S)
            if m:
                result[key] = as_int(m.group(1))
                break
    return result


def parse_store_rows(text: str) -> list[dict[str, Any]]:
    stores: list[dict[str, Any]] = []
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines() if x.strip()]
    methods = {"자동", "수동", "반자동"}
    for i, line in enumerate(lines):
        if line not in methods:
            continue
        method = line
        name = lines[i - 1] if i >= 1 else ""
        address = lines[i + 1] if i + 1 < len(lines) else ""
        # Skip headers and malformed rows.
        if name in {"구분", "선택", "번호", "상호명", "상호"} or len(name) < 2:
            continue
        if address in methods or len(address) < 4:
            address = ""
        stores.append({"name": name, "method": method, "address": address})
    # Stable de-duplication while keeping duplicate wins at same store if method differs.
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for s in stores:
        key = (s["name"], s["method"], s["address"])
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return unique


def browser_fetch_round(page, round_no: int) -> dict[str, Any]:
    print(f"[{round_no}] 공식 화면 브라우저 조회")
    page.goto(RESULT_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(1500)

    # Try selecting the desired round from any select containing that option.
    selected = False
    for select in page.locator("select").all():
        try:
            options = select.locator("option").all_text_contents()
            target_idx = next((i for i, t in enumerate(options) if re.sub(r"\D", "", t) == str(round_no)), None)
            if target_idx is not None:
                values = select.locator("option").evaluate_all("els => els.map(e => e.value)")
                select.select_option(values[target_idx])
                selected = True
                page.wait_for_timeout(1800)
                break
        except Exception:
            continue

    if not selected:
        # Query candidates used by old/new versions of the official site.
        candidates = [
            f"{RESULT_URL}?result=byWin&lottoId=LO40&drwNo={round_no}",
            f"{RESULT_URL}?drwNo={round_no}",
            f"https://www.dhlottery.co.kr/gameResult.do?method=byWin&drwNo={round_no}",
        ]
        for url in candidates:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(1200)
                body = page.locator("body").inner_text()
                if f"{round_no}회" in body or f"제{round_no}회" in body:
                    break
            except Exception:
                continue

    body_text = page.locator("body").inner_text(timeout=20_000)
    prize = parse_money_table_text(body_text)

    stores: list[dict[str, Any]] = []
    # Click a visible winning-store link/button; opening a new tab is supported.
    store_targets = page.get_by_text(re.compile("당첨판매점"))
    if store_targets.count() > 0:
        try:
            with page.context.expect_page(timeout=4_000) as popup_info:
                store_targets.first.click(timeout=5_000)
            popup = popup_info.value
            popup.wait_for_load_state("domcontentloaded")
            popup.wait_for_timeout(1200)
            stores = parse_store_rows(popup.locator("body").inner_text())
            popup.close()
        except Exception:
            try:
                store_targets.first.click(timeout=5_000)
                page.wait_for_timeout(1200)
                stores = parse_store_rows(page.locator("body").inner_text())
            except Exception:
                pass

    return {
        "firstPerGame": prize["firstPerGame"],
        "firstWinners": prize["firstWinners"],
        "secondWinners": prize["secondWinners"],
        "totalSales": prize["totalSales"],
        "stores": stores,
    }


def is_incomplete(item: dict[str, Any]) -> bool:
    p = item.get("prize", {})
    first = p.get("first", {})
    second = p.get("second", {})
    return any([
        not first.get("perGameAmount"),
        not first.get("winnerCount"),
        not second.get("winnerCount"),
        not p.get("totalSalesAmount"),
        not item.get("stores"),
    ])


def merge_round(target: dict[str, Any], incoming: dict[str, Any]) -> bool:
    changed = False
    target = normalize_round(target)
    if incoming.get("date") and not target.get("date"):
        target["date"] = incoming["date"]
        changed = True
    win = incoming.get("winning", {})
    if win.get("numbers") and not target["winning"].get("numbers"):
        target["winning"]["numbers"] = win["numbers"]
        changed = True
    if win.get("bonus") is not None and target["winning"].get("bonus") is None:
        target["winning"]["bonus"] = win["bonus"]
        changed = True
    inc_prize = incoming.get("prize", {})
    for path in (("first", "perGameAmount"), ("first", "winnerCount"), ("second", "winnerCount")):
        group, key = path
        value = inc_prize.get(group, {}).get(key)
        if value and target["prize"][group].get(key) != value:
            target["prize"][group][key] = value
            changed = True
    total = inc_prize.get("totalSalesAmount")
    if total and target["prize"].get("totalSalesAmount") != total:
        target["prize"]["totalSalesAmount"] = total
        changed = True
    if incoming.get("stores"):
        if target.get("stores") != incoming["stores"]:
            target["stores"] = incoming["stores"]
            changed = True
    return changed


def main() -> int:
    data = load_data()
    results = [normalize_round(x) for x in data["results"]]
    results.sort(key=lambda x: int(x.get("round", 0)))
    by_round = {int(x["round"]): x for x in results}
    latest = max(by_round, default=0)
    changed = False

    # Add every newly available round using the official JSON endpoint.
    candidate = latest + 1
    while True:
        fresh = fetch_legacy_json(candidate)
        if not fresh:
            break
        fresh = normalize_round(fresh)
        by_round[candidate] = fresh
        results.append(fresh)
        latest = candidate
        candidate += 1
        changed = True

    # Prioritize current/last 20 rounds, then a small batch of older incomplete rounds.
    recent = [r for r in range(latest, max(0, latest - 20), -1) if r in by_round and is_incomplete(by_round[r])]
    older = [r for r in sorted(by_round, reverse=True) if r <= latest - 20 and is_incomplete(by_round[r])]
    cursor = as_int(data.get("service", {}).get("incompleteCursor")) or 0
    if older:
        cursor %= len(older)
        old_batch = (older[cursor: cursor + 3] + older[:max(0, cursor + 3 - len(older))])[:3]
    else:
        old_batch = []
    targets = []
    for r in recent[:5] + old_batch:
        if r not in targets:
            targets.append(r)

    browser_errors: list[str] = []
    if targets:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(locale="ko-KR", timezone_id="Asia/Seoul", viewport={"width": 1440, "height": 1800})
            page = context.new_page()
            for round_no in targets:
                item = by_round[round_no]
                # Fill easy official JSON fields first.
                legacy = fetch_legacy_json(round_no)
                if legacy and merge_round(item, normalize_round(legacy)):
                    changed = True
                try:
                    detail = browser_fetch_round(page, round_no)
                    incoming = {
                        "round": round_no,
                        "winning": {},
                        "prize": {
                            "first": {"perGameAmount": detail["firstPerGame"], "winnerCount": detail["firstWinners"]},
                            "second": {"winnerCount": detail["secondWinners"]},
                            "totalSalesAmount": detail["totalSales"],
                        },
                        "stores": detail["stores"],
                    }
                    if merge_round(item, incoming):
                        changed = True
                    print(f"[{round_no}] 결과: 1등금={detail['firstPerGame']}, 1등수={detail['firstWinners']}, 2등수={detail['secondWinners']}, 판매액={detail['totalSales']}, 판매점={len(detail['stores'])}")
                except Exception as exc:
                    msg = f"[{round_no}] 브라우저 상세조회 실패: {exc}"
                    print(msg)
                    browser_errors.append(msg)
            context.close()
            browser.close()

    results.sort(key=lambda x: int(x.get("round", 0)))
    data["results"] = results
    complete_count = sum(1 for x in results if not is_incomplete(x))
    incomplete_count = len(results) - complete_count
    service = data.setdefault("service", {})
    service.update({
        "collectorVersion": "2.6-playwright-official",
        "mode": "new-round-fast-recent-priority-browser",
        "recentPriorityCount": 20,
        "olderBatchSize": 3,
        "incompleteCursor": (cursor + len(old_batch)) if older else 0,
        "completedRoundCount": complete_count,
        "incompleteRoundCount": incomplete_count,
        "lastRunAt": datetime.now(KST).isoformat(timespec="seconds"),
        "lastBrowserErrors": browser_errors[-5:],
    })
    # Service diagnostics must always be saved even if no prize value changed.
    save_data(data)
    print(f"완료: 최신 {data['latestRound']}회 / 완성 {complete_count}회 / 미완성 {incomplete_count}회")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
