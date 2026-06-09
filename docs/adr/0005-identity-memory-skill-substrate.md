# ADR 0005 — 정체성·기억·스킬 기반층 (Synergy 일부 차용)

- **상태(Status):** Accepted — 부분 구현 (typed memory 거버넌스, 2026-06-09)
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

### 2.2 Typed Memory — 6개 타입으로 분리 (오염 방지)

> 리뷰 반영(2026-06-09): 초안의 4-타입(retro/work-log/decision/failure)은
> **`assumptions` 타입이 없어 임시 가정을 `decision`으로 기록**하게 만들고,
> `decision`을 "ADR과 동일"로 융합해 가정이 최고 신뢰 지식으로 둔갑하는
> 구조적 오염 경로를 가졌다(C-MEM-1). 아래 6-타입으로 교체한다.

기억은 **타입별로 물리적으로 분리**한다. 특히 `assumptions`(임시 가정)와
`decisions`(확정 의사결정)를 섞으면 아키텍처가 장기적으로 붕괴한다.

```text
memory/
  decisions/      # 확정된 의사결정 (승급 게이트를 통과한 것만)
  assumptions/    # 임시 가정 (미확정 · 만료/승급 대상)
  failures/       # 실패 사례 (범위·신뢰도 메타 포함)
  playbooks/      # 재사용 가능한 절차 (earned skill 후보)
  snippets/       # 코드/명령어 조각
  retrospectives/ # 회고
```

| 타입 | 신뢰 수준 | 활용 | 오염 방지 규칙 |
|---|---|---|---|
| `decisions` | 높음(확정) | 계획·라우팅의 전제 | **`assumptions`에서 승급 게이트(§2.5)를 통과해야만 진입** |
| `assumptions` | 낮음(잠정) | 후보 전제, 검증 대상 | **만료(TTL)·신뢰도 필수. 확정 전엔 계획의 *전제*로 쓰지 않음** |
| `failures` | 사례 | guardrail 후보(ADR 0003) | **범위(scope)·신뢰도·재현 횟수 메타 필수. 무조건 영구 non-goal 금지(§2.5)** |
| `playbooks` | 사례→승급 | earned skill 후보 | **authored 스킬과 dedup 후 HITL 승급(§2.4)** |
| `snippets` | 참조 | 도구 호출 재사용 | 출처 ref 필수 |
| `retrospectives` | 회고 | trajectory 추출 입력 | 가정/결정을 명시적으로 라벨링 |

**출처 연결(provenance) — 해시 체인 보존:** 모든 memory 항목은 자신을 정당화한
ledger 이벤트의 `entry_hash`를 `provenance` 필드로 보관한다(`ledger.py:30-40`).
이로써 "이 `decision`을 입증하는 ledger 이벤트는?"에 답할 수 있고 `verify_chain()`
보증이 L0 경계에서 끊기지 않는다.

> **현재 상태 vs 제안(중요):** Typed Memory는 EvidenceLedger의 *증류물(distilled)* 로
> 설계하지만, **증류 메커니즘은 아직 없다.** `build_session_bootstrap`
> (`workflows.py:108-123`)은 ledger를 **읽지 않고** 호출자가 넘긴 `memory_facts`
> 리스트의 `source` 필드만 필터한다(`workflows.py:118-122`). 따라서 실제 구현에는
> ① ledger 이벤트(`AuditEventType`)를 읽어 typed-memory 항목을 생성하는 distiller,
> ② ledger payload에 `source`/`provenance`를 기록하는 변경, ③ memory 스토어 자체가
> **모두 신규**로 필요하다. 초안의 "이 지점이 증류 게이트다"는 과장이었다 —
> 이 필터는 *증류*가 아니라 *입력 화이트리스트*에 불과하다.

### 2.3 Agenda — 장기 목표 / 대기 작업

- 개별 `GoalContract` *위*의 백로그. 항목은 **계약 이전 의도(pre-contract
  intention)** 이며, Agent Harness(ADR 0004)가 선택하는 순간 `GoalContract`로
  인스턴스화된다 → GoalContract와 중복되지 않는다.

### 2.4 Rewarded Trajectory → 재사용 스킬

- **성공한 작업 절차**를 재사용 가능한 **earned skill**로 증류.
- 스킬은 두 계층으로 구분:
  - **authored skill**: gstack/Superpowers/`workflows.py` — 고정·큐레이션된 플레이북.
  - **earned skill**: rewarded trajectory에서 추출된 프로젝트 고유 스킬.
