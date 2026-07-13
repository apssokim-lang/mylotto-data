#!/usr/bin/env python3
"""운영 JSON은 건드리지 않고 테스트용 1233회 JSON을 생성합니다."""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'data' / 'lotto_results.json'
TARGET = ROOT / 'data' / 'lotto_results_test.json'
KST = timezone(timedelta(hours=9))

payload = json.loads(SOURCE.read_text(encoding='utf-8'))
results = [row for row in payload.get('results', []) if int(row.get('round', 0)) != 1233]
results.append({
    'round': 1233,
    'date': '2026-07-18',
    'numbers': [1, 7, 14, 23, 32, 41],
    'bonus': 9,
    'firstPrizeStores': [
        {'name': '자동갱신 테스트 판매점 A', 'method': '자동', 'address': '서울특별시 테스트구 자동로 1'},
        {'name': '자동갱신 테스트 판매점 B', 'method': '수동', 'address': '광주광역시 테스트구 수동로 2'},
        {'name': '자동갱신 테스트 판매점 C', 'method': '반자동', 'address': '부산광역시 테스트구 반자동로 3'},
    ],
})
results.sort(key=lambda row: int(row['round']))
payload['schemaVersion'] = 2
payload['latestRound'] = 1233
payload['updatedAt'] = datetime.now(KST).isoformat(timespec='seconds')
payload['testData'] = True
payload['testNotice'] = '1233회는 자동갱신 연결 검증용 가상 데이터입니다.'
payload['results'] = results
TARGET.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(f'[완료] {TARGET.relative_to(ROOT)} 생성')
