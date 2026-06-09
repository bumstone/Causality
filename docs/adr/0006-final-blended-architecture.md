# ADR 0006 — 최종 혼합 아키텍처: 5계층 분리와 충돌·중복·최적화

- **상태(Status):** Proposed
- **날짜:** 2026-06-09
- **종합 대상:** [ADR 0001](0001-task-contract-as-binding-rules.md) · [0002](0002-three-layer-control-stack.md) · [0003](0003-contract-harness.md) · [0004](0004-agent-harness-task-routing.md) · [0005](0005-identity-memory-skill-substrate.md)

## 1. 동기 (Context)

ADR 0001~0005가 도입한 개념들(Task Contract, 3계층 실행 제어, Contract Harness,
Agent Harness, 정체성·기억·스킬 기반층)은 **그대로 쌓으면 라우터·정지조건·기억이
중복**된다. 이 ADR은 이들을 **하나의 계층 구조로 분리**하고, 충돌·중복·최적화를
명시적으로 해소한다.

## 2. 결정 (Decision) — 5계층 분리

각 계층은 **단일 책임**을 갖고, 인접 계층과만 통신한다. 위→아래는 *제어 흐름*,
아래→위는 *진화(환류) 흐름*이다.

```text
┌──────────────────────────────────────────────────────────────────────────┐
│ L0  IDENTITY & MEMORY SUBSTRATE   (지속·cross-session)        [Synergy 차용] │
│   · Agent Identity : 역할별 PermissionContract + scoped memory view         │
│   · Typed Memory   : {retro, work-log, decision, failure→guardrail}         │
│   · Agenda         : 장기 목표 + 대기 작업 백로그                            │
│   · Skill Library  : authored(gstack/Superpowers/workflows) + earned        │
└──────────────────────────────────────────────────────────────────────────┘
      │ agenda에서 다음 작업 선택 / scoped memory·skill 주입        ▲ distill(진화)
      ▼                                                            │
┌──────────────────────────────────────────────────────────────────────────┐
│ L1  DISPATCH — Agent Harness   (작업 유형 라우터)                    [신규]  │
│   planning→gstack office-hours+ceo-review · impl→Superpowers TDD+debug       │
│   long-run→Contract Harness+limited Ouroboros · release→gstack ship+QA       │
└──────────────────────────────────────────────────────────────────────────┘
      │ 선택된 플레이북 1개 + identity set
      ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ L2  CONTRACT HARNESS   (실행 직전 구속 envelope)            [ADR 0001·0003] │
│   freeze TaskContract = {Objective, Non-goals, Allowed tools,               │
│                          Verification, Stop condition, Escalation}          │
└──────────────────────────────────────────────────────────────────────────┘
      │ frozen contract
      ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ L3  EXECUTION CONTROL   (3계층 + 경계 게이트)                    [ADR 0002] │
│   Stage Designer ─[evaluate_plan]→ Planner ─[evaluate_plan]→ Executor        │
│                   ─[can_execute_action / complete]→                         │
│        ▲__________ REPAIR (limited Ouroboros loop · Stop로 bounded) _________│
└──────────────────────────────────────────────────────────────────────────┘
      │ 모든 이벤트
      ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ L4  EVIDENCE & AUDIT — EvidenceLedger   (append-only · hash-chained) [기존] │
│   → success/latency/cost 계측(ADR 0002) → L0로 증류 환류                    │
└──────────────────────────────────────────────────────────────────────────┘
```

### 2.1 진화(환류) 루프

L4(ledger) → **증류** → L0(typed memory + rewarded trajectory). 그 결과가:

- 실패 사례 → **guardrail** → L2 Contract Harness의 non-goals 자동 주입
- 성공 절차 → **earned skill**(HITL 승급) → L0 Skill Library → L1/L3 재사용

이것이 Synergy의 "경험 기반 학습"을 **협업·사회 기능 없이** 단일 프로젝트에 구현한
형태다.

## 3. 충돌 해소 (Conflicts)

