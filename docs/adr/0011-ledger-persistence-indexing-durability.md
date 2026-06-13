# ADR 0011 — Evidence Ledger 영속화: 인덱스·내구성 쓰기

- **상태(Status):** Accepted — 부분 구현 (R2 인덱싱 구현, R4 durable-write 결정·미구현)
- **날짜:** 2026-06-13
- **관련:** [ADR 0006](0006-final-blended-architecture.md) §6.1 · [code-review-2026-06-13](../code-review-2026-06-13.md)

## 1. 맥락 (Context)

L4 `EvidenceLedger`는 해시 체인 append-only JSONL, L0 memory/skills/agenda도 파일에 쓴다.
두 결함:

- **읽기(R2):** `append`마다 `latest_hash()`가 파일 전체 스캔 → N append = **O(N²)**.
  `events()`·contract 범위 조회(reflect/gates/skills)도 매번 전체 read+필터. 호출부가
  `contract_id ==` 필터를 손으로 재구현해 "전역 latest hash가 다른 계약 것" 출처 오염
  footgun 반복(codex r3382219479).
- **쓰기(R4):** 모두 plain `open("a")`/`write_text`. lock·fsync·atomic rename 없어 동시
  실행·크래시·부분 write에서 체인/state가 깨질 수 있다.

## 2. 결정 (Decision)

### 2.1 R2 — lazy 인메모리 인덱스 (구현됨, `ledger.py`)

첫 접근 시 1회 로드, 이후 append마다 증분 유지.

- `_latest_hash` 캐시 → append 해시 체이닝 **O(1)**.
- `_by_contract` 인덱스 → `events_for_contract`/`latest_hash_for_contract` O(k) 제공.
  reflect·skills가 이 접근자를 쓰게 교체해 footgun 제거.
- `events()`는 복사본 반환(캐시 보호).
- **가정:** 단일 writer. 다중 writer 내구성은 §2.2.

### 2.2 R4 — durable write path (결정, 미구현)

ledger/memory/skills/agenda 쓰기를 공통 헬퍼로 통일: **R4a** read/append 중복 제거,
**R4b** temp write + `os.replace` + `fsync`(파일·디렉터리) + torn-line 복구, **R4c**
`flock`로 writer 직렬화. R4c가 §2.1 캐시의 단일-writer 전제를 lock으로 보증한다.

## 3. 대안 (Alternatives)

- **영속 오프셋 인덱스(.idx):** 거대 원장엔 유리하나 현재 규모엔 과설계+R4와 결합돼 복잡.
  인메모리로 충분, 후속 여지.
- **SQLite:** 사람이 읽는 JSONL·해시 체인 단순성 상실 → 배제.
- **R4 선행:** 더 근본적이나 O(N²)가 즉시 비용·저위험이라 R2 우선.

## 4. 영향 (Consequences)

- **긍정:** append O(N²)→O(1), contract 조회 O(k), 출처 footgun 구조 제거. JSONL·해시
  체인·가독성 유지.
- **비용:** 인덱스가 전체 이벤트를 메모리 보관(현재 OK; 거대 원장은 오프셋 인덱스 후속).
  캐시 권위는 단일 writer 전제 — R4c 전까지 동일 파일 다중 인스턴스 동시 append 금지(기존도
  비안전, 무회귀).
- **중립:** 공개 API 추가만, 기존 시그니처 불변.
