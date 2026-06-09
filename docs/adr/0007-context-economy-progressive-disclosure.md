# ADR 0007 — Context Economy & Progressive Disclosure (운영 규칙)

- **상태(Status):** Accepted — 부분 구현 (파일 레이아웃·운영규칙, 2026-06-09)
- **날짜:** 2026-06-09
- **관련:** [ADR 0003](0003-contract-harness.md) · [ADR 0004](0004-agent-harness-task-routing.md) · [ADR 0005](0005-identity-memory-skill-substrate.md) · [ADR 0006](0006-final-blended-architecture.md)

## 1. 동기 (Context) — 적용 여부 판정

원칙: **긴 운영 규칙·체크리스트·역할 설명·설계 템플릿을 매번 프롬프트에 복사하지
않는다. 에이전트는 필요할 때만 저장소 문서를 참조한다.**

**판정: 적용한다(APPLIES).** 이미 부분적으로 코드에 존재한다:

- `session-bootstrap` 워크플로: "스킬 라이브러리를 매 턴 주입하지 말 것 / 검증된
  사실만 기억에 진입"(`workflows.py:74-75`, `118-122`).
- ADR 0006 §5 최적화: identity별 *scoped memory view*만 로드(컨텍스트 팽창 회피).
- Claude Code 스킬 자체의 점진적 공개 패턴(SKILL 요약만 상주, 본문은 필요 시 로드)과
  동형이다.

본 ADR은 이 산재된 관행을 **명시적·집행 가능한 운영 규칙**과 **파일 레이아웃**으로
승격한다. 이는 메모리 오염 방지(ADR 0005)와 비용·지연 최적화(ADR 0006)를 직접
강화한다.

## 2. 결정 (Decision)

### 2.1 기본 원칙 (operating rules)

1. **Always-loaded context는 최소화** — 상시 로드는 *얇은 규칙 + 라우팅 + 활성
   TaskContract + ledger tail* 까지만.
2. **Detailed workflow는 파일로 분리** — 본문은 on-demand.
3. **작업 유형 확정 후** 해당 `workflow/<type>.md` *하나만* 읽는다.
4. **모든 역할 설명을 한 번에 로드하지 않는다** — 선택된 계층/번들의 것만.
5. **관련 없는 checklist는 읽지 않는다** — 검증 시점에 `checklists/<type>.md`만.
6. **완료 후 요약만 memory에 남긴다** — 전체 트랜스크립트가 아니라 *타입이 지정된
   요약*(ADR 0005 §2.5 거버넌스 준수).

### 2.2 always-loaded vs on-demand 경계

| 시점 | 로드 대상 | 비고 |
|---|---|---|
| 상시(always) | `AGENTS.md`/`CLAUDE.md`(얇은 규칙+라우팅), 활성 `TaskContract`, ledger tail | 라우팅 결정에 필요한 최소치 |
| 작업 유형 확정 후 | `workflow/<type>.md` 1개 | L1 디스패치(ADR 0004) 직후 |
| 실행 중 스킬 매칭 시 | `skills/<name>.md` | authored 우선, earned 후순(ADR 0005 §2.4) |
| 검증 시점 | `checklists/<type>.md` | ADR 0001 Verification 조항과 연결 |
| 계획/실행 중 필요 시 | scoped `memory/<type>/…` | 현재 작업 관련 guardrail·decision만, 전체 X |
| 완료 시 | (쓰기) `memory/<type>/…` 타입 요약 1건 | distillation, provenance ref 동반 |

### 2.3 권장 파일 레이아웃 ↔ 현재 구조 매핑

| 권장(요청) | 역할 | 현재 구조 | 처리 |
|---|---|---|---|
| `AGENTS.md` | **Codex 전용** 실행 규칙 | `AGENTS.md`(generic) + `.codex/causality-routing.md` | **재정의**: AGENTS.md를 Codex 실행 규칙 단일 진입점으로. `.codex/` 라우팅을 여기로 병합 |
| `CLAUDE.md` | **Claude 전용** 실행 규칙 | `CLAUDE.md`(Claude) | 유지(얇게 유지) |
| `workflow/*.md` | 작업 유형별 워크플로 | `.claude/commands/*.md` + `.causality/causality-workflows.json` + `workflows.py` | **단일 출처 = `workflows.py`/manifest**, `workflow/*.md`는 *생성 뷰*. 슬래시 커맨드는 본문 대신 `workflow/<type>.md`를 가리키는 thin 포인터 |
| `checklists/*.md` | 검증 체크리스트 | (없음) | **신규**. ADR 0001 Verification + gstack QA 체크리스트 수용 |
| `skills/*.md` | 재사용 성공 절차 | (개념만 ADR 0005) | **신규**. authored + earned 스킬 문서(ADR 0005 §2.4) |
| `memory/*.md` | 장기 기억 | (개념만 ADR 0005 §2.2) | ADR 0005의 6-타입과 **동일**: `memory/{decisions,assumptions,failures,playbooks,snippets,retrospectives}/` |

