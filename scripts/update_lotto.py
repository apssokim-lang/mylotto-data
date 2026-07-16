from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests
from playwright.sync_api import sync_playwright

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


def valid_first_amount(v: Any) -> bool:
    n = as_int(v)
    return n is not None and 100_000_000 <= n <= 100_000_000_000


def valid_first_winners(v: Any) -> bool:
    n = as_int(v)
    return n is not None and 1 <= n <= 1_000


def valid_second_winners(v: Any) -> bool:
    n = as_int(v)
    return n is not None and 1 <= n <= 100_000


def valid_total_sales(v: Any) -> bool:
    n = as_int(v)
    return n is not None and 1_000_000_000 <= n <= 10_000_000_000_000


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
    tmp = DATA_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(DATA_PATH)


def normalize_round(item: dict[str, Any]) -> dict[str, Any]:
    if "winning" not in item:
        item["winning"] = {"numbers": item.pop("numbers", []), "bonus": item.pop("bonus", None)}
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


def sanitize_prize(item: dict[str, Any]) -> bool:
    """Remove impossible values already written by an older broken collector."""
    changed = False
    item = normalize_round(item)
    first = item["prize"]["first"]
    second = item["prize"]["second"]
    if first.get("perGameAmount") is not None and not valid_first_amount(first["perGameAmount"]):
        print(f"[{item.get('round')}] 비정상 1등 당첨금 삭제: {first['perGameAmount']}")
        first["perGameAmount"] = None
        changed = True
    if first.get("winnerCount") is not None and not valid_first_winners(first["winnerCount"]):
        print(f"[{item.get('round')}] 비정상 1등 게임 수 삭제: {first['winnerCount']}")
        first["winnerCount"] = None
        changed = True
    if second.get("winnerCount") is not None and not valid_second_winners(second["winnerCount"]):
        print(f"[{item.get('round')}] 비정상 2등 게임 수 삭제: {second['winnerCount']}")
        second["winnerCount"] = None
        changed = True
    total = item["prize"].get("totalSalesAmount")
    if total is not None and not valid_total_sales(total):
        print(f"[{item.get('round')}] 비정상 총 판매금액 삭제: {total}")
        item["prize"]["totalSalesAmount"] = None
        changed = True
    return changed


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
    first_per_game = as_int(payload.get("firstWinamnt"))
    first_winners = as_int(payload.get("firstPrzwnerCo"))
    total_sales = as_int(payload.get("totSellamnt"))
    return {
        "round": round_no,
        "date": payload.get("drwNoDate"),
        "winning": {"numbers": numbers, "bonus": payload.get("bnusNo")},
        "prize": {
            "first": {
                "perGameAmount": first_per_game if valid_first_amount(first_per_game) else None,
                "winnerCount": first_winners if valid_first_winners(first_winners) else None,
            },
            "second": {"winnerCount": None},
            "totalSalesAmount": total_sales if valid_total_sales(total_sales) else None,
        },
        "stores": [],
    }


def parse_rank_table(page) -> dict[str, int | None]:
    result = {"firstPerGame": None, "firstWinners": None, "secondWinners": None, "totalSales": None}

    # Parse only rows whose first cell is exactly 1등 or 2등.
    for tr in page.locator("table tr").all():
        try:
            cells = [re.sub(r"\s+", " ", x).strip() for x in tr.locator("th, td").all_text_contents()]
        except Exception:
            continue
        if not cells:
            continue
        rank = cells[0]
        if rank not in {"1등", "2등"}:
            continue
        # Official header order: 순위 / 등위별 총 당첨금 / 당첨게임 수 / 1게임당 당첨금 / ...
        if len(cells) >= 4:
            game_count = as_int(cells[2])
            per_game = as_int(cells[3])
            if rank == "1등":
                if valid_first_winners(game_count):
                    result["firstWinners"] = game_count
                if valid_first_amount(per_game):
                    result["firstPerGame"] = per_game
            else:
                if valid_second_winners(game_count):
                    result["secondWinners"] = game_count

    body = page.locator("body").inner_text()
    m = re.search(r"총\s*판매\s*금액\s*[:：]?\s*([0-9,]+)\s*원?", body)
    if m:
        total = as_int(m.group(1))
        if valid_total_sales(total):
            result["totalSales"] = total
    return result


def parse_store_rows(text: str) -> list[dict[str, Any]]:
    stores: list[dict[str, Any]] = []
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines() if x.strip()]
    methods = {"자동", "수동", "반자동"}
    for i, line in enumerate(lines):
        if line not in methods:
            continue
        name = lines[i - 1] if i >= 1 else ""
        address = lines[i + 1] if i + 1 < len(lines) else ""
        if name in {"구분", "선택", "번호", "상호명", "상호"} or len(name) < 2:
            continue
        if address in methods or len(address) < 4:
            address = ""
        stores.append({"name": name, "method": line, "address": address})
    unique, seen = [], set()
    for s in stores:
        key = (s["name"], s["method"], s["address"])
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return unique


