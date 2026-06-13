# Code Review — 2026-06-13 Honest Implementation Status

이 문서는 2026-06-13 코드리뷰 기준으로, ADR 0001~0008의 구현 슬라이스가 실제 코드에
얼마나 연결되어 있는지 재점검한 결과다. IDE에서 언급된 `codereview20260613.html` 원본은
저장소 안에 없으므로, 현재 소스와 테스트를 기준으로 검증 가능한 상태만 기록한다.

## 결론

ADR 0001~0008의 핵심 프리미티브는 대부분 머지되어 있다. 다만 “서비스로 안전하게 굴러가는
완전 폐쇄 루프”라고 표현하기에는 아직 과장이 있다. 현재 상태는 **프리미티브 구현 완료 +
엔진의 얇은 happy-path 배선 완료 + 운영 안전성/진화 루프 read-path 미완성**으로 보는 것이
정직하다.

## 반영됨 — 머지 완료

- `TaskContract` / `GoalContract` / `non_goals` / risk 기반 escalation 파생 뷰.
- 집행 게이트 함수: `evaluate_plan`, `can_execute_action`, `complete`, `check_tool_allowed`,
  `check_non_goal`, `should_stop`.
- `ContractHarness.bind`가 실행 전 계약을 만들고 ledger에 `GOAL_CONTRACT`를 남김.
- bounded loop: `run_bounded_loop`가 `should_stop`과 `complete`를 소비.
- Review 자동화: `run_review`가 verifier를 호출·기록·집계.
- Reflect: `reflect_on_contract`가 contract-scoped ledger trail을 retrospective/failures로 증류.
- TypedMemory: 6개 memory type, assumption→decision 승급 게이트, failure scope/TTL metadata 저장.
- SkillStore: skill candidate distill, n-of-m outcome tracking, dedup/HITL promotion gate.
- Agenda: pending work backlog persistence.
- CausalityEngine: Agenda → Dispatch → Harness → Loop → Review → Reflect → Skill candidate의
  happy path를 `run_task` / `run_next`로 연결.
- Context-economy layout, CI, README 한/영 문서, SVG 다이어그램.

## 미반영 / 설계 약속 대비 갭

| 우선순위 | 갭 | 현재 사실 | 왜 중요한가 | 권장 다음 작업 |
|---|---|---|---|---|
| P0 | 집행 게이트의 엔진 배선 | `CausalityEngine.run_task`는 `run_bounded_loop`를 통해 `should_stop`/`complete`는 간접 호출하지만, `evaluate_plan`, `can_execute_action`, `check_tool_allowed`, `check_non_goal`은 실행 경로에서 호출하지 않는다. | contract가 선언한 allowed tools, non-goals, high-risk plan/action boundary가 실제 작업 콜백 앞에서 강제되지 않는다. | `work` 콜백을 직접 호출하지 말고 `ExecutionAdapter`/`ToolRunner`를 도입해 모든 tool/action 전에 tool, non-goal, plan/action gate를 통과시킨다. |
| P0 | failures → non_goals 자동 주입 없음 | Reflect는 failures memory를 쓰지만 ContractHarness가 이후 실행에서 scoped failures를 읽어 non-goals/guardrails로 변환하지 않는다. | 진화 루프가 write-only라 같은 실패가 다음 계약의 경계로 환류되지 않는다. | `GuardrailInjector`를 만들어 scope/TTL/confidence가 유효한 failures만 contract bind 전에 제안 non-goals로 주입하거나 HITL 승인 대상으로 올린다. |
| P0 | TTL 만료·회수·읽기시 검증 없음 | `ttl_days`는 metadata로 저장되지만 `TypedMemory.entries()`는 만료 여부를 필터링하지 않는다. 회수/revoke 모델도 없다. | stale assumption/failure가 계속 guardrail처럼 작동하거나, 반대로 TTL 약속이 문서상 거버넌스에만 머문다. | `MemoryEntry`에 expiry 계산, `entries(active_only=True)`, revoke tombstone, sweep 명령을 추가한다. |
| P1 | earned skill 디스패치 재사용 없음 | `SkillStore.promoted()`는 읽을 수 있지만 AgentHarness/CausalityEngine이 promoted skill을 라우팅·계획·실행 context에 자동 주입하지 않는다. | Skill update가 저장소 승급에서 멈추며, 다음 작업 비용/품질 개선으로 이어지지 않는다. | dispatch 후 task type/objective 기반 promoted skill retrieval을 추가하고, authored skill과 earned skill 우선순위를 명시한다. |
| P1 | 락·fsync·원자적 rename 없음 | Ledger, memory, skill, agenda 저장소가 plain append/write_text를 사용한다. | 동시 실행, 프로세스 크래시, 부분 write 상황에서 hash chain/state 파일이 깨질 수 있다. | 공통 `DurableJsonlStore`/`DurableJsonStore`를 만들고 file lock, temp-file atomic rename, fsync, corruption recovery test를 추가한다. |
| P2 | 원장 인덱싱 없음 | `EvidenceLedger.events()`가 JSONL 전체를 읽고 `find()`가 메모리에서 필터링한다. contract-scoped latest hash helper도 없다. | ledger가 커지면 run/review/reflect가 느려지고, contract-scoped provenance 실수를 반복할 수 있다. | `events_for_contract`, `latest_hash_for_contract`, event-type/contract index를 추가한다. |

## 우선순위 제안

1. **P0 — 안전한 실행 강제:** 엔진에 plan/tool/action/non-goal gate를 실제로 배선한다.
2. **P0 — 진화 루프 read-path:** failures memory를 다음 Contract Harness 입력으로 안전하게 환류한다.
3. **P0 — TTL 거버넌스 실효화:** 읽기 시 만료 필터링과 회수 모델을 먼저 구현한다.
4. **P1 — 내구성 레이어:** ledger/memory/skills/agenda의 write path를 공통 durable store로 통일한다.
5. **P1 — earned skill 재사용:** promoted skill을 dispatch/context selection에 연결한다.
6. **P2 — ledger indexing:** 성능과 contract-scoped provenance 안전성을 강화한다.

## 문서 표현 가이드

- “전 구현 완료” 대신 **“프리미티브와 happy-path 엔진 배선은 구현됨”**이라고 표현한다.
- “자기개선 루프가 닫힘” 대신 **“Reflect/Skill candidate write-path는 닫혔지만,
  failures guardrail 주입과 earned skill 자동 재사용 read-path는 미완성”**이라고 표현한다.
- “집행 게이트 구현됨”이라고만 쓰지 말고 **“함수는 구현됐으나 run_task의 work 실행 전 강제
  배선은 미완성”**이라고 병기한다.
