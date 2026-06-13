from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .agent_bootstrap import install_agent_files
from .contracts import AuditEventType
from .doc_budget import DEFAULT_DOC_MAX_CHARS, check_docs, format_report, over_budget
from .ledger import EvidenceLedger
from .review_batches import (
    DEFAULT_MAX_LINES,
    format_plan,
    parse_numstat,
    plan_review_batches,
)
from .workflows import workflow_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Causality integration helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create a ledger and workflow manifest")
    init_parser.add_argument("--ledger", default=".causality/ledger.jsonl")
    init_parser.add_argument("--manifest", default=".causality/causality-workflows.json")

    manifest_parser = subparsers.add_parser("manifest", help="print workflow manifest")
    manifest_parser.add_argument("--pretty", action="store_true")

    context_parser = subparsers.add_parser("context", help="print project ledger tail and workflows")
    context_parser.add_argument("--ledger", default=".causality/ledger.jsonl")
    context_parser.add_argument("--limit", type=int, default=5)
    context_parser.add_argument("--pretty", action="store_true")

    install_parser = subparsers.add_parser(
        "install-agent",
        help="install project-level AGENTS.md, CLAUDE.md, and MCP-style config",
    )
    install_parser.add_argument("--project", default=".")
    install_parser.add_argument("--force", action="store_true")

    review_parser = subparsers.add_parser(
        "review-plan",
        help="split a diff into <=N-line review batches (ADR 0009)",
    )
    review_parser.add_argument("--base", default="origin/main", help="diff base ref (default origin/main)")
    review_parser.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    review_parser.add_argument(
        "--committed",
        action="store_true",
        help="plan only committed changes (base...HEAD), e.g. for PR planning; "
        "default includes uncommitted working-tree changes (base vs working tree)",
    )
    review_parser.add_argument(
        "--from-file",
        help="read `git diff --numstat` from this file (use '-' for stdin) instead of running git",
    )
    review_parser.add_argument(
        "--exclude", action="append", default=[], metavar="GLOB", help="path glob to drop (repeatable)"
    )
    review_parser.add_argument("--json", action="store_true")

    doc_parser = subparsers.add_parser(
        "doc-budget",
        help="flag generated MD docs over the caveman char budget (ADR 0010)",
    )
    doc_parser.add_argument("paths", nargs="*", help="MD files (default: docs/**/*.md)")
    doc_parser.add_argument("--max-chars", type=int, default=DEFAULT_DOC_MAX_CHARS)
    doc_parser.add_argument("--json", action="store_true")

    args = parser.parse_args()
    if args.command == "init":
        ledger = EvidenceLedger(args.ledger)
        manifest = workflow_manifest()
        manifest_path = Path(args.manifest)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2), encoding="utf-8")
        ledger.append(
            AuditEventType.EVIDENCE,
            {"kind": "workflow_manifest", "path": str(manifest_path)},
            artifact_paths=[manifest_path],
        )
        print(f"Initialized ledger: {ledger.path}")
        print(f"Wrote workflow manifest: {manifest_path}")
        return 0

    if args.command == "manifest":
        indent = 2 if args.pretty else None
        print(json.dumps(workflow_manifest(), ensure_ascii=True, indent=indent))
        return 0

    if args.command == "context":
        ledger = EvidenceLedger(args.ledger)
        context = {
            "ledger": str(Path(args.ledger)),
            "ledger_tail": ledger.tail(args.limit),
            "workflows": [item["name"] for item in workflow_manifest()["workflows"]],
        }
        indent = 2 if args.pretty else None
        print(json.dumps(context, ensure_ascii=True, indent=indent))
        return 0

    if args.command == "install-agent":
        result = install_agent_files(args.project, force=args.force)
        print(json.dumps(result.to_dict(), ensure_ascii=True, indent=2))
        return 0

    if args.command == "review-plan":
        if args.from_file == "-":
            numstat = sys.stdin.read()
        elif args.from_file:
            numstat = Path(args.from_file).read_text(encoding="utf-8")
        else:
            # Default compares the base to the WORKING TREE (`git diff <base>`),
            # which includes uncommitted tracked changes -- otherwise a large
            # local diff reviewed before committing reports "(no changes)" and
            # bypasses the budget (codex review r3407190893). `--committed` uses
            # the commit-to-commit form for PR planning. (Untracked new files are
            # not shown by git diff; `git add -N` them to include them.)
            diff_range = f"{args.base}...HEAD" if args.committed else args.base
            proc = subprocess.run(
                ["git", "diff", "--numstat", diff_range],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                print(proc.stderr.strip(), file=sys.stderr)
                return 1
            numstat = proc.stdout
        batches = plan_review_batches(
            parse_numstat(numstat), max_lines=args.max_lines, exclude=args.exclude
        )
        if args.json:
            print(json.dumps([b.to_dict() for b in batches], ensure_ascii=True, indent=2))
        else:
            print(format_plan(batches, max_lines=args.max_lines))
        # Exit 2 signals "exceeds budget" so CI/scripts can branch on it.
        return 2 if len(batches) > 1 or any(b.oversized for b in batches) else 0

    if args.command == "doc-budget":
        paths = args.paths or [str(p) for p in Path("docs").rglob("*.md")]
        sizes = check_docs(paths, max_chars=args.max_chars)
        if args.json:
            print(json.dumps([d.to_dict() for d in sizes], ensure_ascii=True, indent=2))
        else:
            print(format_report(sizes, max_chars=args.max_chars))
        return 2 if over_budget(sizes) else 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
