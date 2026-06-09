from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent_bootstrap import install_agent_files
from .contracts import AuditEventType
from .ledger import EvidenceLedger
from .workflows import workflow_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Ouroboros HITL integration helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create a ledger and workflow manifest")
    init_parser.add_argument("--ledger", default=".ouroboros/ledger.jsonl")
    init_parser.add_argument("--manifest", default=".ouroboros/ouroboros-workflows.json")

    manifest_parser = subparsers.add_parser("manifest", help="print workflow manifest")
    manifest_parser.add_argument("--pretty", action="store_true")

    context_parser = subparsers.add_parser("context", help="print project ledger tail and workflows")
    context_parser.add_argument("--ledger", default=".ouroboros/ledger.jsonl")
    context_parser.add_argument("--limit", type=int, default=5)
    context_parser.add_argument("--pretty", action="store_true")

    install_parser = subparsers.add_parser(
        "install-agent",
        help="install project-level AGENTS.md, CLAUDE.md, and MCP-style config",
    )
    install_parser.add_argument("--project", default=".")
    install_parser.add_argument("--force", action="store_true")

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

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
