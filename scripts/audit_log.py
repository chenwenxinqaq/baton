"""Local AI activity audit log (who searched/read/wrote memory, when).

Stored as JSONL in machine-local state (never inside a synced memory library).
Entry bodies are never recorded: only tool, client, library, ref/path, short query.
All functions are best-effort: auditing must never break MCP calls or hooks.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import time

from library_paths import global_state_dir

MAX_BYTES = 2 * 1024 * 1024
KEEP_LINES = 4000
READ_TOOLS = {"memory_search", "memory_list", "memory_read", "memory_history", "memory_lint"}
WRITE_TOOLS = {"memory_upsert", "memory_archive", "memory_restore"}
TOOL_SOURCES = frozenset({"mcp", "cli"})
MAX_AUDIT_REFS = 20
CJK_RE = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")
REMOTE_URL_WITH_USERINFO_RE = re.compile(
    r"(?P<prefix>(?:https?|ssh)://)(?P<userinfo>[^/\s@]+)@",
    re.IGNORECASE,
)
REMOTE_SCP_WITH_PASSWORD_RE = re.compile(
    r"(?P<prefix>^|[\s(])(?P<userinfo>[^/\s:@]+):(?P<password>[^/\s@]+)"
    r"@(?P<host>[^/\s:]+):",
    re.IGNORECASE,
)


def audit_path(home=None):
    return global_state_dir(home=home) / "audit.jsonl"


def estimate_tokens(text):
    """Rough, tokenizer-free token estimate: one per CJK character, one per four other characters.

    It is meant for relative comparisons on the dashboard, not for billing.
    """
    text = str(text or "")
    if not text:
        return 0
    cjk = len(CJK_RE.findall(text))
    return cjk + (len(text) - cjk + 3) // 4


def _redact_remote_urls(value):
    text = REMOTE_URL_WITH_USERINFO_RE.sub(r"\g<prefix>***@", str(value or ""))
    return REMOTE_SCP_WITH_PASSWORD_RE.sub(r"\g<prefix>***@\g<host>:", text)


def _private_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise OSError(f"Refusing symlinked audit directory: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _open_private(path, flags, mode=0o600):
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, mode)
    try:
        os.fchmod(fd, mode)
    except OSError:
        pass
    return fd


@contextmanager
def _audit_lock(path):
    """Lock a stable companion inode so rotation cannot invalidate the lock."""
    path = Path(path)
    _private_dir(path.parent)
    lock_path = path.with_name(path.name + ".lock")
    fd = _open_private(lock_path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _sanitize_value(value):
    if isinstance(value, str):
        return _redact_remote_urls(value)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_value(item) for key, item in value.items()}
    return value


def normalize_client(name):
    raw = (name or "").strip()
    low = raw.lower()
    if not low:
        return "unknown"
    if "workbuddy" in low:
        return "WorkBuddy"
    if "deepseek harness" in low or low == "dsh":
        return "DeepSeek Harness"
    if "cursor" in low:
        return "Cursor"
    if "codex" in low:
        return "Codex"
    if "claude desktop" in low or "claude-desktop" in low or "claude-ai" in low:
        return "Claude Desktop"
    if "claude" in low:
        return "Claude Code"
    if "gemini" in low or "antigravity" in low or "agy" in low:
        return "Antigravity"
    return raw[:40]


def record(event, home=None):
    """Append one event. Returns True if written."""
    try:
        path = audit_path(home=home)
        _private_dir(path.parent)
        now = time.time()
        payload = {"t": round(now, 3), "ts": datetime.fromtimestamp(now).isoformat(timespec="seconds")}
        payload.update({k: _sanitize_value(v) for k, v in event.items() if v not in (None, "")})
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _audit_lock(path):
            fd = _open_private(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            try:
                encoded = line.encode("utf-8")
                offset = 0
                while offset < len(encoded):
                    offset += os.write(fd, encoded[offset:])
                os.fsync(fd)
                too_large = os.fstat(fd).st_size > MAX_BYTES
            finally:
                os.close(fd)
            if too_large:
                _rotate(path)
        return True
    except Exception:
        return False


def _rotate(path):
    try:
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            return
        lines = path.read_text(encoding="utf-8").splitlines()[-KEEP_LINES:]
        tmp = path.with_name(path.name + ".tmp")
        fd = _open_private(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            content = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
            offset = 0
            while offset < len(content):
                offset += os.write(fd, content[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        pass


def read_events(since=None, home=None):
    """Return events (oldest first) with t >= since (epoch)."""
    path = audit_path(home=home)
    if path.is_symlink() or not path.is_file():
        return []
    events = []
    try:
        with _audit_lock(path):
            fd = _open_private(path, os.O_RDONLY)
            try:
                with os.fdopen(fd, encoding="utf-8") as fh:
                    fd = None
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        if since is None or float(ev.get("t", 0)) >= since:
                            events.append(ev)
            finally:
                if fd is not None:
                    os.close(fd)
    except OSError:
        return []
    return events


def has_write_since(since, home=None, client=None):
    """True if a successful write happened since `since` (optionally only by one client)."""
    return latest_write_time(since, home=home, client=client) is not None


def latest_write_time(since, home=None, client=None):
    """Return the latest timestamp of a successful write since `since`, or None."""
    latest = None
    for e in read_events(since=since, home=home):
        if (
            e.get("tool") in WRITE_TOOLS
            and e.get("ok", True)
            and (client is None or e.get("client") == client)
        ):
            try:
                ts = float(e.get("t", 0))
            except (TypeError, ValueError):
                continue
            if latest is None or ts > latest:
                latest = ts
    return latest


def _day_start(now):
    dt = datetime.fromtimestamp(now)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


FOLLOW_WINDOW_SECONDS = 30 * 60
NUDGE_FOLLOW_WINDOW_SECONDS = 10 * 60
SLOW_WRITE_MS = 2000


def search_reads(events):
    """Yield (search event, refs read after it) for every successful MCP search.

    A read follows a search when the same client calls memory_read before its
    next search and within FOLLOW_WINDOW_SECONDS. The read refs are returned
    in read order and may include refs the search did not return.
    """
    calls = [e for e in events if e.get("source") in TOOL_SOURCES and e.get("ok", True)]
    for i, e in enumerate(calls):
        if e.get("tool") != "memory_search":
            continue
        client = e.get("client")
        read = []
        for later in calls[i + 1:]:
            if later.get("client") != client:
                continue
            if later.get("t", 0) - e.get("t", 0) > FOLLOW_WINDOW_SECONDS or later.get("tool") == "memory_search":
                break
            if later.get("tool") == "memory_read":
                later_refs = []
                if later.get("ref"):
                    later_refs.append(later["ref"])
                for ref in later.get("refs") or []:
                    if ref and ref not in later_refs:
                        later_refs.append(ref)
                for ref in later_refs:
                    if ref not in read:
                        read.append(ref)
        yield e, read


def usage_stats(events):
    """Measure how much memory context clients received and how much of it they used.

    A search result counts as used when the same client reads that ref before
    its next search and within FOLLOW_WINDOW_SECONDS. Events without a
    ``tokens`` field (older log lines) are excluded from the token totals.
    """
    calls = [e for e in events if e.get("source") in TOOL_SOURCES and e.get("ok", True)]
    by_tool = {}
    for e in calls:
        if "tokens" not in e:
            continue
        item = by_tool.setdefault(e.get("tool") or "unknown", {"calls": 0, "tokens": 0, "bytes": 0})
        item["calls"] += 1
        item["tokens"] += int(e.get("tokens") or 0)
        item["bytes"] += int(e.get("bytes") or 0)
    for item in by_tool.values():
        item["avg_tokens"] = round(item["tokens"] / item["calls"]) if item["calls"] else 0

    searches = returned = used = unused_searches = 0
    rank_counter = Counter()
    for e, read in search_reads(calls):
        searches += 1
        refs = [r for r in e.get("refs") or [] if r]
        returned += int(e.get("hits") or len(refs))
        read = {ref for ref in read if ref in refs}
        used += len(read)
        if not read:
            unused_searches += 1
        for ref in read:
            rank_counter[refs.index(ref) + 1] += 1

    sessions = [e for e in events if e.get("source") == "hook" and e.get("event") == "sessionStart" and "tokens" in e]

    writes = [e for e in calls if e.get("tool") in WRITE_TOOLS]
    append_writes = sum(1 for e in writes if e.get("tool") == "memory_upsert" and e.get("mode") == "append")
    write_ms = sorted(int(e["ms"]) for e in writes if isinstance(e.get("ms"), (int, float)) and e["ms"] >= 0)
    write_p95_ms = None
    if write_ms:
        idx = min(len(write_ms) - 1, max(0, int(round(0.95 * (len(write_ms) - 1)))))
        write_p95_ms = write_ms[idx]
    slow_writes = sum(1 for ms in write_ms if ms >= SLOW_WRITE_MS)

    nudges = [e for e in events if e.get("source") == "hook" and e.get("event") == "nudge"]
    checkpoints = [e for e in events if e.get("source") == "hook" and e.get("event") == "checkpoint"]
    nudge_conversions = 0
    for nudge in nudges:
        nt = float(nudge.get("t") or 0)
        nclient = nudge.get("client")
        if any(
            w.get("client") == nclient
            and nt <= float(w.get("t") or 0) <= nt + NUDGE_FOLLOW_WINDOW_SECONDS
            for w in writes
        ):
            nudge_conversions += 1

    write_pipeline = {
        "writes": len(writes),
        "append_writes": append_writes,
        "nudges": len(nudges),
        "checkpoints": len(checkpoints),
        "nudge_conversions": nudge_conversions,
        "nudge_conversion_rate": round(nudge_conversions / len(nudges), 3) if nudges else None,
        "write_p95_ms": write_p95_ms,
        "slow_writes": slow_writes,
    }
    return {
        "tokens_total": sum(item["tokens"] for item in by_tool.values()),
        "by_tool": by_tool,
        "searches": searches,
        "results_returned": returned,
        "results_read": used,
        "read_rate": round(used / returned, 3) if returned else None,
        "searches_without_read": unused_searches,
        "read_ranks": {str(rank): count for rank, count in sorted(rank_counter.items())},
        "session_injections": len(sessions),
        "session_injection_tokens": sum(int(e.get("tokens") or 0) for e in sessions),
        "write_pipeline": write_pipeline,
    }


def _ref_library(event, ref):
    """Return the logical library for an audit ref.

    Multi-library events normally carry both ``library`` and a library-qualified
    ref for shared entries.  Older events may omit ``library``; in that case a
    bare ref follows the personal-library convention, while an explicit
    ``library:path`` prefix remains authoritative.
    """
    raw_ref = str(ref or "")
    prefix, separator, _ = raw_ref.partition(":")
    if separator and prefix:
        return prefix
    library = str(event.get("library") or "").strip()
    if library in {"", "*", "all"}:
        return "personal"
    return library


def summarize(now=None, home=None, days=7):
    """Aggregate the last `days` days for the dashboard."""
    now = now or time.time()
    today0 = _day_start(now)
    events = read_events(since=now - days * 86400, home=home)
    tool_events = [e for e in events if e.get("source") in TOOL_SOURCES]

    clients = {}
    for e in tool_events:
        c = e.get("client") or "unknown"
        item = clients.setdefault(c, {"client": c, "reads_today": 0, "writes_today": 0, "calls_7d": 0, "last_t": 0, "errors_today": 0})
        item["calls_7d"] += 1
        item["last_t"] = max(item["last_t"], e.get("t", 0))
        if e.get("t", 0) >= today0:
            if e.get("tool") in READ_TOOLS:
                item["reads_today"] += 1
            elif e.get("tool") in WRITE_TOOLS:
                item["writes_today"] += 1
            if e.get("ok") is False:
                item["errors_today"] += 1

    writes = [e for e in tool_events if e.get("tool") in WRITE_TOOLS and e.get("ok", True)]
    recent_writes = list(reversed(writes[-8:]))

    hit_counter = Counter()
    for e in tool_events:
        if e.get("ok") is False:
            continue
        if e.get("tool") == "memory_read":
            read_refs = []
            if e.get("ref"):
                read_refs.append(e["ref"])
            for ref in e.get("refs") or []:
                if ref and ref not in read_refs:
                    read_refs.append(ref)
            for ref in read_refs:
                hit_counter[(_ref_library(e, ref), ref)] += 2
        elif e.get("tool") == "memory_search":
            for ref in e.get("refs") or []:
                hit_counter[(_ref_library(e, ref), ref)] += 1
    top_hits = [
        {"library": library, "ref": ref, "score": score}
        for (library, ref), score in hit_counter.most_common(5)
    ]

    recent_queries = [
        {"t": e["t"], "client": e.get("client"), "query": e.get("query"), "hits": e.get("hits", 0)}
        for e in reversed(tool_events)
        if e.get("tool") == "memory_search" and e.get("query")
    ][:6]
    empty_searches_today = sum(
        1 for e in tool_events if e.get("tool") == "memory_search" and e.get("t", 0) >= today0 and e.get("hits", 0) == 0 and e.get("ok", True)
    )

    nudges_today = sum(1 for e in events if e.get("source") == "hook" and e.get("event") == "nudge" and e.get("t", 0) >= today0)
    checkpoints_today = sum(
        1 for e in events if e.get("source") == "hook" and e.get("event") == "checkpoint" and e.get("t", 0) >= today0
    )
    sessions_today = sum(1 for e in events if e.get("source") == "hook" and e.get("event") == "sessionStart" and e.get("t", 0) >= today0)

    daily = []
    for offset in range(days - 1, -1, -1):
        day_start = today0 - offset * 86400
        day_end = day_start + 86400
        dt = datetime.fromtimestamp(day_start)
        day_events = [e for e in events if day_start <= float(e.get("t", 0)) < day_end]
        day_mcp = [e for e in day_events if e.get("source") in TOOL_SOURCES]
        daily.append(
            {
                "date": dt.strftime("%m-%d"),
                "iso_date": dt.strftime("%Y-%m-%d"),
                "reads": sum(1 for e in day_mcp if e.get("tool") in READ_TOOLS and e.get("ok", True)),
                "writes": sum(1 for e in day_mcp if e.get("tool") in WRITE_TOOLS and e.get("ok", True)),
                "nudges": sum(1 for e in day_events if e.get("source") == "hook" and e.get("event") == "nudge"),
                "checkpoints": sum(1 for e in day_events if e.get("source") == "hook" and e.get("event") == "checkpoint"),
                "errors": sum(1 for e in day_mcp if e.get("ok") is False),
            }
        )

    return {
        "now": now,
        "clients": sorted(clients.values(), key=lambda x: -x["last_t"]),
        "reads_today": sum(c["reads_today"] for c in clients.values()),
        "writes_today": sum(c["writes_today"] for c in clients.values()),
        "recent_writes": recent_writes,
        "top_hits": top_hits,
        "recent_queries": recent_queries,
        "empty_searches_today": empty_searches_today,
        "nudges_today": nudges_today,
        "checkpoints_today": checkpoints_today,
        "sessions_today": sessions_today,
        "total_events": len(events),
        "usage_today": usage_stats([e for e in events if e.get("t", 0) >= today0]),
        "usage_7d": usage_stats(events),
        "daily": daily,
    }
