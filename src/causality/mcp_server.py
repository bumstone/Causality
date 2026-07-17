from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .agent_bootstrap import (
    SUPPORTED_CLIENTS,
    _assert_safe_install_path,
    _ensure_private_ignore,
    _private_tracking_issue,
    install_agent_files,
)
from .contracts import AuditEventType
from .ledger import EvidenceLedger
from .workflows import workflow_manifest


def _text_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


class CausalityMCPServer:
    """Minimal stdio JSON-RPC server exposing Causality helper tools."""

    def __init__(self, project: str | Path = "."):
        self.project = Path(project).resolve()
        tracking_issue = _private_tracking_issue(self.project)
        if tracking_issue:
            raise ValueError(tracking_issue)
        causality_dir = self.project / ".causality"
        privacy_path = causality_dir / ".gitignore"
        ledger_path = causality_dir / "ledger.jsonl"
        _assert_safe_install_path(self.project, privacy_path)
        _assert_safe_install_path(self.project, ledger_path)
        _assert_safe_install_path(self.project, Path(str(ledger_path) + ".lock"))
        causality_dir.mkdir(parents=True, exist_ok=True)
        _ensure_private_ignore(privacy_path)
        self.ledger = EvidenceLedger(ledger_path)

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        if method == "notifications/initialized":
            return None
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "causality", "version": "0.1.0"},
                }
            elif method == "tools/list":
                result = {"tools": self._tools()}
            elif method == "tools/call":
                params = request.get("params", {})
                result = self._call_tool(params.get("name"), params.get("arguments", {}))
            else:
                return self._error(request_id, -32601, f"unknown method: {method}")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:  # pragma: no cover - defensive JSON-RPC boundary
            return self._error(request_id, -32000, str(exc))

    def _tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "causality_init",
                "description": "Install project-level Causality agent files.",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "client": {
                            "type": "string",
                            "enum": list(SUPPORTED_CLIENTS),
                            "default": "auto",
                        },
                        "verify": {"type": "boolean", "default": False},
                    },
                },
            },
            {
                "name": "causality_context",
                "description": "Return recent ledger metadata and workflow names.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "default": 5}},
                },
            },
            {
                "name": "causality_append_evidence",
                "description": "Append an evidence event to the local ledger.",
                "inputSchema": {
                    "type": "object",
                    "required": ["kind", "payload"],
                    "properties": {
                        "kind": {"type": "string"},
                        "payload": {"type": "object"},
                        "contract_id": {"type": "string"},
                    },
                },
            },
            {
                "name": "causality_workflows",
                "description": "Return the available Causality workflow manifest.",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "causality_init":
            cli_only = {"force", "adopt"}.intersection(arguments)
            if cli_only:
                names = ", ".join(sorted(cli_only))
                raise ValueError(f"{names} are CLI-only options requiring explicit operator action")
            unknown = set(arguments).difference({"client", "verify"})
            if unknown:
                raise ValueError(f"unknown causality_init options: {', '.join(sorted(unknown))}")
            client = arguments.get("client", "auto")
            if not isinstance(client, str) or client not in SUPPORTED_CLIENTS:
                raise ValueError(f"client must be one of: {', '.join(SUPPORTED_CLIENTS)}")
            verify = arguments.get("verify", False)
            if not isinstance(verify, bool):
                raise ValueError("verify must be a boolean")
            result = install_agent_files(
                self.project,
                force=False,
                client=client,
                adopt=False,
                verify=verify,
            )
            return _text_result(json.dumps(result.to_dict(), ensure_ascii=True, indent=2))

        if name == "causality_context":
            limit = int(arguments.get("limit", 5))
            context = {
                "project": str(self.project),
                "ledger_tail": self.ledger.context_tail(limit),
                "workflows": [item["name"] for item in workflow_manifest()["workflows"]],
            }
            return _text_result(json.dumps(context, ensure_ascii=True, indent=2))

        if name == "causality_append_evidence":
            payload = {"kind": arguments["kind"], **dict(arguments.get("payload", {}))}
            event = self.ledger.append(
                AuditEventType.EVIDENCE,
                payload,
                contract_id=arguments.get("contract_id"),
            )
            return _text_result(json.dumps({"event_id": event.event_id}, ensure_ascii=True))

        if name == "causality_workflows":
            return _text_result(json.dumps(workflow_manifest(), ensure_ascii=True, indent=2))

        raise ValueError(f"unknown tool: {name}")

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(project: str | Path = ".") -> int:
    server = CausalityMCPServer(project)
    for line in sys.stdin:
        if not line.strip():
            continue
        response = server.handle(json.loads(line))
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
        sys.stdout.flush()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Causality MCP-style stdio server")
    parser.add_argument("--project", default=".")
    args = parser.parse_args()
    return serve(args.project)


if __name__ == "__main__":
    raise SystemExit(main())