### 2.4 운영 규칙 블록 (AGENTS.md/CLAUDE.md/agent-rules.md에 그대로 삽입)

```markdown
## Context Economy (always-loaded vs on-demand)
- Always load only: this file (thin rules + routing), the active TaskContract,
  the ledger tail. Never paste long workflows, checklists, role descriptions,
  or design templates into the prompt.
- After the task type is fixed (Agent Harness): read only `workflow/<type>.md`.
- Load a skill only when matched: `skills/<name>.md` (authored takes precedence).
- At verification: read only `checklists/<type>.md`.
- Retrieve only scoped memory for the current task (its guardrails/decisions),
  never the whole `memory/` store.
- On completion: append only a typed summary to `memory/<type>/`, with a
  provenance ref to the ledger entry_hash (see ADR 0005 §2.5).
```

## 3. 충돌·중복 해소 (Conflicts)

| ID | 충돌/중복 | 해소 |
|---|---|---|
| C-CTX-1 | 워크플로 4중 표현(`workflows.py` / `causality-workflows.json` / `.claude/commands/*` / 신규 `workflow/*.md`) | **단일 출처 = `workflows.py`/manifest**. 나머지는 모두 *생성 뷰*. 손으로 4곳을 동기화하지 않는다 |
| C-CTX-2 | AGENTS.md가 generic이라 Codex 규칙이 `.codex/`와 분산 | AGENTS.md=Codex 단일 진입점으로 재정의(C-ROUTE-1 라우팅 3중과 함께 처리) |
| C-CTX-3 | Contract Harness(ADR 0003)는 매 실행 전 guardrail 로드 → "always-loaded 최소화"와 충돌 우려 | 충돌 아님: Harness는 *현재 작업에 scoped된 소수 guardrail*만 로드(전체 `memory/` X). 오히려 본 원칙의 사례 |
| C-CTX-4 | "완료 후 요약만 memory" vs ledger 전체 기록 | 충돌 아님: ledger(L4)=raw 전체, memory(L0)=타입 요약. ADR 0005/0006과 정합 |
| C-CTX-5 | agent-rules.md가 라우팅+루프+브라우저+커맨드+MCP를 *상시 로드* | 얇은 코어(규칙+라우팅 포인터)만 상시, 나머지는 on-demand 파일로 분리 |

## 4. 검토한 대안 (Alternatives)

| 대안 | 판단 |
|---|---|
| A. 모든 규칙·체크리스트·역할을 한 파일에 상시 로드 | **기각** — 컨텍스트 팽창·비용·오염(현 문제) |
| B. 원칙을 산문 권고로만 둠 | **기각** — 집행 불가(이전 ADR들의 교훈) |
| C. (채택) 얇은 상시 코어 + 유형별 on-demand 파일 + 단일 출처 생성 뷰 | **채택** |

## 5. 영향 (Consequences)

**긍정:** 프롬프트 토큰·지연 절감, 무관 컨텍스트로 인한 오염·산만 감소, 역할/워크플로
확장이 상시 비용을 늘리지 않음.

**부정/비용:** 파일 분리·생성 뷰 동기화 도구가 필요. `checklists/`·`skills/`는 신규
디렉터리. 잘못 분할하면 "필요한데 안 읽음" 누락 위험 → 라우팅이 정확해야 한다(ADR 0004).

**중립:** 슬래시 커맨드 이름·UX 유지(본문만 thin 포인터화).

### 5.1 신규 vs 재사용

| 구분 | 항목 |
|---|---|
| **신규** | `workflow/*.md`·`checklists/*.md`·`skills/*.md` 디렉터리(생성) · AGENTS.md 재정의 · 운영 규칙 블록(§2.4) · manifest→`workflow/*.md` 생성기 |
| **재사용** | `session-bootstrap` 필터(`workflows.py:108`) · `workflows.py`/manifest(단일 출처) · `memory/` 6-타입(ADR 0005) · L1 라우팅(ADR 0004) |
| **현재 상태** | 위 디렉터리·생성기는 **미구현**. 본 ADR은 운영 규칙과 레이아웃의 *명세*다(전 ADR `Proposed`) |
