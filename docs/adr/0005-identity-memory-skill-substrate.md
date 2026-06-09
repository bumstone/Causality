# ADR 0005 — 정체성·기억·스킬 기반층 (Synergy 일부 차용)

- **상태(Status):** Proposed
- **날짜:** 2026-06-09
- **관련:** [ADR 0003](0003-contract-harness.md), [ADR 0006](0006-final-blended-architecture.md)

## 1. 동기 (Context)

Synergy(arXiv 2603.28428)는 차세대 에이전트를 *Open Agentic Web의 참여자(Agentic
Citizen)* 로 보고 ① 협업 네트워크 ② 지속적 정체성·인격 ③ 평생 진화를 강조한다.
정체성은 **typed memory·notes·agenda·skills·사회적 관계**로, 진화는 추론 시점에
**rewarded trajectory를 능동 회상**하는 경험 학습으로 구현된다.

이 프로젝트는 **로컬 우선·단일 프로젝트** 도구다(`README.md:3`). 따라서 Synergy의
**협업 네트워크·사회적 관계·인격(personhood)** 은 차용하지 **않고**,
**memory / agenda / skill evolution** 만 가져온다.

## 2. 결정 (Decision)

지속(cross-session) **기반층(substrate)** 을 신설하고 4개 구성요소를 둔다. 모두
기존 메커니즘(PermissionContract, EvidenceLedger, verifier 2-pass, HITL 게이트)
위에 얹어 *재사용*한다.

### 2.1 Agent Identity — 역할/권한/기억 분리

- 각 역할(ADR 0002의 Stage Designer / Planner / Executor + gstack 스페셜리스트)에
  **scoped `PermissionContract`** (`contracts.py:82`)와 **scoped memory view**를 부여.
- `subagent-driven-development`의 "disjoint write scopes / 풀 컨텍스트 비공유"
  원칙(`workflows.py:42`)을 정체성 단위로 형식화한 것 — 새 메커니즘이 아니다.

### 2.2 Typed Memory — 구분된 장기 기억 (→ guardrail)

| 타입 | 정의 | 활용 |
|---|---|---|
| retrospective | 작업 후 회고 | rewarded trajectory 추출 입력 |
| work-log | 작업 로그 | ledger tail의 요약 |
| decision | 결정사항 | **ADR이 곧 이 타입** |
| failure-case | 실패 사례 | **→ guardrail → Contract Harness non-goals**(ADR 0003) |

핵심: Typed Memory는 **EvidenceLedger의 *증류물(distilled)*** 이지 병렬 로그가
아니다. ledger가 원천(raw, hash-chained), memory는 큐레이션된 파생물. `build_session_
bootstrap`이 이미 `tool-verified`/`human-approved` 출처만 기억에 넣는다
(`workflows.py:118-122`) — 바로 이 지점이 증류 게이트다.

### 2.3 Agenda — 장기 목표 / 대기 작업

- 개별 `GoalContract` *위*의 백로그. 항목은 **계약 이전 의도(pre-contract
  intention)** 이며, Agent Harness(ADR 0004)가 선택하는 순간 `GoalContract`로
  인스턴스화된다 → GoalContract와 중복되지 않는다.

### 2.4 Rewarded Trajectory → 재사용 스킬

- **성공한 작업 절차**(완료 게이트 PASS + verifier 2-pass + 필요 시 인간 승인,
  `gates.py:62-98`)를 재사용 가능한 **earned skill**로 증류.
- 스킬은 두 계층으로 구분:
  - **authored skill**: gstack/Superpowers/`workflows.py` — 고정·큐레이션된 플레이북.
  - **earned skill**: rewarded trajectory에서 추출된 프로젝트 고유 스킬.
- earned skill은 자동 채택하지 않고 **HITL 승급 게이트**를 거쳐야 라이브러리에 등재
  → 검증되지 않은 절차의 오염 방지(기존 verifier/승인 메커니즘 재사용).

## 3. 검토한 대안 (Alternatives)

| 대안 | 판단 |
|---|---|
| A. Synergy 전체 도입(협업망·사회관계 포함) | **기각** — 로컬 우선 범위 초과, 사용자 요구와 불일치 |
| B. 기억을 ledger에만 저장 | **기각** — raw 증거와 증류 지식을 혼동, 컨텍스트 비용↑ |
| C. earned skill 자동 등재 | **기각** — 미검증 절차 오염 |
| D. (채택) ledger 증류형 substrate + 승급 게이트 | **채택** |

## 4. 영향 (Consequences)

**긍정:** 진화 루프(경험→기억→스킬) 확보. 실패가 guardrail로, 성공이 스킬로
환류되어 비용·재계획이 시간이 지날수록 감소.

**부정/비용:** 신규 스토어 2개(typed memory, agenda) + 승급 게이트 1개. 증류 정책
(무엇을 기억에 남길지)을 정의해야 한다.

**중립:** 협업/사회 기능을 의도적으로 배제하므로 멀티 에이전트 *네트워크* 가 아니라
**단일 프로젝트의 장기 기억**으로 한정된다.
