"""Tests for write-triggered, debounced background Git synchronization."""

from pathlib import Path
import json
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "scripts"))

import async_sync  # noqa: E402
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

    def test_write_during_running_sync_keeps_new_generation_retry_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            root.mkdir()
            first_started = threading.Event()
            release_first = threading.Event()
            calls = []
            calls_lock = threading.Lock()

            def sync_fn(path, **kwargs):
                with calls_lock:
                    calls.append(len(calls) + 1)
                    call_number = len(calls)
                if call_number == 1:
                    first_started.set()
                    self.assertTrue(release_first.wait(timeout=2))
                    raise RuntimeError("first generation failed")
                if call_number == 2:
                    raise RuntimeError("new generation first attempt failed")
                return {"status": "ok", "revision": call_number}

            scheduler = AsyncSyncScheduler(
                sync_fn,
                debounce_seconds=0,
                max_retries=1,
                retry_backoff=0,
                state_dir=Path(tmp) / "state",
            )
            self.addCleanup(scheduler.shutdown)

            scheduler.enqueue(root, sync_config=self.config(), reason="first write")
            self.assertTrue(first_started.wait(timeout=2))
            scheduler.enqueue(root, sync_config=self.config(), reason="second write")
            release_first.set()

            result = scheduler.wait_for_idle(root, timeout=2)
            self.assertEqual(result["status"], "success")
            self.assertEqual(len(calls), 3)
            self.assertEqual(result["result"]["revision"], 3)

    def test_status_refreshes_from_persisted_state_across_schedulers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            root.mkdir()
            state = Path(tmp) / "state"

            first = AsyncSyncScheduler(
                lambda path, **kwargs: {"status": "ok", "owner": "first"},
                debounce_seconds=60,
                state_dir=state,
            )
            second = AsyncSyncScheduler(
                lambda path, **kwargs: {"status": "ok", "owner": "second"},
                debounce_seconds=0,
                state_dir=state,
            )
            self.addCleanup(first.shutdown)
            self.addCleanup(second.shutdown)

            queued = first.enqueue(root, sync_config=self.config(), reason="first write")
            self.assertEqual(queued["status"], "pending")
            self.assertEqual(first.status(root)["status"], "pending")

            second.enqueue(root, sync_config=self.config(), reason="second write")
            result = second.wait_for_idle(root, timeout=2)
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["result"]["owner"], "second")
            self.assertEqual(first.status(root)["status"], "success")
            self.assertEqual(first.status(root)["result"]["owner"], "second")

    def test_status_prefers_newer_in_memory_state_when_persist_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            root.mkdir()
            state = Path(tmp) / "state"
            scheduler = AsyncSyncScheduler(
                lambda path, **kwargs: {"status": "ok"},
                debounce_seconds=60,
                state_dir=state,
            )
            self.addCleanup(scheduler.shutdown)

            first = scheduler.enqueue(root, sync_config=self.config(), reason="first write")
            self.assertEqual(first["generation"], 1)
            self.assertEqual(scheduler.status(root)["generation"], 1)

            with mock.patch.object(async_sync, "_atomic_json", side_effect=OSError("read-only")):
                second = scheduler.enqueue(root, sync_config=self.config(), reason="second write")
                self.assertEqual(second["generation"], 2)
                current = scheduler.status(root)

            self.assertEqual(current["generation"], 2)
            self.assertEqual(current["reason"], "second write")
            status_files = list(state.rglob("sync-status.json"))
            self.assertEqual(len(status_files), 1)
            persisted = json.loads(status_files[0].read_text(encoding="utf-8"))
            self.assertEqual(persisted["generation"], 1)

    def test_stale_pending_recovery_and_external_result_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            root.mkdir()
            state = Path(tmp) / "state"
            calls = []

            scheduler = AsyncSyncScheduler(
                lambda path, **kwargs: (calls.append(path), {"status": "ok"})[1],
                debounce_seconds=0,
                state_dir=state,
            )
            self.addCleanup(scheduler.shutdown)

            # Simulate an orphaned pending status left by a dead worker PID.
            status_path = scheduler._state_path(root.resolve())
            async_sync._atomic_json(
                status_path,
                {
                    "version": 1,
                    "library": str(root.resolve()),
                    "status": "pending",
                    "generation": 1,
                    "pid": 99999999,
                    "updated_at": "2026-11-25T00:00:00+00:00",
                    "reason": "orphaned write",
                },
            )
            self.assertTrue(async_sync.is_stale_active(scheduler.status(root)))
            recovered = scheduler.recover_stale(root, sync_config=self.config(), mode="rw")
            self.assertIsNotNone(recovered)
            final = scheduler.wait_for_idle(root, timeout=2)
            self.assertEqual(final["status"], "success")
            self.assertEqual(len(calls), 1)
            self.assertFalse(async_sync.is_stale_active(final))

            # Manual CLI/UI sync result clears stale pending/error status cleanly.
            recorded = async_sync.record_external_result(
                root,
                result={"status": "ok", "push": {"status": "pushed"}},
                reason="manual sync",
                state_dir=state,
            )
            self.assertEqual(recorded["status"], "success")
            self.assertEqual(scheduler.status(root)["reason"], "manual sync")

    def test_detached_sync_scheduler_spawns_worker_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            root.mkdir()
            state = Path(tmp) / "state"
            spawned = []

            def fake_spawn(cmd):
                spawned.append(cmd)
                return 424242

            scheduler = async_sync.DetachedSyncScheduler(state_dir=state, spawn_fn=fake_spawn)
            self.assertEqual(
                scheduler.enqueue(root, sync_config=self.config(auto_push=False))["reason"],
                "auto_push_disabled",
            )
            queued = scheduler.enqueue(root, sync_config=self.config(), reason="cli upsert")
            self.assertTrue(queued["queued"])
            self.assertEqual(queued["pid"], 424242)
            self.assertEqual(len(spawned), 1)
            self.assertIn("--root", spawned[0])
            self.assertIn(str(root.resolve()), spawned[0])
            self.assertEqual(scheduler.status(root)["status"], "pending")


if __name__ == "__main__":
    unittest.main()
