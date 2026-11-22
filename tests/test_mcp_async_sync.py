"""Integration tests for MCP write-triggered asynchronous sync enqueueing."""

from pathlib import Path
import tempfile
import unittest

import sys

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "scripts"))
sys.path.insert(0, str(CODE / "mcp"))

import server as mcp_server  # noqa: E402


class _RecordingScheduler:
    def __init__(self):
        self.calls = []
        self.status_calls = []

    def enqueue(self, root, **kwargs):
        self.calls.append((Path(root), kwargs))
        return {"queued": True, "status": "pending"}

    def status(self, root):
        self.status_calls.append(Path(root))
        return {"status": "success"}


def _make_library(path):
    path.mkdir()
    (path / "MEMORY.md").write_text(
        "# Unified Memory\n\n## L0\n\n## L1\n\n## L2\n\n## L3\n\n## L4\n",
        encoding="utf-8",
    )
    for folder in ("constraints", "methods", "preferences", "projects", "archive", "user-managed"):
        (path / folder).mkdir()


class McpAsyncSyncTests(unittest.TestCase):
    def test_successful_upsert_enqueues_after_local_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            _make_library(root)
            scheduler = _RecordingScheduler()
            store = mcp_server.MemoryStore(
                root,
                library_id="shared",
                mode="rw",
                sync_config={"driver": "git", "auto_commit": False, "auto_push": True},
                async_scheduler=scheduler,
            )

            result = store.upsert_entry("L1", "async-demo", "Async", "Test", "Body")

            self.assertEqual(result["path"], "methods/async-demo.md")
            self.assertEqual(len(scheduler.calls), 1)
            queued_root, kwargs = scheduler.calls[0]
            self.assertEqual(queued_root, root.resolve())
            self.assertEqual(kwargs["sync_config"]["auto_push"], True)
            self.assertEqual(kwargs["mode"], "rw")
            self.assertIn("upsert methods/async-demo.md", kwargs["reason"])
            self.assertIn("# Async", (root / "methods/async-demo.md").read_text(encoding="utf-8"))

    def test_read_only_or_disabled_push_is_filtered_by_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            _make_library(root)
            scheduler = _RecordingScheduler()
            store = mcp_server.MemoryStore(
                root,
                mode="ro",
                sync_config={"driver": "git", "auto_commit": False, "auto_push": True},
                async_scheduler=scheduler,
            )
            with self.assertRaises(ValueError):
                store.upsert_entry("L1", "readonly", "Read only", "Test", "Body")
            self.assertEqual(scheduler.calls, [])

            rw_store = mcp_server.MemoryStore(
                root,
                mode="rw",
                sync_config={"driver": "git", "auto_commit": False, "auto_push": False},
                async_scheduler=scheduler,
            )
            rw_store.upsert_entry("L1", "disabled", "Disabled", "Test", "Body")
            # MemoryStore filters disabled auto-push before enqueueing; this
            # keeps the scheduler queue free of no-op jobs.
            self.assertEqual(len(scheduler.calls), 0)

    def test_status_is_read_from_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            _make_library(root)
            scheduler = _RecordingScheduler()
            store = mcp_server.MemoryStore(root, async_scheduler=scheduler)
            self.assertEqual(store.async_sync_status()["status"], "success")
            self.assertEqual(scheduler.status_calls, [root.resolve()])


if __name__ == "__main__":
    unittest.main()
