from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.orchestration_checkpoint import (
    CheckpointStore,
    OrchestrationCheckpoint,
    OrchestrationError,
    semantic_request_sha256,
)


class OrchestrationCheckpointTests(unittest.TestCase):
    def test_closed_checkpoint_is_secret_free_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = CheckpointStore(temp_dir, "controller-a")
            secret = "approval-proof-must-not-persist"
            arguments = {
                "task_id": "task-a",
                "idempotency_key": "operation-a",
                "proof": secret,
                "rationale": "reviewed",
            }
            checkpoint = OrchestrationCheckpoint(
                controller_id="controller-a",
                operation="causality_task_approve",
                idempotency_key="operation-a",
                request_sha256=semantic_request_sha256(
                    "causality_task_approve", arguments
                ),
                status="prepared",
                task_id="task-a",
            )
            store.save(checkpoint)

            raw = store.path.read_text(encoding="utf-8")
            self.assertNotIn(secret, raw)
            self.assertNotIn("rationale", raw)
            self.assertEqual(
                store.load(), OrchestrationCheckpoint.from_mapping(json.loads(raw))
            )
            corrupted = json.loads(raw)
            corrupted["unexpected"] = True
            store.path.write_text(json.dumps(corrupted), encoding="utf-8")
            with self.assertRaisesRegex(OrchestrationError, "schema is not closed"):
                store.load()

    def test_controller_mismatch_and_bad_timestamp_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = CheckpointStore(temp_dir, "controller-a")
            with self.assertRaisesRegex(OrchestrationError, "controller mismatch"):
                store.save(
                    OrchestrationCheckpoint(
                        controller_id="controller-b",
                        operation="begin",
                        idempotency_key="begin-a",
                        request_sha256="a" * 64,
                        status="prepared",
                    )
                )
            with self.assertRaisesRegex(OrchestrationError, "ISO-8601"):
                OrchestrationCheckpoint(
                    controller_id="controller-a",
                    operation="begin",
                    idempotency_key="begin-a",
                    request_sha256="a" * 64,
                    status="prepared",
                    updated_at="not-a-time",
                )

    def test_symlinked_checkpoint_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside:
            root = Path(temp_dir)
            runtime = root / ".causality"
            runtime.mkdir()
            try:
                (runtime / "orchestration").symlink_to(
                    Path(outside), target_is_directory=True
                )
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")
            with self.assertRaisesRegex(OrchestrationError, "contains a symlink"):
                CheckpointStore(root, "controller-a")


if __name__ == "__main__":
    unittest.main()
