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

두 ADR은 함께 읽도록 설계되었습니다. 0001은 작업을 감싸는 **구속 envelope**를
정의하고, 0002는 그 envelope 안에서 작업을 수행하는 **실행 제어 구조**를 정의합니다.
