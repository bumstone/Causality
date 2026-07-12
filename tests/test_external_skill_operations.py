from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ExternalSkillOperationTests(unittest.TestCase):
    def test_installed_stdio_exposes_skill_tools_and_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
            proc = subprocess.Popen([sys.executable, "-m", "causality.mcp_server"], cwd=td, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            self.addCleanup(proc.kill)
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n")
            proc.stdin.flush()
            response = json.loads(proc.stdout.readline())
            names = {item["name"] for item in response["result"]["tools"]}
            self.assertIn("causality_skill_outcome", names)
            self.assertIn("causality_skill_promote", names)
            proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)
            proc.stdout.close()
