from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .agent_bootstrap import SUPPORTED_CLIENTS, install_agent_files
from .contracts import AuditEventType
from .ledger import EvidenceLedger
from .workflows import workflow_manifest


def _text_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


class CausalityMCPServer:
    """Minimal stdio JSON-RPC server exposing Causality helper tools."""

    def __init__(self, project: str | Path = "."):
        self.project = Path(project).resolve()
        self.ledger = EvidenceLedger(self.project / ".causality" / "ledger.jsonl")

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
                    "properties": {
                        "force": {"type": "boolean", "default": False},
                        "client": {
                            "type": "string",
                            "enum": list(SUPPORTED_CLIENTS),
                            "default": "auto",
                        },
                        "adopt": {"type": "boolean", "default": False},
                        "verify": {"type": "boolean", "default": False},
                    },
                },
            },
            {
                "name": "causality_context",
                "description": "Return recent ledger events and workflow names.",
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
            result = install_agent_files(
                self.project,
                force=bool(arguments.get("force", False)),
                client=str(arguments.get("client", "auto")),
                adopt=bool(arguments.get("adopt", False)),
                verify=bool(arguments.get("verify", False)),
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
