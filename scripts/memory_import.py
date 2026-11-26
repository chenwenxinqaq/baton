"""Collect native client memories in a local, unindexed review queue."""

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


CLIENTS = {
    "claude": "Claude Code",
    "claude-desktop": "Claude Desktop",
    "codex": "Codex",
    "cursor": "Cursor",
    "workbuddy": "WorkBuddy",
    "antigravity": "Antigravity",
    "deepseek": "DeepSeek Harness",
}
AUTOMATIC_CLIENTS = frozenset({"claude", "codex", "antigravity"})
IMPORT_ID = re.compile(r"([a-z]+(?:-[a-z]+)*)-import-[0-9a-f]{32}")
THREAD_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
MAX_TEXT = 120_000


def inbox(root, client):
    if client not in CLIENTS:
        raise ValueError("不支持的客户端。")
    # The Git driver stages the whole library. Keep unreviewed content outside it.
    directory = (library_state_dir(root) / "imported" / client).resolve()
    if directory.is_relative_to(root.resolve()):
        raise ValueError("导入区需要配置在记忆库目录之外。")
    return directory


@contextmanager
def review_lock(root, client):
    directory = inbox(root, client)
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


def _candidate(client, key, updated_at, body, source, title="", thread_id=""):
    heading = re.search(r"(?m)^#{1,6} +(.+)", body)
    title = title or (heading[1] if heading else body.splitlines()[0])
    return {
        "id": f"{client}-import-" + sha256(key.encode()).hexdigest()[:32],
        "thread_id": thread_id,
        "source_date": _session_date(updated_at),
        "source": source,
        "title": " ".join(title.splitlines()).replace("](", "] (").strip()[:120] or CLIENTS[client],
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
                    yield _candidate("codex", "thread:" + thread_id, updated_at, body, source, thread_id=thread_id)


def _from_summary(path, source):
    header, separator, body = path.read_text(encoding="utf-8").partition("\n\n")
    fields = dict(line.split(": ", 1) for line in header.splitlines() if ": " in line)
    thread_id = fields.get("thread_id", "")
    if not separator or not THREAD_ID.fullmatch(thread_id) or not body.strip():
        raise ValueError("无法识别 Codex 会话摘要的格式。")
    return _candidate("codex", "thread:" + thread_id, fields.get("updated_at"), body.strip(), source, thread_id=thread_id)


def _collect_codex(codex_home):
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
                        candidate = _candidate("codex", "consolidated", int(path.stat().st_mtime), body,
                                               f"Codex {version} · 汇总", title="Codex 记忆汇总")
                        remember(candidate)
                        break
                except (OSError, ValueError):
                    warnings.append(f"{directory}/{filename} 无法读取。")
            if candidates:
                break

    return candidates.values(), warnings


def _collect_markdown(client, groups):
    candidates = []
    warnings = []
    for base, paths in groups:
        for path in sorted(paths):
            try:
                if not path.is_file() or path.resolve() != path:
                    continue
                body = path.read_text(encoding="utf-8").strip()
                if body:
                    relative = path.relative_to(base).as_posix()
                    stamp = path.stat().st_mtime_ns
                    updated = datetime.fromtimestamp(stamp / 1e9, timezone.utc).isoformat()
                    candidate = _candidate(client, str(path), updated, body, CLIENTS[client], title=path.stem)
                    candidate["source_detail"] = " ".join(relative.splitlines())
                    candidates.append(candidate)
            except (ValueError, OSError):
                warnings.append(f"{CLIENTS[client]} 有一份 Markdown 记忆无法读取。")
    return candidates, warnings


def _collect_claude(profile):
    directories = set(profile.glob("projects/*/memory"))
    settings = profile / "settings.json"
    if settings.is_file():
        try:
            configured = json.loads(settings.read_text(encoding="utf-8")).get("autoMemoryDirectory")
            if isinstance(configured, str) and Path(configured).expanduser().is_absolute():
                directories.add(Path(configured).expanduser().resolve())
        except (OSError, ValueError, AttributeError):
            pass
    groups = []
    for directory in sorted(directories):
        if directory.resolve() != directory:
            continue
        topics = [path for path in directory.glob("*.md") if path.name != "MEMORY.md" and not path.is_symlink()]
        base = profile if directory.is_relative_to(profile) else directory
        groups.append((base, topics or [directory / "MEMORY.md"]))
    return _collect_markdown("claude", groups)


def _save_candidates(root, client, candidates, warnings=()):
    added = updated = 0
    if candidates:
        with review_lock(root, client):
            directory = inbox(root, client)
            for candidate in candidates:
                dest = directory / (candidate["id"] + ".json")
                if dest.is_file():
                    existing = json.loads(dest.read_text(encoding="utf-8"))
                    if existing.get("approved") or candidate["source_date"] <= existing["source_date"]:
                        continue
                    updated += 1
                else:
                    added += 1
                _atomic_write_json(dest, candidate)
    return {"found": len(candidates), "added": added, "updated": updated, "warnings": list(dict.fromkeys(warnings))}


def import_memories(root, client, home=None):
    user_home = (Path(home) if home is not None else Path.home()).resolve()
    if client == "codex":
        profile = os.environ.get("CODEX_HOME") if home is None else None
        candidates, warnings = _collect_codex(Path(profile or user_home / ".codex").expanduser().resolve())
    elif client == "claude":
        profile = os.environ.get("CLAUDE_CONFIG_DIR") if home is None else None
        candidates, warnings = _collect_claude(Path(profile or user_home / ".claude").expanduser().resolve())
    elif client == "antigravity":
        gemini = user_home / ".gemini"
        groups = ((gemini, (gemini / directory / "knowledge").glob("**/*.md"))
                  for directory in ("antigravity", "antigravity-ide"))
        candidates, warnings = _collect_markdown(client, groups)
    else:
        raise ValueError("请选择记忆文件或粘贴原文。")
    return _save_candidates(root, client, candidates, warnings)


def import_text(root, client, title, body):
    if client not in CLIENTS:
        raise ValueError("不支持的客户端。")
    # Browsers submit textarea content with CRLF line endings.
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body or len(body) > MAX_TEXT or "\0" in body:
        raise ValueError(f"原文须为 1–{MAX_TEXT:,} 个字符。")
    candidate = _candidate(client, "text:" + body, None, body, CLIENTS[client], title=title.strip())
    return _save_candidates(root, client, [candidate])


def pending(root, client):
    entries = []
    for path in sorted(inbox(root, client).glob(f"{client}-import-*.json")):
        if IMPORT_ID.fullmatch(path.stem):
            item = json.loads(path.read_text(encoding="utf-8"))
            if not item.get("approved"):
                entries.append(item)
    return sorted(entries, key=lambda item: (item["source_date"], item["id"]), reverse=True)


def read_candidate(root, client, import_id):
    match = IMPORT_ID.fullmatch(import_id)
    if not match or match[1] != client:
        raise ValueError("导入条目编号无效。")
    path = inbox(root, client) / (import_id + ".json")
    if not path.is_file():
        raise ValueError("找不到该导入条目。")
    item = json.loads(path.read_text(encoding="utf-8"))
    if item.get("approved"):
        raise ValueError("此条记忆已归入记忆库。")
    return item


def revision(item):
    return sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()


def mark_approved(root, client, item, topic):
    """Keep a receipt so repeated imports cannot reopen an already reviewed thread."""
    _atomic_write_json(inbox(root, client) / (item["id"] + ".json"), {"approved": topic})
