"""Debounced, non-blocking Git synchronization triggered by memory writes.

This module deliberately owns only scheduling.  The existing synchronous write-time
``auto_commit`` behavior remains unchanged; callers opt into this worker by setting
``auto_push`` in a library's Git sync configuration and calling :meth:`enqueue` after a
successful write.  Each scheduler instance coalesces bursts per library, while
``sync_git``'s cross-process library lock protects against another process doing a sync.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Dict, Optional, Union

from library_paths import library_state_dir


TERMINAL_STATES = frozenset({"success", "error", "conflict"})
ACTIVE_STATES = frozenset({"pending", "running"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_text(value: Any, limit: int = 500) -> str:
    """Make persisted status text bounded and safe for logs/UI.

    Remote URLs can contain a username hint and are redacted through sync_git when the
    scheduler is used with the real driver.  The generic replacement below also avoids
    persisting accidental credentials when a custom sync function raises an exception.
    """
    text = str(value or "")
    for marker in ("https://", "http://", "ssh://"):
        # Do not try to parse arbitrary error text here; sync_git.redact_remote_url is
        # injected by the caller for the real driver.  This conservative replacement
        # handles the common ``scheme://user:secret@host`` shape for custom drivers.
        start = 0
        while True:
            pos = text.find(marker, start)
            if pos < 0:
                break
            at = text.find("@", pos)
            slash = text.find("/", pos + len(marker))
            if at >= 0 and (slash < 0 or at < slash):
                text = text[:pos + len(marker)] + "***@" + text[at + 1 :]
                start = pos + len(marker) + 4
            else:
                start = pos + len(marker)
    return text[:limit]


def _redact(value: Any) -> str:
    """Use sync_git's canonical redaction when available, without importing it eagerly."""
    text = _safe_text(value)
    try:
        import sync_git  # local import keeps this scheduler usable in isolated tests

        return _safe_text(sync_git.redact_remote_url(text))
    except Exception:
        return text


def _sanitize(value: Any) -> Any:
    """Recursively bound and redact sync results before writing local state."""
    if isinstance(value, dict):
        return {str(k): _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value[:50]]
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _redact(value)


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            os.chmod(temp, 0o600)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _key(root: Union[Path, str]) -> str:
    return str(Path(root).expanduser().resolve())


@dataclass
class _Job:
    root: Path
    sync_config: Dict[str, Any]
    mode: str = "rw"
    generation: int = 0
    attempts: int = 0
    pending: bool = False
    running: bool = False
    timer: Optional[threading.Timer] = None
    reason: str = ""
    last_status: Dict[str, Any] = field(default_factory=dict)


