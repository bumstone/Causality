# ADR 0001 — Task Contract: 범위 확장이 아닌 구속 규칙 계층

- **상태(Status):** Accepted — 핵심 구현 완료 (2026-06-09)
- **날짜:** 2026-06-09
- **관련:** [ADR 0002 — 3계층 실행 제어 스택](0002-three-layer-control-stack.md)

## 1. 동기 (Context)

우로보로스 루프 앞단에 "작업 계약 생성 단계"를 별도 stage로 배치하자는 제안이
있었다. 목표는 작업이 지켜야 할 규칙(Objective / Non-goals / Allowed tools /
Stop condition / Verification / Escalation)을 계약서처럼 명시하는 것이다.

그러나 현재 구조에서 `GoalContract`는 **이미 루프의 원자적 뿌리(atomic root)**다.
`Causality.create_contract`가 곧 첫 ledger 이벤트이고
(`src/causality/orchestrator.py:24-30`), 이후 모든 상태 전이는 이 계약을
중심으로 일어난다 (`src/causality/contracts.py:21-27`).

> 참고: 코드베이스 전체에 "Geas"라는 토큰은 존재하지 않는다(grep 0건).
> "Geas"는 *구속력 있는 목표 의무*를 가리키는 개념 어휘이며, 이 프로젝트에서
> 그 실체는 `GoalContract`다.

따라서 **계약 생성 stage를 앞에 또 두면 또 하나의 목표 명세를 만드는 셈**이 되어
목표 텍스트·스코프가 중복·확장된다. 즉 "지켜야 할 규칙을 좁히려는" 의도와 반대로
**구속 대상(Geas)의 범위만 넓어지는** 역설이 발생한다. 이것이 이 ADR이 해결하려는
핵심 문제다.

### 1.1 6개 항목의 현재 상태

제안된 Task Contract 6개 항목을, 현재 코드 기준으로 "스코프 확장 vs 제한" 관점에서
분류하면 다음과 같다.

| 항목 | 성격 | 현재 코드 매핑 | 상태 |
|---|---|---|---|
| **Objective** | 단일 목표 (확장 금지) | `GoalContract.title` + `summary` (`contracts.py:136-137`) | ✅ 있음 |
| **Non-goals** | **스코프 차단 (핵심 안티-확장 장치)** | — | ❌ **없음** |
| **Allowed tools** | 도구 제약 | `PermissionContract.allowed_tools` (`contracts.py:83`) | ⚠️ 있으나 **정보용**(미강제) |
| **Stop condition** | 정지 제약 | `GoalContract.stopping_policy` (`contracts.py:142-148`) | ⚠️ 있으나 **루프가 안 읽음** |
| **Verification** | 증명 의무 | `evidence_required` + verifier 2-pass (`gates.py:75-89`) | ✅ 있음 |
| **Escalation** | 인간 위임 조건 | `approval_required` + 게이트 `ESCALATE` (`gates.py:41`, `91`) | ⚠️ 게이트에 **하드코딩** |

핵심 관찰:

- 진짜로 **빠진 데이터는 `non_goals` 하나뿐**이다.
- 나머지는 "이미 존재하지만 *집행되지 않는*" 필드다. `allowed_tools`는 정보용으로만
  저장되고 게이트가 검사하지 않으며, `stopping_policy`는 어떤 코드도 읽지 않는다.

## 2. 결정 (Decision)

**새로운 루프 stage를 추가하지 않는다.** Task Contract는 "목표를 더 쓰는 단계"가
아니라 **불변(immutable) 조항 집합 = 지켜야 할 작업 규칙**으로 설계한다. 구체적으로:

### 2.1 `non_goals` 필드 추가 (유일한 신규 데이터)

`GoalContract`에 단 하나의 필드를 추가한다.

```python
# contracts.py — GoalContract 내부
non_goals: tuple[str, ...] = ()   # 명시적 스코프 차단 — 확장이 아니라 경계 선언
```

기본값이 `()`이므로 기존 직렬화/역직렬화와 하위 호환된다. `to_dict()` /
`from_mapping()`에 키 하나만 추가한다.

### 2.2 `TaskContract` frozen 값객체 도입 (파생 뷰)

`GoalContract`를 *감싸거나 대체하지 않고*, 그로부터 파생되는 **읽기 전용 조항 뷰**를
제공한다.

```python
@dataclass(frozen=True)
class TaskContract:
    objective: str                    # ← title + summary 에서 파생
    non_goals: tuple[str, ...]         # ← GoalContract.non_goals
    allowed_tools: tuple[str, ...]     # ← permissions.allowed_tools
    stop_condition: Mapping[str, Any]  # ← stopping_policy
    verification: tuple[str, ...]      # ← required_evidence_kinds()
    escalation: tuple[str, ...]        # ← 게이트 트리거를 계약으로 명시화

    @classmethod
    def of(cls, contract: "GoalContract") -> "TaskContract": ...
```

- **새 목표 텍스트를 만들지 않는다.** 기존 계약에서 파생만 한다.
- **불변(frozen)** 이라 루프 도중 조항이 확장될 수 없다.
- 생성 시 ledger 이벤트는 지금과 동일하게 단일 `GOAL_CONTRACT` 하나로 유지한다
  (`orchestrator.py:24-30`). 즉 **루프 stage 수가 늘지 않는다.**

