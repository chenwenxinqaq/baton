"""Concurrency and lifecycle checks for the manager's browser sync jobs."""

from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import time
import unittest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "manager"))
sys.path.insert(0, str(CODE / "scripts"))

from manager import Handler, SyncJob  # noqa: E402


class ManagerSyncJobsTests(unittest.TestCase):
    def make_handler(self):
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(
            sync_jobs={},
            sync_jobs_lock=threading.RLock(),
            libraries={},
        )
        handler.path = "/sync"
        handler.headers = {"Accept": "application/json"}
        handler.responses = []
        handler.respond_json = lambda status, data: handler.responses.append((status, data))
        handler.respond = lambda *args, **kwargs: handler.responses.append((args, kwargs))
        return handler

    def test_completed_jobs_are_evicted_after_poll_window(self):
        handler = self.make_handler()
        old_outcome = Future()
        old_outcome.set_result("/sync_hub")
        current_outcome = Future()
        current_outcome.set_result("/sync_hub")
        now = time.time()
        handler.server.sync_jobs.update(
            old=SyncJob("old-id", old_outcome, "同步", False, True, now - Handler.SYNC_JOB_TTL_SECONDS - 1),
            current=SyncJob("current-id", current_outcome, "同步", False, True, now),
        )

        snapshot = handler._sync_jobs_snapshot()

        self.assertEqual([key for key, _ in snapshot], ["current"])
        self.assertEqual(list(handler.server.sync_jobs), ["current"])

    def test_concurrent_requests_share_one_active_job(self):
        handler_a = self.make_handler()
        handler_b = self.make_handler()
        # Both handlers represent requests handled by the same server.
        handler_b.server = handler_a.server
        handler_b.path = handler_a.path
        handler_b.headers = handler_a.headers
        started = threading.Event()
        release = threading.Event()
        calls = []

        def perform():
            calls.append(threading.current_thread().name)
            started.set()
            release.wait(timeout=2)
            return "/sync_hub"

        errors = []

        def invoke(handler):
            try:
                handler.run_sync_job("team", "同步", True, perform)
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        first = threading.Thread(target=invoke, args=(handler_a,), name="request-0")
        first.start()
        self.assertTrue(started.wait(timeout=2))
        second = threading.Thread(target=invoke, args=(handler_b,), name="request-1")
        second.start()
        second.join(timeout=2)
        release.set()
        first.join(timeout=3)

        self.assertFalse(errors)
        self.assertEqual(len(handler_a.server.sync_jobs), 1)
        job = handler_a.server.sync_jobs["team"]
        job.outcome.result(timeout=2)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