class AsyncSyncScheduler:
    """Schedule one debounced Git sync worker per writable library.

    Parameters
    ----------
    sync_fn:
        Callable compatible with ``sync_git.sync``.  It receives ``remote``, ``branch``,
        ``mode='rw'`` and ``browser_login=False``.  Injection keeps tests deterministic.
    debounce_seconds:
        Quiet period after the most recent write before a sync starts.  The default two
        seconds coalesces editor-save bursts without making updates feel stale.
    max_retries / retry_backoff:
        Network/auth failures are retried with exponential backoff.  A conflict is terminal
        and is surfaced as ``conflict`` so that the user can merge manually.
    state_dir:
        Optional machine-local directory for ``sync-status.json`` files.  The production
        default is ``library_state_dir(root)`` (outside the synced repository); callers can
        pass a temporary directory in tests.
    """

    def __init__(
        self,
        sync_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        *,
        debounce_seconds: float = 2.0,
        max_retries: int = 2,
        retry_backoff: float = 5.0,
        state_dir: Optional[Union[Path, str]] = None,
    ) -> None:
        if debounce_seconds < 0 or retry_backoff < 0:
            raise ValueError("debounce_seconds and retry_backoff must be non-negative")
        self.sync_fn = sync_fn
        self.debounce_seconds = float(debounce_seconds)
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff = float(retry_backoff)
        self.state_dir = Path(state_dir).expanduser().resolve() if state_dir else None
        self._lock = threading.RLock()
        self._jobs: Dict[str, _Job] = {}
        self._closed = False

    def _state_path(self, root: Path) -> Path:
        if self.state_dir is not None:
            # Keep separate files if a test scheduler handles more than one library.
            import hashlib

            digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
            return self.state_dir / digest / "sync-status.json"
        return library_state_dir(root) / "sync-status.json"

    def _persist_locked(self, job: _Job, state: str, **extra: Any) -> Dict[str, Any]:
        previous = dict(job.last_status or {})
        payload: Dict[str, Any] = {
            "version": 1,
            "library": str(job.root),
            "status": state,
            "generation": job.generation,
            "attempt": job.attempts,
            "updated_at": _now(),
        }
        # Preserve useful lifecycle timestamps and the latest reason/result.
        for field_name in (
            "queued_at",
            "started_at",
            "finished_at",
            "next_attempt_at",
            "reason",
            "error",
            "result",
        ):
            if field_name in previous:
                payload[field_name] = previous[field_name]
        for name, value in extra.items():
            if value is None:
                # ``None`` is an explicit clear signal.  Without this, a recovered retry
                # would remain marked with the prior error/finished timestamp forever.
                payload.pop(name, None)
            else:
                payload[name] = _sanitize(value)
        job.last_status = payload
        try:
            _atomic_json(self._state_path(job.root), payload)
        except OSError:
            # A status write must never break a memory write or worker.  Keep the in-memory
            # state and let the next status read expose it while this process is alive.
            pass
        return dict(payload)

    def _schedule_locked(self, job: _Job, delay: float, generation: int) -> None:
        if self._closed:
            return
        if job.timer is not None:
            job.timer.cancel()
        job.timer = threading.Timer(max(0.0, delay), self._start, args=(job.root, generation))
        job.timer.daemon = True
        job.timer.name = "unified-memory-sync"
        job.timer.start()

    @staticmethod
    def _is_conflict(result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        statuses = []
        for key in ("status", "pull", "push"):
            value = result.get(key)
            if isinstance(value, dict):
                statuses.append(str(value.get("status") or ""))
            elif value is not None:
                statuses.append(str(value))
        statuses.extend(str(item) for item in (result.get("conflicts") or []))
        return any(
            "conflict" in item.casefold() or item in {"needs_manual_merge", "merged_with_conflicts"}
            for item in statuses
        ) or bool(result.get("needs_manual_merge"))

    @staticmethod
    def _sync_success(result: Any) -> bool:
        if not isinstance(result, dict):
            return True
        status = str(result.get("status") or "ok").casefold()
        return status in {"ok", "success", "up_to_date", "pushed", "merged", "fast_forward"}

    def enqueue(
        self,
        root: Union[Path, str],
        *,
        sync_config: Optional[Dict[str, Any]],
        mode: str = "rw",
        reason: str = "memory write",
    ) -> Dict[str, Any]:
        """Queue a sync after a successful write, or return why it was skipped.

        Only ``driver='git'``, ``auto_push=true`` and writable ``mode='rw'`` jobs are
        accepted.  Calling this from a read-only mount is intentionally a no-op.
        """
        root_path = Path(root).expanduser().resolve()
        config = dict(sync_config or {})
        if mode != "rw":
            return {"queued": False, "reason": "read_only"}
        if str(config.get("driver") or "git").casefold() != "git":
            return {"queued": False, "reason": "unsupported_driver"}
        if not bool(config.get("auto_push", False)):
            return {"queued": False, "reason": "auto_push_disabled"}
        with self._lock:
            if self._closed:
                return {"queued": False, "reason": "scheduler_closed"}
            key = _key(root_path)
            job = self._jobs.get(key)
            if job is None:
                job = self._jobs[key] = _Job(root_path, config, mode=mode)
            else:
                job.sync_config = config
                job.mode = mode
            job.generation += 1
            job.pending = True
            job.reason = _redact(reason)
            # A new write starts a fresh retry budget and supersedes a previous terminal state.
            job.attempts = 0
            now = _now()
            status = self._persist_locked(
                job,
                "pending",
                queued_at=job.last_status.get("queued_at") or now,
                next_attempt_at=None,
                reason=job.reason,
                error=None,
                finished_at=None,
            )
            generation = job.generation
            self._schedule_locked(job, self.debounce_seconds, generation)
            return {"queued": True, **status}

    def _start(self, root: Path, generation: int) -> None:
        key = _key(root)
        with self._lock:
            job = self._jobs.get(key)
            if self._closed or job is None or generation != job.generation or not job.pending:
                return
            if job.running:
                # A long-running sync is still in flight.  Keep the newest write pending and
                # try again after the quiet period; this avoids concurrent Git processes.
                self._schedule_locked(job, self.debounce_seconds, generation)
                return
            job.timer = None
            job.pending = False
            job.running = True
            status = self._persist_locked(
                job,
                "running",
                started_at=_now(),
                next_attempt_at=None,
                reason=job.reason,
                error=None,
            )
            config = dict(job.sync_config)
            attempt = job.attempts + 1
        thread = threading.Thread(
            target=self._run,
            args=(job, generation, config, attempt),
            daemon=True,
            name="unified-memory-sync-worker",
        )
        thread.start()

    def _run(self, job: _Job, generation: int, config: Dict[str, Any], attempt: int) -> None:
        result: Any = None
        error: Optional[BaseException] = None
        try:
            fn = self.sync_fn
            if fn is None:
                import sync_git

                fn = sync_git.sync
            result = fn(
                job.root,
                remote=config.get("remote") or "origin",
                branch=config.get("branch") or "",
                mode="rw",
                pull_only=False,
                push_only=False,
                browser_login=False,
            )
        except BaseException as exc:  # worker must never leak an exception to Timer
            error = exc

        with self._lock:
            current = self._jobs.get(_key(job.root))
            if current is None:
                return
            current.running = False
            current.attempts = attempt
            if current.generation != generation:
                # A write landed while this run was active.  Keep the result as diagnostic data,
                # but schedule a fresh coalesced run for the newer generation.
                current.pending = True
                status = self._persist_locked(
                    current,
                    "pending",
                    reason=current.reason,
                    last_result=result,
                    error=_redact(error) if error else None,
                    next_attempt_at=_now(),
                )
                self._schedule_locked(current, self.debounce_seconds, current.generation)
                return

            if error is not None:
                detail = _redact(f"{error.__class__.__name__}: {error}")
                if attempt <= self.max_retries:
                    current.pending = True
                    delay = self.retry_backoff * (2 ** max(0, attempt - 1))
                    next_at = time.time() + delay
                    status = self._persist_locked(
                        current,
                        "pending",
                        error=detail,
                        next_attempt_at=datetime.fromtimestamp(next_at, timezone.utc).isoformat(timespec="seconds"),
                        reason=current.reason,
                    )
                    self._schedule_locked(current, delay, generation)
                    return
                status = self._persist_locked(
                    current,
                    "error",
                    error=detail,
                    finished_at=_now(),
                    next_attempt_at=None,
                    reason=current.reason,
                )
                return

            if self._is_conflict(result):
                self._persist_locked(
                    current,
                    "conflict",
                    result=result,
                    finished_at=_now(),
                    next_attempt_at=None,
                    reason=current.reason,
                    error=None,
                )
                return
            if not self._sync_success(result):
                detail = _redact((result or {}).get("error") if isinstance(result, dict) else result)
                if attempt <= self.max_retries:
                    current.pending = True
                    delay = self.retry_backoff * (2 ** max(0, attempt - 1))
                    next_at = time.time() + delay
                    self._persist_locked(
                        current,
                        "pending",
                        result=result,
                        error=detail or "sync returned a failure status",
                        next_attempt_at=datetime.fromtimestamp(next_at, timezone.utc).isoformat(timespec="seconds"),
                        reason=current.reason,
                    )
                    self._schedule_locked(current, delay, generation)
                    return
                self._persist_locked(
                    current,
                    "error",
                    result=result,
                    error=detail or "sync returned a failure status",
                    finished_at=_now(),
                    next_attempt_at=None,
                    reason=current.reason,
                )
                return

            self._persist_locked(
                current,
                "success",
                result=result,
                finished_at=_now(),
                next_attempt_at=None,
                reason=current.reason,
                error=None,
            )

    def status(self, root: Union[Path, str]) -> Dict[str, Any]:
        """Return current in-memory status, falling back to persisted state."""
        root_path = Path(root).expanduser().resolve()
        with self._lock:
            job = self._jobs.get(_key(root_path))
            if job is not None and job.last_status:
                return dict(job.last_status)
        path = self._state_path(root_path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, OSError, ValueError):
            return {}

    def wait_for_idle(self, root: Union[Path, str], timeout: float = 10.0) -> Dict[str, Any]:
        """Wait until a queued job reaches success/error/conflict (primarily for tests)."""
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            state = self.status(root)
            if state.get("status") in TERMINAL_STATES:
                return state
            time.sleep(0.01)
        return self.status(root)

    def shutdown(self) -> None:
        """Stop accepting new work and cancel timers; an already-running Git process is left alone."""
        with self._lock:
            self._closed = True
            for job in self._jobs.values():
                if job.timer is not None:
                    job.timer.cancel()
                    job.timer = None


# Short alias for callers that prefer the less verbose name.
SyncScheduler = AsyncSyncScheduler
