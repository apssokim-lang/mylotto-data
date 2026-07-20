from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "lotto_results.json"
UPDATE_PATH = ROOT / "scripts" / "update_lotto.py"

spec = importlib.util.spec_from_file_location("update_lotto", UPDATE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)

data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
module.validate_dataset(data)
service = data.get("service", {})
if service.get("thirdPartySourceUsed") is not False:
    raise ValueError("thirdPartySourceUsed는 false여야 합니다.")
print(f"JSON 검증 통과: 1회~{data['latestRound']}회, 총 {len(data['results'])}개")
