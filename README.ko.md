**한국어** | [English](README.md)

# Causality

Causality는 에이전트 기반 작업을 로컬에서 통제하고 감사하기 위한 제어 하네스입니다.
작업 목표를 계약으로 고정하고, 위험한 실행 전에는 게이트를 통과하게 하며, 결과는
append-only evidence ledger에 남깁니다. 검증된 실패와 성공은 이후 작업의
가드레일과 스킬 재사용으로 환류됩니다.

짧게 말하면, Causality는 프롬프트 모음이 아니라 **에이전트 작업을 증거 기반으로
운영하기 위한 작은 Python 런타임**입니다.

## 현재 상태

코어 제어 경로는 구현되어 회귀 테스트로 보호됩니다. 최신 검증 기준은
[Project Summary](docs/project-summary.md)를 확인하세요.

- 코어 제어 루프: 구현됨.
- plan/action/tool/non-goal 게이트: `run_task`, `ExecutionAdapter`, 파일/서브프로세스
  `ToolAdapter`에서 집행.
- HTTP/browser action: 명시적 capability와 scope 정책을 거쳐 persistent task
  lifecycle과 evidence ledger에서 집행.
- 실패 환류: scoped failure를 승인 후 다음 계약의 `non_goals`로 주입하고 TTL로 만료.
- 스킬 재사용: promoted earned skill을 objective 관련도로 회수해 실행에 표면화.
- verifier 품질: 공허한 pass와 blank evidence ref는 완료 quorum을 채우지 못함.
- secret 안전성: skill distill 경로에서 민감 키, 중첩 구조, token shape,
  bearer/basic authorization 값을 redaction.

이것은 “제품 전체 완성” 선언이 아닙니다. 남은 일은 playbook phase 실행화,
durable resume/skill operations, 외부 자동화 설정 같은 운영 품질입니다.

대표 문서:

- [Project Summary](docs/project-summary.md) — 압축된 아키텍처와 상태.
- [Status Dashboard](docs/status/roadmap.html) — 시각 상태판.
- [Delivery Plans](docs/plans/README.md) — 번호 기반 구현 순서.
- [Implementation Specs](docs/specs/README.md) — 기능 계약과 수용 테스트.
- [ADR Index](docs/adr/README.md) — 설계 결정 이력.

## 아키텍처

Causality는 실행을 다섯 계층으로 분리합니다.

1. **L0 Identity and Memory** — agenda, scoped memory, authored/earned skill.
2. **L1 Dispatch** — 작업 유형 라우팅과 민감 요청 fail-safe.
3. **L2 Contract** — objective, non-goals, tools, verification, stop,
   escalation을 고정한 Task Contract.
4. **L3 Execution Control** — plan/action/completion gate, bounded loop,
   review와 verifier decision.
5. **L4 Evidence Ledger** — durable write와 read cache를 갖춘 hash-chained
   JSONL 증거 원장.

제어는 L0에서 L4로 흐르고, reflection은 ledger evidence를 memory와 skill로 되돌립니다.

## 설치되는 것

`causality install-agent`는 얇은 프로젝트용 agent 파일을 설치합니다.

- Codex 라우팅용 `AGENTS.md`.
- Claude 명령 UX용 `CLAUDE.md`와 `.claude/commands/*`.
- `.causality/agent-rules.md`, workflow manifest, MCP config, local ledger.
- `/onboard`의 `skills/onboard-project.md`를 포함한 on-demand context:
  `workflow/`, `checklists/`, `skills/`, `memory/`.

`workflow/*.md`는 `src/causality/workflows.py`의 생성된 view입니다. 로컬 프로젝트에
의도적으로 커스터마이징하는 경우가 아니라면 직접 편집하지 않는 편이 안전합니다.

## 설치

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
python -m unittest discover -s tests
```

Windows 로컬 체크아웃:

```powershell
git clone https://github.com/bumstone/Causality.git D:\dev\Causality
cd D:\dev\Causality
.\scripts\install.ps1
```

Linux/WSL:

```bash
git clone https://github.com/bumstone/Causality.git ~/dev/Causality
cd ~/dev/Causality
bash scripts/install.sh
```

## CLI

```powershell
causality init
causality context --pretty
causality manifest --pretty
causality install-agent --client codex --adopt --verify
causality review-plan
causality doc-budget --enforce docs/project-summary.md
```

MCP 스타일 클라이언트:

```powershell
python -I -m causality.mcp_server --project .
```

## 저장소 구성

```text
src/causality/        런타임 패키지
tests/                회귀 테스트
docs/project-summary.md
docs/status/          현재 상태판
docs/plans/           번호 기반 구현 순서
docs/specs/           상세 구현 계약
docs/adr/             설계 결정 이력
workflow/             생성된 workflow view
scripts/              install/update/doctor helper
```

## 개발 원칙

- 통과한 테스트는 제거 대상이 아니라 회귀 방지 자산입니다.
- 최신 상태는 `docs/project-summary.md`와 `docs/status/roadmap.html`로 모읍니다.
- ADR은 결정 이력으로 유지하고, ADR index를 live status dashboard로 쓰지 않습니다.
- 생성된 install artifact는 긴 설명을 복제하지 말고 canonical rule로 링크합니다.

## 라이선스 및 출처 표기

이 저장소는 [MIT LICENSE](LICENSE) 하의 원본 구현이며, 상위 소스를 벤더링하지
않습니다. 참조한 상위 프로젝트는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에
명시되어 있습니다.
