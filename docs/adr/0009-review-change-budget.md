# ADR 0009 — Reviewable Change Budget: ≤1000줄 단위 리뷰

- **상태(Status):** Accepted — 구현 (`review_batches.py` + `causality review-plan`, 2026-06-13)
- **관련:** [ADR 0006](0006-final-blended-architecture.md) · [ADR 0007](0007-context-economy-progressive-disclosure.md)

## 1. 동기 (Context)

리뷰 단위가 커지면 리뷰 품질이 급락한다 — 사람은 큰 diff에서 피로로 결함을 놓치고,
외부 리뷰 에이전트(Codex/Greptile 등)는 컨텍스트·토큰 한계로 후반부를 부실하게 본다.
실제로 이 저장소의 리브랜드 PR(전 파일 토큰 치환)은 한 번에 보기에 과대했다.

따라서 **모든 리뷰 단위를 1000줄 이하로 제한**한다. 두 경로 모두 규율한다:

1. **PR로 리뷰하는 경우** — PR 하나가 1000줄을 넘지 않도록 작업을 쪼개서 연다.
2. **PR을 쓰지 않는 경우** — 아직 리뷰하지 않은 변경점(working/branch diff)을 1000줄
   이하 배치로 나눠 배치마다 리뷰(`/code-review` 등)를 1회씩 돌린다.

## 2. 결정 (Decision)

### 2.1 규칙

- **리뷰 예산 = 배치당 변경 줄 ≤ 1000** (`DEFAULT_MAX_LINES`).
- **줄 계산** = `added + deleted`(리뷰어가 실제로 읽는 양; 포지(forge)의 "lines
  changed"와 동일). 바이너리 파일은 0줄로 계산.
- **제외(예산에서 빼는 것):** gitignore된 파일(이미 diff에 안 잡힘), 생성 산출물
  (`docs/assets/*` SVG, `docs/_review/*`), lock 파일, 벤더링 코드. fnmatch glob으로
  지정.
- **단일 파일이 1000줄을 초과**하면 그 파일만의 배치로 분리하고 `oversized`로 표시 —
  규칙은 그 파일을 **hunk/라인-레인지 단위로 내부 분할**해 리뷰하도록 요구한다.

### 2.2 PR 경로 — 1000줄 이하로 쪼개서 열기

작업을 PR로 올리기 전에 `causality review-plan`으로 분할 계획을 본다. 배치가 2개
이상이면 **여러 PR로 분리**한다(권장 순서):

1. **관심사(concern)별** — 한 PR은 하나의 ADR/기능/수정 계열만. (이 ADR 자체도
   루프-수정 PR과 분리해 별도 PR로 연다 = dogfooding.)
2. **의존성 순서대로** — 토대 → 그 위 계층(스택형 PR 가능).
3. **디렉터리/계층별** — `src/` 코어와 `docs/`·`tests/`를 분리하기보다, 한 변경의
   코드+테스트+문서는 같은 PR에 두되 전체가 예산 이내가 되도록 작업 자체를 잘게.

### 2.3 비-PR 경로 — diff를 배치로 나눠 리뷰

PR 없이 로컬/브랜치 변경을 리뷰할 때:

```bash
causality review-plan --base origin/main            # 기본: 작업트리 포함(미커밋 변경까지)
causality review-plan --base origin/main --json      # 기계가 소비할 형태
causality review-plan --base origin/main --committed # 커밋만(base...HEAD), PR 계획용
git diff --numstat origin/main | causality review-plan --from-file -
```

**중요(codex r3407190893):** 비-PR 경로의 핵심은 "아직 리뷰 안 한 *로컬* 변경"이고
그건 대개 **미커밋** 상태다. 따라서 기본은 `git diff <base>`(작업트리 대비)로 미커밋
변경을 포함한다 — 커밋-대-커밋(`base...HEAD`)만 보면 큰 로컬 diff가 "(변경 없음)"으로
예산을 우회한다. 커밋분만 보려면 `--committed`. (추적되지 않은 새 파일은 `git diff`에
안 잡히므로 `git add -N`으로 intent-add 후 계획한다.)

`/code-review`(또는 외부 리뷰 에이전트)는 이 계획을 받아 **배치마다 한 번씩** 리뷰를
수행한다. 즉 "리뷰하지 않은 변경점"을 1000줄 이하 묶음으로 나눠 순차 리뷰한다.

### 2.4 예외(override)

진짜로 쪼갤 수 없는 원자적 변경(전역 리네임/포매터 적용/생성 파일 일괄 갱신)은:

- 커밋/PR 본문에 `review-budget: exempt — <사유>`를 명시하고,
- 리뷰는 **대표 표본 + 기계적 변환 노트**(예: "sed로 토큰 X→Y 치환, 나머지 동일")로
  대체한다. 무분별한 면제를 막기 위해 사유는 필수.

### 2.5 집행 (도구)

규칙은 산문이 아니라 **실행 가능**해야 한다(프로젝트 기조, ADR 0001):

- `src/causality/review_batches.py` — `parse_numstat` → `plan_review_batches`
  (그리디 패킹, oversized 플래그) → `format_plan`. 순수 함수라 테스트됨
  (`tests/test_review_batches.py`).
- `causality review-plan` CLI — git numstat을 읽어 배치 계획을 출력하고, **예산
  초과 시 종료코드 2**를 반환(스크립트/CI가 분기 가능).
- (후속) CI advisory job: PR diff가 예산을 넘으면 빌드 로그에 경고(비차단). 하드
  차단은 §2.4 면제 흐름이 정착한 뒤 검토.

## 3. 검토한 대안 (Alternatives)

| 대안 | 판단 |
|---|---|
| 줄 수 제한 없이 리뷰어 재량 | **기각** — 큰 diff에서 결함 누락이 실측됨 |
| 파일 수로 제한 | **기각** — 파일 1개가 5000줄일 수 있음; 줄이 리뷰 부하의 직접 척도 |
| net diff(추가−삭제)로 계산 | **기각** — 삭제도 읽어야 함; added+deleted가 정직 |
| 하드 차단(>1000 PR 거부) | **부분** — 정당한 대형 기계적 변경을 막음 → advisory + override로 시작 |
| (채택) added+deleted ≤1000 배치 + 도구 + override | **채택** |

## 4. 영향 (Consequences)

**긍정:** 리뷰 1회의 인지·토큰 부하가 상한선 아래로 고정 → 사람·에이전트 리뷰 품질
일관. 외부 리뷰 에이전트 루프(ADR 0006/코드리뷰 워크플로)와 자연 결합(배치=리뷰 1회).
PR 이력이 관심사별로 깔끔.

**부정/비용:** 작업을 미리 쪼개는 계획 비용; 스택형 PR 관리 부담; 줄 계산이 git
numstat에 의존.

**중립:** 예산값(1000)은 `--max-lines`로 조정 가능. 제외 목록은 호출자가 glob으로 전달.
