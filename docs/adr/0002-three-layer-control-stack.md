# ADR 0002 — 3계층 실행 제어 스택 (Stage Designer / Planner / Executor)

- **상태(Status):** Proposed
- **날짜:** 2026-06-09
- **관련:** [ADR 0001 — Task Contract](0001-task-contract-as-binding-rules.md)

## 1. 동기 (Context)

현재 "역할"은 두 곳에 산재한다.

- 워크플로 6종 (`src/ouroboros_hitl/workflows.py:27-76`)
- 슬래시 커맨드 5종 (`src/ouroboros_hitl/agent_bootstrap.py:108-165`)

이들은 사용자 의도 라우팅(`agent-rules.md`의 Automatic Routing,
`agent_bootstrap.py:17-26`)에는 유용하지만, **실제 실행 제어(execution control)
관점에서는 책임이 겹치고 경계가 모호**하다. 예를 들어 `/ouroboros-verify`는
검증 계획 수립과 검증 실행을 모두 포함하고, `writing-plans`는 단계 설계와 검증 기준
작성을 함께 다룬다.

### 1.1 외부 패턴 참조

| 패턴 | 구조 | 이 프로젝트와의 관계 |
|---|---|---|
| **AgentX** (arXiv 2509.07595) | **stage designer → planner → executor** 3-에이전트. 프롬프트를 분해해 워크플로 자동 생성. ReAct·Magentic-One 대비 competitive/우위 보고 | 본 ADR이 차용하는 역할 단순화 모델 |
| **Magentic-One** (arXiv 2411.04468) | Orchestrator의 **task ledger**(사실·계획) + **progress ledger**(진행·정체 카운터→자동 replan) | **이미 구현됨**: `EvidenceLedger` + `stopping_policy.no_progress_iterations` + `GateDecision.REPAIR` |
| **ReAct** | thought→action 단일 인터리브 루프 | 장기 과제·컨텍스트 관리 취약, 비용 증가 경향 → 비교 기준선 |

**핵심 통찰:** 이 프로젝트는 *이미* Magentic-One형(ledger + 정체→replan)이다.
따라서 AgentX의 3계층은 새 런타임이 아니라 기존 역할 위에 얹는
**"역할 단순화 오버레이"** 로 도입하는 것이 자연스럽다.

## 2. 결정 (Decision)

실행 제어를 **3계층 + 경계 게이트 3종**으로 재구성한다. 기존 워크플로/슬래시
커맨드는 **폐기하지 않고** 각 계층 아래의 *플레이북*으로 재배치한다(이름 유지 →
하위 호환).

### 2.1 계층 정의와 역할 재배치

| 계층 | 책임 | 흡수되는 기존 역할/워크플로 | 경계 게이트 |
|---|---|---|---|
| **Stage Designer** | 이번 작업의 **단계 구조 설계** + 계약 envelope | `session-bootstrap`, `writing-plans`(분해), `subagent-driven-development`, **Task Contract 생성(ADR 0001)**, `/ouroboros-plan`(구조화 부분) | → `evaluate_plan` |
| **Planner** | 각 단계의 **실행 계획 + 검증 기준** 작성 | `writing-plans`(acceptance/verification), `test-driven-development`(RED·기준), `root-cause-protocol`, `/ouroboros-root-cause` | → `evaluate_plan` |
| **Executor** | **코드 수정 · 테스트 · 커밋 · 문서화** + 검증·완료 | `verification-before-completion`, `test-driven-development`(GREEN/REFACTOR), `/ouroboros-verify`, `/ouroboros-a11y-observe`, `/ouroboros-complete` | `can_execute_action`, `complete` |

기존 HITL 게이트 3종(`gates.py`: `evaluate_plan` / `can_execute_action` /
`complete`)이 **계층 경계와 자연스럽게 1:1로 정렬**된다. 즉 "역할 8개"가 아니라
**"계층 3개 + 경계 게이트 3개"** 로 실행 제어 모델이 단순해진다.

### 2.2 통합 다이어그램 (ADR 0001 + 0002)

