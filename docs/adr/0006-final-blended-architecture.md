# ADR 0006 — 최종 혼합 아키텍처: 5계층 분리와 충돌·중복·최적화

- **상태(Status):** Accepted — 부분 구현 (`CausalityEngine` happy path 연결, 2026-06-13 재점검)
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
│   long-run→Contract Harness+limited Causality · release→gstack ship+QA       │
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
│        ▲__________ REPAIR (limited Causality loop · Stop로 bounded) _________│
└──────────────────────────────────────────────────────────────────────────┘
      │ 모든 이벤트
      ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ L4  EVIDENCE & AUDIT — EvidenceLedger   (append-only · hash-chained) [기존] │
│   → success/latency/cost 계측(ADR 0002) → L0로 증류 환류                    │
└──────────────────────────────────────────────────────────────────────────┘
```

### 2.1 진화(환류) 루프 — 목표 구조 (write-path 부분 구현)

L4(ledger) → **증류** → L0(typed memory + rewarded trajectory). 그 결과가:

- 실패 사례 → **guardrail**(범위·TTL 포함, ADR 0005 §2.5) → L2 Contract Harness non-goals
- 성공 절차 → **earned skill**(재현성 + dedup + HITL 승급) → L0 Skill Library → L1/L3 재사용

> **이는 목표 구조이지 현재 상태가 아니다.** Synergy "경험 기반 학습"을 협업·사회
> 기능 없이 단일 프로젝트에 *적용하려는 설계*이다. 현재 코드는 ledger append, Review,
> Reflect, SkillStore, Engine happy path까지 구현했지만, failures→non_goals 주입과 earned skill
> 자동 재사용 read-path는 아직 없다 — §6 실현 가능성 표 참조.

## 3. 충돌 해소 (Conflicts)

| # | 충돌 | 해소 |
|---|---|---|
| C1 | 라우터 3중(agent-rules intent / Agent Harness / Stage Designer) | L1=**무엇을** 실행할지, L3 Stage Designer=**어떻게 단계화**할지로 고도 분리. agent-rules intent 라우팅은 L1로 흡수(ADR 0004) |
| C2 | 정지 조건 다원화(Contract Harness / TaskContract.stop / stopping_policy / "limited loop" / Magentic 정체) | **단일 출처** = `stopping_policy`. `should_stop`와 `run_bounded_loop` 소비자는 구현됨. 남은 갭은 `work` 내부 정체 신호 표준화와 plan/action/tool/non-goal gate 배선(§6.1) |
| C3 | 기억 vs ledger | L4 ledger=raw 원천, L0 typed memory=증류 파생. `reflect_on_contract`와 `SkillStore.distill` write-path는 구현됨. 남은 갭은 scoped memory/guardrail/skill read-path 자동 주입 |
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
| **차용(외부)** | gstack 라이프사이클(L1 플레이북) · Synergy memory/agenda/skill(L0) · AgentX 3계층(L3) · Magentic-One 정체→replan(L3↔L4, **목표**) |
| **의도적 배제** | Synergy 협업 네트워크·사회적 관계·인격 |

> ⚠️ **정정(리뷰 반영):** Magentic-One의 *정체→replan(stall→replan)* 은 **아직
> 미구현**이다. 현재 `GateDecision.REPAIR`(`gates.py`)는 검증 미달(verifier<2 /
> 치명 실패 / 증거 누락)에서만 발생하며 정체 카운터가 아니다. `no_progress_iterations`는
> `should_stop`이 소비해 **루프를 정지**시키지만(`gates.py:174`, `loop.py`로 카운트 공급),
> 정체를 **replan(REPAIR)으로 전환하는 stall→replan은 없다**. ADR 0002의 "이미 구현됨"
> 표기는 본 정정으로 무효화한다.

## 6. 자기개선 루프 실현 가능성 (Run → Review → Fix, Reflect → Skill update)

우로보로스 HITL의 *현재 프리미티브* 기준으로 각 단계가 실현 가능한지 검증한다.
범례: ✔ 구현 · ◐ 부분 구현 · ✖ 미구현.

| 단계 | 매핑 프리미티브 | 판정 | 비고 |
|---|---|---|---|
| **Run** | `record_evidence`/`record_verifier`로 ledger append, `work` 콜백 실행 | ◐ | `orchestrator.py` / `engine.py`; `work` 앞 plan/action/tool/non-goal gate 강제는 아직 없음 |
| **Review** | `run_review`가 N개 독립 verifier를 호출·기록·집계(≥2 pass) | ✔ | `review.py` |
| **Fix** | `run_bounded_loop`이 `complete`의 `REPAIR`를 받아 재시도, `should_stop`로 정지 | ✔ | `loop.py` |
| **Reflect** | `reflect_on_contract`가 ledger를 retrospective+failures로 증류(contract-scoped provenance) | ✔ | `reflect.py` |
| **Skill update** | `SkillStore` distill → 재현성 n-of-m → dedup → HITL 승급 | ◐ | `skills.py`; promoted skill의 자동 디스패치/컨텍스트 재사용은 없음 |
| **(통합)** | `CausalityEngine`이 Agenda→Dispatch→Harness→Loop→Review→Reflect→Skill candidate 연결 | ◐ | `engine.py`; happy path 배선이며 운영 안전성/read-path 갭은 남음 |

**결론(2026-06-13 재점검):** C-LOOP-1의 "프리미티브 전무" 상태는 해소되었다. 다만
"자기개선 반복 루프가 완전히 닫혔다"고 쓰는 것은 과장이다. 현재 코드는
`CausalityEngine.run_task` / `run_next`로 happy path를 실행할 수 있지만, failures→non_goals
자동 주입, TTL 만료/회수/읽기시 검증, earned skill 자동 재사용, 집행 게이트의 `work` 전 강제,
내구성 있는 저장소 write path, ledger indexing은 남아 있다.

### 6.1 다음 구현 순서

1. **P0 — 집행 게이트 배선:** `run_task`가 `work`를 직접 호출하지 않도록 표준 실행 어댑터를
   도입하고 `evaluate_plan` / `can_execute_action` / `check_tool_allowed` / `check_non_goal`을
   실제 action 앞에 강제한다.
2. **P0 — guardrail 환류:** `reflect_on_contract`가 쓴 failures를 다음 `ContractHarness.bind`에서
   scope/TTL/confidence 기준으로 읽어 non_goals 후보로 주입한다.
3. **P0 — TTL 실효화:** `TypedMemory.entries()` 계열에 만료 필터와 revoke/tombstone 모델을 추가한다.
4. **P1 — Skill read-path:** promoted earned skill을 L1/L3 dispatch/context selection에 자동 재사용한다.
5. **P1 — Durable stores:** ledger/memory/skills/agenda에 lock, fsync, atomic rename을 공통 적용한다.
6. **P2 — Ledger indexing:** contract/event-type index와 contract-scoped latest hash helper를 추가한다.

## 7. 알려진 위험 / 미해결 이슈 (code-review 반영)

2026-06-09 비판적 리뷰에서 확정된 결함과 처리:

| ID | 위험 | 처리 |
|---|---|---|
| C-MEM-1 | `assumptions` 타입 부재 + `decision`=ADR 융합 → 가정이 확정지식으로 둔갑 | **해결**: ADR 0005 §2.2 6-타입 분리 + §2.5 승급 게이트 |
| C-MEM-2 | failure→guardrail 단방향 래칫(만료·범위 없음) | **부분 해결**: failure scope/TTL metadata 저장은 구현. TTL 만료 필터·회수·다음 계약 non_goals 주입은 미구현 |
| C-MEM-3 | earned skill이 lucky/brittle 성공을 못 거름 + authored 중복 | **부분 해결**: 재현성 n-of-m + dedup + HITL 승급은 구현. promoted skill의 자동 재사용은 미구현 |
| C-MEM-4 | 기억 저장 스토리 3중 모순 + 해시체인 출처 소실 | **해결**: ADR 0005 §2.2 provenance(ledger `entry_hash`) 연결 |
| C-LOOP-1 | Reflect/Skill-update 프리미티브 전무 | **부분 해결**: `review.py`·`reflect.py`·`skills.py`·`engine.py` 구현. 단, guardrail/skill read-path가 없어 완전 폐쇄 루프는 아님(§6) |
| C-STOP-1 | `should_stop`/stopping_policy 소비자 없음, "limited loop" 미정의 | **해결**: `should_stop`+`run_bounded_loop`이 stopping_policy(max_iterations/no_progress_iterations/max_failed_hypotheses)를 소비(`gates.py`/`loop.py`). "limited loop"=세 ceiling으로 정의. 남은 갭=stall→replan 신호·gate 배선(§5.1·§6.1) |
| C-MAG-1 | Magentic stall→replan을 REPAIR로 "이미 구현됨" 오기재 | **정정**: §5.1 정정 박스 |
| C-ESC-1 | TaskContract.escalation(무동작) vs gate 위험기반 ESCALATE 이중화 | **처리**: 게이트가 `contract.escalation`을 *읽도록* 일원화(ADR 0001 §2.3 보강) |
| C-ROUTE-1 | agent-rules intent 라우팅이 3곳(AGENT_RULES/AGENTS_MD/CODEX_ROUTING) 잔존, "흡수" 과장 | **처리**: ADR 0004 §2.1 — 구현 시 3곳 제거를 마이그레이션 항목으로 명시 |
| C-ROUTE-2 | gstack 라이프사이클(office-hours→…→ship)이 제3 라우터 | **처리**: 라이프사이클 단계 순서는 *선택된 번들 내부*의 책임으로 귀속(L1은 번들 선택만) |

> 2026-06-13 코드리뷰 재점검: 위 표에서 "해결"은 반드시 런타임 경로까지 닫혔다는 뜻이
> 아니다. 저장/write-path만 구현된 항목과 실행/read-path까지 강제되는 항목을 구분해 추적한다.
> 상세 갭과 우선순위는 `docs/code-review-2026-06-13.md`를 기준으로 한다.

## 8. 영향 (Consequences)

**긍정:** 책임이 5계층으로 분리되어 충돌·중복이 구조적으로 제거된다. 각 외부 패턴이
*자기 계층*에서만 작동하므로 직접 충돌하지 않는다. 진화 루프로 장기 비용이 감소한다.

**부정/비용:** 계층 수 증가로 초기 학습 곡선·문서화 부담. 증류 정책·승급 기준 등
운영 규칙을 추가로 정의해야 한다. 특히 gate 강제 배선, memory/skill read-path, durable store, ledger index가 남아 있다(§6.1).

**중립:** 단일 프로젝트·로컬 우선 성격 유지(협업망 배제). 슬래시 커맨드 UX 하위 호환.

## 9. 참고 문헌

- AgentX (arXiv 2509.07595) — Stage Designer/Planner/Executor 3계층
- Magentic-One (arXiv 2411.04468) — task/progress ledger, 정체→replan
- Synergy (arXiv 2603.28428) — typed memory, agenda, rewarded trajectory, skill evolution
- gstack (github.com/garrytan/gstack) — office-hours → … → ship 라이프사이클
