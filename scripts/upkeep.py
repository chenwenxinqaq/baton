"""Sleep-time upkeep / Morning Note generator and one-click applier.

Combines ``memory_lint`` consolidation checks with recent ``audit_log`` read/write
activity to propose concrete, reversible cleanup actions:
  - merge duplicate entries on the same topic (and archive the older copy)
  - archive stale or expired snapshots that are no longer being read
  - compact oversized entries with a reviewable unified diff (full copy backed up)
  - rebuild drifted MEMORY.md indexes

Every apply action goes through ``MemoryStore`` so trash backups, locks, and Git
sync behave identically to manual edits. Dismissed suggestions are persisted in
local per-library state outside the Git-tracked memory directory.
"""

from contextlib import contextmanager
from datetime import datetime
import difflib
from hashlib import sha256
import json
from pathlib import Path
import re

import audit_log
import l0_guard
from library_paths import _atomic_write_json, library_state_dir, lock_file, unlock_file
import memory_lint

HEADING_H2_RE = re.compile(r"^##\s+(.+?)\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _state_dir(root):
    root_path = Path(root).resolve()
    directory = (library_state_dir(root_path) / "upkeep").resolve()
    if directory.is_relative_to(root_path):
        raise ValueError("整理日报状态目录必须位于记忆库目录之外。")
    return directory


@contextmanager
def _state_lock(root):
    directory = _state_dir(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / ".lock").open("a") as handle:
        lock_file(handle.fileno())
        try:
            yield directory
        finally:
            unlock_file(handle.fileno())


def _load_dismissed(root):
    path = _state_dir(root) / "dismissed.json"
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def dismiss_suggestion(root, suggestion_id, fingerprint=""):
    """Hide one upkeep suggestion until its underlying files change."""
    sid = str(suggestion_id or "").strip()
    if not sid:
        raise ValueError("建议编号不能为空。")
    with _state_lock(root) as directory:
        data = _load_dismissed(root)
        data[sid] = {
            "dismissed_at": datetime.now().astimezone().isoformat(),
            "fingerprint": str(fingerprint or ""),
        }
        _atomic_write_json(directory / "dismissed.json", data)
    return {"dismissed": sid}


def reset_dismissed(root, suggestion_id=""):
    """Clear one or all dismissed upkeep suggestions."""
    sid = str(suggestion_id or "").strip()
    with _state_lock(root) as directory:
        path = directory / "dismissed.json"
        if not sid:
            path.unlink(missing_ok=True)
            return {"cleared": "all"}
        data = _load_dismissed(root)
        data.pop(sid, None)
        _atomic_write_json(path, data)
        return {"cleared": sid}


def _suggestion_id(kind, *parts):
    digest = sha256("\0".join([kind, *(str(p) for p in parts)]).encode("utf-8")).hexdigest()[:12]
    return f"upkeep-{kind}-{digest}"


def _fingerprint(*parts):
    return sha256("\0".join(str(p or "") for p in parts).encode("utf-8")).hexdigest()[:16]


def _strip_leading_h1(body):
    lines = (body or "").strip().splitlines()
    if lines and lines[0].startswith("# "):
        rest = lines[1:]
        while rest and not rest[0].strip():
            rest.pop(0)
        return "\n".join(rest)
    return (body or "").strip()


def _unified_diff(before_text, after_text, from_label, to_label):
    return "\n".join(
        difflib.unified_diff(
            (before_text or "").splitlines(),
            (after_text or "").splitlines(),
            fromfile=from_label,
            tofile=to_label,
            lineterm="",
            n=2,
        )
    )


def _split_h2_blocks(body):
    """Split Markdown body into ``(preamble, [(heading, block_lines), ...])``, ignoring fenced blocks."""
    lines = (body or "").splitlines()
    preamble = []
    sections = []
    cur_heading = None
    cur_lines = []
    in_fence = False
    fence_char = ""
    for line in lines:
        fm = FENCE_RE.match(line)
        if fm:
            marker = fm.group(1)[0]
            if not in_fence:
                in_fence = True
                fence_char = marker
            elif marker == fence_char:
                in_fence = False
                fence_char = ""
        if not in_fence:
            hm = HEADING_H2_RE.match(line)
            if hm:
                if cur_heading is not None:
                    sections.append((cur_heading, cur_lines))
                cur_heading = hm.group(1).strip()
                cur_lines = [line]
                continue
        if cur_heading is None:
            preamble.append(line)
        else:
            cur_lines.append(line)
    if cur_heading is not None:
        sections.append((cur_heading, cur_lines))
    return preamble, sections


def _summarize_section(heading, block_lines, max_lines=3, max_chars=280):
    """Condense one long older H2 section to a compact digest while keeping key takeaways."""
    body_lines = block_lines[1:]
    picked = []
    total = 0
    in_fence = False
    for raw in body_lines:
        if FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = raw.strip()
        if not line:
            continue
        if len(line) > 160:
            line = line[:159].rstrip() + "…"
        picked.append(line if line.startswith(("-", "*", ">", "1.", "2.", "3.")) else f"- {line}")
        total += len(line)
        if len(picked) >= max_lines or total >= max_chars:
            break
    omitted = max(0, len(body_lines) - len(picked))
    summary_lines = [f"## {heading}"]
    if picked:
        summary_lines.extend(picked)
    if omitted:
        summary_lines.append(f"> （已精简 {omitted} 行细节；完整原文保存在本机历史版本中，可随时对比或恢复）")
    return "\n".join(summary_lines)


def _compact_long_body(entry):
    """Produce a deterministic compacted version of an oversized entry, or '' if not shrinkable."""
    body = (entry.get("body") or "").strip()
    if len(body) <= memory_lint.LONG_CHARS:
        return ""
    preamble, sections = _split_h2_blocks(body)
    if len(sections) >= 3:
        # Keep the preamble (capped) + last 2 sections intact; condense earlier long sections.
        keep_tail = 2
        out_parts = []
        pre_text = "\n".join(preamble).strip()
        if pre_text:
            out_parts.append(pre_text)
        for idx, (heading, block_lines) in enumerate(sections):
            block_text = "\n".join(block_lines).strip()
            if idx >= len(sections) - keep_tail or len(block_text) <= 450:
                out_parts.append(block_text)
            else:
                out_parts.append(_summarize_section(heading, block_lines))
        compacted = "\n\n".join(part for part in out_parts if part).strip()
    else:
        # Single long block: keep heading/intro + first 18 non-empty lines + last 10 non-empty lines.
        lines = body.splitlines()
        if len(lines) < 35:
            return ""
        head = lines[:18]
        tail = lines[-10:]
        skipped = len(lines) - len(head) - len(tail)
        compacted = "\n".join(
            head
            + ["", f"> （已精简中间 {skipped} 行过程记录；完整原文已保存在本机历史版本中）", ""]
            + tail
        ).strip()

    saved = len(body) - len(compacted)
    if saved < 500 or saved < len(body) * 0.2:
        return ""
    return compacted


def _build_merged_body(keep_entry, src_entry):
    """Fold ``src_entry`` into ``keep_entry`` while preserving full attribution."""
    keep_body = (keep_entry.get("body") or "").strip()
    src_content = _strip_leading_h1(src_entry.get("body") or "")
    src_title = src_entry.get("title") or src_entry["name"]
    src_date = (src_entry.get("updated_at") or "")[:10] or "日期未知"
    header = f"## 合并自：{src_title}（{src_entry['path']}，更新于 {src_date}）"
    return f"{keep_body}\n\n{header}\n\n{src_content}".strip()


def _recent_read_counts(audit_home=None, days=7):
    counts = {}
    try:
        events = audit_log.read_events(since=datetime.now().timestamp() - days * 86400, home=audit_home)
    except Exception:
        return counts
    for ev in events:
        if ev.get("tool") != "memory_read" or not ev.get("ok"):
            continue
        refs = ev.get("refs") if isinstance(ev.get("refs"), list) else ([ev.get("ref")] if ev.get("ref") else [])
        for ref in refs:
            raw = str(ref or "").strip()
            if not raw:
                continue
            path = raw.split(":", 1)[1] if ":" in raw else raw
            counts[path] = counts.get(path, 0) + 1
    return counts


def build_morning_note(store, *, stale_days=memory_lint.STALE_DAYS, audit_days=7, include_dismissed=False, audit_home=None):
    """Build a structured upkeep report with actionable suggestions and review diffs."""
    entries = store.collect_entries(include_archived=True)
    entry_map = {e["path"]: e for e in entries}
    index = {
        path: meta
        for path, meta in store.parse_index().items()
        if meta.get("layer", "") in store.allowed_layers
    }
    findings = memory_lint.lint_entries(entries, index=index, stale_days=stale_days)
    read_counts = _recent_read_counts(audit_home=audit_home, days=audit_days)
    dismissed_map = _load_dismissed(store.root)

    raw_suggestions = []

    # 1. Duplicate topic merge proposals
    for a_entry, b_entry, overlap in memory_lint.find_duplicate_pairs(entries):
        if a_entry.get("source") != "index" or b_entry.get("source") != "index":
            continue
        if "L4" not in store.allowed_layers:
            continue
        # Keep the more recently updated (tie-break: longer body) entry as primary.
        a_key = (a_entry.get("updated_epoch") or 0, len(a_entry.get("body") or ""))
        b_key = (b_entry.get("updated_epoch") or 0, len(b_entry.get("body") or ""))
        keep, src = (a_entry, b_entry) if a_key >= b_key else (b_entry, a_entry)
        merged_body = _build_merged_body(keep, src)
        diff = _unified_diff(
            keep.get("body") or "",
            merged_body,
            f"当前 {keep['path']}",
            f"合并后 {keep['path']}（并归档 {src['path']}）",
        )
        sid = _suggestion_id("merge", keep["path"], src["path"])
        fp = _fingerprint(keep.get("updated_at"), keep.get("body"), src.get("updated_at"), src.get("body"))
        raw_suggestions.append(
            {
                "id": sid,
                "kind": "merge_duplicate",
                "severity": "warning",
                "title": f"合并重复主题：「{src.get('title') or src['name']}」→「{keep.get('title') or keep['name']}」",
                "reason": (
                    f"两篇条目主题重合度 {overlap:.0%}；保留更新较近的 {keep['path']}，"
                    f"将 {src['path']} 的内容并入新小节后归档至 L4。"
                ),
                "target_path": keep["path"],
                "target_name": keep["name"],
                "target_layer": keep["layer"],
                "secondary_path": src["path"],
                "secondary_name": src["name"],
                "overlap": round(overlap, 3),
                "reads_recent": read_counts.get(keep["path"], 0) + read_counts.get(src["path"], 0),
                "fingerprint": fp,
                "diff": diff,
                "can_apply": store.mode == "rw",
                "_merged_body": merged_body,
            }
        )

    # 2. Stale / expired snapshot archive proposals
    archived_candidates = {}
    for finding in findings:
        if finding["check"] not in {"stale-snapshot", "expired-entry"}:
            continue
        ref = finding["ref"]
        entry = entry_map.get(ref)
        if not entry or entry.get("source") != "index" or entry.get("layer") == "L4":
            continue
        if "L4" not in store.allowed_layers:
            continue
        existing = archived_candidates.get(ref)
        if existing is None:
            archived_candidates[ref] = (entry, [finding])
        else:
            existing[1].append(finding)

    for ref, (entry, reasons) in sorted(archived_candidates.items()):
        reads = read_counts.get(ref, 0)
        read_note = f"近 {audit_days} 天未被 AI 读取" if reads == 0 else f"近 {audit_days} 天被读取 {reads} 次（建议先核实再归档）"
        checks = ", ".join(r["check"] for r in reasons)
        detail_text = "；".join(r["detail"] for r in reasons)
        sid = _suggestion_id("archive", ref)
        fp = _fingerprint(entry.get("updated_at"), entry.get("valid_until"), checks)
        raw_suggestions.append(
            {
                "id": sid,
                "kind": "archive_stale",
                "severity": "warning",
                "title": f"归档过期/陈旧快照：「{entry.get('title') or entry['name']}」",
                "reason": f"{detail_text}。{read_note}。应用后移入 archive/（L4）并保留历史快照身份。",
                "target_path": ref,
                "target_name": entry["name"],
                "target_layer": entry["layer"],
                "secondary_path": f"archive/{entry['name']}.md",
                "reads_recent": reads,
                "fingerprint": fp,
                "diff": "",
                "can_apply": store.mode == "rw",
            }
        )

    # 3. Oversized entry compaction proposals with unified diff
    for finding in findings:
        if finding["check"] != "oversized":
            continue
        ref = finding["ref"]
        entry = entry_map.get(ref)
        if not entry or entry.get("source") != "index" or entry.get("layer") == "L4":
            continue
        compacted = _compact_long_body(entry)
        if not compacted:
            continue
        before_body = (entry.get("body") or "").strip()
        before_chars = len(before_body)
        after_chars = len(compacted)
        saved_chars = before_chars - after_chars
        before_tok = audit_log.estimate_tokens(before_body)
        after_tok = audit_log.estimate_tokens(compacted)
        diff = _unified_diff(
            before_body,
            compacted,
            f"当前 {ref} ({before_chars} 字 / ~{before_tok} tokens)",
            f"精简后 {ref} ({after_chars} 字 / ~{after_tok} tokens)",
        )
        sid = _suggestion_id("trim", ref)
        fp = _fingerprint(entry.get("updated_at"), before_body)
        raw_suggestions.append(
            {
                "id": sid,
                "kind": "trim_oversized",
                "severity": "info",
                "title": f"精简超长条目：「{entry.get('title') or entry['name']}」（省 {saved_chars:,} 字）",
                "reason": (
                    f"当前正文 {before_chars:,} 字（约 {before_tok:,} tokens），"
                    f"精简后 {after_chars:,} 字（约 {after_tok:,} tokens）。完整原文会自动备份到本地历史版本。"
                ),
                "target_path": ref,
                "target_name": entry["name"],
                "target_layer": entry["layer"],
                "before_chars": before_chars,
                "after_chars": after_chars,
                "saved_chars": saved_chars,
                "reads_recent": read_counts.get(ref, 0),
                "fingerprint": fp,
                "diff": diff,
                "can_apply": store.mode == "rw",
                "_compacted_body": compacted,
            }
        )

    # 4. Index drift fix
    index_issues = [f for f in findings if f["check"] in {"index-missing", "index-orphan"}]
    if index_issues:
        refs = sorted({f["ref"] for f in index_issues})
        sid = _suggestion_id("index", "MEMORY.md")
        fp = _fingerprint(*refs)
        raw_suggestions.append(
            {
                "id": sid,
                "kind": "rebuild_index",
                "severity": "warning",
                "title": f"修复 MEMORY.md 索引漂移（{len(index_issues)} 处）",
                "reason": "检测到磁盘条目与 MEMORY.md 索引不一致（" + "、".join(refs[:4]) + ("…" if len(refs) > 4 else "") + "）。",
                "target_path": "MEMORY.md",
                "fingerprint": fp,
                "diff": "",
                "can_apply": store.mode == "rw",
            }
        )

    suggestions = []
    dismissed_count = 0
    for item in raw_suggestions:
        rec = dismissed_map.get(item["id"])
        is_dismissed = bool(rec and (not rec.get("fingerprint") or rec.get("fingerprint") == item["fingerprint"]))
        item["dismissed"] = is_dismissed
        if is_dismissed:
            dismissed_count += 1
            if not include_dismissed:
                continue
        suggestions.append(item)

    counts_by_kind = {}
    for item in suggestions:
        counts_by_kind[item["kind"]] = counts_by_kind.get(item["kind"], 0) + 1

    try:
        audit_summary = audit_log.summarize(home=audit_home)
    except Exception:
        audit_summary = {}

    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "library": store.library_id,
        "entries_count": len(entries),
        "stale_days": stale_days,
        "pending_l0_count": l0_guard.count_proposals(store.root) if store.role == "personal" else 0,
        "recent_reads_7d": sum(d.get("reads", 0) for d in audit_summary.get("daily") or []),
        "recent_writes_7d": sum(d.get("writes", 0) for d in audit_summary.get("daily") or []),
        "dismissed_count": dismissed_count,
        "counts_by_kind": counts_by_kind,
        "suggestions": [
            {k: v for k, v in item.items() if not k.startswith("_")}
            for item in suggestions
        ],
        "_internal": {item["id"]: item for item in raw_suggestions},
    }


