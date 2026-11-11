# 근거 import 형식 (B05.2, B08.2 · 보완 B)

뉴스·거시/실적 일정은 아직 API 공급자가 연결되지 않았다. 대신 **검증 가능한 파일 import**만 받는다.
파일이 있다는 사실만으로는 커버리지가 인정되지 않는다. 각 데이터 파일 옆에 manifest가 있어야 하며,
그 manifest가 as_of 시점에 이미 가용했고 내용 해시가 일치할 때만 사용된다.

경로: `{data_root}/imports/calendar/<name>.csv` + `<name>.csv.manifest.json`
      `{data_root}/imports/news/<name>.jsonl` + `<name>.jsonl.manifest.json`

## manifest (공통)
```json
{
  "source_id": "BLS/Fed calendar export (manually verified)",
  "source_url": "https://…",
  "fetched_at": "2026-01-01T00:00:00Z",
  "available_at": "2026-01-01T00:05:00Z",
  "coverage_start": "2026-01-01T00:00:00Z",
  "coverage_end": "2026-01-31T23:59:59Z",
  "revision": 1,
  "content_sha256": "<sha256 of the data file>",
  "verified_by": "<who/what verified completeness>",
  "notes": ""
}
```
| 필드 | 규칙 |
|---|---|
| `source_id`, `fetched_at`, `coverage_start`, `coverage_end`, `content_sha256` | 필수 |
| `available_at` | 이 manifest(그리고 그 버전의 데이터)를 실행 환경이 사용할 수 있게 된 시각. 없으면 `fetched_at`. **as_of보다 늦으면 그 as_of에서는 사용하지 않는다** |
| `coverage_start` < `coverage_end` | 이 구간의 일정/뉴스를 빠짐없이 수집·검증했다는 주장. 모든 시각은 timezone 포함 ISO 8601 |
| `content_sha256` | 데이터 파일 SHA-256. 불일치 시 사용 안 함 |

판정
- 일정(MACRO_CALENDAR): as_of를 포함하는 검증 구간(겹치거나 이어지는 manifest는 합침)이 있으면 AVAILABLE이고,
  `calendar_coverage_until = 그 구간의 끝`. 위험 규칙 R4는 이 값이 예측 기간 끝 이상일 때만 "사건 없음"을 PASS로 본다.
  구간이 as_of를 포함하지 않으면 STALE, manifest가 없거나 무효면 MISSING.
- 뉴스(NEWS): 검증 구간이 `as_of − 1시간` 이후까지 이어져야 AVAILABLE, 아니면 STALE. 뉴스 구간은 수집 시각을 넘을 수 없다.
- 확인된 **빈 일정**(구간 안에 사건 0건)은 정상 PASS 사례다. 확인되지 않은 일정은 "사건 없음"이 아니다.

## calendar CSV 열
`event_id,name,source_id,scheduled_time,importance,revision,available_at,actual_value,actual_available_at`
- 수정된 일정·발표치는 같은 `event_id`의 새 `revision` 행으로 추가한다(덮어쓰지 않음). as_of에는 `available_at <= as_of`인 최신 revision만 쓴다.
- `actual_value`는 `actual_available_at <= as_of`일 때만 노출된다.

## news JSONL 한 줄
`{"evidence_id","source_id","source_url","title","excerpt","published_at","received_at","available_at","verification_status"}`
- `verification_status`: VERIFIED / REPORTED / UNVERIFIED / RETRACTED. 소셜·루머는 UNVERIFIED로 둔다(사실로 승격하지 않음).
