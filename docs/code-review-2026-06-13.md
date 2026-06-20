# Code Review — 2026-06-13 (정직한 구현 상태)

> 최종 갱신 2026-06-20: #15~#18 머지 반영. 시각화: `docs/status/roadmap.html`.

ADR 0001~0011 프리미티브 + `CausalityEngine` happy-path 배선 머지됨(157 tests). 단
**실행-강제·진화 read-path가 미완** — write-path는 닫혔고 guardrail 주입·skill 재사용이 빠짐. 상세 로컬본: `docs/_review/`.

## 남은 갭 (미반영, 우선순위)

| P | 갭 | 현재 사실 | 다음 작업 |
|---|---|---|---|
| P0 | 집행 게이트 미배선 | `engine.run_task`의 step이 `work`→review만 호출. `evaluate_plan`/`can_execute_action`/`check_tool_allowed`/`check_non_goal` 미강제(loop는 should_stop·complete만 간접 사용) | action 전 gate를 통과시키는 ExecutionAdapter로 `work` 감싸기 |
| P0 | failures→non_goals 환류 + TTL 집행 | Reflect는 `record_failure`로 기록만, bind는 호출자 non_goals만 사용(read-path 없음) | bind 전 `entries("failures", active_only=True)`의 scoped 유효 failure를 non_goals로 주입(HITL) |
| P1 | earned skill 재사용 | `SkillStore.promoted()` 읽기만 가능, dispatch/context 자동 주입 없음 | task type/objective로 promoted 회수, authored>earned 우선 |
| R4f | 전체 read-path 인덱싱 | R2 캐시로 append O(1) 해결, `events()` 조회는 매번 재파싱 | 규모 필요시 오프셋 인덱스(후순위) |

## ✅ 반영됨

- **TTL 메커니즘(#15):** `is_expired`/`entries(active_only)`/`revoke`/`sweep` + scoped `record_failure`. (루프 집행 연결은 P0-B.)
- **R2 ledger 인덱싱(#11):** size-guarded latest-hash 캐시 + `events_for_contract`/`latest_hash_for_contract` 접근자.
- **R4 durable write(#13/#14/#17):** 공통 `durable.py` — atomic rewrite, append fsync+torn 복구, `flock` writer 직렬화.
- **프로세스:** Codex 리뷰 autofix Action(#16), PR ~5분 bounded watch 규칙(#18), 리뷰예산 ADR0009, 문서예산 ADR0010.

## 순서
P0(게이트 배선 → failures→non_goals/TTL 집행) → P1(skill 재사용) → R4f(후순위).

## 문서 표현
- "전 구현 완료" 대신 **"프리미티브+happy-path 배선 구현됨"**.
- "루프 닫힘" 대신 **"write-path 닫힘, guardrail 주입·skill 재사용 read-path 미완"**.
