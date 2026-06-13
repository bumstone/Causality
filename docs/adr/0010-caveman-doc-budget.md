# ADR 0010 — Caveman Doc Budget

- 상태: Accepted — 구현 (`doc_budget.py` + `causality doc-budget`, 2026-06-13)
- 관련: [0007](0007-context-economy-progressive-disclosure.md)(context economy) · [0009](0009-review-change-budget.md)(review budget)

## 동기

AI 생성 MD가 길수록 (1) 생성 토큰 낭비, (2) 매 로드 비용↑. 기존 ADR 4k~15k자.
context economy(0007)는 *로드*를 줄였고, 본 규칙은 *생성 크기*를 줄인다.

## 결정

- **크기**: AI 생성 working MD ≤ **2000자/파일**(≈500 tok). 초과 → 분할 또는 압축.
- **문체 = caveman**: 텔레그래프식. 표·불릿 > 산문. 식별자·결정·근거만 남김.
  군더더기(전환구·헤징·재진술) 제거. "AI가 파싱 가능한 최소 형체"가 목표.
- **면제**: `README*.md`, `THIRD_PARTY_NOTICES.md`, `LICENSE` (사람용 정본).
- **grandfather**: 0010 이전 문서는 그대로 둠(기회 될 때 축약). 신규만 강제.
- **깊은 설계**: 산문 늘리지 말고 코드+표+다이어그램(SVG)로.

## 집행

- `doc_budget.check_docs/expand_markdown` → 디렉터리는 `*.md`로 전개, 비UTF8/누락 스킵.
- `causality doc-budget [paths]` = 기본 advisory(exit 0); `--enforce`면 초과 시 **exit 2**
  (CI는 `--enforce <변경 파일>`). 기본 advisory라 grandfather 문서가 bare run을 깨지 않음.
- `agent-rules.md` Context Economy에 1줄 추가 → 생성 시점에 적용.
- dogfood: 본 ADR ≤2000자.

## 대안

| 안 | 판정 |
|---|---|
| 토큰 직접 카운트 | 기각 — tokenizer 의존. 글자수가 근사·결정적 |
| 하드 차단 | 부분 — advisory + 면제 + grandfather로 시작 |
| 제한 없음 | 기각 — 현 ADR 평균 7k자, 토큰 부담 실측 |

## 영향

- **+** 토큰↓, 로드↓, 결정 가독성↑(스캔 빠름).
- **−** 표현 압축 부담; 뉘앙스 손실 위험 → 깊은 건 코드/표로 보완.
- 중립: 기존 장문 ADR은 디버트로 남음(`doc-budget`이 가시화).