- 승급 조건(아래 4개를 **모두** 충족해야 라이브러리 등재):
  1. 완료 게이트 PASS + verifier 2-pass (`gates.py:62-98`) — *결과* 검증.
  2. **재현성 n-of-m** — 같은 절차가 서로 다른 입력/상태에서 m회 중 n회 성공
     (1회 성공/운/취약 경로 배제). 결과 게이트는 *outcome*만 보므로 *절차 품질*은
     별도 신호가 필요하다.
  3. **authored 스킬과 dedup** — 기존 authored 플레이북(예: `test-driven-development`,
     `workflows.py:52`)과 의미 중복이면 등재 거부(스킬 폭증 방지).
  4. **HITL 승급 승인** — 기존 승인 메커니즘 재사용.
- 디스패치 우선순위: **authored > earned**(동일 작업에 둘 다 매칭 시 authored 우선).

> **현재 상태 vs 제안:** `gates.py:62-98`(`HITLGate.complete`)는 한 계약의 완료
> 여부만 yes/no로 답할 뿐, *trajectory(행동 순서)* 를 반환하지 않는다. 따라서
> ledger의 행동 시퀀스를 읽어 절차를 추출하는 distiller와 skill 스토어, 재현성
> 측정기는 **모두 신규**다. 초안이 `complete`을 earned-skill 파이프라인의 근거로
> 인용한 것은 과장이었다.

### 2.5 메모리 거버넌스 — 오염 방지 규칙 (리뷰 반영)

"과거 실패한 판단"이나 "임시 아이디어"가 장기 지식처럼 재사용되는 것을 막는
4가지 게이트. 모두 **provenance(ledger ref)** 를 동반해 기록한다.

1. **승급 게이트 (assumption → decision):** 가정은 자동으로 결정이 되지 않는다.
   *확정 증거*(verifier pass / 인간 승인 / 도구 출력)가 가정을 입증할 때만,
   그 evidence ref와 함께 `decisions/`로 승급하는 **명시적 이벤트**를 남긴다.
   승급되지 않은 `assumptions/` 항목은 계획의 *전제*로 사용 금지(후보로만 노출).
2. **검증은 출처가 아니라 유효성으로 (validity, not provenance):** 현재 유일한
   필터 `build_session_bootstrap`(`workflows.py:121`)은 *누가 기록했나*(`source`)만
   본다 → "한 번 승인 = 영원히 참"이라는 오염 벡터. 따라서 읽기 시점에
   **재검증/만료 확인**을 추가한다(아래 3).
3. **만료·신뢰도·범위 (failures → guardrail):** `failures/`→`non_goals` 자동주입은
   단방향 래칫이 되면 안 된다. 각 guardrail은 **TTL, 재현 횟수, 작업 유형 scope**를
   갖는다. 일회성(플래키)·이미 수정된 원인에서 온 guardrail은 만료/회수된다.
   `non_goals`는 bare tuple이 아니라 **메타데이터를 동반**해야 한다(ADR 0001 §2.1
   `non_goals` 필드 확장).
4. **회수 경로 (revocation):** 잘못 승급된 decision·과적합 guardrail·취약 earned
   skill은 회수 이벤트로 무효화할 수 있어야 한다(append-only ledger에 회수 기록).

> 핵심: `assumptions`와 `decisions`를 분리하지 않으면, 승급/만료/검증 게이트가
> 없으면, 단 하나의 입력 필터가 "approved-once = true-forever"로 오염을 통과시킨다.
> 이 ADR은 그 경계를 타입과 게이트로 강제한다.

## 3. 검토한 대안 (Alternatives)

| 대안 | 판단 |
|---|---|
| A. Synergy 전체 도입(협업망·사회관계 포함) | **기각** — 로컬 우선 범위 초과, 사용자 요구와 불일치 |
| B. 기억을 ledger에만 저장 | **기각** — raw 증거와 증류 지식을 혼동, 컨텍스트 비용↑ |
| C. earned skill 자동 등재 | **기각** — 미검증 절차 오염 |
| D. 4-타입 단순 memory(assumptions 없음) | **기각** — 가정↔결정 융합 오염(리뷰 C-MEM-1) |
| E. memory를 ledger의 typed projection으로만 구현 | **부분 채택** — provenance(ledger ref) 연결은 채택, 단 읽기 패턴·신뢰수준이 달라 별도 스토어 유지 |
| F. (채택) 6-타입 분리 + provenance + 4-게이트 거버넌스 | **채택** |

## 4. 영향 (Consequences)

**긍정:** 진화 루프(경험→기억→스킬) 확보. 실패가 guardrail로, 성공이 스킬로
환류되어 비용·재계획이 시간이 지날수록 감소.

**부정/비용:** 신규 스토어 2개(typed memory, agenda) + 승급 게이트 1개. 증류 정책
(무엇을 기억에 남길지)을 정의해야 한다.

**중립:** 협업/사회 기능을 의도적으로 배제하므로 멀티 에이전트 *네트워크* 가 아니라
**단일 프로젝트의 장기 기억**으로 한정된다.
