"""Local AI activity audit log (who searched/read/wrote memory, when).

Stored as JSONL in machine-local state (never inside a synced memory library).
Entry bodies are never recorded: only tool, client, library, ref/path, short query.
All functions are best-effort: auditing must never break MCP calls or hooks.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import time

from library_paths import global_state_dir

MAX_BYTES = 2 * 1024 * 1024
KEEP_LINES = 4000
READ_TOOLS = {"memory_search", "memory_list", "memory_read"}
WRITE_TOOLS = {"memory_upsert", "memory_archive"}


def audit_path(home=None):
    return global_state_dir(home=home) / "audit.jsonl"


def normalize_client(name):
    raw = (name or "").strip()
    low = raw.lower()
    if not low:
        return "unknown"
    if "cursor" in low:
        return "Cursor"
    if "codex" in low:
        return "Codex"
    if "claude" in low:
        return "Claude Code"
    if "gemini" in low or "antigravity" in low or "agy" in low:
        return "Antigravity"
    return raw[:40]


def record(event, home=None):
    """Append one event. Returns True if written."""
    try:
        path = audit_path(home=home)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        payload = {"t": round(now, 3), "ts": datetime.fromtimestamp(now).isoformat(timespec="seconds")}
        payload.update({k: v for k, v in event.items() if v not in (None, "")})
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, line.encode("utf-8"))
            if os.fstat(fd).st_size > MAX_BYTES:
                _rotate(path)
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


def _rotate(path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-KEEP_LINES:]
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def read_events(since=None, home=None):
    """Return events (oldest first) with t >= since (epoch)."""
    path = audit_path(home=home)
    if not path.is_file():
        return []
    events = []
    try:
        with path.open(encoding="utf-8") as fh:
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
    except OSError:
        return []
    return events


def has_write_since(since, home=None, client=None):
    """True if a successful write happened since `since` (optionally only by one client)."""
    return any(
        e.get("tool") in WRITE_TOOLS and e.get("ok", True) and (client is None or e.get("client") == client)
        for e in read_events(since=since, home=home)
    )


def _day_start(now):
    dt = datetime.fromtimestamp(now)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def summarize(now=None, home=None, days=7):
    """Aggregate the last `days` days for the dashboard."""
    now = now or time.time()
    today0 = _day_start(now)
    events = read_events(since=now - days * 86400, home=home)
    tool_events = [e for e in events if e.get("source") == "mcp"]

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
        if e.get("tool") == "memory_read" and e.get("ref"):
            hit_counter[e["ref"]] += 2
        elif e.get("tool") == "memory_search":
            for ref in e.get("refs") or []:
                hit_counter[ref] += 1
    top_hits = [{"ref": r, "score": s} for r, s in hit_counter.most_common(5)]

    recent_queries = [
        {"t": e["t"], "client": e.get("client"), "query": e.get("query"), "hits": e.get("hits", 0)}
        for e in reversed(tool_events)
        if e.get("tool") == "memory_search" and e.get("query")
    ][:6]
    empty_searches_today = sum(
        1 for e in tool_events if e.get("tool") == "memory_search" and e.get("t", 0) >= today0 and e.get("hits", 0) == 0 and e.get("ok", True)
    )

    nudges_today = sum(1 for e in events if e.get("source") == "hook" and e.get("event") == "nudge" and e.get("t", 0) >= today0)
    sessions_today = sum(1 for e in events if e.get("source") == "hook" and e.get("event") == "sessionStart" and e.get("t", 0) >= today0)

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
        "sessions_today": sessions_today,
        "total_events": len(events),
    }