def public_morning_note(store, **kwargs):
    """Return the JSON-serializable Morning Note without internal draft fields."""
    note = build_morning_note(store, **kwargs)
    note.pop("_internal", None)
    return note


def apply_suggestion(store, suggestion_id, *, expected_fingerprint="", stale_days=memory_lint.STALE_DAYS, audit_home=None):
    """Apply one Morning Note suggestion safely through ``MemoryStore``."""
    if store.mode == "ro":
        raise ValueError(f"Library '{store.library_id}' is read-only.")
    note = build_morning_note(store, stale_days=stale_days, include_dismissed=True, audit_home=audit_home)
    item = note["_internal"].get(str(suggestion_id or "").strip())
    if not item:
        raise ValueError("找不到对应的整理建议，可能已处理或条目已变化。")
    if expected_fingerprint and expected_fingerprint != item["fingerprint"]:
        raise ValueError("目标条目在此期间已被修改，请刷新整理日报后重新确认。")

    kind = item["kind"]
    if kind == "merge_duplicate":
        merged_body = item["_merged_body"]
        updated = store.upsert_entry(
            layer=item["target_layer"],
            name=item["target_name"],
            body=merged_body,
            mode="replace",
        )
        archived = store.archive_entry(name=item["secondary_name"])
        return {
            "applied": item["id"],
            "kind": kind,
            "path": updated["path"],
            "archived_path": archived.get("to") or archived.get("path"),
        }
    if kind == "archive_stale":
        archived = store.archive_entry(name=item["target_name"])
        return {
            "applied": item["id"],
            "kind": kind,
            "path": archived.get("to") or archived.get("path"),
            "from_path": item["target_path"],
        }
    if kind == "trim_oversized":
        updated = store.upsert_entry(
            layer=item["target_layer"],
            name=item["target_name"],
            body=item["_compacted_body"],
            mode="replace",
        )
        return {
            "applied": item["id"],
            "kind": kind,
            "path": updated["path"],
            "saved_chars": item.get("saved_chars", 0),
            "backup": (updated.get("previous") or {}).get("backup", ""),
        }
    if kind == "rebuild_index":
        import memoryctl

        target = "personal" if store.library_id == "personal" else store.library_id
        rebuilt = memoryctl.rebuild_library_index(target)
        return {
            "applied": item["id"],
            "kind": kind,
            "path": "MEMORY.md",
            "indexed_topics": rebuilt.get("indexed_topics", 0),
        }
    raise ValueError(f"不支持的整理建议类型：{kind}")
