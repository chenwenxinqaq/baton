"""Tests for write-triggered, debounced background Git synchronization."""

from pathlib import Path
import json
import sys
import tempfile
import threading
import time
import unittest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "scripts"))

from async_sync import AsyncSyncScheduler  # noqa: E402


class AsyncSyncSchedulerTests(unittest.TestCase):
    def config(self, **extra):
        value = {"driver": "git", "remote": "origin", "branch": "main", "auto_push": True}
        value.update(extra)
        return value

    def test_debounces_burst_and_coalesces_to_one_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            root.mkdir()
            state = Path(tmp) / "state"
            calls = []
            gate = threading.Lock()

            def sync_fn(path, **kwargs):
                with gate:
                    calls.append((Path(path), kwargs))
                return {"status": "ok", "push": {"status": "pushed"}}

            scheduler = AsyncSyncScheduler(sync_fn, debounce_seconds=0.05, state_dir=state)
            self.addCleanup(scheduler.shutdown)
            first = scheduler.enqueue(root, sync_config=self.config(), reason="first write")
            second = scheduler.enqueue(root, sync_config=self.config(), reason="second write")
            self.assertTrue(first["queued"])
            self.assertEqual(second["status"], "pending")
            result = scheduler.wait_for_idle(root, timeout=2)
            self.assertEqual(result["status"], "success")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], root.resolve())
            self.assertEqual(calls[0][1]["mode"], "rw")
            self.assertFalse(calls[0][1]["browser_login"])
            self.assertEqual(result["reason"], "second write")

    def test_read_only_and_auto_push_disabled_are_noops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            root.mkdir()
            scheduler = AsyncSyncScheduler(debounce_seconds=0, state_dir=Path(tmp) / "state")
            self.addCleanup(scheduler.shutdown)
            self.assertEqual(
                scheduler.enqueue(root, sync_config=self.config(), mode="ro")["reason"],
                "read_only",
            )
            self.assertEqual(
                scheduler.enqueue(root, sync_config=self.config(auto_push=False))["reason"],
                "auto_push_disabled",
            )
            self.assertEqual(scheduler.status(root), {})

    def test_retries_transient_failure_and_persists_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            root.mkdir()
            state = Path(tmp) / "state"
            attempts = []

            def sync_fn(path, **kwargs):
                attempts.append(1)
                if len(attempts) == 1:
                    raise RuntimeError("https://alice:secret@gitee.example/team/memory.git failed")
                return {"status": "ok", "message": "pushed"}

            scheduler = AsyncSyncScheduler(
                sync_fn,
                debounce_seconds=0,
                max_retries=1,
                retry_backoff=0.01,
                state_dir=state,
            )
            self.addCleanup(scheduler.shutdown)
            scheduler.enqueue(root, sync_config=self.config())
            result = scheduler.wait_for_idle(root, timeout=2)
            self.assertEqual(result["status"], "success")
            self.assertEqual(len(attempts), 2)
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("secret", serialized)
            status_files = list(state.rglob("sync-status.json"))
            self.assertEqual(len(status_files), 1)
            self.assertEqual(status_files[0].stat().st_mode & 0o777, 0o600)

    def test_conflict_is_terminal_and_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            root.mkdir()
            calls = []

            def sync_fn(path, **kwargs):
                calls.append(1)
                return {"status": "needs_manual_merge", "conflicts": ["projects/topic.md"]}

            scheduler = AsyncSyncScheduler(
                sync_fn,
                debounce_seconds=0,
                max_retries=3,
                retry_backoff=0.01,
                state_dir=Path(tmp) / "state",
            )
            self.addCleanup(scheduler.shutdown)
            scheduler.enqueue(root, sync_config=self.config())
            result = scheduler.wait_for_idle(root, timeout=2)
            self.assertEqual(result["status"], "conflict")
            self.assertEqual(len(calls), 1)
            self.assertIn("projects/topic.md", result["result"]["conflicts"])

    def test_new_enqueue_clears_terminal_status_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            root.mkdir()
            calls = []

            def sync_fn(path, **kwargs):
                calls.append(1)
                return {"status": "ok", "revision": len(calls)}

            scheduler = AsyncSyncScheduler(
                sync_fn,
                debounce_seconds=0.05,
                state_dir=Path(tmp) / "state",
            )
            self.addCleanup(scheduler.shutdown)

            scheduler.enqueue(root, sync_config=self.config(), reason="first write")
            first = scheduler.wait_for_idle(root, timeout=2)
            self.assertEqual(first["status"], "success")
            self.assertIn("result", first)
            self.assertIn("finished_at", first)

            second = scheduler.enqueue(root, sync_config=self.config(), reason="second write")
            self.assertEqual(second["status"], "pending")
            self.assertEqual(second["reason"], "second write")
            self.assertNotIn("result", second)
            self.assertNotIn("started_at", second)
            self.assertNotIn("finished_at", second)
            self.assertNotIn("error", second)
            self.assertNotIn("next_attempt_at", second)

            final = scheduler.wait_for_idle(root, timeout=2)
            self.assertEqual(final["status"], "success")
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