| # | 충돌 | 해소 |
|---|---|---|
| C1 | 라우터 3중(agent-rules intent / Agent Harness / Stage Designer) | L1=**무엇을** 실행할지, L3 Stage Designer=**어떻게 단계화**할지로 고도 분리. agent-rules intent 라우팅은 L1로 흡수(ADR 0004) |
| C2 | 정지 조건 다원화(Contract Harness / TaskContract.stop / stopping_policy / "limited loop" / Magentic 정체) | **단일 출처** = `stopping_policy`(`contracts.py:142`), `should_stop` 게이트가 집행(ADR 0001). "limited Ouroboros loop"=낮은 `max_iterations` 설정값 |
| C3 | 기억 vs ledger | L4 ledger=raw 원천, L0 typed memory=증류 파생. 증류 게이트=`build_session_bootstrap` 필터(`workflows.py:118`) |
| C4 | 스킬 폭증(gstack 23 + Superpowers + workflows + earned) | **2계층화**: authored(고정) vs earned(승급 필요). L1이 작업당 1개 번들만 호출(ADR 0004) |
| C5 | 정체성 vs 권한 모델 | Agent Identity는 신규 모델이 아니라 역할별 `PermissionContract` 스코핑(ADR 0005) |
| C6 | 아키텍처 블렌딩 충돌 | L1이 작업 유형별로 **하나만** 선택 → 동시 적용 금지 |

## 4. 중복 제거 (Duplication)

| 중복 | Before | After |
|---|---|---|
| 라우팅 | agent-rules + Stage Designer (2) | L1 디스패치 1개 + L3 단계화(위임) |
| 정지 조건 | 4~5곳 | `stopping_policy` 1곳 + `should_stop` 1게이트 |
| 로그/기억 | ledger + (제안)별도 memory | ledger(raw) → 증류 → typed memory(파생) |
| 계약 데이터 | Task Contract / Contract Harness 각각 | ADR 0001 데이터 1개를 ADR 0003 절차가 *생산* |
| 게이트 | 신규 게이트 다수 우려 | 기존 3게이트(evaluate_plan/can_execute_action/complete) 재사용 + 집행 메서드만 추가 |

## 5. 최적화 (Optimization)

- **컨텍스트 경제:** 각 identity는 *scoped memory view*만 받는다(전체 이력 X). ReAct식
  컨텍스트 팽창을 회피하고 `session-bootstrap` 필터를 계층화한 것(`workflows.py:108`).
- **비용 체감:** earned skill 재사용으로 재계획 비용이 시간이 지날수록 감소(진화 루프).
- **지연(latency):** L1이 작업 유형별 최소 플레이북만 실행. 사소한 작업은 우회
  (`agent-rules.md:28`). 꼬리 지연(p95/p99) 관리는 ADR 0002 평가축으로 모니터링.
- **과설계 회피:** 신규 런타임을 만들지 않는다. 실제 신규 산출물은
  ① `non_goals` 필드 ② typed memory·agenda 스토어 ③ earned-skill 승급 게이트
  ④ 집행형 게이트 메서드 ⑤ L1 디스패처 — 나머지는 기존 ledger/permission/verifier/
  gate **재사용**.

### 5.1 신규 vs 재사용 요약

| 구분 | 항목 |
|---|---|
| **신규** | `non_goals` 필드 · Typed Memory 스토어 · Agenda 스토어 · earned-skill 승급 게이트 · L1 Agent Harness 디스패처 · 게이트 집행 메서드 3종 |
| **재사용** | EvidenceLedger(L4) · PermissionContract · HITL 게이트 3종 · verifier 2-pass · stopping_policy · session-bootstrap 필터 · 슬래시 커맨드/워크플로(재배치) |
| **차용(외부)** | gstack 라이프사이클(L1 플레이북) · Synergy memory/agenda/skill(L0) · AgentX 3계층(L3) · Magentic-One 정체→replan(L3↔L4) |
| **의도적 배제** | Synergy 협업 네트워크·사회적 관계·인격 |

## 6. 영향 (Consequences)

**긍정:** 책임이 5계층으로 분리되어 충돌·중복이 구조적으로 제거된다. 각 외부 패턴이
*자기 계층*에서만 작동하므로 직접 충돌하지 않는다. 진화 루프로 장기 비용이 감소한다.

**부정/비용:** 계층 수 증가로 초기 학습 곡선·문서화 부담. 증류 정책·승급 기준 등
운영 규칙을 추가로 정의해야 한다.

**중립:** 단일 프로젝트·로컬 우선 성격 유지(협업망 배제). 슬래시 커맨드 UX 하위 호환.

## 7. 참고 문헌

- AgentX (arXiv 2509.07595) — Stage Designer/Planner/Executor 3계층
- Magentic-One (arXiv 2411.04468) — task/progress ledger, 정체→replan
- Synergy (arXiv 2603.28428) — typed memory, agenda, rewarded trajectory, skill evolution
- gstack (github.com/garrytan/gstack) — office-hours → … → ship 라이프사이클
