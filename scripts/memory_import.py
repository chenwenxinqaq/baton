"""Collect native client memories in a local, unindexed review queue."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3

from library_paths import _atomic_write_json, library_state_dir, lock_file, unlock_file


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
YAML_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*\s*:")
MAX_TEXT = 120_000
MAX_DESCRIPTION = 800


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
        lock_file(handle.fileno())
        try:
            yield
        finally:
            unlock_file(handle.fileno())


def _session_date(value):
    try:
        if isinstance(value, int):
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else ""
    except (ValueError, OSError, OverflowError):
        return ""


def _unquote_yaml_scalar(raw):
    val = raw.strip()
    if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, str) else str(parsed)
        except ValueError:
            return val[1:-1]
    if len(val) >= 2 and val.startswith("'") and val.endswith("'"):
        return val[1:-1].replace("''", "'")
    return val


def strip_markdown_frontmatter(text):
    """Strip leading YAML frontmatter from native client Markdown, returning (meta, body).

    Only recognizes blocks where every non-empty, non-comment line is a top-level
    ``key: value`` entry or an indented YAML continuation (such as Claude Code's
    nested ``metadata:`` block), so horizontal rules in prose stay intact.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    stripped = normalized.lstrip("\ufeff")
    if not stripped.startswith("---\n") and stripped != "---":
        return {}, normalized.strip()
    lines = stripped.split("\n")
    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() in {"---", "..."}:
            end_idx = idx
            break
    if end_idx is None:
        return {}, normalized.strip()

    header_lines = lines[1:end_idx]
    fields = {}
    saw_key = False
    for line in header_lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in {" ", "\t"}:
            if not saw_key:
                return {}, normalized.strip()
            continue
        if not YAML_KEY_RE.match(line):
            return {}, normalized.strip()
        saw_key = True
        key, _, raw_val = line.partition(":")
        key = key.strip()
        if key not in fields:
            fields[key] = _unquote_yaml_scalar(raw_val)
    if not saw_key:
        return {}, normalized.strip()
    body = "\n".join(lines[end_idx + 1 :]).strip()
    return fields, body


def _clean_single_line(text, limit):
    return " ".join(str(text or "").splitlines()).strip()[:limit]


def _candidate(client, key, updated_at, body, source, title="", thread_id="", description="", fallback_title=""):
    heading = re.search(r"(?m)^#{1,6} +(.+)", body)
    resolved_title = title or (heading[1] if heading else "") or fallback_title or body.splitlines()[0]
    item = {
        "id": f"{client}-import-" + sha256(key.encode()).hexdigest()[:32],
        "thread_id": thread_id,
        "source_date": _session_date(updated_at),
        "source": source,
        "title": " ".join(resolved_title.splitlines()).replace("](", "] (").strip()[:120] or CLIENTS[client],
        "body": body,
    }
    clean_desc = _clean_single_line(description, MAX_DESCRIPTION)
    if clean_desc:
        item["description"] = clean_desc
    return item


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
                raw = path.read_text(encoding="utf-8")
                meta, body = strip_markdown_frontmatter(raw)
                description = meta.get("description", "")
                if not body and description:
                    body = description
                if body:
                    relative = path.relative_to(base).as_posix()
                    stamp = path.stat().st_mtime_ns
                    updated = datetime.fromtimestamp(stamp / 1e9, timezone.utc).isoformat()
                    fallback = meta.get("name") or ("" if path.name == "MEMORY.md" else path.stem)
                    candidate = _candidate(
                        client,
                        str(path),
                        updated,
                        body,
                        CLIENTS[client],
                        title=meta.get("title", ""),
                        description=description,
                        fallback_title=fallback,
                    )
                    candidate["source_detail"] = " ".join(relative.splitlines())
                    candidates.append(candidate)
            except (ValueError, OSError):
                warnings.append(f"{CLIENTS[client]} 有一份 Markdown 记忆无法读取。")
    return candidates, warnings


def _claude_groups(profile):
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
    return groups


def _body_digest(body):
    return sha256(body.encode("utf-8")).hexdigest()


def _candidate_digest(item):
    desc = item.get("description") or ""
    if not desc:
        return _body_digest(item["body"])
    return _body_digest(f"{desc}\0{item['body']}")


