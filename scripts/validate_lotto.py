from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / "data" / "lotto_results.json"
data = json.loads(path.read_text(encoding="utf-8"))
results = data.get("results")
assert isinstance(results, list) and results, "results가 비었습니다."
rounds = [int(x["round"]) for x in results]
assert len(rounds) == len(set(rounds)), "중복 회차가 있습니다."
by_round = {int(x["round"]): x for x in results}
latest = int(data["latestRound"])
assert latest == max(rounds), "latestRound가 실제 최신 회차와 다릅니다."
for round_no, item in by_round.items():
    winning = item.get("winning") or {}
    nums = [int(v) for v in winning.get("numbers", [])]
    bonus = int(winning.get("bonus"))
    assert len(nums) == 6 and len(set(nums)) == 6, f"{round_no}회 번호 오류"
    assert all(1 <= v <= 45 for v in nums), f"{round_no}회 번호 범위 오류"
    assert 1 <= bonus <= 45 and bonus not in nums, f"{round_no}회 보너스 오류"
# 최근 20회에서 연속 회차가 완전히 같은 7개 숫자를 가지면 배포 차단
floor = max(1, latest - 19)
for r in range(floor, latest):
    if r in by_round and r + 1 in by_round:
        a = by_round[r]["winning"]
        b = by_round[r + 1]["winning"]
        assert (a["numbers"], a["bonus"]) != (b["numbers"], b["bonus"]), f"{r}/{r+1}회 동일번호 오염 의심"
service = data.get("service", {})
assert service.get("collectorVersion") == "6.0-official-immutable-round-engine"
assert service.get("sourcePolicy") == "official-dhlottery-only"
assert service.get("thirdPartySourceUsed") is False
assert service.get("immutableRoundMerge") is True
print(f"JSON 최종 검증 통과: 1~{latest}회, 총 {len(results)}개")
