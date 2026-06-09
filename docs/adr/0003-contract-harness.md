# ADR 0003 — Contract Harness: 모든 실행 직전의 구속 의식(ritual)

- **상태(Status):** Accepted — 구현 완료 (2026-06-09)
- **날짜:** 2026-06-09
- **관련:** [ADR 0001](0001-task-contract-as-binding-rules.md), [ADR 0006](0006-final-blended-architecture.md)

## 1. 동기 (Context)

ADR 0001은 Task Contract를 **데이터 모델 + 게이트 집행**으로 정의했다. 그러나
"그 계약을 *언제·어떻게* 채우는가"는 미정이었다. 에이전트가 작업을 시작하기 전에
**"무엇을 끝내야 하고, 무엇은 하지 말아야 하는지"를 고정**하지 않으면, 계약은
사후 정당화 도구로 전락한다.

Contract Harness는 그 공백을 메우는 **실행 직전 강제 의식(pre-run ritual)** 이다.

## 2. 결정 (Decision)

모든 에이전트 실행 전에 다음 5단계를 강제하고, 그 산출물로 **frozen
`TaskContract`(ADR 0001)** 를 만든다.

```text
Before every agent run:
1. Summarize objective          → TaskContract.objective
2. Define non-goals             → TaskContract.non_goals      (+ 실패 사례 guardrail 주입)
3. Define allowed files/tools   → TaskContract.allowed_tools  (+ identity write_scope)
4. Define verification command  → TaskContract.verification
5. Define stop condition        → TaskContract.stop_condition (limited Ouroboros loop)
```

핵심 규칙:

- 5단계가 모두 채워지기 전에는 L3 실행 제어(ADR 0002)로 진입할 수 없다 — 즉
  Contract Harness는 **"하고 싶은 것"과 "하기로 구속된 것" 사이의 관문**이다.
- 산출된 `TaskContract`는 **불변**이며 단일 `GOAL_CONTRACT` ledger 이벤트로 기록된다
  (`orchestrator.py:24-30`). 새 stage가 생기지 않는다.
- `non_goals`(2단계)와 `allowed_tools`(3단계)는 가능하면 **자동 주입**한다:
  - non-goals ← Typed Memory의 *실패 사례 → guardrail*(ADR 0005)
  - allowed_tools ← 현재 Agent Identity의 `write_scope`/`allowed_tools`(ADR 0005)
- 4단계 verification은 **실행 가능한 명령**이어야 한다(예: `python -m unittest`).
  산문 주장은 증거가 아니다(`agent-rules.md:36`).

> **반환값(구현, 리뷰 반영):** `bind()`는 `TaskContract`만 반환하면 호출자가 게이트에
> 넘길 객체가 없다(게이트는 모두 `GoalContract`를 요구). 따라서 게이트 가능한
> `GoalContract`와 frozen `TaskContract`를 함께 담은 **`BoundContract`**(`contract`,
> `task`)를 반환한다. 호출자는 `bound.contract`를 런타임 게이트에 넘기고
> `bound.task`로 구속 조항을 읽는다. (codex review r3381964877)

### 2.1 ADR 0001과의 관계 (중복 아님)

| | ADR 0001 (Task Contract) | ADR 0003 (Contract Harness) |
|---|---|---|
| 무엇 | 데이터 모델 + 게이트 집행 | 그 데이터를 채우는 **절차** |
| 고도 | 정적 구조 | 동적 의식(런타임 진입점) |
| 산출물 | `TaskContract`, 게이트 메서드 | `TaskContract` **인스턴스** |

둘은 같은 대상을 다른 고도에서 다룬다. Contract Harness는 별도 데이터 구조를 만들지
않고 ADR 0001의 `TaskContract`만 *생산*한다.

## 3. 검토한 대안 (Alternatives)

| 대안 | 판단 |
|---|---|
| A. `agent-rules.md` 산문 권고만 | **기각** — 강제력 없음, ADR 0001 동기와 동일 |
| B. 실행 중 임시(ad-hoc)로 계약 채우기 | **기각** — 도중 스코프 확장 → Geas 확장 재발 |
| C. (채택) 실행 직전 5단계 강제 + frozen 산출 | **채택** |

## 4. 영향 (Consequences)

**긍정:** 모든 실행이 구속 envelope로 시작된다. 실패 사례가 자동으로 non-goal
guardrail이 되어 같은 실수를 반복하지 않는다(진화 루프, ADR 0005·0006).

**부정/비용:** 실행마다 소액의 사전 비용. 자동 주입(memory/identity)이 없으면
사람이 5단계를 채워야 한다.

**중립:** `session-bootstrap` 워크플로(`workflows.py:68-75`)의 자연스러운 확장
지점이다 — 부트스트랩이 컨텍스트를 로드한 직후 Contract Harness가 계약을 고정한다.
