# ADR 0011 — Evidence Ledger 영속화: 읽기 캐시·내구성 쓰기

- **상태(Status):** Accepted — 구현 (R2 캐시 + R4 durable + R4f read-path 캐시)
- **날짜:** 2026-06-13 (R4f 2026-06-20)
- **관련:** [ADR 0006](0006-final-blended-architecture.md) §6.1

## 1. 맥락 (Context)

L4 `EvidenceLedger`는 해시 체인 append-only JSONL, L0 memory/skills/agenda도 파일에 쓴다.

- **읽기(R2/R4f):** `append`마다 `latest_hash()` 전체 스캔(N append=**O(N²)**),
  `events()`/`find()`/`verify_chain()`도 호출마다 전체 재읽기+재파싱. contract 조회는
  손으로 `contract_id ==` 필터 재구현해 footgun 반복(codex r3382219479).
- **쓰기(R4):** 모두 plain `open("a")`/`write_text`. lock·fsync·atomic rename 없어 동시
  실행·크래시·부분 write에서 체인/state가 깨질 수 있다.

## 2. 결정 (Decision)

### 2.1 R2 — size-guarded latest-hash 캐시 + contract 접근자 (`ledger.py`)

`latest_hash`를 **파일 크기로 무효화**하는 캐시 → append 전체 스캔 제거, **amortized O(1)**.
형제 인스턴스 append는 크기 변화로 무효화(codex r3407872680). append-only라 크기 단조 증가.
`events_for_contract`/`latest_hash_for_contract` 접근자가 손필터를 대체해 footgun 제거.

### 2.2 R4 — durable write path (`durable.py`)

4개 저장소 쓰기를 공통 헬퍼로 통일. **R4a:** read/append/rewrite 추출. **R4b:** rewrite=
temp+`fsync`+`os.replace`(원자), append=`fsync`+torn tail 절단, read=torn 마지막 줄 drop.
**R4c:** `<path>.lock` `flock`로 writer 직렬화; ledger는 read-latest+append를 락 내 수행해
체인 fork 방지.

### 2.3 R4f — size-guarded parsed-events 캐시 (`ledger.py`)

크기 신호를 **파싱된 events**로 확장: 크기 불변이면 재읽기·재파싱 없이 캐시 재사용, append는
warm 유지. §3 반려 사유 2건 해소 — staleness는 size guard, 공유 가변 footgun(codex
r3407872681)은 `_isolate()` 사본 반환이 막고 read-only 내부 스캔만 캐시를 직접 읽는다.

## 3. 대안 (Alternatives)

- **인메모리 전체 인덱스:** 본래 stale·가변 공유(codex 2건)로 반려, R4f가 size guard+사본으로 해소해 채택.
- **영속 .idx 인덱스:** 거대 원장엔 유리하나 현 규모 과설계 → 보류.
- **SQLite:** JSONL·해시 체인 단순성 상실 → 배제.

## 4. 영향 (Consequences)

- **긍정:** append O(N²)→O(1), read-path 재읽기·재파싱 제거(크기 불변 시), footgun 제거.
  JSONL·해시 체인·시그니처 불변.
- **비용:** 캐시 hit 시 `_isolate()` 사본 비용만. cross-process는 POSIX `flock` 한정.
- **중립:** size 신호는 append-only 전제.