def _refresh_kind(existing, candidate):
    """Say whether a scanned source should replace what the queue holds for it.

    Returns "update" for a newer copy of a pending candidate, "reopen" for a
    source edited after its candidate was filed, and "" to leave things alone.
    Receipts written before the source date was recorded cannot be compared and
    stay closed.
    """
    seen = existing.get("source_date")
    if existing.get("approved"):
        if not seen or candidate["source_date"] <= seen:
            return ""
        return "" if existing.get("body_sha256") == _candidate_digest(candidate) else "reopen"
    return "update" if candidate["source_date"] > (seen or "") else ""


def _save_candidates(root, client, candidates, warnings=()):
    added = updated = 0
    if candidates:
        with review_lock(root, client):
            directory = inbox(root, client)
            for candidate in candidates:
                dest = directory / (candidate["id"] + ".json")
                if dest.is_file():
                    existing = json.loads(dest.read_text(encoding="utf-8"))
                    kind = _refresh_kind(existing, candidate)
                    if not kind:
                        continue
                    if kind == "reopen":
                        candidate = dict(candidate, previously_approved=existing["approved"])
                    updated += 1
                else:
                    added += 1
                _atomic_write_json(dest, candidate)
    return {"found": len(candidates), "added": added, "updated": updated, "warnings": list(dict.fromkeys(warnings))}


def _markdown_groups(client, home=None):
    """List the (base, files) groups of native Markdown memory the importer reads for a client."""
    user_home = (Path(home) if home is not None else Path.home()).resolve()
    if client == "claude":
        profile = os.environ.get("CLAUDE_CONFIG_DIR") if home is None else None
        return _claude_groups(Path(profile or user_home / ".claude").expanduser().resolve())
    if client == "antigravity":
        gemini = user_home / ".gemini"
        return [(gemini, list((gemini / directory / "knowledge").glob("**/*.md")))
                for directory in ("antigravity", "antigravity-ide")]
    raise ValueError("请选择记忆文件或粘贴原文。")


def import_memories(root, client, home=None):
    if client == "codex":
        user_home = (Path(home) if home is not None else Path.home()).resolve()
        profile = os.environ.get("CODEX_HOME") if home is None else None
        candidates, warnings = _collect_codex(Path(profile or user_home / ".codex").expanduser().resolve())
    else:
        candidates, warnings = _collect_markdown(client, _markdown_groups(client, home))
    return _save_candidates(root, client, candidates, warnings)


def unfiled_sources(client, root=None, home=None):
    """List native memory files the importer would read that the library has not filed.

    A file counts as filed once its candidate was approved and the file has not
    changed since. Without a library root, every importable file is listed.
    """
    directory = inbox(Path(root), client) if root is not None else None
    unfiled = []
    for base, paths in _markdown_groups(client, home):
        for path in sorted(paths):
            for candidate in _collect_markdown(client, [(base, [path])])[0]:
                if directory is not None:
                    try:
                        existing = json.loads((directory / (candidate["id"] + ".json")).read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        existing = {}
                    if existing.get("approved") and not _refresh_kind(existing, candidate):
                        continue
                unfiled.append(str(path))
    return unfiled


def import_text(root, client, title, body, fallback_title=""):
    if client not in CLIENTS:
        raise ValueError("不支持的客户端。")
    # Browsers submit textarea content with CRLF line endings.
    raw = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw or len(raw) > MAX_TEXT or "\0" in raw:
        raise ValueError(f"原文须为 1–{MAX_TEXT:,} 个字符。")
    meta, cleaned_body = strip_markdown_frontmatter(raw)
    description = meta.get("description", "")
    if not cleaned_body and description:
        cleaned_body = description
    if not cleaned_body:
        raise ValueError(f"原文须为 1–{MAX_TEXT:,} 个字符。")
    candidate = _candidate(
        client,
        "text:" + raw,
        None,
        cleaned_body,
        CLIENTS[client],
        title=title.strip() or meta.get("title", ""),
        description=description,
        fallback_title=fallback_title.strip() or meta.get("name", ""),
    )
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
    """Keep a receipt so repeated imports reopen a reviewed source only after it changes."""
    receipt = {"approved": topic, "source_date": item["source_date"], "body_sha256": _candidate_digest(item)}
    _atomic_write_json(inbox(root, client) / (item["id"] + ".json"), receipt)
