"""Local review queue protecting L0 constraints against unapproved agent writes.

When an AI client calls ``memory_upsert`` for ``layer="L0"``, the proposed
constraint is staged here (outside the Git-tracked memory library) instead of
immediately overwriting ``constraints/``. A human reviews the diff in the
Desktop/Web console or via ``umem l0 approve`` before the rule takes effect.
"""

from contextlib import contextmanager
from datetime import datetime
import difflib
from hashlib import sha256
import json
import os
from pathlib import Path
import re

from library_paths import _atomic_write_json, library_state_dir, lock_file, unlock_file

PROPOSAL_ID_RE = re.compile(r"^l0-[0-9a-f]{16}$")


def l0_guard_enabled():
    """Return True unless the user explicitly disables L0 write protection."""
    raw = os.environ.get("UNIFIED_MEMORY_L0_GUARD", "1").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def proposal_id_for_name(name):
    """Derive one stable queue ID per L0 slug so repeated drafts coalesce."""
    digest = sha256(str(name).strip().encode("utf-8")).hexdigest()[:16]
    return f"l0-{digest}"


def queue_dir(root):
    root_path = Path(root).resolve()
    directory = (library_state_dir(root_path) / "l0-proposals").resolve()
    if directory.is_relative_to(root_path):
        raise ValueError("L0 待确认区需要配置在记忆库目录之外。")
    return directory


@contextmanager
def queue_lock(root):
    directory = queue_dir(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / ".lock").open("a") as handle:
        lock_file(handle.fileno())
        try:
            yield directory
        finally:
            unlock_file(handle.fileno())


def proposal_revision(item):
    payload = "\0".join(
        str(item.get(field) or "")
        for field in ("name", "title", "description", "body", "topic_type", "verified_at", "valid_until")
    )
    return sha256(payload.encode("utf-8")).hexdigest()[:16]


def _now_iso():
    return datetime.now().astimezone().isoformat()


def stage_proposal(
    root,
    *,
    name,
    title,
    description,
    body,
    topic_type="feedback",
    action="create",
    client="",
    mode="replace",
    section="",
    base_updated_at="",
    verified_at="",
    valid_until="",
    migrated_from=(),
):
    """Persist or update one pending L0 constraint proposal in local state."""
    pid = proposal_id_for_name(name)
    now = _now_iso()
    with queue_lock(root) as directory:
        dest = directory / f"{pid}.json"
        created_at = now
        if dest.is_file():
            try:
                existing = json.loads(dest.read_text(encoding="utf-8"))
                created_at = existing.get("created_at") or now
            except (OSError, ValueError):
                pass
        item = {
            "id": pid,
            "name": name,
            "layer": "L0",
            "target_path": f"constraints/{name}.md",
            "action": action,
            "title": title,
            "description": description,
            "body": body,
            "topic_type": topic_type or "feedback",
            "mode": mode or "replace",
            "section": section or "",
            "base_updated_at": base_updated_at or "",
            "verified_at": verified_at or "",
            "valid_until": valid_until or "",
            "migrated_from": list(migrated_from or ()),
            "client": (client or "").strip() or "AI 客户端",
            "created_at": created_at,
            "updated_at": now,
        }
        item["revision"] = proposal_revision(item)
        _atomic_write_json(dest, item)
        return item


def list_proposals(root):
    """List all pending L0 proposals for a library, newest first."""
    directory = queue_dir(root)
    if not directory.is_dir():
        return []
    items = []
    for path in sorted(directory.glob("l0-*.json")):
        if path.is_symlink() or not PROPOSAL_ID_RE.fullmatch(path.stem):
            continue
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(item, dict) and item.get("id") == path.stem and item.get("name"):
            item.setdefault("revision", proposal_revision(item))
            items.append(item)
    items.sort(key=lambda it: (it.get("updated_at") or "", it.get("id") or ""), reverse=True)
    return items


def count_proposals(root):
    return len(list_proposals(root))


def read_proposal(root, proposal_id):
    pid = str(proposal_id or "").strip()
    if not PROPOSAL_ID_RE.fullmatch(pid):
        raise ValueError("待确认 L0 提案编号无效。")
    path = queue_dir(root) / f"{pid}.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("找不到对应的待确认 L0 提案，可能已处理。")
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("待确认 L0 提案文件损坏。") from exc
    if not isinstance(item, dict) or item.get("id") != pid:
        raise ValueError("待确认 L0 提案内容无效。")
    item.setdefault("revision", proposal_revision(item))
    return item


def delete_proposal(root, proposal_id):
    pid = str(proposal_id or "").strip()
    if not PROPOSAL_ID_RE.fullmatch(pid):
        raise ValueError("待确认 L0 提案编号无效。")
    with queue_lock(root) as directory:
        path = directory / f"{pid}.json"
        if path.is_symlink() or not path.is_file():
            raise ValueError("找不到对应的待确认 L0 提案，可能已处理。")
        item = json.loads(path.read_text(encoding="utf-8"))
        path.unlink()
        return item


def proposal_diff(root, proposal):
    """Return a unified diff between the current live L0 entry (if any) and the proposal."""
    target_rel = proposal.get("target_path") or f"constraints/{proposal['name']}.md"
    live_path = Path(root).resolve() / target_rel
    before_lines = []
    if live_path.is_file() and not live_path.is_symlink():
        try:
            before_lines = live_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            before_lines = []
    after_body = (proposal.get("body") or "").strip()
    if not after_body.startswith("# "):
        after_body = f"# {proposal.get('title') or proposal['name']}\n\n{after_body}"
    after_preview = [
        f"title: {proposal.get('title') or proposal['name']}",
        f"description: {proposal.get('description') or ''}",
        f"type: {proposal.get('topic_type') or 'feedback'}",
    ]
    if proposal.get("verified_at"):
        after_preview.append(f"verified_at: {proposal['verified_at']}")
    if proposal.get("valid_until"):
        after_preview.append(f"valid_until: {proposal['valid_until']}")
    after_lines = ["---", *after_preview, "---", "", *after_body.splitlines()]
    diff = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"当前生效 ({target_rel})" if before_lines else "（新建约束）",
            tofile=f"提议生效 ({target_rel})",
            lineterm="",
            n=3,
        )
    )
    return "\n".join(diff)