def select_round_and_submit(page, round_no: int) -> None:
    page.goto(RESULT_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(1200)

    chosen = False
    for select in page.locator("select").all():
        try:
            opts = select.locator("option")
            texts = opts.all_text_contents()
            values = opts.evaluate_all("els => els.map(e => e.value)")
            idx = next((i for i, t in enumerate(texts) if as_int(t) == round_no), None)
            if idx is None:
                continue
            select.select_option(values[idx])
            chosen = True
            # Explicitly dispatch events because the official page may not react to select_option alone.
            select.dispatch_event("change")
            break
        except Exception:
            continue
    if not chosen:
        raise RuntimeError(f"{round_no}회 선택 항목을 찾지 못했습니다.")

    # Click the lookup button near the round selector.
    clicked = False
    for label in ("조회하기", "조회"):
        btn = page.get_by_text(label, exact=True)
        if btn.count() > 0:
            try:
                btn.first.click(timeout=5_000)
                clicked = True
                break
            except Exception:
                pass
    if not clicked:
        # Some versions use a submit button without visible exact text.
        for css in ("button[type=submit]", "input[type=submit]"):
            loc = page.locator(css)
            if loc.count() > 0:
                loc.first.click(timeout=5_000)
                clicked = True
                break
    if not clicked:
        raise RuntimeError("회차 조회 버튼을 찾지 못했습니다.")

    page.wait_for_load_state("domcontentloaded", timeout=30_000)
    page.wait_for_timeout(1400)

    # Never save values unless the page confirms the requested round.
    body = page.locator("body").inner_text()
    if not re.search(rf"(?:제\s*)?{round_no}\s*회", body):
        selected_values = []
        for sel in page.locator("select").all():
            try:
                selected_values.append(sel.input_value())
            except Exception:
                pass
        raise RuntimeError(f"요청한 {round_no}회 화면이 아님. 선택값={selected_values[:5]}")


def browser_fetch_round(page, round_no: int) -> dict[str, Any]:
    print(f"[{round_no}] 공식 화면 회차별 조회")
    select_round_and_submit(page, round_no)
    prize = parse_rank_table(page)

    stores: list[dict[str, Any]] = []
    targets = page.get_by_text(re.compile("당첨판매점"))
    if targets.count() > 0:
        try:
            with page.context.expect_page(timeout=4_000) as popup_info:
                targets.first.click(timeout=5_000)
            popup = popup_info.value
            popup.wait_for_load_state("domcontentloaded")
            popup.wait_for_timeout(1000)
            stores = parse_store_rows(popup.locator("body").inner_text())
            popup.close()
        except Exception:
            try:
                targets.first.click(timeout=5_000)
                page.wait_for_timeout(1000)
                stores = parse_store_rows(page.locator("body").inner_text())
            except Exception:
                pass

    return {**prize, "stores": stores}


def is_incomplete(item: dict[str, Any]) -> bool:
    item = normalize_round(item)
    first = item["prize"]["first"]
    second = item["prize"]["second"]
    return any([
        not valid_first_amount(first.get("perGameAmount")),
        not valid_first_winners(first.get("winnerCount")),
        not valid_second_winners(second.get("winnerCount")),
        not valid_total_sales(item["prize"].get("totalSalesAmount")),
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

    p = incoming.get("prize", {})
    candidates = [
        ("first", "perGameAmount", valid_first_amount),
        ("first", "winnerCount", valid_first_winners),
        ("second", "winnerCount", valid_second_winners),
    ]
    for group, key, validator in candidates:
        value = p.get(group, {}).get(key)
        if validator(value) and target["prize"][group].get(key) != as_int(value):
            target["prize"][group][key] = as_int(value)
            changed = True
    total = p.get("totalSalesAmount")
    if valid_total_sales(total) and target["prize"].get("totalSalesAmount") != as_int(total):
        target["prize"]["totalSalesAmount"] = as_int(total)
        changed = True
    if incoming.get("stores") and target.get("stores") != incoming["stores"]:
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

    # Clean all previously corrupted numbers before doing any new fetch.
    for item in results:
        if sanitize_prize(item):
            changed = True

    # Add newly published rounds using the lightweight official JSON endpoint.
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

    recent = [r for r in range(latest, max(0, latest - 20), -1) if r in by_round and is_incomplete(by_round[r])]
    older = [r for r in sorted(by_round, reverse=True) if r <= latest - 20 and is_incomplete(by_round[r])]
    cursor = as_int(data.get("service", {}).get("incompleteCursor")) or 0
    if older:
        cursor %= len(older)
        old_batch = (older[cursor:cursor + 2] + older[:max(0, cursor + 2 - len(older))])[:2]
    else:
        old_batch = []
    targets: list[int] = []
    for r in recent[:10] + old_batch:
        if r not in targets:
            targets.append(r)

    browser_errors: list[str] = []
    if targets:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(locale="ko-KR", timezone_id="Asia/Seoul", viewport={"width": 1440, "height": 1800})
            page = context.new_page()
            for round_no in targets:
                item = by_round[round_no]
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
                    print(f"[{round_no}] 검증 결과: 1등금={detail['firstPerGame']}, 1등수={detail['firstWinners']}, 2등수={detail['secondWinners']}, 판매액={detail['totalSales']}, 판매점={len(detail['stores'])}")
                except Exception as exc:
                    msg = f"[{round_no}] 회차별 상세조회 실패: {exc}"
                    print(msg)
                    browser_errors.append(msg)
            context.close()
            browser.close()

    results.sort(key=lambda x: int(x.get("round", 0)))
    data["results"] = results
    complete_count = sum(1 for x in results if not is_incomplete(x))
    service = data.setdefault("service", {})
    service.update({
        "collectorVersion": "2.7-round-verified-row-parser",
        "mode": "verified-round-submit-row-parser-with-sanity-checks",
        "recentPriorityCount": 20,
        "recentBatchSize": 10,
        "olderBatchSize": 2,
        "incompleteCursor": (cursor + len(old_batch)) if older else 0,
        "completedRoundCount": complete_count,
        "incompleteRoundCount": len(results) - complete_count,
        "lastRunAt": datetime.now(KST).isoformat(timespec="seconds"),
        "lastBrowserErrors": browser_errors[-10:],
    })
    save_data(data)
    print(f"완료: 최신 {data['latestRound']}회 / 완성 {complete_count}회 / 미완성 {len(results)-complete_count}회")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
