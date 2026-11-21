"""Read Codex-generated memories into a local, unindexed review queue."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3

from library_paths import _atomic_write_json, library_state_dir


IMPORT_ID = re.compile(r"codex-import-[0-9a-f]{32}")
THREAD_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")


def inbox(root):
    # The Git driver stages the whole library. Keep unreviewed content outside it.
    directory = (library_state_dir(root) / "imported" / "codex").resolve()
    if directory.is_relative_to(root.resolve()):
        raise ValueError("Codex 导入区需要配置在记忆库目录之外。")
    return directory


@contextmanager
def review_lock(root):
    directory = inbox(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / ".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _session_date(value):
    try:
        if isinstance(value, int):
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else ""
    except (ValueError, OSError, OverflowError):
        return ""


def _candidate(thread_id, updated_at, body, source):
    heading = re.search(r"(?m)^#{1,6} +(.+)", body)
    title = heading[1].replace("](", "] (").strip()[:120] if heading else ""
    return {
        "id": "codex-import-" + sha256(("thread:" + thread_id).encode()).hexdigest()[:32],
        "thread_id": thread_id,
        "source_date": _session_date(updated_at),
        "source": source,
        "title": title or f"Codex 会话 · {thread_id[:12]}",
        "body": body,
    }


def _from_database(path, source):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        rows = db.execute("SELECT thread_id, source_updated_at, raw_memory, rollout_summary FROM stage1_outputs")
        for thread_id, updated_at, memory, summary in rows:
            if isinstance(thread_id, str) and THREAD_ID.fullmatch(thread_id):
                memory = (memory or "").strip()
                summary = (summary or "").strip()
                body = "\n\n".join(dict.fromkeys(part for part in (memory, summary) if part))
                if body:
                    yield _candidate(thread_id, updated_at, body, source)


def _from_summary(path, source):
    header, separator, body = path.read_text(encoding="utf-8").partition("\n\n")
    fields = dict(tuple(line.split(": ", 1)) for line in header.splitlines() if ": " in line)
    thread_id = fields.get("thread_id", "")
    if not separator or not THREAD_ID.fullmatch(thread_id) or not body.strip():
        raise ValueError("无法识别 Codex 会话摘要的格式。")
    return _candidate(thread_id, fields.get("updated_at"), body.strip(), source)


def import_memories(root, home=None):
    """Snapshot structured memories and deduplicate dual writes by thread ID."""
    codex_home = (Path(home) / ".codex" if home is not None else
                  Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")).expanduser().resolve()
    candidates = {}
    warnings = []

    def remember(candidate):
        previous = candidates.get(candidate["id"])
        if previous is None or candidate["source_date"] > previous["source_date"]:
            candidates[candidate["id"]] = candidate

    # Prefer the database for equal dates, then prefer v2. Markdown can outlive a database row.
    versions = (("v2", "memories_v2_1.sqlite", "memories_v2"), ("v1", "memories_1.sqlite", "memories"))
    for version, db_name, _ in versions:
        path = codex_home / db_name
        if path.is_file():
            try:
                for candidate in _from_database(path, f"Codex {version}"):
                    remember(candidate)
            except (sqlite3.Error, OSError, ValueError):
                warnings.append(f"{db_name} 无法读取。")
    for version, _, directory in versions:
        for path in sorted((codex_home / directory / "rollout_summaries").glob("*.md")):
            try:
                remember(_from_summary(path, f"Codex {version}"))
            except (OSError, ValueError):
                warnings.append(f"{directory} 中有一份会话摘要无法读取。")

    # Some profiles retain only the consolidated document. Import it as one draft.
    if not candidates:
        for version, _, directory in versions:
            for filename in ("MEMORY.md", "memory_summary.md"):
                path = codex_home / directory / filename
                if not path.is_file():
                    continue
                try:
                    body = path.read_text(encoding="utf-8").strip()
                    if body:
                        candidate = _candidate("consolidated", int(path.stat().st_mtime), body, f"Codex {version} · 汇总")
                        candidate.update(
                            id="codex-import-" + sha256(b"consolidated").hexdigest()[:32],
                            thread_id="", title="Codex 记忆汇总",
                        )
                        remember(candidate)
                        break
                except (OSError, ValueError):
                    warnings.append(f"{directory}/{filename} 无法读取。")
            if candidates:
                break

    added = updated = 0
    if candidates:
        with review_lock(root):
            for candidate in candidates.values():
                dest = inbox(root) / (candidate["id"] + ".json")
                if dest.is_file():
                    existing = json.loads(dest.read_text(encoding="utf-8"))
                    if existing.get("approved") or candidate["source_date"] <= existing["source_date"]:
                        continue
                    updated += 1
                else:
                    added += 1
                _atomic_write_json(dest, candidate)
    return {"found": len(candidates), "added": added, "updated": updated, "warnings": warnings}


def pending(root):
    entries = []
    for path in sorted(inbox(root).glob("codex-import-*.json")):
        if IMPORT_ID.fullmatch(path.stem):
            item = json.loads(path.read_text(encoding="utf-8"))
            if not item.get("approved"):
                entries.append(item)
    return sorted(entries, key=lambda item: (item["source_date"], item["id"]), reverse=True)


def read_candidate(root, import_id):
    if not IMPORT_ID.fullmatch(import_id):
        raise ValueError("导入条目编号无效。")
    path = inbox(root) / (import_id + ".json")
    if not path.is_file():
        raise ValueError("找不到该导入条目。")
    item = json.loads(path.read_text(encoding="utf-8"))
    if item.get("approved"):
        raise ValueError("此条记忆已归入记忆库。")
    return item


def revision(item):
    return sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()


def mark_approved(root, item, topic):
    """Keep a receipt so repeated imports cannot reopen an already reviewed thread."""
    _atomic_write_json(inbox(root) / (item["id"] + ".json"), {"approved": topic})
