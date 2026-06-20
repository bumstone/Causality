# Code Review — 2026-06-13 (정직한 구현 상태)

> 최종 갱신 2026-06-20: #11~#22 + R4f 반영. 시각화: `docs/status/roadmap.html`.

ADR 0001~0011 프리미티브 + `CausalityEngine` 배선 머지됨(194 tests). **P0 실행-강제·환류 + P1 skill 재사용 + R4f read-path 캐시까지 닫힘** — 코어 갭 없음. 상세 로컬본: `docs/_review/`.

## 남은 갭

코어 read-path 갭 없음. 영속 오프셋 인덱스(.idx)는 현 규모 과설계로 보류(ADR 0011 §3).

## ✅ 반영됨

- **P1 earned skill 재사용(이 PR):** `SkillStore.recall(objective, authored=…)`가 objective 관련도로 promoted(earned) 회수, **authored>earned 우선**·재현성 tiebreak. `run_task`가 dispatch 시 회수해 `TaskRun.recalled_skills`로 표면화 + `ExecutionAdapter.recalled_skills`로 work에 주입.
- **P0-A 집행 게이트(#19):** `run_task`가 루프 전 `evaluate_plan`(+`approve_plan` HITL 훅), action마다 `ExecutionAdapter`로 `check_non_goal`/`check_tool_allowed`/`can_execute_action` 강제.
- **P0-B failures→non_goals + TTL 집행(#20):** bind 전 `entries("failures", active_only=True)` scoped 유효 failure를 `confirm_guardrails` 승인분만 non_goals로 주입, `failure_scope`/`failure_ttl_days`로 환류·만료.
- **R4f read-path 캐시(이 PR):** size-guarded parsed-events 캐시로 `events()`/`find()`/`verify_chain()` 재읽기·재파싱 제거(크기 불변 시), `_isolate()` 사본으로 가변 공유 방지.
- **TTL(#15) · R2 인덱싱(#11) · R4 durable(#13/#14/#17).**
- **프로세스:** Codex autofix Action(#16), PR ~5분 bounded watch(#18), 예산 ADR0009/0010.

## 순서
✅P0(게이트 → failures→non_goals/TTL) → ✅P1(skill 재사용) → ✅R4f(read-path 캐시) 완료.

## 문서 표현
- "전 구현 완료" 대신 **"프리미티브+배선 구현됨"**.
- 이제 **"P0 실행-강제·환류 + P1 skill 재사용 + R4f read-path 캐시 닫힘; 코어 갭 없음"**.
