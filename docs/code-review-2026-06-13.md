# Code Review — 2026-06-13 (정직한 구현 상태)

> 최종 갱신 2026-06-20: #11~#19 + P0-B 반영. 시각화: `docs/status/roadmap.html`.

ADR 0001~0011 프리미티브 + `CausalityEngine` 배선 머지됨(176 tests). **P0 실행-강제·환류 read-path 닫힘** — 남은 건 P1 skill 재사용과 R4f 인덱싱(후순위). 상세 로컬본: `docs/_review/`.

## 남은 갭 (미반영)

| P | 갭 | 현재 사실 | 다음 작업 |
|---|---|---|---|
| P1 | earned skill 재사용 | `SkillStore.promoted()` 읽기만, dispatch/context 자동 주입 없음 | task type/objective로 promoted 회수, authored>earned 우선 |
| R4f | 전체 read-path 인덱싱 | R2 캐시로 append O(1), `events()` 조회는 매번 재파싱 | 규모 필요시 오프셋 인덱스(후순위) |

## ✅ 반영됨

- **P0-A 집행 게이트(#19):** `run_task`가 루프 전 `evaluate_plan`(+`approve_plan` HITL 훅), action마다 `ExecutionAdapter`로 `check_non_goal`/`check_tool_allowed`/`can_execute_action` 강제. 차단 시 STOP/ESCALATE 종료.
- **P0-B failures→non_goals + TTL 집행(이 PR):** bind 전 `entries("failures", active_only=True)`의 scoped 유효 failure를 `confirm_guardrails` HITL 승인분만 non_goals로 주입. `failure_scope`로 run 간 환류, 만료분은 미회수(TTL 루프 집행).
- **TTL 메커니즘(#15):** `is_expired`/`entries(active_only)`/`revoke`/`sweep`.
- **R2 인덱싱(#11) · R4 durable(#13/#14/#17):** latest-hash 캐시 + atomic·fsync·flock.
- **프로세스:** Codex autofix Action(#16), PR ~5분 bounded watch(#18), 예산 ADR0009/0010.

## 순서
✅P0(게이트 → failures→non_goals/TTL) 완료 → **P1(skill 재사용)** → R4f(후순위).

## 문서 표현
- "전 구현 완료" 대신 **"프리미티브+배선 구현됨"**.
- 이제 **"P0 실행-강제·환류 read-path 닫힘; P1 skill 재사용·R4f 인덱싱 잔여"**.
