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
| [0001](0001-task-contract-as-binding-rules.md) | Task Contract — 범위 확장이 아닌 구속 규칙 계층 | **Accepted (구현)** |
| [0002](0002-three-layer-control-stack.md) | 3계층 실행 제어 스택 (Stage Designer / Planner / Executor) | Proposed |
| [0003](0003-contract-harness.md) | Contract Harness — 모든 실행 직전의 구속 의식 | **Accepted (구현)** |
| [0004](0004-agent-harness-task-routing.md) | Agent Harness — 작업 유형별 아키텍처 디스패치 | Proposed |
| [0005](0005-identity-memory-skill-substrate.md) | 정체성·기억·스킬 기반층 (Synergy 일부 차용) | **Accepted (부분 구현)** |
| [0006](0006-final-blended-architecture.md) | 최종 혼합 아키텍처 — 5계층 분리와 충돌·중복·최적화 | Proposed |
| [0007](0007-context-economy-progressive-disclosure.md) | Context Economy & Progressive Disclosure (운영 규칙) | **Accepted (부분 구현)** |

## 읽는 순서

- **0001 · 0002**: 기반 결정. 작업을 감싸는 **구속 envelope**(0001)와 그 안의
  **실행 제어 구조**(0002).
- **0003 · 0004 · 0005**: 0001/0002 위에 얹는 운영 계층 — 실행 직전 의식(0003),
  작업 유형 라우터(0004), 지속 기억·스킬 기반층(0005).
- **0006**: 위 다섯을 하나의 **5계층 아키텍처**로 종합하고 충돌·중복·최적화를
  해소하는 capstone. 전체 그림은 여기서 본다.
- **0007**: 운영 규칙 — 긴 규칙/체크리스트/역할을 매번 로드하지 않고 필요할 때만
  파일을 참조하는 **context economy / 점진적 공개**. always-loaded 경계와 권장 파일
  레이아웃(`workflow/ checklists/ skills/ memory/`)을 명시.

## 비판적 리뷰 (2026-06-09)

`/code-review`(high) 결과 10건의 결함을 확정하고 반영했다. 핵심:

- **메모리 오염**: `assumptions` 타입 부재 + `decision`=ADR 융합 → ADR 0005가
  6-타입 분리 + 승급/만료/검증 거버넌스(§2.5)로 교체.
- **과장 표현 교정**: `should_stop`·`non_goals`·skill 스토어·stall→replan 등은
  **현재 미구현(제안)** 임을 ADR 0001/0002/0004/0006에 명시.
- **자기개선 루프**: Run→Review→Fix는 기존 요소 조합으로 가능, Reflect→Skill update는
  신규 — ADR 0006 §6 실현 가능성 표 + §7 알려진 위험 표(C-* IDs).

"해결/처리"는 설계상 해소이며, 일부는 아래 구현 단계에서 코드로 반영되었다.

## 구현 상태 (2026-06-09)

권장 순서(0001 → 0003 → 0007)로 기초 슬라이스를 구현한 뒤, 루프 드라이버와 타입
메모리 거버넌스를 이어 구현했다(40 tests OK).

| ADR | 구현된 것 | 코드 | 테스트 |
|---|---|---|---|
| 0001 | `non_goals` 필드, `TaskContract`(frozen·파생·read-only), 집행 게이트 `check_tool_allowed`/`check_non_goal`/`should_stop`, escalation 파생 뷰 | `contracts.py`, `gates.py`, `orchestrator.py` | `test_contracts.py`, `test_gates.py` |
| 0003 | `ContractHarness.bind`(5단계 검증 → frozen TaskContract, 단일 GOAL_CONTRACT 기록) | `contract_harness.py` | `test_contract_harness.py` |
| 0007 | 부트스트랩이 `workflow/`·`checklists/`·`skills/`·`memory/<6타입>/` 생성, 워크플로 문서 단일출처 생성, Context Economy 운영규칙, AGENTS.md=Codex 재정의 | `agent_bootstrap.py` | `test_agent_bootstrap.py` |
| 0006 §6.1-1 | bounded 루프 드라이버 `run_bounded_loop`(should_stop로 정지, Run→Review→Fix) | `loop.py` | `test_loop.py` |
| 0005 §2.2/§2.5 | 타입 메모리 `TypedMemory`(6타입 분리, assumption→decision 승급 게이트, failure scope/TTL, provenance) | `memory.py` | `test_memory.py` |

**후속(미구현):** 자동 verifier 호출자(Review 자동화), Reflect 추출기·earned-skill
distiller/승급(0005 진화 절반), Agent Harness 디스패처(0004), 3계층 메타데이터(0002).
