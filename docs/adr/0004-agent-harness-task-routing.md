# ADR 0004 — Agent Harness: 작업 유형별 아키텍처 디스패치

- **상태(Status):** Proposed
- **날짜:** 2026-06-09
- **관련:** [ADR 0002](0002-three-layer-control-stack.md), [ADR 0003](0003-contract-harness.md), [ADR 0006](0006-final-blended-architecture.md)

## 1. 동기 (Context)

이 킷은 세 상류 아키텍처(Ouroboros / Superpowers / gstack)를 결합한다
(`README.md:4-8`). 이들을 **한 번에 섞어 직접 충돌**시키면(예: 모든 작업에 gstack
23 스페셜리스트 + Superpowers TDD + Ouroboros 루프를 동시에 적용) 비용·지연이
폭증하고 책임이 모호해진다.

또한 라우터가 이미 여럿이다: `agent-rules.md`의 intent 라우팅
(`agent_bootstrap.py:17-26`)과 ADR 0002 Stage Designer의 단계 분해가 부분적으로
겹친다. **라우팅 책임을 한 곳으로 모아야 한다.**

## 2. 결정 (Decision)

**작업 유형(task type)을 단일 디스패치 지점에서 분류**하고, 충돌 없이 *적합한
플레이북 번들 하나*만 호출한다. 이것이 Agent Harness이며, ADR 0002의 실행 제어
**위에** 위치한다.

```text
if task == product/feature planning:
    use gstack office-hours + ceo-review
elif task == implementation:
    use Superpowers TDD + debugging
elif task == long-running autonomous work:
    use Contract Harness + limited Ouroboros loop
elif task == release:
    use gstack ship + QA checklist
else:
    # 사소한 작업은 직접 응답 (agent-rules.md:28)
```

근거(gstack 라이프사이클 `office-hours → plan → implement → review → QA → ship →
retro`)에 비춰 매핑은 일관적이다:

| 작업 유형 | 호출 플레이북 | 상류 | 실행 시 표현(ADR 0002) |
|---|---|---|---|
| product/feature planning | office-hours + ceo-review | gstack | Stage Designer 중심 |
| implementation | TDD + debugging | Superpowers | Planner + Executor 중심 |
| long-running autonomous | Contract Harness + limited Ouroboros loop | Ouroboros | 3계층 풀 루프(bounded) |
| release | ship + QA checklist | gstack | Executor 중심 + 완료 게이트 |

### 2.1 라우터 중복 제거

- Agent Harness(L1, **어떤 플레이북인가**)와 Stage Designer(L3, **그 플레이북을
  어떤 단계로 실행하는가**)는 **고도가 다르다** → 중복이 아니라 위임 관계.
- `agent-rules.md`의 intent 라우팅은 Agent Harness로 **흡수·대체할 계획**이다.

> **현재 상태 vs 마이그레이션(리뷰 C-ROUTE-1):** intent 라우팅 프로즈는 현재
> `agent_bootstrap.py`가 **3곳**에 생성한다 — `AGENT_RULES`(생성 파일
> 17-26행), `AGENTS_MD`(`agent_bootstrap.py:77-83`), `CODEX_ROUTING`
> (`agent_bootstrap.py:173-182`). Agent Harness 디스패처는 **아직 코드에 없다.**
> 따라서 "흡수·대체"는 *제안*이며, 구현 시 **이 3곳의 라우팅 블록 제거(또는
> Agent Harness 위임으로 치환)** 를 명시적 마이그레이션 항목으로 둔다.
>
> **제3 라우터 주의(C-ROUTE-2):** gstack 라이프사이클
> `office-hours → plan → implement → review → QA → ship → retro` 자체가 *단계 순서
> 라우터*다. 이는 L1(번들 선택)도 L3(번들 내부 단계화)도 아닌 별도 축이 되기 쉽다.
> 처리: **라이프사이클의 단계 순서는 선택된 번들 *내부*의 책임**으로 귀속하고,
> L1은 "어떤 번들인가"만 결정한다(번들이 자신의 phase 순서를 안다).

## 3. 검토한 대안 (Alternatives)

| 대안 | 판단 |
|---|---|
| A. 모든 아키텍처 동시 적용(블렌딩) | **기각** — 사용자 요구("직접 충돌시키지 않고"), 비용·지연 폭증 |
| B. `agent-rules.md` intent 라우팅 유지 | **기각** — Agent Harness와 이중 라우터 |
| C. (채택) 단일 task-type 디스패치 → 플레이북 1개 | **채택** |

## 4. 영향 (Consequences)

**긍정:** 작업 유형마다 최소 플레이북만 실행 → 비용·지연 절감. 아키텍처 간 충돌
제거. 사소한 작업은 우회.

**부정/비용:** task-type 분류 오류 시 잘못된 플레이북 선택 → 분류 기준을 명시하고
모호하면 에스컬레이션(ADR 0001 Escalation 조항)으로 보완.

**중립:** `agent_bootstrap.py`의 라우팅 템플릿을 task-type 기준으로 재서술해야 하나
슬래시 커맨드 이름·UX는 유지된다.
