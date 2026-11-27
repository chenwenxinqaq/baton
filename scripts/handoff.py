"""Cross-client session handoff packs for continuing unfinished work across AI clients.

A handoff pack captures an in-flight task snapshot (verified findings, concrete
next steps, key file paths, and referenced memory entries) in local per-library
state outside the Git-tracked memory directory. Another client (e.g. Claude Code
picking up after Codex) can discover the open handoff at SessionStart, claim it
via ``memory_handoff`` or ``umem handoff claim``, or paste the generated Markdown
briefing directly.
"""

from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import re
import secrets

from library_paths import _atomic_write_json, library_state_dir, lock_file, unlock_file
import secret_guard

HANDOFF_ID_RE = re.compile(r"^handoff-\d{8}-\d{6}-[0-9a-f]{6}$")
MAX_TITLE_CHARS = 120
MAX_SUMMARY_CHARS = 40_000
MAX_LIST_ITEMS = 30


def handoff_dir(root):
    root_path = Path(root).resolve()
    directory = (library_state_dir(root_path) / "handoffs").resolve()
    if directory.is_relative_to(root_path):
        raise ValueError("会话交接包目录必须位于记忆库目录之外。")
    return directory


@contextmanager
def handoff_lock(root):
    directory = handoff_dir(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / ".lock").open("a") as handle:
        lock_file(handle.fileno())
        try:
            yield directory
        finally:
            unlock_file(handle.fileno())


def _now_dt():
    return datetime.now().astimezone()


def _clean_lines(values, *, max_items=MAX_LIST_ITEMS, max_len=400):
    if values is None:
        return []
    if isinstance(values, str):
        raw_items = values.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    elif isinstance(values, (list, tuple)):
        raw_items = [str(v) for v in values]
    else:
        raise ValueError("List field must be a list of strings or newline-separated text.")
    cleaned = []
    for raw in raw_items:
        item = raw.strip().lstrip("-* ").strip()
        if not item:
            continue
        if len(item) > max_len:
            item = item[: max_len - 1].rstrip() + "…"
        if item not in cleaned:
            cleaned.append(item)
        if len(cleaned) >= max_items:
            break
    return cleaned


def _validate_id(handoff_id):
    hid = str(handoff_id or "").strip()
    if not HANDOFF_ID_RE.fullmatch(hid):
        raise ValueError("会话交接包编号无效。")
    return hid


