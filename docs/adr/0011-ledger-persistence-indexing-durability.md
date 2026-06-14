# ADR 0011 — Evidence Ledger 영속화: latest-hash 캐시·내구성 쓰기

- **상태(Status):** Accepted — 부분 구현 (R2 캐시 + R4a 공통 헬퍼 구현, R4b/R4c 미구현)
- **날짜:** 2026-06-13
- **관련:** [ADR 0006](0006-final-blended-architecture.md) §6.1 · [code-review-2026-06-13](../code-review-2026-06-13.md)

## 1. 맥락 (Context)

L4 `EvidenceLedger`는 해시 체인 append-only JSONL, L0 memory/skills/agenda도 파일에 쓴다.

- **읽기(R2):** `append`마다 `latest_hash()`가 파일 전체 스캔 → N append = **O(N²)**.
  contract 범위 조회도 호출부가 `contract_id ==` 필터를 손으로 재구현해 출처 오염 footgun
  반복(codex r3382219479).
- **쓰기(R4):** 모두 plain `open("a")`/`write_text`. lock·fsync·atomic rename 없어 동시
  실행·크래시·부분 write에서 체인/state가 깨질 수 있다.

## 2. 결정 (Decision)

### 2.1 R2 — size-guarded latest-hash 캐시 + contract 접근자 (구현됨, `ledger.py`)

- `latest_hash`만 캐시하고 **파일 크기로 무효화**. append 전체 스캔을 없애 단일 writer
  append를 **amortized O(1)**로. 같은 파일의 다른 인스턴스가 append하면 크기 변화가 캐시를
  무효화해 tail을 재읽어 stale·체인 깨짐 방지(codex r3407872680). append-only라 크기 단조 증가.
- `events()`/`find()`는 호출마다 디스크 재파싱 → 공유 가변 캐시 없음(반환 payload를 mutate해도
  원장 무오염, codex r3407872681).
- `events_for_contract`/`latest_hash_for_contract` 접근자가 손필터를 대체해 footgun 구조 제거.

### 2.2 R4 — durable write path (R4a 구현, R4b/R4c 미구현)

ledger/memory/skills/agenda 쓰기를 공통 헬퍼로 통일. **R4a(구현, `durable.py`):**
read/append/rewrite 중복을 `DurableJsonl`+`write_text_durably`로 추출 — 순수 리팩터링
(출력 바이트·읽기 의미 불변, fsync/lock 미추가). **R4b:** temp write + `os.replace` +
`fsync` + torn-line 복구. **R4c:** `flock`(cross-process 직렬화). 전체 인덱싱도 R4 후속.

## 3. 대안 (Alternatives)

- **인메모리 전체 이벤트 인덱스:** O(k) 조회는 좋으나 같은 파일 다중 인스턴스에서 stale·가변
  공유 버그(codex 2건) 유발. append 비용만 캐시로 해소, 전체 인덱싱은 R4.
- **영속 오프셋 인덱스(.idx):** 거대 원장엔 유리하나 현 규모엔 과설계+R4와 결합돼 복잡.
- **SQLite:** 사람이 읽는 JSONL·해시 체인 단순성 상실 → 배제.

## 4. 영향 (Consequences)

- **긍정:** append O(N²)→amortized O(1), footgun 구조 제거, 형제 인스턴스 정확성 유지.
  JSONL·해시 체인·기존 시그니처 불변.
- **비용:** `events()` 계열은 호출마다 전체 read(기존 동일, 무회귀). 전체 인덱싱은 R4 후속.
- **중립:** size 신호는 append-only 전제. cross-process 동시성은 R4c lock 전까지 미보증.
