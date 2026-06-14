# Code Review — 2026-06-13 (정직한 구현 상태)

ADR 0001~0010 프리미티브 + `CausalityEngine` happy-path 배선은 머지됨(125 tests). 단
**"완전 폐쇄 운영 루프"는 과장** — 실행 강제·진화 read-path가 미완. 상세 로컬본: `docs/_review/`.

## 미반영 갭 (우선순위)

| P | 갭 | 현재 사실 | 다음 작업 |
|---|---|---|---|
| P0 | 집행 게이트 미배선 | `run_task`가 `work` 앞에 `evaluate_plan`/`can_execute_action`/`check_tool_allowed`/`check_non_goal` 미강제(should_stop·complete만 간접) | `work` 대신 ExecutionAdapter, action 전 gate 통과 |
| P0 | failures→non_goals 환류 없음 | Reflect는 failures 기록만, ContractHarness가 안 읽음(write-only) | bind 전 scoped/TTL 유효 failure를 non_goals로 주입(HITL) |
| P0 | TTL 루프 미집행 | **메커니즘 구현**: `MemoryEntry.is_expired`(created_at+ttl_days) + `entries(active_only)` 만료 필터 + `revoke(entry_id)`/`sweep`(durable rewrite). 단 run-loop·failures→non_goals가 active 필터를 아직 호출 안 함 | failures→non_goals 주입 시 `active_only` 사용해 루프 집행 완성 |
| P1 | earned skill 재사용 없음 | `promoted()` 읽기 가능하나 dispatch/context 자동 주입 없음 | task type/objective로 promoted 회수, authored>earned 우선 |
| P1 | 락·fsync·원자 rename 없음 | **R4a 완료**: 4개 저장소가 공통 `durable.py`(`DurableJsonl`+`write_text_durably`) 사용. fsync/atomic/flock은 미추가 | R4b(temp rename+fsync+torn-line)+R4c(flock)+손상복구 테스트 (ADR 0011 §2.2) |
| ✅ | ledger 인덱싱 | **해결(ADR 0011 §2.1)**: size-guarded latest-hash 캐시(append O(N²)→amortized O(1)) + `events_for_contract`/`latest_hash_for_contract` 접근자, reflect·skills가 사용 | 전체 read-path 인덱싱은 R4 |

## 순서
P0(게이트 배선 → failures read-path → TTL) → P1(durable store=ADR 0011 R4 → skill 재사용). ✅R2(ledger indexing) 완료(ADR 0011 §2.1).

## 문서 표현
- "전 구현 완료" 대신 **"프리미티브+happy-path 배선 구현됨"**.
- "루프 닫힘" 대신 **"write-path 닫힘, guardrail 주입·skill 재사용 read-path 미완"**.