### 2.3 게이트를 "기록"에서 "집행"으로 승격

`HITLGate`에 조항을 실제로 강제하는 메서드를 추가한다. (현재 게이트는 위반을
검사하지 않고 결정을 기록만 한다.)

| 신규 게이트 메서드 | 집행하는 조항 | 위반 시 결정 |
|---|---|---|
| `check_tool_allowed(contract, tool)` | Allowed tools | `STOP` 또는 `ESCALATE` |
| `check_non_goal(contract, action_desc)` | Non-goals | `STOP` |
| `should_stop(contract, iteration_state)` | Stop condition (`max_iterations` 등) | `STOP` |

이로써 `.causality/agent-rules.md`의 "Required Loop" 규칙
(`agent_bootstrap.py:31-38`)이 산문 권고에서 **집행 가능한 계약**으로 승격된다.

> **에스컬레이션 일원화(리뷰 C-ESC-1):** 현재 `ESCALATE`는 `gates.py:41,57,91`에서
> `approval_required`(위험 기반)와 `IRREVERSIBLE_ACTIONS`로 **하드코딩**되어 있어,
> `TaskContract.escalation` 조항을 따로 두면 *무동작 텍스트* 와 *실제 게이트 동작* 이
> 갈린다. 따라서 escalation은 별도 모델을 만들지 않고 **게이트가
> `contract.escalation`(+ 위험 기반 기본값)을 읽어 판단하도록 일원화**한다. 조항이
> 곧 트리거가 되어야 하며, 조항 수정이 런타임에 반영되지 않으면 안 된다.
>
> **현재 상태:** 위 3개 메서드와 `should_stop`이 소비하는 `iteration_state` 호출
> 계약은 **모두 신규**다. `orchestrator.py`는 현재 `evaluate_plan`/
> `can_execute_action`/`complete`만 노출하며, `iteration_state`(반복 횟수)를 계산·전달
> 하는 주체가 없다(§4 참조).

### 2.4 비확장 보장 메커니즘

이 설계가 "Geas 확장"을 구조적으로 막는 이유:

1. **데이터 추가 최소화** — 새 목표 명세 대신 `non_goals` 한 필드만 추가한다.
2. **Non-goals가 스코프를 좁힌다** — 명시적 경계 선언이 곧 확장 차단 장치다.
3. **불변성** — 파생된 `TaskContract`는 frozen이라 실행 중 조항 확장이 불가능하다.

## 3. 검토한 대안 (Alternatives)

| 대안 | 내용 | 판단 |
|---|---|---|
| A. 별도 pre-loop stage | 루프 앞에 독립 "작업 계약 생성" 단계 신설 | **기각** — 목표 명세 중복 → 동기에서 지적한 Geas 확장 그 자체 |
| B. agent-rules.md 산문만 추가 | 규칙을 문서에만 서술 | **기각** — 집행 불가, 현 상태와 동일(이미 정보용 필드 존재) |
| C. GoalContract 전체 frozen화 | 6개 항목을 모두 GoalContract 필드로 넣고 동결 | **부분 채택** — `state`는 `transition()`이 가변으로 사용(`orchestrator.py:39`)하므로 전체 동결 불가. 대신 `non_goals`만 추가하고 조항은 파생 frozen 뷰로 분리 |
| D. (채택) 파생 frozen 뷰 + 게이트 집행 | 2절 결정 | **채택** |

## 4. 영향 (Consequences)

**긍정:**

- `non_goals`로 스코프 축소가 명시적으로 보장된다.
- `allowed_tools` / `stop_condition`이 정보용에서 **실제 집행 대상**으로 바뀐다.
- 기본값/파생 설계 덕분에 ledger 스키마와 기존 호출부가 하위 호환된다.

**부정 / 비용:**

- `gates.py`에 집행 로직과 테스트가 추가된다.
- 새 게이트는 외부 런타임이 호출해야 효과가 난다(퍼사드는 primitive 제공 방식 유지).
- `should_stop`은 런타임이 `iteration_state`(반복 횟수 등)를 전달해야 한다 — 현재
  `stopping_policy`를 읽는 주체가 없으므로 호출 계약을 새로 정의해야 한다.

**중립:**

- `GOAL_CONTRACT` ledger payload에 `non_goals` 키만 추가되고 이벤트 종류는 불변.
- 슬래시 커맨드/워크플로 변경 없음 (그 재배치는 ADR 0002에서 다룸).

## 5. 구현 스케치 (참고용, 미반영)

```text
contracts.py   : GoalContract += non_goals; TaskContract(frozen) 신규; to_dict/from_mapping 갱신
gates.py       : check_tool_allowed / check_non_goal / should_stop 추가 (+ GATE_DECISION 기록)
orchestrator.py: can_execute_action 경로에서 check_tool_allowed/check_non_goal 연동 (선택)
tests/         : test_contracts.py(non_goals 왕복), test_gates.py(집행/위반 경로)
docs/          : causality_integration.md에 Task Contract 조항 표 반영
```