```text
┌── Task Contract (불변 조항: Objective / Non-goals / Allowed tools / Stop / Verify / Escalate) ──┐
│                                                                                                  │
│  Stage Designer ─[evaluate_plan]→ Planner ─[evaluate_plan]→ Executor ─[can_execute / complete]→  │
│        ▲________________________ REPAIR (정체→replan, Magentic-One형) __________________________│ │
│                                                                                                  │
└──────────── EvidenceLedger (모든 이벤트 기록 → success / latency / cost 계측) ──────────────────┘
```

- **계층 경계 = HITL 게이트.** 각 계층이 Task Contract envelope(ADR 0001)를
  벗어나면 게이트가 `STOP`/`ESCALATE`를 반환한다.
- **replan 루프 = Magentic-One의 정체→재계획.** 기존 `GateDecision.REPAIR`와
  `stopping_policy`를 그대로 사용한다.

### 2.3 평가 방법론 (AgentX 방식 차용)

기존 `EvidenceLedger`가 측정 인프라를 이미 제공하므로 **추가 도구 없이 계측 가능**하다.

| 지표 | 측정 방법 (기존 ledger 활용) | 패턴별 예상 특성 |
|---|---|---|
| **Success rate** | `complete` 게이트 `PASS` 비율, verifier 2-pass 달성률 (`gates.py:62-98`) | AgentX ≳ Magentic-One > ReAct(장기 과제) |
| **Latency** | ledger 이벤트 `timestamp` 간 델타 (p50 / **p95 / p99**) (`ledger.py:30-40`) | 3계층은 단계 분리로 p99 안정, ReAct는 루프 길어질수록 꼬리 지연 악화 |
| **Cost** | 이벤트 payload에 token/$ 필드를 추가해 누적 | ReAct는 컨텍스트 관리 부재로 비용↑, 3계층은 단계별 컨텍스트 격리로 절감 |

> Magentic-One 자료는 **p50이 아니라 p95/p99(꼬리 지연)을 예산화**할 것을 강조한다.
> 사용자가 체감하는 것은 평균이 아니라 꼬리이기 때문이다.

## 3. 검토한 대안 (Alternatives)

| 대안 | 내용 | 판단 |
|---|---|---|
| A. 현행 유지 | 워크플로 6 + 슬래시 5 그대로 | **기각** — 실행 제어 관점에서 책임 중복·경계 모호 |
| B. ReAct식 단일 루프로 평탄화 | thought→action 한 루프로 통합 | **기각** — 장기 과제/비용 취약, 게이트·증거 모델과 상충 |
| C. Magentic-One Orchestrator 단일화 | 단일 오케스트레이터 + 2-ledger | **부분** — 이미 ledger형이나 역할 가시성이 낮음 |
| D. (채택) AgentX 3계층 + 경계 게이트 | 2절 결정 | **채택** — 가시성과 제어의 균형, 기존 게이트와 1:1 정렬 |

## 4. 영향 (Consequences)

**긍정:**

- 실행 제어가 "역할 8개"에서 "계층 3개 + 게이트 3개"로 단순해진다.
- 계층 경계가 기존 HITL 게이트와 정렬되어 추가 게이트 개념이 필요 없다.
- ledger 기반으로 success/latency/cost를 곧바로 계측·비교할 수 있다.

**부정 / 비용:**

- `workflows.py`의 워크플로 메타데이터와 `agent_bootstrap.py`의 라우팅/커맨드
  템플릿을 3계층 기준으로 재배치해야 한다.
- 문서(`agent_automation.md`, `ouroboros_integration.md`)의 역할 서술을 갱신해야 한다.

**중립:**

- 슬래시 커맨드 이름(`/ouroboros-*`)은 유지 → 사용자 UX 하위 호환.
- 런타임 게이트 시그니처 변경 없음.

## 5. 구현 스케치 (참고용, 미반영)

```text
workflows.py     : OUROBOROS_WORKFLOWS에 layer 메타 부여(stage_designer|planner|executor)
agent_bootstrap.py: agent-rules.md 라우팅을 3계층 기준으로 재서술, 커맨드 이름 유지
ouroboros_integration.md: 3계층 + 게이트 경계 다이어그램 반영
(선택) eval 하니스: ledger를 읽어 success/latency/cost 집계하는 스크립트
```