def render_briefing(item, store=None):
    """Render a self-contained Markdown briefing ready to hand to another AI client."""
    title = item.get("title") or item["id"]
    source_client = item.get("source_client") or "当前客户端"
    target_client = item.get("target_client") or "任意客户端"
    updated_at = (item.get("updated_at") or item.get("created_at") or "")[:19].replace("T", " ")
    lines = [
        f"# 跨客户端会话交接：{title}",
        "",
        f"- 交接编号：`{item['id']}`",
        f"- 来源 / 目标：{source_client} → {target_client}",
        f"- 状态：{item.get('status', 'open')}（更新于 {updated_at or '刚刚'}）",
    ]
    if item.get("workspace"):
        lines.append(f"- 工作目录：`{item['workspace']}`")

    lines.extend(["", "## 已验证结论与当前上下文", "", (item.get("summary") or "").strip()])

    next_steps = item.get("next_steps") or []
    if next_steps:
        lines.extend(["", "## 待完成步骤", ""])
        for step in next_steps:
            lines.append(f"- [ ] {step}")

    files = item.get("files") or []
    if files:
        lines.extend(["", "## 关键文件", ""])
        for fpath in files:
            lines.append(f"- `{fpath}`")

    memory_refs = item.get("memory_refs") or []
    if memory_refs:
        lines.extend(["", "## 关联记忆条目", ""])
        index_meta = {}
        if store is not None:
            try:
                index_meta = store.parse_index()
            except Exception:
                index_meta = {}
        for ref in memory_refs:
            meta = index_meta.get(ref) or {}
            if meta.get("title"):
                desc = f" — {meta['description']}" if meta.get("description") else ""
                lines.append(f"- `{ref}`（{meta['title']}）{desc}")
            else:
                lines.append(f"- `{ref}`")
        refs_json = json.dumps(memory_refs, ensure_ascii=False)
        lines.append("")
        lines.append(f"> 接手提示：可用 `memory_read(paths={refs_json})` 批量读取上述关联记忆。")

    if store is not None:
        try:
            l0_lines = [
                f"- `{path}`（{meta.get('title') or path}）：{meta.get('description') or ''}"
                for path, meta in store.parse_index().items()
                if meta.get("layer") == "L0"
            ]
        except Exception:
            l0_lines = []
        if l0_lines:
            lines.extend(["", "## 当前生效的 L0 约束提醒", "", *l0_lines[:5]])

    lines.extend(
        [
            "",
            "---",
            f"请基于以上交接继续推进未完成步骤；开始前如涉及关键文件请先核对当前磁盘状态，完成后可调用 `memory_handoff(action=\"complete\", handoff_id=\"{item['id']}\")` 结单，并将长期结论沉淀回记忆库。",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def create_handoff(
    root,
    *,
    title,
    summary,
    next_steps=(),
    files=(),
    memory_refs=(),
    source_client="",
    target_client="",
    workspace="",
    store=None,
):
    """Create and persist a cross-client session handoff pack."""
    clean_title = str(title or "").strip()
    clean_summary = str(summary or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not clean_title or "\n" in clean_title or len(clean_title) > MAX_TITLE_CHARS:
        raise ValueError(f"交接标题须为单行且长度为 1–{MAX_TITLE_CHARS} 个字符。")
    if not clean_summary or len(clean_summary) > MAX_SUMMARY_CHARS:
        raise ValueError(f"交接摘要不能为空且不超过 {MAX_SUMMARY_CHARS:,} 个字符。")
    clean_steps = _clean_lines(next_steps)
    clean_files = _clean_lines(files)
    clean_refs = _clean_lines(memory_refs)
    clean_source = str(source_client or "").strip()[:60] or "Unified Memory"
    clean_target = str(target_client or "").strip()[:60]
    clean_workspace = str(workspace or "").strip()[:500]

    secret_guard.ensure_no_secrets(
        clean_title,
        clean_summary,
        "\n".join(clean_steps),
        "\n".join(clean_files),
    )

    now = _now_dt()
    ts_iso = now.isoformat()
    hid = f"handoff-{now.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    item = {
        "id": hid,
        "title": clean_title,
        "status": "open",
        "source_client": clean_source,
        "target_client": clean_target,
        "workspace": clean_workspace,
        "summary": clean_summary,
        "next_steps": clean_steps,
        "files": clean_files,
        "memory_refs": clean_refs,
        "created_at": ts_iso,
        "updated_at": ts_iso,
        "claimed_by": "",
        "claimed_at": "",
        "done_at": "",
    }
    with handoff_lock(root) as directory:
        _atomic_write_json(directory / f"{hid}.json", item)
    result = dict(item)
    result["briefing"] = render_briefing(item, store=store)
    return result


def list_handoffs(root, *, include_done=False, limit=20):
    """List session handoff packs for a library, newest first."""
    directory = handoff_dir(root)
    if not directory.is_dir():
        return []
    items = []
    for path in sorted(directory.glob("handoff-*.json")):
        if path.is_symlink() or not HANDOFF_ID_RE.fullmatch(path.stem):
            continue
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(item, dict) or item.get("id") != path.stem:
            continue
        if not include_done and item.get("status") == "done":
            continue
        items.append(item)
    items.sort(key=lambda it: (it.get("updated_at") or "", it.get("id") or ""), reverse=True)
    cap = max(1, min(int(limit or 20), 100))
    return items[:cap]


def count_active(root):
    return len(list_handoffs(root, include_done=False, limit=100))


def read_handoff(root, handoff_id="", *, store=None):
    """Read one handoff pack (or the latest active one when handoff_id is empty)."""
    hid = str(handoff_id or "").strip()
    if not hid:
        active = list_handoffs(root, include_done=False, limit=1)
        if not active:
            raise ValueError("当前没有进行中的会话交接包。")
        item = active[0]
    else:
        hid = _validate_id(hid)
        path = handoff_dir(root) / f"{hid}.json"
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"找不到会话交接包：{hid}")
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("会话交接包文件损坏。") from exc
    result = dict(item)
    result["briefing"] = render_briefing(item, store=store)
    return result


def claim_handoff(root, handoff_id="", *, client="", store=None):
    """Claim an open handoff pack for the receiving client and return its briefing."""
    with handoff_lock(root) as directory:
        target = read_handoff(root, handoff_id, store=store)
        if target.get("status") == "done":
            raise ValueError(f"会话交接包 {target['id']} 已结单。")
        now_iso = _now_dt().isoformat()
        updated = {k: v for k, v in target.items() if k != "briefing"}
        updated["status"] = "claimed"
        updated["claimed_by"] = str(client or "").strip() or "AI 客户端"
        updated["claimed_at"] = now_iso
        updated["updated_at"] = now_iso
        _atomic_write_json(directory / f"{updated['id']}.json", updated)
    result = dict(updated)
    result["briefing"] = render_briefing(updated, store=store)
    return result


def complete_handoff(root, handoff_id):
    """Mark one handoff pack as completed."""
    hid = _validate_id(handoff_id)
    with handoff_lock(root) as directory:
        path = directory / f"{hid}.json"
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"找不到会话交接包：{hid}")
        item = json.loads(path.read_text(encoding="utf-8"))
        now_iso = _now_dt().isoformat()
        item["status"] = "done"
        item["done_at"] = now_iso
        item["updated_at"] = now_iso
        _atomic_write_json(path, item)
        return item


def delete_handoff(root, handoff_id):
    """Delete one handoff pack from local state."""
    hid = _validate_id(handoff_id)
    with handoff_lock(root) as directory:
        path = directory / f"{hid}.json"
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"找不到会话交接包：{hid}")
        item = json.loads(path.read_text(encoding="utf-8"))
        path.unlink()
        return item


def active_handoff_hint(root):
    """Return a concise one-line SessionStart hint when an open handoff pack awaits pickup."""
    try:
        active = list_handoffs(root, include_done=False, limit=3)
    except Exception:
        return ""
    open_items = [it for it in active if it.get("status") == "open"] or active
    if not open_items:
        return ""
    top = open_items[0]
    extra = f"（另有 {len(open_items) - 1} 个）" if len(open_items) > 1 else ""
    target = f" → {top['target_client']}" if top.get("target_client") else ""
    return (
        f"- 待接力会话交接包{extra}：「{top['title']}」"
        f"（{top.get('source_client') or '其他客户端'}{target}，id={top['id']}）；"
        f"若用户要接着干，调用 memory_handoff(action='claim', handoff_id='{top['id']}') 获取完整交接包。"
    )
