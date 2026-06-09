# 아키텍처 결정 기록 (ADR)

이 디렉터리는 Ouroboros HITL의 설계 결정을 기록합니다. 각 ADR은
동기(Context) · 결정(Decision) · 대안(Alternatives) · 영향(Consequences)
순으로 작성하며, 결정의 근거가 되는 기존 코드 위치를 `file:line`으로 인용합니다.

상태(Status) 값:

- `Proposed`: 제안됨. 코드에 아직 반영되지 않음.
- `Accepted`: 채택됨. 구현이 진행 중이거나 완료됨.
- `Superseded by ADR-XXXX`: 다른 ADR로 대체됨.

## 목록

| ADR | 제목 | 상태 |
|---|---|---|
| [0001](0001-task-contract-as-binding-rules.md) | Task Contract — 범위 확장이 아닌 구속 규칙 계층 | Proposed |
| [0002](0002-three-layer-control-stack.md) | 3계층 실행 제어 스택 (Stage Designer / Planner / Executor) | Proposed |
| [0003](0003-contract-harness.md) | Contract Harness — 모든 실행 직전의 구속 의식 | Proposed |
| [0004](0004-agent-harness-task-routing.md) | Agent Harness — 작업 유형별 아키텍처 디스패치 | Proposed |
| [0005](0005-identity-memory-skill-substrate.md) | 정체성·기억·스킬 기반층 (Synergy 일부 차용) | Proposed |
| [0006](0006-final-blended-architecture.md) | 최종 혼합 아키텍처 — 5계층 분리와 충돌·중복·최적화 | Proposed |

## 읽는 순서

- **0001 · 0002**: 기반 결정. 작업을 감싸는 **구속 envelope**(0001)와 그 안의
  **실행 제어 구조**(0002).
- **0003 · 0004 · 0005**: 0001/0002 위에 얹는 운영 계층 — 실행 직전 의식(0003),
  작업 유형 라우터(0004), 지속 기억·스킬 기반층(0005).
- **0006**: 위 다섯을 하나의 **5계층 아키텍처**로 종합하고 충돌·중복·최적화를
  해소하는 capstone. 전체 그림은 여기서 본다.
