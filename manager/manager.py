#!/usr/bin/env python3
"""Unified Memory & Skills Web Console: browse, create, and edit memories, manage skills, and control agents."""

import argparse
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime
import difflib
import errno
import fcntl
from hashlib import sha256
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
from string import Template
import sys
import time
import threading
from tempfile import NamedTemporaryFile
from urllib.parse import parse_qs, unquote, urlencode, urlsplit


APP = Path(__file__).resolve().parent
ROOT_DIR = APP.parent
if str(ROOT_DIR / "mcp") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "mcp"))
if str(ROOT_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "scripts"))

from library_paths import (
    ALL_LAYERS,
    config_signature,
    data_root,
    library_state_dir,
    library_unavailable_reason,
    resolve_libraries,
    unavailable_issue,
)
import audit_log  # noqa: E402
import memory_import  # noqa: E402
import secret_guard  # noqa: E402
import server as mcp_server  # noqa: E402
import sync_git  # noqa: E402
from async_sync import AsyncSyncScheduler  # noqa: E402

LAYERS = {
    "L0": ("操作约束", "feedback", "操作红线、只读范围与环境授权边界"),
    "L1": ("可复用方法", "reference", "经实践验证的排障、评测、验证与交付方法"),
    "L2": ("协作偏好", "feedback", "回答口径、评审顺序与个人协作习惯"),
    "L3": ("项目状态", "project", "版本、部署、基线对齐与恢复入口快照"),
    "L4": ("事故归档", "reference", "已定位根因的故障、修复措施与复验记录"),
}
LAYER_SHORT = {
    "L0": "约束",
    "L1": "方法",
    "L2": "偏好",
    "L3": "项目",
    "L4": "归档",
}
SKILL_CATS = {
    "infra": ("核心/基建", "L3", "记忆维护与技能管理"),
    "general": ("通用辅助", "L2", "使用者安装的通用技能"),
}
ALLOWED_LAYER_TYPES = {
    "L0": {"feedback", "reference"},
    "L1": {"reference"},
    "L2": {"feedback"},
    "L3": {"project"},
    "L4": {"reference"},
}
VALID_TOPIC_TYPES = {"user", "feedback", "project", "reference"}
STATES = {"active": "生效中", "archived": "已归档"}
SOURCES = {"all": "全部来源", "index": "现有记忆", "user": "用户管理"}
ENTRY_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}")
TOPIC_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
MASKED_REMOTE_URL_RE = re.compile(r"^(?:https?|ssh)://\*\*\*@", re.IGNORECASE)
ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+")
INDEX_LAYER = re.compile(r"^## (L[0-4])(?:\s|$)")
INDEX_LINK = re.compile(
    r"^\s*[-*+]\s+\[(.+?)\]\((?:<([^>]+)>|([^\s()]+))\)(?:\s+[—–-]\s+(.+))?$"
)
INLINE_TOKEN_RE = re.compile(
    r"(`[^`]+`)|(\*\*[^*]+\*\*)|(\[(.+?)\]\(([^)\s]+)\))"
)
ORDERED_ITEM_RE = re.compile(r"^\s*\d+\.\s+(.+)$")
UNORDERED_ITEM_RE = re.compile(r"^\s*[-*+]\s+(.+)$")
TABLE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")

MAX_POST = 1_200_000
MAX_TITLE_CHARS = 120
MAX_DESC_CHARS = 800
MAX_BODY_CHARS = 120_000
CUR_ATTR = ' aria-current="page"'


DESKTOP = False
PAGE_TEMPLATE = Template((APP / "page.html").read_text(encoding="utf-8"))


@dataclass(frozen=True)
class SyncJob:
    identifier: str
    outcome: Future
    action: str
    interactive: bool
    inline: bool
    created_at: float = 0.0


NAV_ICONS = {
    "library": '<path d="M5 4h11a3 3 0 013 3v13H8a3 3 0 01-3-3z"/><path d="M5 17a3 3 0 013-3h11"/>',
    "activity": '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    "skills": '<path d="M12 3l2 6 6 2-6 2-2 6-2-6-6-2 6-2z"/>',
    "clients": '<path d="M9 2v5M15 2v5M6 7h12v4a6 6 0 01-12 0zM12 17v5"/>',
    "sync": '<path d="M4 8h13l-3-3M20 16H7l3 3"/>',
}


def desktop_navigation(key, selected):
    routes = [
        ("工作区", "library", "记忆库", {}),
        ("", "activity", "读写动态", {"view": "activity"}),
        ("", "skills", "技能", {"tab": "skills"}),
        ("管理", "clients", "客户端接入", {"view": "clients"}),
        ("", "sync", "共享与同步", {"view": "sync_hub"}),
    ]
    links = []
    section = "记忆库"
    for group, slug, label, params in routes:
        if group:
            links.append(f'<span class="nav-group">{group}</span>')
        if slug == selected:
            section = label
        href = escape(query(library=key, **params))
        icon = f'<svg viewBox="0 0 24 24" aria-hidden="true">{NAV_ICONS[slug]}</svg>'
        links.append(f'<a href="{href}"{CUR_ATTR if slug == selected else ""}>'
                     f'<span class="nav-icon" aria-hidden="true">{icon}</span>{label}</a>')
    return dict(desktop_nav="".join(links), section_name=section,
                library_key=escape(key), list_title_short="技能" if selected == "skills" else "记忆")


def desktop_results(listing, layers, sources, sort_controls="", label="条目", count=0):
    layer_bar = f'<nav class="desk-pills" aria-label="快速分层筛选">{"".join(layers)}</nav>' if layers else ""
    source_bar = f'<nav class="desk-sources" aria-label="来源筛选">{sources}</nav>' if sources else ""
    return f'''<section class="desk-results"><div class="desk-tools"><h2>{label} · {count}</h2>
      {sort_controls}</div>{layer_bar}{source_bar}{listing}</section>'''


def desktop_status_strip(key, library):
    """One-line health summary for the desktop home: AI activity, clients without calls, sync state."""
    try:
        summary = audit_log.summarize()
    except Exception:
        return ""
    chips = [
        f'<span class="meta-chip">今日 AI 查询 {summary["reads_today"]} · 写入 {summary["writes_today"]}</span>',
    ]
    try:
        import memoryctl

        seen = {c["client"] for c in summary["clients"]}
        silent = []
        for aid, spec in memoryctl.agent_specs().items():
            info = memoryctl.inspect_agent(aid, spec)
            if info["state"] in {"managed", "partial", "drifted"} and info["name"] not in seen:
                silent.append(info["name"])
        if silent:
            chips.append(f'<span class="chip-warn">! {escape("、".join(silent))} 已配置但近 7 天无调用</span>')
    except Exception:
        pass
    try:
        sync_meta = render_library_sync_bar(library, key, "", tab="")
        chips.append(sync_meta["chips"])
        if sync_meta["conflicts"]:
            chips.append(f'<span class="chip-warn">! {len(sync_meta["conflicts"])} 个同步冲突待处理</span>')
    except Exception:
        pass
    try:
        report = mcp_server.MemoryStore(library.root).lint()
        by_sev = (report.get("summary") or {}).get("by_severity") or {}
        err_cnt = by_sev.get("error", 0)
        warn_cnt = by_sev.get("warning", 0)
        if err_cnt:
            chips.append(f'<span class="chip-warn">! 体检 {err_cnt} 项需要修复</span>')
        elif warn_cnt:
            chips.append(f'<span class="chip-warn">! 体检 {warn_cnt} 项待复核</span>')
    except Exception:
        pass
    href = escape(query(library=key, view="activity"))
    return f'<div class="desk-status check-chips">{"".join(chips)}<a href="{href}">查看读写动态 →</a></div>'


def load_template():
    return PAGE_TEMPLATE


def load_style():
    base = (APP / "style.css").read_bytes()
    return base + (APP / "desktop.css").read_bytes() if DESKTOP else base


def load_script():
    return (APP / "app.js").read_bytes()


class InvalidEntry(ValueError):
    """An entry could not be saved or decoded."""


class StaleEntry(Exception):
    """The file was edited after this form opened."""


@dataclass(frozen=True)
class IndexedTopic:
    title: str
    description: str
    layer: str
    path: str
    created_at: str = ""
    updated_at: str = ""
    created_epoch: float = 0.0
    updated_epoch: float = 0.0


@dataclass(frozen=True)
class TopicEntry:
    path: str = ""
    slug: str = ""
    meta_name: str = ""
    title: str = ""
    description: str = ""
    file_description: str = ""
    layer: str = "L1"
    entry_type: str = "reference"
    created_at: str = ""
    updated_at: str = ""
    verified_at: str = ""
    valid_until: str = ""
    body: str = ""
    revision: str = ""


@dataclass(frozen=True)
class Entry:
    name: str = ""
    title: str = ""
    description: str = ""
    layer: str = "L2"
    entry_type: str = ""
    body: str = ""
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""
    verified_at: str = ""
    valid_until: str = ""
    created_epoch: float = 0.0
    updated_epoch: float = 0.0
    revision: str = ""

    def resolved_type(self):
        allowed = ALLOWED_LAYER_TYPES.get(self.layer, {"feedback"})
        if self.entry_type in allowed:
            return self.entry_type
        return LAYERS[self.layer][1]

    def markdown(self):
        metadata = {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "type": self.resolved_type(),
            "layer": self.layer,
            "status": self.status,
            "created_at": self.created_at or self.updated_at,
            "updated_at": self.updated_at,
        }
        if self.verified_at:
            metadata["verified_at"] = self.verified_at
        if self.valid_until:
            metadata["valid_until"] = self.valid_until
        fields = "\n".join(
            f"{key}: {json.dumps(value, ensure_ascii=False)}"
            for key, value in metadata.items()
        )
        return f"---\n{fields}\n---\n\n{self.body}\n"

    @classmethod
    def from_file(cls, name, raw):
        text = raw.decode("utf-8").replace("\r\n", "\n")
        opening, boundary, body = text.removeprefix("---\n").partition("\n---\n")
        if not text.startswith("---\n") or not boundary:
            raise InvalidEntry("缺少条目头部。")
        fields = {}
        for line in opening.splitlines():
            key, separator, value = line.partition(":")
            if not separator or key in fields:
                raise InvalidEntry("条目头部格式无效。")
            fields[key] = json.loads(value)
        required = {"name", "title", "description", "type", "layer", "status", "updated_at"}
        allowed_keys = required | {"created_at", "verified_at", "valid_until"}
        if not (required <= fields.keys() <= allowed_keys) or any(not isinstance(v, str) for v in fields.values()):
            raise InvalidEntry("条目头部字段不匹配。")
        if fields["name"] != name or fields["layer"] not in LAYERS or fields["status"] not in STATES:
            raise InvalidEntry("条目名称、层级或状态无效。")
        entry_type = fields.pop("type")
        if entry_type not in ALLOWED_LAYER_TYPES[fields["layer"]]:
            raise InvalidEntry("条目类型与层级不匹配。")
        upd_str = fields["updated_at"]
        cre_str = fields.pop("created_at", "") or upd_str
        verified_str = fields.pop("verified_at", "").strip()
        valid_until_str = fields.pop("valid_until", "").strip()
        upd_dt, _ = mcp_server.parse_timestamp(upd_str)
        cre_dt, _ = mcp_server.parse_timestamp(cre_str)
        if cre_dt > upd_dt:
            cre_dt = upd_dt
        return cls(
            **fields,
            created_at=cre_str,
            verified_at=verified_str,
            valid_until=valid_until_str,
            created_epoch=cre_dt.timestamp(),
            updated_epoch=upd_dt.timestamp(),
            entry_type=entry_type,
            body=body.removeprefix("\n").removesuffix("\n"),
            revision=sha256(raw).hexdigest(),
        )


def parse_index_markdown(markdown):
    """Parse L0-L4 indexed topics from MEMORY.md text."""
    topics = {}
    layer = ""
    for line in markdown.splitlines():
        if line.startswith("#"):
            heading = INDEX_LAYER.match(line)
            layer = heading[1] if heading else ""
        match = INDEX_LINK.match(line) if layer else None
        if not match:
            continue
        title, named_path, plain_path, description = match.groups()
        try:
            url = urlsplit(named_path or plain_path)
        except ValueError:
            continue
        relative = Path(unquote(url.path))
        if (
            url.scheme
            or url.netloc
            or relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
            or relative.parts[0] == "user-managed"
            or relative.suffix.lower() != ".md"
            or relative == Path("MEMORY.md")
        ):
            continue
        path = relative.as_posix()
        topics.setdefault(path, IndexedTopic(title.strip(), description or "", layer, path))
    return topics


class Library:
    def __init__(
        self,
        root,
        label,
        *,
        library_id="personal",
        role="personal",
        mode="rw",
        allowed_layers=None,
        allow_user_managed=True,
        sync_config=None,
        async_scheduler=None,
    ):
        root = data_root(root).resolve(strict=True)
        self.root = root
        self.library_id = library_id
        self.role = role
        self.mode = mode
        self.allowed_layers = frozenset(allowed_layers if allowed_layers is not None else ALL_LAYERS)
        self.allow_user_managed = bool(allow_user_managed)
        self.sync_config = sync_config
        # One scheduler is shared by all libraries in a manager process.  Keeping
        # this injectable preserves the lightweight Library API used by tests and
        # lets the process owner control worker shutdown.
        self.async_scheduler = async_scheduler
        self.index = root / "MEMORY.md"
        if root.name.casefold() in {"memories", "memories_v2"} or not self.index.is_file():
            raise InvalidEntry("请选择包含 MEMORY.md 的手工记忆库目录。")
        self.folder = root / "user-managed"
        if self.allow_user_managed:
            self.folder.mkdir(mode=0o700, exist_ok=True)
            if self.folder.is_symlink() or not self.folder.is_dir():
                raise InvalidEntry("用户管理目录必须是普通目录。")
        self.state_dir = library_state_dir(root)
        self.lock_path = self.state_dir / "library.lock"
        self.trash_dir = self.state_dir / "trash"
        self.label = label
        # The web UI keeps the primary write API stable (save_topic still returns a path), while
        # retaining the latest non-fatal Git snapshot outcome for notices and diagnostics.
        self.last_sync_status = None

    def _enqueue_async_sync(self, message):
        """Queue a debounced remote Git sync after a successful local write.

        This is deliberately best-effort.  Local writes and their existing
        ``auto_commit`` behavior remain authoritative; scheduler failures are
        persisted by the worker and must never turn a completed web write into
        an HTTP error.
        """
        scheduler = self.async_scheduler
        if scheduler is None:
            return None
        config = self.sync_config or {}
        if (
            self.mode != "rw"
            or str(config.get("driver") or "").casefold() != "git"
            or not bool(config.get("auto_push", False))
        ):
            return None
        try:
            return scheduler.enqueue(
                self.root,
                sync_config=config,
                mode=self.mode,
                reason=message,
            )
        except Exception:
            return None

    def async_sync_status(self):
        """Return the latest persisted/in-process asynchronous sync status."""
        scheduler = self.async_scheduler
        if scheduler is not None:
            try:
                return scheduler.status(self.root)
            except Exception:
                pass
        path = self.state_dir / "sync-status.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (FileNotFoundError, OSError, ValueError):
            return {}

    @property
    def is_readonly(self):
        return self.mode == "ro"

    def _ensure_writable(self, layer=None, user_managed=False):
        if self.is_readonly:
            raise InvalidEntry(f"记忆库「{self.label}」为只读挂载，不支持写入或删除。")
        if user_managed and not self.allow_user_managed:
            raise InvalidEntry(f"共享记忆库「{self.label}」不支持保存个人手记。")
        if layer is not None and layer not in self.allowed_layers:
            allowed_str = " / ".join(sorted(self.allowed_layers))
            raise InvalidEntry(f"记忆库「{self.label}」未启用层级 {layer}（允许：{allowed_str}）。")

    def _maybe_auto_commit(self, message):
        """Auto-commit to Git without making the primary web write fail.

        Existing manager callers intentionally ignore the return value, so a successful write
        still returns the same path/None as before.  The returned status and
        ``last_sync_status`` make failures observable to callers that opt in, while the audit
        event and stderr warning provide a durable/operator-visible trail.
        """
        self.last_sync_status = None
        if not self.sync_config or self.sync_config.get("driver") != "git":
            return None
        if not self.sync_config.get("auto_commit", True):
            return None

        def redacted(value):
            return sync_git.redact_remote_url(str(value or ""))[:320]

        def failure(result, *, error="", reason=None):
            if isinstance(result, dict):
                status = dict(result)
            else:
                status = {"committed": False, "reason": reason or "sync_error"}
            status["committed"] = False
            reason = redacted(status.get("reason") or "unknown")
            detail = redacted(error or status.get("error") or "")
            status["reason"] = reason
            if detail:
                status["error"] = detail
            status["warning"] = f"记忆已保存，但 Git 快照未提交（原因：{reason}）。"
            self.last_sync_status = status
            audit_log.record(
                {
                    "source": "manager",
                    "event": "git_auto_commit",
                    "client": "Unified Memory Web",
                    "tool": "git_auto_commit",
                    "ok": False,
                    "library": self.library_id,
                    "message": redacted(message),
                    "reason": reason,
                    "error": detail,
                }
            )
            try:
                print(f"[unified-memory] {status['warning']}", file=sys.stderr)
            except Exception:
                pass
            return status

        try:
            result = sync_git.auto_commit(self.root, message)
        except Exception as exc:
            return failure(None, error=exc, reason="exception")
        if isinstance(result, dict) and not result.get("committed") and result.get("reason") in {
            "clean",
            "nothing_allowed",
        }:
            self.last_sync_status = result
            return result
        if not isinstance(result, dict) or not result.get("committed"):
            return failure(result)
        self.last_sync_status = result
        return result

    @contextmanager
    def write_lock(self):
        """Serialize concurrent writes with MCP servers."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _backup_to_trash(self, src_path, slug_hint):
        """Save a safety backup copy into .state/trash/ before deleting a memory."""
        if not src_path.is_file():
            return
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_slug = re.sub(r"[^A-Za-z0-9._-]+", "_", slug_hint)[:80] or "entry"
        dest = self.trash_dir / f"{stamp}-{safe_slug}-{secrets.token_hex(2)}.md"
        shutil.copy2(src_path, dest)

    def topic_versions(self, slug, limit=8):
        """List saved previous versions of one entry.

        The MCP server and this UI share one trash directory per library
        (``library_state_dir``), so both see every backup the other made.
        """
        if not mcp_server.NAME_RE.fullmatch(slug or ""):
            return []
        store = mcp_server.MemoryStore(self.root, label=self.label, allowed_layers=self.allowed_layers)
        try:
            return store.entry_history(slug, limit=limit).get("versions", [])
        except (ValueError, OSError):
            return []

    def topic_links(self, topic_path, body):
        """Return (outgoing links, incoming backlinks) for one topic."""
        store = mcp_server.MemoryStore(self.root, label=self.label, allowed_layers=self.allowed_layers)
        try:
            return store.entry_links(topic_path, body)
        except (ValueError, OSError):
            return [], []

    def verify_topic(self, topic_path, expected_revision=""):
        """Mark a topic as verified today; clear an already-expired valid_until date."""
        _, current_topics = self.read_index()
        try:
            cur = self.read_topic(topic_path, current_topics)
        except (FileNotFoundError, InvalidEntry) as exc:
            raise StaleEntry from exc
        if expected_revision and cur.revision != expected_revision:
            raise StaleEntry
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        next_valid_until = "" if mcp_server.is_date_expired(cur.valid_until) else cur.valid_until
        return self.save_topic(
            orig_path=cur.path,
            expected_revision=cur.revision,
            layer=cur.layer,
            title=cur.title,
            description=cur.description,
            body=cur.body,
            slug=cur.slug,
            entry_type=cur.entry_type,
            verified_at=today,
            valid_until=next_valid_until,
        )

    def restore_version(self, version):
        """Put a saved version back as the current entry; returns its path."""
        if self.is_readonly:
            raise InvalidEntry("该库为只读挂载，无法恢复历史版本。")
        store = mcp_server.MemoryStore(self.root, label=self.label, allowed_layers=self.allowed_layers)
        # restore_entry takes the shared write lock itself, so do not wrap it.
        result = store.restore_entry(version)
        return result.get("path", "")

    def read_markdown(self, path, resource_roots=()):
        resolved = path.resolve(strict=True)
        allowed = resolved.is_relative_to(self.root) or any(resolved.is_relative_to(r) for r in resource_roots)
        if not allowed or resolved.suffix.lower() != ".md" or not resolved.is_file():
            raise InvalidEntry("只能读取当前库或技能目录中的 Markdown。")
        with resolved.open(encoding="utf-8") as stream:
            content = stream.read(MAX_POST + 1)
        if len(content) > MAX_POST:
            raise InvalidEntry("Markdown 超过可预览的大小。")
        return content

    def read_index(self):
        markdown = self.read_markdown(self.index)
        parsed = parse_index_markdown(markdown)
        enriched = {}
        for rel_path, t in parsed.items():
            md_file = self.root / rel_path
            if md_file.is_file():
                try:
                    raw = md_file.read_text(encoding="utf-8", errors="replace")
                    meta, _ = mcp_server.parse_frontmatter(raw)
                    cre_at, upd_at, cre_ep, upd_ep = mcp_server.file_timestamps(md_file, meta)
                    enriched[rel_path] = replace(
                        t,
                        created_at=cre_at,
                        updated_at=upd_at,
                        created_epoch=cre_ep,
                        updated_epoch=upd_ep,
                    )
                    continue
                except OSError:
                    pass
            enriched[rel_path] = t
        return markdown, enriched

    def read_index_with_revision(self):
        raw = self.index.read_bytes()
        text = raw.decode("utf-8").replace("\r\n", "\n")
        return text, sha256(raw).hexdigest()

    def save_index_markdown(self, new_markdown, expected_revision):
        self._ensure_writable()
        normalized = new_markdown.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized or len(normalized) > MAX_BODY_CHARS:
            raise InvalidEntry("MEMORY.md 内容不能为空且须在长度上限内。")
        found_layers = []
        for line in normalized.splitlines():
            m = INDEX_LAYER.match(line)
            if m:
                found_layers.append(m.group(1))
        for req_layer in ("L0", "L1", "L2", "L3", "L4"):
            if req_layer not in found_layers:
                raise InvalidEntry(f"MEMORY.md 必须保留完整分层标题，缺少 ## {req_layer}。")
        parsed_topics = parse_index_markdown(normalized)
        for rel_path in parsed_topics:
            candidate = (self.root / rel_path).resolve()
            if not candidate.is_relative_to(self.root) or not candidate.is_file():
                raise InvalidEntry(f"索引引用的文件不存在：{rel_path}")

        with self.write_lock():
            cur_rev = sha256(self.index.read_bytes()).hexdigest()
            if cur_rev != expected_revision:
                raise StaleEntry
            mcp_server.atomic_write(self.index, normalized + "\n")
        message = f"memory({self.library_id}): update MEMORY.md"
        self._maybe_auto_commit(message)
        self._enqueue_async_sync(message)

    @staticmethod
    def _topic_revision(raw_bytes, topic):
        meta_sig = f"\n{topic.layer}\n{topic.title}\n{topic.description}".encode("utf-8")
        return sha256(raw_bytes + meta_sig).hexdigest()

    @staticmethod
    def _canonical_topic_path(topic_path, topics):
        """Resolve a topic URL against the current library's indexed paths only."""
        if not isinstance(topic_path, str):
            raise InvalidEntry("条目路径无效。")
        topic_path = topic_path.strip()
        if (
            not topic_path
            or "\x00" in topic_path
            or "\\" in topic_path
            or topic_path.startswith("/")
            or any(part in {"", ".", ".."} for part in topic_path.split("/"))
        ):
            raise InvalidEntry("条目路径无效。")
        if topic_path in topics:
            return topic_path
        if not topic_path.endswith(".md"):
            with_suffix = f"{topic_path}.md"
            if with_suffix in topics:
                return with_suffix
        raise InvalidEntry("条目不在索引中。")

    def read_topic(self, topic_path, topics=None):
        if topics is None:
            _, topics = self.read_index()
        canonical_path = self._canonical_topic_path(topic_path, topics)
        topic = topics[canonical_path]
        detail_path = (self.root / topic.path).resolve(strict=True)
        if not detail_path.is_relative_to(self.root) or not detail_path.is_file():
            raise InvalidEntry("找不到对应的记忆 Markdown 文件。")
        raw_bytes = detail_path.read_bytes()
        if len(raw_bytes) > MAX_POST:
            raise InvalidEntry("条目超过大小限制。")
        detail = raw_bytes.decode("utf-8").replace("\r\n", "\n")
        meta, body = mcp_server.parse_frontmatter(detail)
        cre_at, upd_at, _, _ = mcp_server.file_timestamps(detail_path, meta)
        stem = Path(topic.path).stem
        return TopicEntry(
            path=topic.path,
            slug=stem,
            meta_name=meta.get("name", stem),
            title=topic.title,
            description=topic.description,
            file_description=meta.get("description", ""),
            layer=topic.layer,
            entry_type=meta.get("type", LAYERS[topic.layer][1]),
            created_at=cre_at,
            updated_at=upd_at,
            verified_at=(meta.get("verified_at") or "").strip(),
            valid_until=(meta.get("valid_until") or "").strip(),
            body=body.rstrip("\n"),
            revision=self._topic_revision(raw_bytes, topic),
        )

    def _generate_unique_slug(self, title, preferred_slug=""):
        slug = preferred_slug.strip()
        if slug:
            if not TOPIC_SLUG_RE.fullmatch(slug) or slug.lower() == "readme":
                raise InvalidEntry("文件标识仅支持英文字母、数字、点、下划线或连字符（且不能为 readme）。")
            for folder in mcp_server.LAYER_DIRS.values():
                if (self.root / folder / f"{slug}.md").exists():
                    raise InvalidEntry(f"已存在同名文件 {folder}/{slug}.md，请更换文件标识或直接编辑原条目。")
            return slug

        tokens = ASCII_TOKEN_RE.findall(title.casefold())
        prefix = "-".join(tokens)[:42].strip("-") if tokens else "mem"
        if not prefix:
            prefix = "mem"
        stamp = date.today().strftime("%Y%m%d")
        candidate = f"{prefix}-{stamp}"
        while any((self.root / folder / f"{candidate}.md").exists() for folder in mcp_server.LAYER_DIRS.values()):
            candidate = f"{prefix}-{stamp}-{secrets.token_hex(2)}"
        return candidate

    def save_topic(self, orig_path, expected_revision, layer, title, description, body, slug="", entry_type="",
                   verified_at=None, valid_until=None):
        self._ensure_writable(layer=layer)
        validate_common_fields(layer, title, description, body, verified_at=verified_at, valid_until=valid_until)
        norm_verified = None if verified_at is None else str(verified_at).strip()
        norm_valid_until = None if valid_until is None else str(valid_until).strip()
        with self.write_lock():
            store = mcp_server.MemoryStore(self.root)
            _, current_topics = self.read_index()

            if orig_path:
                try:
                    cur = self.read_topic(orig_path, current_topics)
                except (FileNotFoundError, InvalidEntry) as exc:
                    raise StaleEntry from exc
                orig_path = cur.path
                if cur.revision != expected_revision:
                    raise StaleEntry

                stem = cur.slug
                new_folder = mcp_server.LAYER_DIRS[layer]
                new_rel = f"{new_folder}/{stem}.md"
                new_file = self.root / new_rel
                if new_rel != orig_path and new_file.exists():
                    raise InvalidEntry(f"目标层级目录已存在同名文件：{new_rel}")

                fm_name = cur.meta_name or stem
                if description.strip() == cur.description.strip() and cur.file_description:
                    fm_desc = cur.file_description
                else:
                    fm_desc = description.strip() or title

                if entry_type in VALID_TOPIC_TYPES:
                    fm_type = entry_type
                elif layer == cur.layer and cur.entry_type in VALID_TOPIC_TYPES:
                    fm_type = cur.entry_type
                else:
                    fm_type = LAYERS[layer][1]

                ts_now = mcp_server.now_iso(cur.updated_at)
                cre_at = cur.created_at or ts_now
                eff_verified = cur.verified_at if norm_verified is None else norm_verified
                eff_valid_until = cur.valid_until if norm_valid_until is None else norm_valid_until
                if eff_verified and eff_valid_until and eff_valid_until < eff_verified:
                    raise InvalidEntry("有效期截止日期不能早于核实日期。")
                name_serialized = (
                    fm_name if TOPIC_SLUG_RE.fullmatch(fm_name) else json.dumps(fm_name, ensure_ascii=False)
                )
                desc_serialized = json.dumps(fm_desc, ensure_ascii=False)
                extra_fm = ""
                if eff_verified:
                    extra_fm += f"verified_at: {json.dumps(eff_verified)}\n"
                if eff_valid_until:
                    extra_fm += f"valid_until: {json.dumps(eff_valid_until)}\n"
                content = (
                    f"---\n"
                    f"name: {name_serialized}\n"
                    f"description: {desc_serialized}\n"
                    f"type: {fm_type}\n"
                    f"created_at: {json.dumps(cre_at)}\n"
                    f"updated_at: {json.dumps(ts_now)}\n"
                    f"{extra_fm}"
                    f"---\n\n"
                    f"{body.strip()}\n"
                )
                prev_bytes = new_file.read_bytes() if new_file.is_file() else None
                prev_index_text = self.index.read_text(encoding="utf-8")
                mcp_server.atomic_write(new_file, content)
                idx_desc = description.strip() or title
                try:
                    if new_rel != orig_path:
                        store._remove_index_line(orig_path)
                    store._upsert_index_line(layer, title, new_rel, idx_desc)
                except Exception:
                    if prev_bytes is None:
                        new_file.unlink(missing_ok=True)
                    else:
                        mcp_server.atomic_write(new_file, prev_bytes.decode("utf-8"))
                    try:
                        mcp_server.atomic_write(self.index, prev_index_text)
                    except OSError:
                        pass
                    raise
                if new_rel != orig_path:
                    (self.root / orig_path).unlink(missing_ok=True)
                message = f"memory({self.library_id}): update {new_rel}"
                self._maybe_auto_commit(message)
                self._enqueue_async_sync(message)
                return new_rel

            ts_now = mcp_server.now_iso()
            final_slug = self._generate_unique_slug(title, slug)
            new_folder = mcp_server.LAYER_DIRS[layer]
            new_rel = f"{new_folder}/{final_slug}.md"
            new_file = self.root / new_rel
            fm_type = entry_type if entry_type in VALID_TOPIC_TYPES else LAYERS[layer][1]
            idx_desc = description.strip() or title
            desc_serialized = json.dumps(idx_desc, ensure_ascii=False)
            eff_verified = norm_verified or ""
            eff_valid_until = norm_valid_until or ""
            extra_fm = ""
            if eff_verified:
                extra_fm += f"verified_at: {json.dumps(eff_verified)}\n"
            if eff_valid_until:
                extra_fm += f"valid_until: {json.dumps(eff_valid_until)}\n"
            content = (
                f"---\n"
                f"name: {final_slug}\n"
                f"description: {desc_serialized}\n"
                f"type: {fm_type}\n"
                f"created_at: {json.dumps(ts_now)}\n"
                f"updated_at: {json.dumps(ts_now)}\n"
                f"{extra_fm}"
                f"---\n\n"
                f"{body.strip()}\n"
            )
            mcp_server.atomic_write(new_file, content)
            try:
                store._upsert_index_line(layer, title, new_rel, idx_desc)
            except Exception:
                new_file.unlink(missing_ok=True)
                raise
            message = f"memory({self.library_id}): create {new_rel}"
            self._maybe_auto_commit(message)
            self._enqueue_async_sync(message)
            return new_rel

    def archive_topic(self, topic_path, expected_revision):
        self._ensure_writable(layer="L4")
        with self.write_lock():
            store = mcp_server.MemoryStore(self.root)
            _, current_topics = self.read_index()
            try:
                cur = self.read_topic(topic_path, current_topics)
            except (FileNotFoundError, InvalidEntry) as exc:
                raise StaleEntry from exc
            if cur.revision != expected_revision:
                raise StaleEntry
            topic_path = cur.path
            if cur.layer == "L4":
                return cur.path

            new_rel = f"archive/{cur.slug}.md"
            dest = self.root / new_rel
            if dest.exists():
                raise InvalidEntry(f"归档目录已存在同名文件：{new_rel}")

            ts_now = mcp_server.now_iso(cur.updated_at)
            cre_at = cur.created_at or ts_now
            fm_name = cur.meta_name or cur.slug
            name_serialized = (
                fm_name if TOPIC_SLUG_RE.fullmatch(fm_name) else json.dumps(fm_name, ensure_ascii=False)
            )
            fm_desc = cur.file_description or cur.description or cur.title
            desc_serialized = json.dumps(fm_desc, ensure_ascii=False)
            fm_type = cur.entry_type if cur.entry_type in VALID_TOPIC_TYPES else "reference"
            extra_fm = ""
            if cur.verified_at:
                extra_fm += f"verified_at: {json.dumps(cur.verified_at)}\n"
            if cur.valid_until:
                extra_fm += f"valid_until: {json.dumps(cur.valid_until)}\n"
            content = (
                f"---\n"
                f"name: {name_serialized}\n"
                f"description: {desc_serialized}\n"
                f"type: {fm_type}\n"
                f"created_at: {json.dumps(cre_at)}\n"
                f"updated_at: {json.dumps(ts_now)}\n"
                f"{extra_fm}"
                f"---\n\n"
                f"{cur.body.strip()}\n"
            )
            mcp_server.atomic_write(dest, content)
            try:
                store._remove_index_line(topic_path)
                store._upsert_index_line("L4", cur.title, new_rel, cur.description or fm_desc)
            except Exception:
                dest.unlink(missing_ok=True)
                raise
            (self.root / topic_path).unlink(missing_ok=True)
            message = f"memory({self.library_id}): archive {topic_path} -> {new_rel}"
            self._maybe_auto_commit(message)
            self._enqueue_async_sync(message)
            return new_rel

    def delete_topic(self, topic_path, expected_revision):
        self._ensure_writable()
        with self.write_lock():
            store = mcp_server.MemoryStore(self.root)
            _, current_topics = self.read_index()
            try:
                cur = self.read_topic(topic_path, current_topics)
            except (FileNotFoundError, InvalidEntry) as exc:
                raise StaleEntry from exc
            if cur.revision != expected_revision:
                raise StaleEntry
            topic_path = cur.path
            target = self.root / topic_path
            self._backup_to_trash(target, cur.slug)
            target.unlink()
            store._remove_index_line(topic_path)
        message = f"memory({self.library_id}): delete {topic_path}"
        self._maybe_auto_commit(message)
        self._enqueue_async_sync(message)

    def path(self, name):
        if not self.allow_user_managed:
            raise InvalidEntry("共享记忆库不支持个人手记。")
        if not ENTRY_NAME.fullmatch(name) or name == "readme":
            raise InvalidEntry("条目名称无效。")
        return self.folder / f"{name}.md"

    def read(self, name):
        path = self.path(name)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(path, flags), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise InvalidEntry("条目必须是普通 Markdown 文件。")
            raw = stream.read(MAX_POST + 1)
        if len(raw) > MAX_POST:
            raise InvalidEntry("条目超过大小限制。")
        return Entry.from_file(name, raw)

    def list(self):
        if not self.allow_user_managed or not self.folder.is_dir():
            return [], []
        entries, unreadable = [], []
        for path in sorted(self.folder.glob("*.md")):
            if path.name == "README.md" or sync_git.is_conflict_filename(path.name):
                continue
            try:
                entries.append(self.read(path.stem))
            except (ValueError, OSError):
                unreadable.append(path.name)
        entries.sort(key=lambda entry: (entry.updated_at, entry.title.casefold()), reverse=True)
        return entries, unreadable

    def save(self, entry):
        self._ensure_writable(layer=entry.layer, user_managed=True)
        try:
            secret_guard.ensure_no_secrets(entry.title, entry.description, entry.body)
            norm_v = mcp_server.normalize_iso_date(entry.verified_at, "verified_at") if entry.verified_at else ""
            norm_u = mcp_server.normalize_iso_date(entry.valid_until, "valid_until") if entry.valid_until else ""
        except ValueError as exc:
            raise InvalidEntry(str(exc)) from exc
        if norm_v and norm_u and norm_u < norm_v:
            raise InvalidEntry("有效期截止日期不能早于核实日期。")
        entry = replace(entry, verified_at=norm_v, valid_until=norm_u)
        path = self.path(entry.name)
        with self.write_lock():
            if entry.revision:
                try:
                    current = self.read(entry.name)
                except (FileNotFoundError, ValueError) as exc:
                    raise StaleEntry from exc
                if entry.revision != current.revision:
                    raise StaleEntry
                ts_now = mcp_server.now_iso(current.updated_at)
            else:
                ts_now = mcp_server.now_iso()
            entry = replace(entry, updated_at=ts_now)
            staged = None
            try:
                with NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.folder, prefix=".memory-", delete=False
                ) as stream:
                    staged = Path(stream.name)
                    stream.write(entry.markdown())
                    stream.flush()
                    os.fsync(stream.fileno())
                if entry.revision:
                    staged.replace(path)
                else:
                    os.link(staged, path)
            finally:
                if staged is not None:
                    staged.unlink(missing_ok=True)
        message = f"memory({self.library_id}): save user-managed/{entry.name}.md"
        self._maybe_auto_commit(message)
        self._enqueue_async_sync(message)

    def delete_user_entry(self, name, expected_revision):
        self._ensure_writable(user_managed=True)
        path = self.path(name)
        with self.write_lock():
            try:
                current = self.read(name)
            except (FileNotFoundError, ValueError) as exc:
                raise StaleEntry from exc
            if current.revision != expected_revision:
                raise StaleEntry
            self._backup_to_trash(path, name)
            path.unlink()
        message = f"memory({self.library_id}): delete user-managed/{name}.md"
        self._maybe_auto_commit(message)
        self._enqueue_async_sync(message)


def validate_common_fields(layer, title, description, body, verified_at=None, valid_until=None):
    if layer not in LAYERS:
        raise InvalidEntry("请选择 L0–L4 中的一层。")
    if not title or len(title) > MAX_TITLE_CHARS or len(title.splitlines()) != 1:
        raise InvalidEntry(f"标题须为单行且长度为 1–{MAX_TITLE_CHARS} 个字符。")
    if "](" in title:
        raise InvalidEntry("标题中不能包含 Markdown 链接分隔符 ']('。")
    if len(description) > MAX_DESC_CHARS or len(description.splitlines()) > 1:
        raise InvalidEntry(f"检索摘要须为单行且不超过 {MAX_DESC_CHARS} 个字符。")
    if not body.strip() or len(body) > MAX_BODY_CHARS:
        raise InvalidEntry(f"正文不能为空且不超过 {MAX_BODY_CHARS:,} 个字符。")
    try:
        norm_v = "" if verified_at is None else mcp_server.normalize_iso_date(verified_at, "verified_at")
        norm_u = "" if valid_until is None else mcp_server.normalize_iso_date(valid_until, "valid_until")
    except ValueError as exc:
        raise InvalidEntry("核实日期和有效期截止日期须为 YYYY-MM-DD 格式。") from exc
    if norm_v and norm_u and norm_u < norm_v:
        raise InvalidEntry("有效期截止日期不能早于核实日期。")
    try:
        secret_guard.ensure_no_secrets(title, description, body)
    except ValueError as exc:
        raise InvalidEntry(str(exc)) from exc


def hidden(name, value):
    return f'<input type="hidden" name="{escape(name)}" value="{escape(value)}">'


def query(**fields):
    clean = {k: v for k, v in fields.items() if v != ""}
    return "/?" + urlencode(clean) if clean else "/"


def topic_read_error(library, topic_path, exc):
    """Return a user-facing diagnosis without exposing local filesystem paths."""
    display_path = topic_path or "（空）"
    reason = str(exc)
    if reason == "条目不在索引中。":
        return (
            f'当前记忆库「{library.label}」的索引中没有条目「{display_path}」。'
            "该链接可能来自其他记忆库，或索引已更新；请切换到包含它的记忆库，"
            "或返回当前库概览。"
        )
    if reason == "找不到对应的记忆 Markdown 文件。":
        return (
            f'当前记忆库「{library.label}」的索引包含「{display_path}」，'
            "但对应 Markdown 文件不存在；请刷新索引或从列表重新打开。"
        )
    if reason == "条目路径无效。":
        return f'条目路径「{display_path}」无效，请从当前记忆库列表重新打开。'
    if isinstance(exc, OSError):
        return (
            f'无法读取当前记忆库「{library.label}」中的「{display_path}」对应文件，'
            "请检查索引与记忆目录是否一致。"
        )
    return f'无法读取当前记忆库「{library.label}」中的「{display_path}」，请检查条目格式。'


def display_remote_url(remote_url):
    """Return a credential-free URL for HTML values and status text."""
    return sync_git.redact_remote_url(remote_url or "")


def reject_masked_remote_url(remote_url):
    """Do not persist the UI's redacted placeholder as a real Git remote."""
    if MASKED_REMOTE_URL_RE.match(str(remote_url or "").strip()):
        raise InvalidEntry("远端地址中的凭据已隐藏，请重新填写不带 token 的仓库地址。")


def render_layer_radios(selected_layer):
    pills = []
    for lk, (lname, _, _) in LAYERS.items():
        rid = f"layer-opt-{lk}"
        checked = " checked" if lk == selected_layer else ""
        pills.append(
            f'<span class="radio-pill">'
            f'<input type="radio" id="{rid}" name="layer" value="{lk}"{checked} required>'
            f'<label for="{rid}"><span class="layer-badge layer-{lk}">{lk}</span> {escape(lname)}</label>'
            f"</span>"
        )
    return (
        f'<fieldset class="choice-fieldset">'
        f"<legend>归入层级</legend>"
        f'<div class="radio-cards">{"".join(pills)}</div>'
        f"</fieldset>"
    )


def render_scope_radios(selected_scope):
    opts = [
        ("index", "主知识库（写入对应分层目录并自动同步 MEMORY.md，已接入的 Agent 默认检索）"),
        ("user", "个人手记区（仅存入 user-managed/，由你手动管理）"),
    ]
    pills = []
    for val, label_txt in opts:
        rid = f"scope-opt-{val}"
        checked = " checked" if val == selected_scope else ""
        pills.append(
            f'<span class="radio-pill">'
            f'<input type="radio" id="{rid}" name="scope" value="{val}"{checked}>'
            f'<label for="{rid}">{escape(label_txt)}</label>'
            f"</span>"
        )
    return (
        f'<fieldset class="choice-fieldset">'
        f"<legend>存储位置</legend>"
        f'<div class="radio-cards">{"".join(pills)}</div>'
        f"</fieldset>"
    )


def render_memory_editor_html(
    *,
    mode,
    scope,
    token,
    library_key,
    layer,
    title,
    description,
    body,
    cancel_link,
    slug="",
    orig_topic="",
    entry_name="",
    revision="",
    entry_type="",
    verified_at="",
    valid_until="",
    file_meta_note="",
):
    """Render unified accessible Markdown editor for indexed topics and user-managed notes."""
    is_new = mode == "new"
    intro_text = (
        "新建记忆默认写入主知识库并同步更新 MEMORY.md 索引；也可切换为仅保存在个人手记区（user-managed/）。"
        if is_new
        else (
            f"正在编辑主知识库条目（保存时自动同步更新 MEMORY.md 索引；切换层级会自动迁移目录文件）。"
            if scope == "index"
            else "正在编辑个人手记（位于 user-managed/，不会改动自动索引条目）。"
        )
    )
    scope_html = (
        render_scope_radios(scope)
        if is_new
        else hidden("scope", scope)
    )
    slug_field_html = ""
    if is_new:
        slug_field_html = f"""
          <div class="field">
            <label for="slug">文件标识（可选）</label>
            <input id="slug" name="slug" type="text" value="{escape(slug)}" maxlength="90"
                   autocorrect="off" autocapitalize="none" spellcheck="false"
                   aria-describedby="slug-help" placeholder="例如：weekly-report-export">
            <span id="slug-help" class="field-hint">英文/数字/连字符文件名（主知识库使用），留空则根据标题与日期自动生成。</span>
          </div>"""

    title_row = (
        f'<div class="form-row-two">'
        f'<div class="field">'
        f'<label for="title">标题 <span class="required">必填</span></label>'
        f'<input id="title" name="title" type="text" value="{escape(title)}" maxlength="{MAX_TITLE_CHARS}" '
        f'placeholder="例如：每周报告的导出方法" required autofocus>'
        f"</div>"
        f"{slug_field_html}"
        f"</div>"
        if is_new
        else f'<div class="field">'
        f'<label for="title">标题 <span class="required">必填</span></label>'
        f'<input id="title" name="title" type="text" value="{escape(title)}" maxlength="{MAX_TITLE_CHARS}" required>'
        f"</div>"
    )

    submit_txt = (
        "保存新记忆"
        if is_new
        else ("保存记忆并同步索引" if scope == "index" else "保存个人手记")
    )
    hidden_inputs = (
        hidden("csrf", token)
        + hidden("library", library_key)
        + hidden("orig_topic", orig_topic)
        + hidden("name", entry_name)
        + hidden("revision", revision)
        + hidden("entry_type", entry_type)
        + hidden("create_id", f"entry-{secrets.token_hex(10)}")
    )
    footer_meta = f'<div class="file-info">{file_meta_note}</div>' if file_meta_note else ""

    return f"""
      <div class="editor-wrap">
        <p class="editor-intro">{escape(intro_text)}</p>
        <form class="edit-form" method="post" action="/save#feedback" aria-labelledby="detail-title">
          {hidden_inputs}
          {scope_html}
          {render_layer_radios(layer if layer in LAYERS else "L1")}
          {title_row}
          <div class="field">
            <label for="description">一句话索引摘要 / 检索线索</label>
            <input id="description" name="description" type="text" value="{escape(description)}" maxlength="{MAX_DESC_CHARS}"
                   aria-describedby="desc-help" placeholder="写明核心结论与下次遇到什么场景时 Agent 应该优先读这条">
            <span id="desc-help" class="field-hint">主知识库记忆会将此摘要同步展示在 <code>MEMORY.md</code> 索引行（单行 ≤ {MAX_DESC_CHARS} 字）。</span>
          </div>
          <div class="form-row-two">
            <div class="field">
              <label for="verified_at">核实日期（可选）</label>
              <input id="verified_at" name="verified_at" type="date" value="{escape(verified_at)}">
              <span class="field-hint">事实最后一次人工或实测核实的日期（YYYY-MM-DD），有助于判断记忆新鲜度。</span>
            </div>
            <div class="field">
              <label for="valid_until">有效期至（可选）</label>
              <input id="valid_until" name="valid_until" type="date" value="{escape(valid_until)}">
              <span class="field-hint">临时授权、限时策略或里程碑快照的截止日；过期后搜索降权并提示复核。</span>
            </div>
          </div>
          <div class="field">
            <label for="body">正文（Markdown） <span class="required">必填</span></label>
            <textarea class="auto-grow-textarea" id="body" name="body" rows="18" maxlength="{MAX_BODY_CHARS}"
                      aria-describedby="body-help"
                      placeholder="记录已验证结论、适用条件、证据路径与失效边界…" required>{escape(body)}</textarea>
            <span id="body-help" class="field-hint">支持标准 Markdown 与库内相对链接；严禁写入明文密码、Cookie 或 Token。</span>
          </div>
          <div class="form-actions">
            <button class="btn btn-primary" type="submit">{submit_txt}</button>
            <a class="btn btn-ghost" href="{escape(cancel_link)}">取消</a>
            <span class="shortcut-hint">快捷键：⌘S / Ctrl+S 保存</span>
          </div>
        </form>
        {footer_meta}
      </div>"""


def render_index_editor_html(token, library_key, markdown_text, revision, cancel_link, index_path):
    """Render direct Markdown editor for MEMORY.md (§0 rules + L0-L4 index)."""
    hidden_inputs = (
        hidden("csrf", token)
        + hidden("library", library_key)
        + hidden("revision", revision)
    )
    return f"""
      <div class="editor-wrap">
        <p class="editor-intro">直接编辑权威入口 <code>MEMORY.md</code>（保存前会自动校验 <code>## L0</code>–<code>## L4</code> 章节结构及所有链接文件是否存在）。</p>
        <form class="edit-form" method="post" action="/save_index#feedback" aria-labelledby="detail-title">
          {hidden_inputs}
          <div class="field">
            <label for="index-body">MEMORY.md 全文（Markdown） <span class="required">必填</span></label>
            <textarea class="auto-grow-textarea" id="index-body" name="body" rows="24" maxlength="{MAX_BODY_CHARS}"
                      aria-describedby="index-help" required autofocus>{escape(markdown_text)}</textarea>
            <span id="index-help" class="field-hint">保持每条索引格式为 <code>- [标题](路径.md) — 一句话核心摘要</code>，严禁写入明文凭据。</span>
          </div>
          <div class="form-actions">
            <button class="btn btn-primary" type="submit">保存 MEMORY.md</button>
            <a class="btn btn-ghost" href="{escape(cancel_link)}">取消</a>
            <span class="shortcut-hint">快捷键：⌘S / Ctrl+S 保存</span>
          </div>
        </form>
        <div class="file-info"><code>{escape(str(index_path))}</code></div>
      </div>"""


def render_delete_dialog_html(token, library_key, scope, target_id, display_path, revision):
    """Render native modal <dialog> for confirming destructive memory deletion."""
    hidden_inputs = (
        hidden("csrf", token)
        + hidden("library", library_key)
        + hidden("scope", scope)
        + hidden("target", target_id)
        + hidden("revision", revision)
    )
    note = (
        "将从主知识库目录与 MEMORY.md 索引中同步移除"
        if scope == "index"
        else "将从个人手记目录 user-managed/ 中移除"
    )
    return f"""
      <dialog class="confirm-dialog" id="delete-dialog" aria-labelledby="delete-dialog-title" closedby="any">
        <h2 id="delete-dialog-title">确认删除这条记忆？</h2>
        <p class="confirm-dialog-desc">{escape(note)} <code>{escape(display_path)}</code>。删除前系统会自动在 <code>.state/trash/</code> 保存一份备份副本。</p>
        <div class="confirm-dialog-actions">
          <button class="btn btn-secondary" type="button" commandfor="delete-dialog" command="close" autofocus>取消</button>
          <form method="post" action="/delete#feedback">
            {hidden_inputs}
            <button class="btn btn-danger" type="submit">确认删除</button>
          </form>
        </div>
      </dialog>"""


def format_inline_md(text, base_rel_dir, root, topics, link_params):
    """Safely render inline code, bold, and links (rewriting internal .md links)."""
    parts = []
    last = 0
    for m in INLINE_TOKEN_RE.finditer(text):
        parts.append(escape(text[last : m.start()]))
        code_tok, bold_tok, _, link_label, link_href = m.groups()
        if code_tok:
            parts.append(f'<code class="inline-code">{escape(code_tok[1:-1])}</code>')
        elif bold_tok:
            parts.append(f"<strong>{escape(bold_tok[2:-2])}</strong>")
        else:
            parts.append(render_md_link(link_label, link_href, base_rel_dir, root, topics, link_params))
        last = m.end()
    parts.append(escape(text[last:]))
    return "".join(parts)


def render_md_link(label, href, base_rel_dir, root, topics, link_params):
    """Rewrite intra-library and intra-skill Markdown links into clickable in-app URLs."""
    safe_label = escape(label)
    if href.startswith(("https://", "http://")):
        return f'<a href="{escape(href)}" target="_blank" rel="noopener noreferrer">{safe_label} ↗</a>'
    parsed = urlsplit(href)
    clean_path = unquote(parsed.path)
    if not parsed.scheme and not clean_path.startswith("/") and clean_path:
        unnorm = root / base_rel_dir / clean_path
        resolved = unnorm.resolve()
        if resolved.suffix.lower() == ".md" and resolved.is_file():
            norm_logical = Path(os.path.normpath(unnorm))
            if norm_logical.is_relative_to(root / "skills"):
                rel_skill = norm_logical.relative_to(root / "skills")
                parts = rel_skill.parts
                if len(parts) >= 2:
                    sk_folder = parts[0]
                    sub_doc = "/".join(parts[1:])
                    owner = link_params.get("skill", "")
                    if owner:
                        import memoryctl

                        resources = memoryctl.resource_catalog(root / "skills").get(owner, [])
                        if sk_folder in resources:
                            sub_doc = "../" + "/".join(parts)
                            sk_folder = owner
                    sk_params = {
                        "library": link_params["library"],
                        "tab": "skills",
                        "skill": sk_folder,
                        "doc": "" if sub_doc == "SKILL.md" else sub_doc,
                    }
                    return f'<a href="{escape(query(**sk_params))}#detail-title">{safe_label}</a>'
            if resolved.is_relative_to(root):
                rel_posix = resolved.relative_to(root).as_posix()
                if rel_posix == "MEMORY.md":
                    app_url = query(library=link_params["library"], view="index") + "#detail-title"
                    return f'<a href="{escape(app_url)}">{safe_label}</a>'
                if rel_posix in topics:
                    mem_params = {k: v for k, v in link_params.items() if k != "tab"}
                    app_url = query(**mem_params, topic=rel_posix) + "#detail-title"
                    return f'<a href="{escape(app_url)}">{safe_label}</a>'
    return f'<span class="local-ref" title="{escape(href)}">{safe_label}</span>'


def split_table_row(line):
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def render_markdown_html(md_text, rel_doc_path, root, topics, link_params, skip_h1=""):
    """Render safe structured HTML from Markdown (headings, lists, tables, code blocks)."""
    _, body = mcp_server.parse_frontmatter(md_text)
    base_dir = Path(rel_doc_path).parent
    lines = body.splitlines()
    blocks = []
    idx = 0
    skipped_first_h1 = False

    while idx < len(lines):
        raw = lines[idx]
        stripped = raw.strip()

        if not stripped:
            idx += 1
            continue

        if stripped.startswith("```"):
            idx += 1
            code_lines = []
            while idx < len(lines) and not lines[idx].strip().startswith("```"):
                code_lines.append(lines[idx])
                idx += 1
            if idx < len(lines):
                idx += 1
            blocks.append(f'<pre class="code-block"><code>{escape(chr(10).join(code_lines))}</code></pre>')
            continue

        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            if 1 <= hashes <= 4 and stripped[hashes : hashes + 1] == " ":
                htext = stripped[hashes + 1 :].strip()
                if hashes == 1 and not skipped_first_h1 and skip_h1 and htext == skip_h1:
                    skipped_first_h1 = True
                    idx += 1
                    continue
                tag = f"h{min(max(hashes, 2), 4)}"
                blocks.append(f"<{tag}>{format_inline_md(htext, base_dir, root, topics, link_params)}</{tag}>")
                idx += 1
                continue

        if "|" in stripped and idx + 1 < len(lines) and TABLE_SEP_RE.match(lines[idx + 1]):
            headers = split_table_row(lines[idx])
            idx += 2
            tbody_rows = []
            while idx < len(lines) and "|" in lines[idx] and lines[idx].strip():
                cells = split_table_row(lines[idx])
                tds = "".join(
                    f"<td>{format_inline_md(c, base_dir, root, topics, link_params)}</td>" for c in cells
                )
                tbody_rows.append(f"<tr>{tds}</tr>")
                idx += 1
            ths = "".join(
                f"<th>{format_inline_md(h, base_dir, root, topics, link_params)}</th>" for h in headers
            )
            blocks.append(
                f'<div class="table-wrap"><table><thead><tr>{ths}</tr></thead>'
                f'<tbody>{"".join(tbody_rows)}</tbody></table></div>'
            )
            continue

        if stripped.startswith(">"):
            q_lines = []
            while idx < len(lines) and lines[idx].strip().startswith(">"):
                q_lines.append(lines[idx].strip().lstrip(">").strip())
                idx += 1
            joined = format_inline_md(" ".join(q_lines), base_dir, root, topics, link_params)
            blocks.append(f"<blockquote><p>{joined}</p></blockquote>")
            continue

        if UNORDERED_ITEM_RE.match(raw):
            items = []
            while idx < len(lines):
                m = UNORDERED_ITEM_RE.match(lines[idx])
                if not m:
                    break
                items.append(f"<li>{format_inline_md(m.group(1), base_dir, root, topics, link_params)}</li>")
                idx += 1
            blocks.append(f'<ul>{"".join(items)}</ul>')
            continue

        if ORDERED_ITEM_RE.match(raw):
            items = []
            while idx < len(lines):
                m = ORDERED_ITEM_RE.match(lines[idx])
                if not m:
                    break
                items.append(f"<li>{format_inline_md(m.group(1), base_dir, root, topics, link_params)}</li>")
                idx += 1
            blocks.append(f'<ol>{"".join(items)}</ol>')
            continue

        para_lines = []
        while idx < len(lines):
            cur = lines[idx]
            s = cur.strip()
            if (
                not s
                or s.startswith(("```", "#", ">"))
                or UNORDERED_ITEM_RE.match(cur)
                or ORDERED_ITEM_RE.match(cur)
                or ("|" in s and idx + 1 < len(lines) and TABLE_SEP_RE.match(lines[idx + 1]))
            ):
                break
            para_lines.append(s)
            idx += 1
        blocks.append(f"<p>{format_inline_md(' '.join(para_lines), base_dir, root, topics, link_params)}</p>")

    return f'<article class="prose">{"".join(blocks)}</article>'


def render_card_time_html(created_at, updated_at, sort_by, now_dt):
    """Render concise relative timestamp badge for sidebar cards."""
    if not updated_at and not created_at:
        return ""
    newly = mcp_server.is_newly_created(created_at, updated_at)
    if sort_by == "created":
        ts_val = created_at or updated_at
        mode = "new"
        suffix = "写入"
    else:
        ts_val = updated_at or created_at
        mode = "new" if newly else "upd"
        suffix = "写入" if newly else "更新"

    rel_txt, _, iso_attr = mcp_server.format_relative_zh(ts_val, now_dt=now_dt)
    try:
        dt_val, _ = mcp_server.parse_timestamp(ts_val)
        is_fresh = (now_dt - dt_val).total_seconds() <= 86400
    except ValueError:
        is_fresh = False
    cls = "entry-time entry-time-fresh" if is_fresh else "entry-time"
    return (
        f'<time class="{cls}" datetime="{escape(iso_attr)}" '
        f'data-ts="{escape(iso_attr)}" data-mode="{mode}">{escape(rel_txt)}{suffix}</time>'
    )


VERSION_REASONS = {"update": "编辑前备份", "layer-change": "换层前备份", "archive": "归档前备份"}


def render_topic_versions_html(token, library_key, slug, versions, readonly):
    """List previous versions of one entry, with a restore button per version.

    The agent tools memory_history / memory_restore read the same backups, so
    people and agents recover an entry the same way.
    """
    if not versions:
        return ""
    rows = []
    for row in versions:
        saved = str(row.get("saved_at") or "").replace("T", " ")
        reason = VERSION_REASONS.get(row.get("reason"), "备份")
        chars = row.get("chars") or 0
        action = ""
        if not readonly:
            action = f"""
              <form method="post" action="/restore_version#feedback" aria-label="恢复 {escape(saved)} 的版本">
                {hidden("csrf", token)}{hidden("library", library_key)}
                {hidden("version", str(row.get("version") or ""))}{hidden("slug", slug)}
                <button class="btn btn-ghost btn-compact" type="submit">恢复此版本</button>
              </form>"""
        rows.append(
            f'<li><span class="version-when">{escape(saved)}</span>'
            f'<span class="meta-chip">{escape(reason)}</span>'
            f'<span class="version-size">{chars} 字</span>{action}</li>'
        )
    return f"""
      <section class="detail-block" aria-labelledby="versions-title">
        <h2 class="block-title" id="versions-title">历史版本</h2>
        <p class="block-hint">每次编辑、换层或归档前都会在本机备份一份。恢复会先备份当前版本，因此两个方向都不会丢内容。</p>
        <ul class="version-list">{"".join(rows)}</ul>
      </section>"""


def render_topic_links_html(library_key, links, backlinks):
    """Render outgoing references and incoming backlinks for one memory entry."""
    if not links and not backlinks:
        return ""

    def _pill_list(items):
        return "".join(
            f'<a class="link-pill" href="{escape(query(library=library_key, topic=item["ref"]) + "#detail-title")}">'
            f'<span class="link-pill-title">{escape(item.get("title") or item["ref"])}</span>'
            f'<code>{escape(item["ref"])}</code></a>'
            for item in items
            if item.get("ref")
        )

    blocks = []
    if links:
        blocks.append(
            f'<div class="link-group"><span class="link-group-label">引用了 ({len(links)})</span>'
            f'<div class="link-pills">{_pill_list(links)}</div></div>'
        )
    if backlinks:
        blocks.append(
            f'<div class="link-group"><span class="link-group-label">被引用 ({len(backlinks)})</span>'
            f'<div class="link-pills">{_pill_list(backlinks)}</div></div>'
        )
    return f"""
      <section class="detail-block" aria-labelledby="links-title">
        <h2 class="block-title" id="links-title">关联记忆</h2>
        <div class="link-groups">{"".join(blocks)}</div>
      </section>"""


def render_detail_timestamps_html(path_str, created_at, updated_at, now_dt, status_note=""):
    """Render semantic <time> metadata line showing both update and creation times."""
    parts = [f"<code>{escape(path_str)}</code>"]
    if status_note:
        parts.append(f"<span>{escape(status_note)}</span>")
    if updated_at or created_at:
        newly = mcp_server.is_newly_created(created_at, updated_at)
        upd_rel, upd_full, upd_iso = mcp_server.format_relative_zh(updated_at or created_at, now_dt=now_dt)
        if newly:
            parts.append(
                f'<span class="detail-time-item"><strong>写入时间：</strong>'
                f'<time datetime="{escape(upd_iso)}" data-ts="{escape(upd_iso)}" data-mode="bare">{escape(upd_rel)}</time>'
                f"（{escape(upd_full)}）</span>"
            )
        else:
            cre_rel, cre_full, cre_iso = mcp_server.format_relative_zh(created_at, now_dt=now_dt)
            parts.append(
                f'<span class="detail-time-item"><strong>最近更新：</strong>'
                f'<time datetime="{escape(upd_iso)}" data-ts="{escape(upd_iso)}" data-mode="bare">{escape(upd_rel)}</time>'
                f"（{escape(upd_full)}）</span>"
            )
            parts.append(
                f'<span class="detail-time-item"><strong>首次写入：</strong>'
                f'<time datetime="{escape(cre_iso)}" data-ts="{escape(cre_iso)}" data-mode="bare">{escape(cre_rel)}</time>'
                f"（{escape(cre_full)}）</span>"
            )
    return f'<p class="detail-path">{" · ".join(parts)}</p>'


def entry_link(item, href, is_user, selected, sort_by="updated", now_dt=None):
    now_dt = now_dt or datetime.now().astimezone()
    current = CUR_ATTR if selected else ""
    badge = f'<span class="layer-badge layer-{item.layer}">{item.layer}</span>'
    user_tag = '<span class="entry-tag-user">手记</span>' if is_user else ""
    time_html = render_card_time_html(item.created_at, item.updated_at, sort_by, now_dt)
    summary = f'<p class="entry-desc">{escape(item.description)}</p>' if item.description else ""
    return f"""
      <li><a class="entry-link" href="{escape(href)}#detail-title"{current}>
        <div class="entry-card-head">
          {badge}
          <strong class="entry-title">{escape(item.title)}</strong>
          {user_tag}
          {time_html}
        </div>
        {summary}
      </a></li>"""


def skill_entry_link(sk, href, selected):
    current = CUR_ATTR if selected else ""
    cat_label, badge_layer, _ = SKILL_CATS.get(sk["category"], ("通用", "L2", ""))
    badge = f'<span class="layer-badge layer-{badge_layer}">{escape(cat_label)}</span>'
    off_tag = "" if sk["enabled"] else '<span class="entry-tag-off">已停用</span>'
    summary = f'<p class="entry-desc">{escape(sk["description"])}</p>' if sk["description"] else ""
    return f"""
      <li><a class="entry-link" href="{escape(href)}#detail-title"{current}>
        <div class="entry-card-head">
          {badge}
          <strong class="entry-title">{escape(sk["name"])}</strong>
          {off_tag}
        </div>
        {summary}
      </a></li>"""


def library_nav_html(libraries, current_key):
    if len(libraries) <= 1:
        return ""
    links = []
    for key, library in libraries.items():
        current = CUR_ATTR if key == current_key else ""
        suffix = ""
        if library.role == "shared":
            suffix = " · 只读" if library.is_readonly else " · 共写"
        links.append(
            f'<li><a href="{escape(query(library=key))}"{current}>{escape(library.label)}{suffix}</a></li>'
        )
    return f'<nav aria-label="切换记忆库"><ul class="lib-pills">{"".join(links)}</ul></nav>'


def render_library_sync_bar(library, key, token, tab=""):
    """Build compact status summary chips and quick sync action for the collapsible ops strip."""
    sync_cfg = library.sync_config or {}
    remote = sync_cfg.get("remote") or "origin"
    branch = sync_cfg.get("branch") or ""
    st = sync_git.git_status(library.root, remote=remote, branch=branch)
    async_status = library.async_sync_status() if hasattr(library, "async_sync_status") else {}
    role_badge = "个人主库" if library.role == "personal" else ("共享只读" if library.is_readonly else "团队共写")

    parts = [f'<span class="meta-chip">{role_badge}</span>']
    if sync_cfg.get("auto_push") and library.mode == "rw":
        async_state = str(async_status.get("status") or "").casefold()
        if async_state in {"pending", "running"}:
            parts.append('<span class="chip-ok">⟳ 后台同步中</span>')
        elif async_state == "conflict":
            parts.append('<span class="chip-warn">⚠ 后台同步有冲突</span>')
        elif async_state == "error":
            parts.append('<span class="chip-warn">! 后台同步失败</span>')
        else:
            parts.append('<span class="meta-chip">后台同步已开启</span>')
    if st.get("is_repo"):
        br_txt = st.get("branch") or "main"
        parts.append(f'<span class="meta-chip">Git: {escape(br_txt)}</span>')
        if st.get("remote_url"):
            parts.append('<span class="chip-ok">✓ 远端已配</span>')
            if st.get("behind"):
                parts.append(f'<span class="chip-warn">↓ 落后 {st["behind"]}</span>')
            if st.get("ahead"):
                parts.append(f'<span class="chip-warn">↑ 待推送 {st["ahead"]}</span>')
        else:
            parts.append('<span class="chip-off">○ 未绑远端</span>')
        if st.get("dirty_count"):
            parts.append(f'<span class="chip-warn">● {st["dirty_count"]} 未提交</span>')
    else:
        parts.append('<span class="chip-off">○ 未启用 Git</span>')

    actions = []
    conflicts = st.get("conflicts") or []
    if conflicts:
        conflict_href = query(library=key, view="conflicts") + "#detail-title"
        actions.append(
            f'<a class="btn btn-warn btn-compact" href="{escape(conflict_href)}">⚠ 处理 {len(conflicts)} 个冲突</a>'
        )
    if st.get("is_repo") and st.get("remote_url"):
        sync_label = "拉取更新" if library.is_readonly else "Git 同步"
        actions.append(
            f'<form method="post" action="/sync#feedback">'
            f'{hidden("csrf", token)}{hidden("library", key)}{hidden("tab", tab)}'
            f'<button class="btn btn-secondary btn-compact" type="submit">{sync_label}</button>'
            f"</form>"
        )
    hub_href = query(library=key, view="sync_hub") + "#detail-title"
    actions.append(f'<a class="btn btn-ghost btn-compact" href="{escape(hub_href)}">共享与同步</a>')
    return {
        "chips": "".join(parts),
        "actions": "".join(actions),
        "conflicts": conflicts,
        "status": st,
    }


def format_comp_chip(label, state, suffix=""):
    text = f"{label}{suffix}"
    if state == "managed":
        return f'<span class="chip-ok">✓ {escape(text)}</span>'
    if state in {"drifted", "modified", "conflict", "symlink"}:
        return f'<span class="chip-warn">! {escape(text)}待同步</span>'
    return f'<span class="chip-off">○ {escape(label)}未配</span>'


def render_agent_panel(token, key, tab="", agent_home=None, library=None, flat=False):
    """Render compact collapsible ops bar (sync + agent summary) with expandable client cards."""
    sync_meta = render_library_sync_bar(library, key, token, tab=tab) if library is not None else None
    try:
        import memoryctl

        specs = memoryctl.agent_specs(home=agent_home)
        cards = []
        managed_count = 0
        drifted_count = 0
        for aid, spec in specs.items():
            info = memoryctl.inspect_agent(aid, spec)
            state = info["state"]
            if state == "managed":
                managed_count += 1
                pill_cls = "status-pill status-pill-managed"
                pill_txt = "● 已配置"
                btn_html = f"""
                  <form method="post" action="/agent#feedback">
                    {hidden("csrf", token)}{hidden("library", key)}{hidden("tab", tab)}
                    {hidden("target", aid)}{hidden("op", "disconnect")}
                    <button class="btn btn-danger-ghost btn-compact" type="submit">断开</button>
                  </form>"""
            elif state in {"drifted", "partial"}:
                drifted_count += 1
                pill_cls = "status-pill status-pill-drifted"
                pill_txt = "▲ 待同步"
                btn_html = f"""
                  <form method="post" action="/agent#feedback">
                    {hidden("csrf", token)}{hidden("library", key)}{hidden("tab", tab)}
                    {hidden("target", aid)}{hidden("op", "connect")}
                    <button class="btn btn-warn btn-compact" type="submit">同步</button>
                  </form>
                  <form method="post" action="/agent#feedback">
                    {hidden("csrf", token)}{hidden("library", key)}{hidden("tab", tab)}
                    {hidden("target", aid)}{hidden("op", "disconnect")}
                    <button class="btn btn-danger-ghost btn-compact" type="submit">断开</button>
                  </form>"""
            else:
                pill_cls = "status-pill"
                pill_txt = "○ 未接入"
                btn_html = f"""
                  <form method="post" action="/agent#feedback">
                    {hidden("csrf", token)}{hidden("library", key)}{hidden("tab", tab)}
                    {hidden("target", aid)}{hidden("op", "connect")}
                    <button class="btn btn-primary btn-compact" type="submit">接入</button>
                  </form>"""

            if aid in memory_import.CLIENTS and key == "primary":
                inbox_link = escape(query(view="memory_import", client=aid) + "#detail-title")
                label = "导入记忆"
                if aid in memory_import.AUTOMATIC_CLIENTS:
                    btn_html += render_native_import_button(token, aid)
                    label = "待确认记忆"
                btn_html += f'<a class="btn btn-ghost btn-compact" href="{inbox_link}">{label}</a>'
            comp = info["components"]
            sk_comp = comp.get("skill", {})
            sk_suffix = f" ({sk_comp.get('linked', 0)}/{sk_comp.get('total', 0)})" if "total" in sk_comp else ""
            chips = "".join(
                [
                    format_comp_chip("指令规则", comp["instructions"]["state"]) if "instructions" in comp else "",
                    format_comp_chip("自动读写 Hooks", comp["hooks"]["state"]) if "hooks" in comp else "",
                    format_comp_chip("MCP 服务", comp["mcp"]["state"]),
                    format_comp_chip("Skills", sk_comp["state"], sk_suffix) if sk_comp else "",
                ]
            )
            issue_html = (
                f'<p class="agent-issue">{escape("；".join(info["issues"]))}</p>'
                if info["issues"]
                else ""
            )
            cards.append(
                f"""
            <li class="agent-item">
              <div class="agent-info">
                <div class="agent-name-row">
                  <strong>{escape(info["name"])}</strong>
                  <span class="{pill_cls}">{pill_txt}</span>
                </div>
                <div class="check-chips">{chips}</div>
                {issue_html}
              </div>
              <div class="agent-btns">{btn_html}</div>
            </li>"""
            )

        connect_all_btn = ""
        if managed_count < len(specs):
            connect_all_btn = f"""
              <form method="post" action="/agent#feedback">
                {hidden("csrf", token)}{hidden("library", key)}{hidden("tab", tab)}
                {hidden("target", "all")}{hidden("op", "connect")}
                <button class="btn btn-primary btn-compact" type="submit">一键同步全部</button>
              </form>"""

        rebuild_btn = ""
        if library is not None and not library.is_readonly:
            rebuild_btn = f"""
              <form method="post" action="/rebuild_index#feedback">
                {hidden("csrf", token)}{hidden("library", key)}{hidden("tab", tab)}
                <button class="btn btn-ghost btn-compact" type="submit">重建索引</button>
              </form>"""

        stray_list = memoryctl.scan_stray_agent_memories(home=agent_home)
        stray_alert = (
            f'<p class="agent-issue">发现未归集的隐藏目录散落记忆（请合并后清理）：{escape(", ".join(stray_list))}</p>'
            if stray_list
            else ""
        )
        agent_chip = (
            f'<span class="chip-warn">▲ 客户端待同步 ({drifted_count})</span>'
            if drifted_count
            else f'<span class="chip-ok">✓ 客户端 {managed_count}/{len(specs)} 已配</span>'
        )
        sync_chips = sync_meta["chips"] if sync_meta else ""
        sync_actions = sync_meta["actions"] if sync_meta else ""
        conflict_banner = ""
        if sync_meta and sync_meta["conflicts"]:
            c_href = query(library=key, view="conflicts") + "#detail-title"
            conflict_banner = (
                f'<p class="agent-issue">发现 {len(sync_meta["conflicts"])} 个同步冲突副本，合并并清理后方可推送：'
                f'{escape("、".join(sync_meta["conflicts"]))} · '
                f'<a href="{escape(c_href)}">进入可视化对比合并 →</a></p>'
            )

        actions_html = f"""
                <div class="agents-global-actions" role="group" aria-label="客户端管理操作">
                  {connect_all_btn}
                  {rebuild_btn}
                  <form method="post" action="/doctor#feedback">
                    {hidden("csrf", token)}{hidden("library", key)}{hidden("tab", tab)}
                    <button class="btn btn-secondary btn-compact" type="submit">自检修复</button>
                  </form>
                </div>"""
        clients_html = f'<ul class="agents-cards" aria-label="跨 Agent 统一接入控制">{"".join(cards)}</ul>'
        if flat:
            return f"""
        <div class="ops-flat">
          <div class="agents-ribbon-top">
            <div class="check-chips">{sync_chips}{agent_chip}</div>
            {actions_html}
          </div>
          {conflict_banner}
          {stray_alert}
          {clients_html}
        </div>"""
        return f"""
        <div class="ops-strip">
          <div class="ops-strip-top">
            <div class="ops-strip-left">
              <div class="check-chips">{sync_chips}{agent_chip}</div>
            </div>
            <div class="ops-strip-right">
              {sync_actions}
            </div>
          </div>
          {conflict_banner}
          {stray_alert}
          <details class="ops-drawer">
            <summary class="ops-drawer-summary">
              <span>客户端接入与运维面板（{managed_count}/{len(specs)} 客户端已配置）</span>
              <span class="ops-drawer-hint">展开管理 ▾</span>
            </summary>
            <div class="ops-drawer-body">
              <div class="agents-ribbon-top">{actions_html}</div>
              {clients_html}
            </div>
          </details>
        </div>"""
    except OSError:
        return (
            '<div class="agents-ribbon">'
            '<p class="agents-subtitle">当前进程受沙盒限制无法直接检测客户端目录；在本地终端运行 python3 manager/manager.py 即可启用客户端接入管理。</p>'
            "</div>"
        )


def render_native_import_button(token, client):
    return f"""
      <form method="post" action="/memory_import">
        {hidden("csrf", token)}{hidden("op", "import")}{hidden("client", client)}
        <button class="btn btn-ghost btn-compact" type="submit">一键导入记忆</button>
      </form>"""


IMPORT_INSTRUCTIONS = {
    "claude-desktop": "可在 Claude Desktop 中复制要保留的记忆或项目说明，或选择已导出的 Markdown 文件。",
    "cursor": "从 Cursor 的记忆管理页复制已保存的记忆，或选择希望导入的 .mdc 规则。",
    "workbuddy": "可在 WorkBuddy「设置 → 记忆与进化」复制要保留的条目，或选择工作空间的 MEMORY.md。",
    "deepseek": "选择已有的记忆文件，或粘贴想保留的内容。",
}


def render_memory_import(token, library, client, selected, q, draft):
    try:
        entries = memory_import.pending(library.root, client)
        item = memory_import.read_candidate(library.root, client, selected) if selected else None
    except (OSError, ValueError) as exc:
        return f'<p class="empty">{escape(str(exc))}</p>'

    form = ""
    if item:
        title = draft.get("title", item["title"])
        description = draft.get("description", "")
        body = draft.get("body", item["body"])
        origin = " · ".join(part for part in (item["source"], item.get("source_detail"), item["source_date"], item["thread_id"]) if part)
        form = f"""
          <form class="edit-form" method="post" action="/memory_import">
            {hidden("csrf", token)}{hidden("op", "approve")}{hidden("client", client)}{hidden("import_id", item["id"])}
            {hidden("revision", memory_import.revision(item))}
            <h2>确认并归层</h2>
            <p>{escape(origin)}</p>
            <label>标题 <input type="text" name="title" maxlength="{MAX_TITLE_CHARS}" required value="{escape(title)}"></label>
            <label>检索摘要 <input type="text" name="description" maxlength="{MAX_DESC_CHARS}" value="{escape(description)}"></label>
            <label>正文 <textarea name="body" rows="14" required>{escape(body)}</textarea></label>
            {render_layer_radios(draft.get("layer", ""))}
            <p>确认后会写入个人记忆库。若已启用个人库 Git 同步，这些条目会随个人库同步。</p>
            <button class="btn btn-primary" type="submit">确认归入所选层</button>
          </form>
        """
    search = q.casefold()
    rows = []
    for item in entries:
        searchable = " ".join((item["title"], item["body"], item["thread_id"])).casefold()
        if search and search not in searchable:
            continue
        href = escape(query(view="memory_import", client=client, topic=item["id"], q=q) + "#detail-title")
        label = escape(item["source_date"][:10] or "日期未知")
        rows.append(f'<li><a href="{href}">{escape(item["title"])}</a> <small>{label}</small></li>')
    listing = f'<ul class="entry-list">{"".join(rows)}</ul>' if rows else '<p class="empty">没有找到待确认记忆。</p>'
    tabs = "".join(
        f'<a class="pill-tab" href="{escape(query(view="memory_import", client=aid))}"'
        f'{CUR_ATTR if aid == client else ""}>{escape(name)}</a>'
        for aid, name in memory_import.CLIENTS.items()
    )
    automatic = render_native_import_button(token, client) if client in memory_import.AUTOMATIC_CLIENTS else ""
    guidance = IMPORT_INSTRUCTIONS.get(client, "也可选择记忆文件或直接粘贴原文。")
    return f"""
      <section class="import-review">
        <nav class="sort-switch" aria-label="记忆来源">{tabs}</nav>
        <p>待确认的内容只保存在本机。请核对原文，保留值得长期使用的部分后再归层。</p>
        {automatic}
        <details {"open" if not automatic and not item else ""}>
          <summary>从文件或文本导入</summary>
          <p>{escape(guidance)}</p>
          <form class="edit-form" method="post" action="/memory_import">
            {hidden("csrf", token)}{hidden("op", "text")}{hidden("client", client)}
            <label>标题 <input type="text" name="title" maxlength="{MAX_TITLE_CHARS}"></label>
            <label hidden data-import-file-picker>选择记忆文件 <input type="file" accept=".md,.mdc,.txt,text/plain,text/markdown" data-import-file></label>
            <p role="status" data-import-file-status></p>
            <label>原文 <textarea name="body" rows="7" maxlength="{memory_import.MAX_TEXT}" required></textarea></label>
            <button class="btn btn-ghost" type="submit">加入待确认</button>
          </form>
        </details>
        {form}
        <h2>待确认 · {len(entries)} 条</h2>
        <form action="/" method="get">
          {hidden("view", "memory_import")}{hidden("client", client)}
          <label>搜索待确认记忆 <input type="search" name="q" value="{escape(q)}"></label>
          <button class="btn btn-ghost btn-compact" type="submit">搜索</button>
        </form>
        {listing}
      </section>
    """


def render_share_dialog_html(token, key, topic_entry, writable_shared_libs):
    """Render native modal dialog to promote/share a personal topic into a writable shared library."""
    if not writable_shared_libs:
        return ""
    lib_options = []
    for lk, lib in writable_shared_libs:
        layers_str = "/".join(sorted(lib.allowed_layers))
        lib_options.append(
            f'<option value="{escape(lk)}">{escape(lib.label)}（允许：{escape(layers_str)}）</option>'
        )
    # Collect union of allowed layers across writable shared libs, preferring topic's own layer if allowed.
    default_lib = writable_shared_libs[0][1]
    default_layer = topic_entry.layer if topic_entry.layer in default_lib.allowed_layers else sorted(default_lib.allowed_layers)[0]
    layer_options = []
    for lyr in ALL_LAYERS:
        sel = " selected" if lyr == default_layer else ""
        lname = LAYERS[lyr][0]
        layer_options.append(f'<option value="{lyr}"{sel}>{lyr} · {escape(lname)}</option>')

    return f"""
    <dialog id="share-dialog" class="confirm-dialog share-dialog" closedby="any" aria-labelledby="share-dialog-title">
      <div class="dialog-body">
        <h2 id="share-dialog-title">分享个人记忆到共享库</h2>
        <p>将当前条目 <strong>{escape(topic_entry.title)}</strong>（<code>{escape(topic_entry.slug)}.md</code>）复制到选定的团队共享库。</p>
        <form action="/share_topic#feedback" method="post" class="share-form">
          {hidden("csrf", token)}
          {hidden("library", key)}
          {hidden("topic", topic_entry.path)}
          <div class="field-group">
            <label class="field-label" for="share-target-lib">目标共享库</label>
            <select id="share-target-lib" name="target_library" required>{"".join(lib_options)}</select>
          </div>
          <div class="field-group">
            <label class="field-label" for="share-target-layer">写入层级</label>
            <select id="share-target-layer" name="target_layer" required>{"".join(layer_options)}</select>
            <p class="field-hint">共享库默认只允许 L1 / L3 / L4；如原条目为 L0/L2，建议改选 L1 方法或 L3 项目后分享。</p>
          </div>
          <label class="checkbox-row">
            <input type="checkbox" name="overwrite" value="1">
            <span>若目标库已存在同名条目则覆盖更新（旧版自动进本机回收站）</span>
          </label>
          <div class="dialog-actions">
            <button class="btn btn-ghost" type="button" commandfor="share-dialog" command="close">取消</button>
            <button class="btn btn-primary" type="submit">确认分享</button>
          </div>
        </form>
      </div>
    </dialog>"""


def render_layer_checkboxes(selected_layers, prefix="layer_"):
    boxes = []
    selected = set(selected_layers)
    for lyr in ALL_LAYERS:
        chk = " checked" if lyr in selected else ""
        boxes.append(
            f'<label class="layer-check-chip">'
            f'<input type="checkbox" name="{prefix}{lyr}" value="1"{chk}>'
            f'<span>{lyr} {LAYER_SHORT[lyr]}</span>'
            f"</label>"
        )
    return f'<div class="layer-check-row">{"".join(boxes)}</div>'


def render_sync_hub_html(token, key, libraries, agent_home=None):
    """Render graphical Multi-Library Mount & Git Sync Hub management view."""
    import memoryctl

    all_libs = memoryctl.mount_list(home=agent_home)
    personal_info = next((item for item in all_libs if item["id"] == "personal"), None)
    personal_sync = (personal_info or {}).get("sync") or {}
    personal_git = (personal_info or {}).get("git") or {}
    personal_remote = display_remote_url(
        personal_sync.get("remote_url") or personal_git.get("remote_url") or ""
    )
    personal_branch = personal_sync.get("branch") or personal_git.get("branch") or "main"
    personal_auto_commit = personal_sync.get("auto_commit", True)
    personal_auto_push = personal_sync.get("auto_push", False)

    cards = []
    for item in all_libs:
        # mount_list also keeps configuration-level diagnostics that do not have a
        # usable mount id (for example, a malformed top-level mounts.json file).
        # Render those as a normal unavailable card instead of letting html.escape(None)
        # abort the whole sync hub.
        lid = item.get("id") or "unavailable"
        nav_key = "primary" if lid == "personal" else lid
        if not item.get("available", True):
            issue = item.get("issue") or {}
            reason = issue.get("message") or "记忆库目录不可用。"
            label = item.get("label") or lid
            status = issue.get("code") or "unavailable"
            cards.append(
                f"""
          <article class="hub-card hub-card-unavailable">
            <div class="hub-card-head">
              <div>
                <strong>{escape(label)}</strong>
                <span class="hub-card-id"><code>{escape(lid)}</code></span>
              </div>
              <div class="hub-card-actions"><span class="chip-warn">! 不可用</span></div>
            </div>
            <div class="check-chips"><span class="chip-warn">配置问题：{escape(status)}</span></div>
            <p class="hub-card-path">目录：<code>{escape(str(item.get("path") or ""))}</code></p>
            <p class="agent-issue">{escape(reason)}</p>
          </article>"""
            )
            continue
        git = item.get("git") or {}
        sync_cfg = item.get("sync") or {}
        is_personal = lid == "personal"
        is_ro = item["mode"] == "ro"
        role_txt = "个人主库 · 读写" if is_personal else ("团队共享 · 只读" if is_ro else "团队共享 · 共写")
        status_chips = [
            f'<span class="meta-chip">{role_txt}</span>',
            f'<span class="meta-chip">层级: {escape("/".join(item["layers"]))}</span>',
        ]
        if not item.get("enabled", True):
            status_chips.append('<span class="chip-off">已停用</span>')
        if git.get("is_repo"):
            status_chips.append(f'<span class="meta-chip">Git: {escape(git.get("branch") or "main")}</span>')
            if git.get("remote_url"):
                status_chips.append('<span class="chip-ok">✓ 远端已绑</span>')
            else:
                status_chips.append('<span class="chip-off">○ 仅本地 Git</span>')
            if git.get("dirty_count"):
                status_chips.append(f'<span class="chip-warn">● {git["dirty_count"]} 未提交</span>')
            if git.get("ahead"):
                status_chips.append(f'<span class="chip-warn">↑ {git["ahead"]}</span>')
            if git.get("behind"):
                status_chips.append(f'<span class="chip-warn">↓ {git["behind"]}</span>')
            if git.get("conflicts"):
                status_chips.append(f'<span class="chip-warn">⚠ {len(git["conflicts"])} 冲突</span>')
        else:
            status_chips.append('<span class="chip-off">○ 未初始化 Git</span>')

        remote_line = (
            f'<p class="hub-card-path">远端：<code>{escape(display_remote_url(git.get("remote_url") or sync_cfg.get("remote_url") or ""))}</code></p>'
            if (git.get("remote_url") or sync_cfg.get("remote_url"))
            else ""
        )

        act_btns = []
        if nav_key in libraries:
            act_btns.append(f'<a class="btn btn-ghost btn-compact" href="{escape(query(library=nav_key))}">浏览条目</a>')
        if git.get("conflicts") and nav_key in libraries:
            act_btns.append(
                f'<a class="btn btn-warn btn-compact" href="{escape(query(library=nav_key, view="conflicts"))}#detail-title">处理冲突</a>'
            )
        if git.get("is_repo") and git.get("remote_url") and nav_key in libraries:
            test_label = {
                "github-browser": "连接 GitHub（仅验证）",
                "https-credential": "测试读取连接（钥匙串凭据）",
                "ssh": "测试读取连接（SSH 密钥）",
            }.get(sync_git.auth_method(git["remote_url"]), "测试读取连接")
            act_btns.append(
                f'<form method="post" action="/sync_test#feedback">'
                f'{hidden("csrf", token)}{hidden("library", nav_key)}'
                f'<button class="btn btn-ghost btn-compact" type="submit">{test_label}</button></form>'
            )
            s_lbl = "拉取更新" if is_ro else "立即同步"
            act_btns.append(
                f'<form method="post" action="/sync#feedback">'
                f'{hidden("csrf", token)}{hidden("library", nav_key)}{hidden("return_view", "sync_hub")}'
                f'<button class="btn btn-secondary btn-compact" type="submit">{s_lbl}</button>'
                f"</form>"
            )
        if not is_ro and nav_key in libraries:
            act_btns.append(
                f'<form method="post" action="/rebuild_index#feedback">'
                f'{hidden("csrf", token)}{hidden("library", nav_key)}{hidden("return_view", "sync_hub")}'
                f'<button class="btn btn-ghost btn-compact" type="submit">重建索引</button>'
                f"</form>"
            )

        manage_drawer = ""
        if not is_personal:
            cur_remote = display_remote_url(sync_cfg.get("remote_url") or git.get("remote_url") or "")
            cur_branch = sync_cfg.get("branch") or git.get("branch") or "main"
            ro_sel = " selected" if is_ro else ""
            rw_sel = " selected" if not is_ro else ""
            en_chk = " checked" if item.get("enabled", True) else ""
            ac_chk = " checked" if sync_cfg.get("auto_commit", True) else ""
            ap_chk = " checked" if sync_cfg.get("auto_push", False) else ""
            manage_drawer = f"""
            <details class="mount-edit-drawer">
              <summary>修改挂载与同步设置 / 解除挂载 ▾</summary>
              <form method="post" action="/mount_update#feedback" class="hub-inline-form">
                {hidden("csrf", token)}{hidden("library", key)}{hidden("mount_id", lid)}
                <div class="form-row-two">
                  <div class="field-group">
                    <label class="field-label">显示名称</label>
                    <input type="text" name="label" value="{escape(item['label'])}" required maxlength="60">
                  </div>
                  <div class="field-group">
                    <label class="field-label">协作权限</label>
                    <select name="mode">
                      <option value="ro"{ro_sel}>只读订阅（ro）</option>
                      <option value="rw"{rw_sel}>团队共写（rw）</option>
                    </select>
                  </div>
                </div>
                <div class="form-row-two">
                  <div class="field-group">
                    <label class="field-label">Git 远端地址（可选）</label>
                    <input type="text" name="remote_url" value="{escape(cur_remote)}" placeholder="git@github.com:org/repo.git 或 https://...">
                  </div>
                  <div class="field-group">
                    <label class="field-label">分支</label>
                    <input type="text" name="branch" value="{escape(cur_branch)}" placeholder="main">
                  </div>
                </div>
                <div class="field-group">
                  <label class="field-label">允许生效的记忆层级</label>
                  {render_layer_checkboxes(item["layers"])}
                </div>
                <div class="checkbox-inline-group">
                  <label class="checkbox-row"><input type="checkbox" name="enabled" value="1"{en_chk}><span>启用挂载</span></label>
                  <label class="checkbox-row"><input type="checkbox" name="auto_commit" value="1"{ac_chk}><span>写入时自动本地 Git 提交</span></label>
                  <label class="checkbox-row"><input type="checkbox" name="auto_push" value="1"{ap_chk}><span>写入后防抖异步 Git 同步</span></label>
                </div>
                <div class="form-actions">
                  <button class="btn btn-primary btn-compact" type="submit">保存设置</button>
                </div>
              </form>
              <form method="post" action="/mount_remove#feedback" class="mount-remove-form">
                {hidden("csrf", token)}{hidden("library", key)}{hidden("mount_id", lid)}
                <button class="btn btn-danger-ghost btn-compact" type="submit">解除挂载（保留本地目录文件）</button>
              </form>
            </details>"""

        cards.append(
            f"""
          <article class="hub-card">
            <div class="hub-card-head">
              <div>
                <strong>{escape(item["label"])}</strong>
                <span class="hub-card-id"><code>{escape(lid)}</code></span>
              </div>
              <div class="hub-card-actions">{"".join(act_btns)}</div>
            </div>
            <div class="check-chips">{"".join(status_chips)}</div>
            <p class="hub-card-path">目录：<code>{escape(item["path"])}</code></p>
            {remote_line}
            {manage_drawer}
          </article>"""
        )

    p_ac_chk = " checked" if personal_auto_commit else ""
    p_ap_chk = " checked" if personal_auto_push else ""
    return f"""
    <div class="sync-hub">
      <section class="hub-section">
        <h2 class="hub-section-title">已挂载记忆库（{len(all_libs)}）</h2>
        <p class="hub-section-desc">MCP 会跨所有已启用的库联合检索，写入默认只落个人主库；共享库默认隔离 L0 个人红线与 L2 个人偏好。</p>
        <div class="hub-card-list">{"".join(cards)}</div>
      </section>

      <section class="hub-section">
        <h2 class="hub-section-title">个人主库跨设备同步（GitHub / Gitee / 自建 Git）</h2>
        <p class="hub-section-desc">粘贴你有写入权限的仓库地址。GitHub HTTPS 仓库首次连接时会打开浏览器授权；Gitee、GitLab 和自建平台的 HTTPS 地址使用钥匙串中保存的用户名与访问令牌（可写成 https://用户名@主机/...）；SSH 地址使用本机 SSH 密钥。同步会上传本地记忆和提交历史。</p>
        <form method="post" action="/sync_setup#feedback" class="hub-form">
          {hidden("csrf", token)}{hidden("library", key)}{hidden("target_library", "personal")}
          <div class="form-row-two">
            <div class="field-group">
              <label class="field-label" for="p-remote-url">Git 仓库地址</label>
              <input id="p-remote-url" type="text" name="remote_url" value="{escape(personal_remote)}" required
                     placeholder="https://github.com/账号/个人记忆库">
            </div>
            <div class="field-group">
              <label class="field-label" for="p-branch">分支</label>
              <input id="p-branch" type="text" name="branch" value="{escape(personal_branch)}" placeholder="main">
            </div>
          </div>
          <label class="checkbox-row">
            <input type="checkbox" name="auto_commit" value="1"{p_ac_chk}>
            <span>Agent 或网页每次修改后自动在本地 Git 提交快照</span>
          </label>
          <label class="checkbox-row">
            <input type="checkbox" name="auto_push" value="1"{p_ap_chk}>
            <span>写入后防抖异步拉取并推送（仅读写库，约 2 秒合并一批写入）</span>
          </label>
          <div class="form-actions">
            <button class="btn btn-primary" type="submit" name="after_save" value="sync">连接并同步</button>
            <button class="btn btn-ghost" type="submit">仅保存地址</button>
          </div>
        </form>
      </section>

      <section class="hub-section">
        <h2 class="hub-section-title">挂载新的共享记忆库</h2>
        <p class="hub-section-desc">粘贴团队仓库链接并选择权限即可加入。私有 GitHub 仓库会打开浏览器授权；其他平台的 HTTPS 地址读取钥匙串中的用户名与访问令牌，SSH 地址使用本机密钥。共享库使用独立目录，个人库保存在原处。</p>
        <form method="post" action="/mount_add#feedback" class="hub-form">
          {hidden("csrf", token)}{hidden("library", key)}
          <div class="field-group">
            <label class="field-label" for="m-git">团队仓库地址（HTTPS 或 SSH）</label>
            <input id="m-git" type="text" name="git_url" placeholder="https://github.com/团队/记忆库 或 https://用户名@gitee.com/团队/记忆库">
          </div>
          <div class="field-group">
            <label class="field-label" for="m-mode">权限模式</label>
            <select id="m-mode" name="mode">
              <option value="ro">只读订阅</option><option value="rw">团队共写</option>
            </select>
          </div>
          <details><summary>库名称、目录与分支</summary>
          <div class="form-row-two">
            <div class="field-group">
              <label class="field-label" for="m-id">库标识 ID（英文短名）</label>
              <input id="m-id" type="text" name="mount_id" pattern="[a-z0-9][a-z0-9_-]{{0,39}}" placeholder="默认从仓库地址生成">
            </div>
            <div class="field-group">
              <label class="field-label" for="m-label">显示名称</label>
              <input id="m-label" type="text" name="label" maxlength="60" placeholder="默认使用仓库名称">
            </div>
          </div>
          <div class="form-row-two">
            <div class="field-group">
              <label class="field-label" for="m-path">本地保存目录（挂载本地库时必填）</label>
              <input id="m-path" type="text" name="path" placeholder="默认保存在 ~/UnifiedMemory-Shared 下">
            </div>
            <div class="field-group">
              <label class="field-label" for="m-branch">分支</label>
              <input id="m-branch" type="text" name="branch" placeholder="使用远端默认分支">
            </div>
          </div>
          </details>
          <div class="field-group">
            <label class="field-label">允许生效的记忆层级（推荐保持默认以保护个人红线与偏好）</label>
            {render_layer_checkboxes(["L1", "L3", "L4"])}
          </div>
          <label class="checkbox-row">
            <input type="checkbox" name="auto_commit" value="1" checked>
            <span>共写模式下写入后自动在本地 Git 提交</span>
          </label>
          <label class="checkbox-row">
            <input type="checkbox" name="auto_push" value="1">
            <span>共写模式下写入后防抖异步拉取并推送（可随时关闭）</span>
          </label>
          <div class="form-actions">
            <button class="btn btn-primary" type="submit">挂载共享库</button>
          </div>
        </form>
      </section>
    </div>"""


def render_conflicts_view_html(token, key, library):
    """Render side-by-side diff and one-click conflict resolution workbench."""
    conflicts = sync_git.find_conflict_files(library.root)
    if not conflicts:
        return (
            '<div class="empty-state-card">'
            "<h3>当前记忆库没有待处理的同步冲突</h3>"
            f'<p>所有条目状态一致。<a href="{escape(query(library=key))}">返回记忆库概览</a></p>'
            "</div>"
        )

    blocks = []
    for idx, conflict_rel in enumerate(conflicts, start=1):
        orig_rel = sync_git.original_path_for_conflict(conflict_rel)
        orig_path = library.root / orig_rel
        conflict_path = library.root / conflict_rel
        local_text = orig_path.read_text(encoding="utf-8", errors="replace") if orig_path.is_file() else ""
        remote_text = conflict_path.read_text(encoding="utf-8", errors="replace") if conflict_path.is_file() else ""

        diff_lines = list(
            difflib.unified_diff(
                local_text.splitlines(),
                remote_text.splitlines(),
                fromfile=f"本地当前版 ({orig_rel})",
                tofile=f"远端冲突版 ({conflict_rel})",
                lineterm="",
                n=3,
            )
        )
        diff_html_rows = []
        for line in diff_lines:
            if line.startswith("+++") or line.startswith("---"):
                cls = "diff-meta"
            elif line.startswith("@@"):
                cls = "diff-hunk"
            elif line.startswith("+"):
                cls = "diff-add"
            elif line.startswith("-"):
                cls = "diff-del"
            else:
                cls = "diff-ctx"
            diff_html_rows.append(f'<div class="diff-line {cls}">{escape(line)}</div>')
        diff_box = (
            f'<div class="diff-view">{"".join(diff_html_rows)}</div>'
            if diff_html_rows
            else '<p class="field-hint">两份文件内容完全相同，可直接点击「保留本地版本」清理冲突副本。</p>'
        )

        blocks.append(
            f"""
        <article class="conflict-card">
          <div class="conflict-card-head">
            <div>
              <strong>冲突 #{idx}：<code>{escape(orig_rel)}</code></strong>
              <p class="hub-card-path">远端副本：<code>{escape(conflict_rel)}</code></p>
            </div>
            <div class="conflict-quick-actions">
              <form method="post" action="/resolve_conflict#feedback">
                {hidden("csrf", token)}{hidden("library", key)}
                {hidden("conflict_file", conflict_rel)}{hidden("strategy", "keep_local")}
                <button class="btn btn-secondary btn-compact" type="submit">保留本地版本</button>
              </form>
              <form method="post" action="/resolve_conflict#feedback">
                {hidden("csrf", token)}{hidden("library", key)}
                {hidden("conflict_file", conflict_rel)}{hidden("strategy", "use_remote")}
                <button class="btn btn-primary btn-compact" type="submit">采用远端版本</button>
              </form>
            </div>
          </div>

          <h3 class="conflict-subhead">行级差异（红 = 本地，绿 = 远端）</h3>
          {diff_box}

          <div class="conflict-columns">
            <div class="conflict-col">
              <p class="conflict-col-title">本地当前版本（{escape(orig_rel)}）</p>
              <pre class="markdown-raw conflict-pre">{escape(local_text)}</pre>
            </div>
            <div class="conflict-col">
              <p class="conflict-col-title">远端同步版本（{escape(conflict_rel)}）</p>
              <pre class="markdown-raw conflict-pre">{escape(remote_text)}</pre>
            </div>
          </div>

          <details class="mount-edit-drawer">
            <summary>手工合并两边内容后保存 ▾</summary>
            <form method="post" action="/resolve_conflict#feedback" class="edit-form hub-inline-form">
              {hidden("csrf", token)}{hidden("library", key)}
              {hidden("conflict_file", conflict_rel)}{hidden("strategy", "custom_merge")}
              <div class="field-group">
                <label class="field-label">合并后的完整 Markdown（含 frontmatter）</label>
                <textarea name="merged_content" rows="14" required>{escape(local_text)}</textarea>
              </div>
              <div class="form-actions">
                <button class="btn btn-primary btn-compact" type="submit">保存合并结果并清除冲突</button>
              </div>
            </form>
          </details>
        </article>"""
        )

    return f'<div class="conflict-list">{"".join(blocks)}</div>'


def filter_and_score(library, topics, entries, q, layer, status, source, window="", sort_by="updated"):
    """Filter topics and user entries by layer/status/source/window, temporal cues, and sort order."""
    q_stripped = q.strip()
    has_temporal_cue = bool(q_stripped and mcp_server.TEMPORAL_CUE_RE.search(q_stripped))
    residual_q = mcp_server.TEMPORAL_STOP_RE.sub(" ", q_stripped).strip() if has_temporal_cue else q_stripped
    pure_temporal = bool(has_temporal_cue and not residual_q)
    raw_terms, subtokens = mcp_server.build_query_features("" if pure_temporal else (residual_q or q_stripped))

    now_ep = datetime.now().astimezone().timestamp()
    window_cutoff = now_ep - 7 * 86400 if window == "7d" else None
    scored_rows = []

    if source != "user" and status == "active":
        for topic in topics.values():
            if layer and topic.layer != layer:
                continue
            if (window == "7d" or pure_temporal) and not layer and topic.layer == "L4":
                continue
            if window_cutoff is not None and topic.updated_epoch < window_cutoff:
                continue
            score = 1
            if raw_terms:
                body_text = ""
                try:
                    body_text = library.read_markdown(library.root / topic.path)
                except (ValueError, OSError):
                    body_text = ""
                score, _ = mcp_server.score_fields(
                    raw_terms,
                    subtokens,
                    Path(topic.path).stem.casefold(),
                    topic.title.casefold(),
                    topic.description.casefold(),
                    body_text.casefold(),
                )
                if score == 0:
                    continue
                if has_temporal_cue and now_ep - topic.updated_epoch <= 7 * 86400:
                    score += 4
            scored_rows.append((score, "index", topic))

    if source != "index":
        for item in entries:
            if item.status != status or (layer and item.layer != layer):
                continue
            if window_cutoff is not None and item.updated_epoch < window_cutoff:
                continue
            score = 1
            if raw_terms:
                score, _ = mcp_server.score_fields(
                    raw_terms,
                    subtokens,
                    item.name.casefold(),
                    item.title.casefold(),
                    item.description.casefold(),
                    item.body.casefold(),
                )
                if score == 0:
                    continue
                if has_temporal_cue and now_ep - item.updated_epoch <= 7 * 86400:
                    score += 4
            scored_rows.append((score, "user", item))

    if raw_terms or pure_temporal:
        scored_rows.sort(key=lambda r: (-r[0], -r[2].updated_epoch, r[2].layer, r[2].title.casefold()))
    elif sort_by == "created":
        scored_rows.sort(key=lambda r: (-r[2].created_epoch, -r[2].updated_epoch, r[2].layer, r[2].title.casefold()))
    elif sort_by == "updated":
        scored_rows.sort(key=lambda r: (-r[2].updated_epoch, -r[2].created_epoch, r[2].layer, r[2].title.casefold()))
    return scored_rows


def filter_skills(skills_matrix, q, cat, sk_state):
    """Filter unified skills by category, enabled state, and search query."""
    raw_terms, subtokens = mcp_server.build_query_features(q)
    out = []
    for sk in skills_matrix:
        if cat and sk["category"] != cat:
            continue
        if sk_state == "enabled" and not sk["enabled"]:
            continue
        if sk_state == "disabled" and sk["enabled"]:
            continue
        score = 1
        if raw_terms:
            body_txt = ""
            sk_md = Path(sk["path"]) / "SKILL.md"
            if sk_md.is_file():
                body_txt = sk_md.read_text(encoding="utf-8", errors="replace")
            score, _ = mcp_server.score_fields(
                raw_terms,
                subtokens,
                sk["folder"].casefold(),
                sk["name"].casefold(),
                sk["description"].casefold(),
                body_txt.casefold(),
            )
            if score == 0:
                continue
        out.append((score, sk))
    if raw_terms:
        out.sort(key=lambda r: (-r[0], r[1]["name"].casefold()))
    return [r[1] for r in out]


def _ago_zh(epoch, now=None):
    now = now or time.time()
    diff = max(0, int(now - epoch))
    if diff < 60:
        return "刚刚"
    if diff < 3600:
        return f"{diff // 60} 分钟前"
    if diff < 86400:
        return f"{diff // 3600} 小时前"
    return f"{diff // 86400} 天前"


def _ref_link(ref, key, event_library=""):
    """Return an HTML link for 'library:path' (or bare path) refs.

    Bare paths historically mean the personal library.  The activity dashboard
    is global, though, so it must use the audit event's library instead of the
    currently selected navigation library when one is available.
    """
    ref = str(ref or "")
    lib, _, path = ref.partition(":") if ":" in ref else ("", "", ref)
    if not path:
        return escape(ref)
    selected_library = lib or str(event_library or "").strip() or key
    if selected_library == "personal":
        selected_library = "primary"
    if path.startswith("user-managed/") and path.endswith(".md"):
        href = query(library=selected_library, entry=Path(path).stem) + "#detail-title"
    else:
        href = query(library=selected_library, topic=path) + "#detail-title"
    return f'<a href="{escape(href)}">{escape(ref)}</a>'


_TOOL_LABELS = {"memory_search": "搜索", "memory_read": "读取", "memory_list": "列表", "memory_upsert": "写入",
                "memory_archive": "归档", "memory_libraries": "库列表", "memory_history": "历史版本",
    "memory_lint": "体检",
                "memory_restore": "恢复版本"}


def _usage_section(today, week):
    """Context-cost block on the activity page: tokens returned to AI clients and how much was read."""
    if not week.get("tokens_total") and not week.get("searches"):
        return ""
    rate = week.get("read_rate")
    stats = (
        ("今日返回", f'{today.get("tokens_total", 0):,}', "token"),
        ("近 7 天返回", f'{week.get("tokens_total", 0):,}', "token"),
        ("结果被读取", "—" if rate is None else f"{rate * 100:.0f}%",
         f'{week.get("results_read", 0)}/{week.get("results_returned", 0)} 条'),
        ("搜了没读", f'{week.get("searches_without_read", 0)}', f'/ {week.get("searches", 0)} 次搜索'),
    )
    stat_html = "".join(
        f'<div class="usage-stat"><span>{escape(label)}</span>'
        f'<strong>{escape(value)}<small>{escape(unit)}</small></strong></div>'
        for label, value, unit in stats
    )
    tools = sorted((week.get("by_tool") or {}).items(), key=lambda kv: -kv[1]["tokens"])
    tool_rows = "".join(
        f'<tr><td>{escape(_TOOL_LABELS.get(tool, tool))}</td><td>{item["calls"]}</td>'
        f'<td>{item["tokens"]:,}</td><td>{item["avg_tokens"]:,}</td></tr>'
        for tool, item in tools
    )
    if week.get("session_injections"):
        avg = round(week["session_injection_tokens"] / week["session_injections"])
        tool_rows += (f'<tr><td>会话开头注入</td><td>{week["session_injections"]}</td>'
                      f'<td>{week["session_injection_tokens"]:,}</td><td>{avg:,}</td></tr>')
    tool_html = (
        '<table class="usage-table"><thead><tr><th>工具</th><th>次数</th><th>token</th><th>平均</th></tr></thead>'
        f'<tbody>{tool_rows}</tbody></table>'
        if tool_rows else '<p class="act-meta">还没有带大小的记录：重启客户端后的新调用才会记录 token。</p>'
    )
    ranks = list((week.get("read_ranks") or {}).items())
    top = max((count for _, count in ranks), default=0)
    rank_html = "".join(
        f'<li><span class="usage-rank">第 {escape(rank)} 条</span>'
        f'<progress class="usage-bar" max="100" value="{max(4, round(count * 100 / top))}">{count}</progress>'
        f'<span class="usage-count">{count} 次</span></li>'
        for rank, count in ranks
    ) if top else ""
    rank_html = (f'<ul class="usage-ranks" role="list">{rank_html}</ul>' if rank_html
                 else '<p class="act-meta">近 7 天没有读取过搜索结果。</p>')
    note = ("token 为估算值：中文 1 字约 1 token，其他约 4 字符 1 token。"
            "「被读取」指同一客户端在下一次搜索前、30 分钟内读了这条结果。")
    return f"""
        <section class="activity-usage" aria-labelledby="usage-heading">
          <div class="usage-head"><h3 id="usage-heading">上下文用量</h3>
            <span class="usage-hint" title="{escape(note)}">估算 ⓘ</span></div>
          <div class="usage-stats">{stat_html}</div>
          <div class="usage-cols">
            <div><h4>近 7 天按工具</h4>{tool_html}</div>
            <div><h4>AI 读的结果排在第几条</h4>
              <p class="usage-sub">一次搜索会返回多条结果，这里统计 AI 读的是排在第几条的那个。</p>{rank_html}</div>
          </div>
        </section>"""


_LINT_CHECK_LABELS = {
    "secret-leak": "包含疑似明文凭据",
    "expired-entry": "有效期已过，待复核",
    "duplicate-frontmatter": "正文里混进了 frontmatter",
    "index-missing": "不在索引里",
    "index-orphan": "索引指向了不存在的文件",
    "missing-description": "缺摘要",
    "missing-date": "缺验证日期",
    "stale-snapshot": "快照太久没更新",
    "oversized": "条目过长",
    "duplicate-topic": "疑似同主题重复",
}


def _daily_trend_section(daily, pipeline=None):
    """Render a compact 7-day read/write trend chart so users can spot write-pipeline drops."""
    if not daily:
        return ""
    peak = max((max(d.get("reads", 0), d.get("writes", 0)) for d in daily), default=0)
    total_writes = sum(d.get("writes", 0) for d in daily)
    total_reads = sum(d.get("reads", 0) for d in daily)
    cols = []
    for day in daily:
        reads = day.get("reads", 0)
        writes = day.get("writes", 0)
        nudges = day.get("nudges", 0)
        checkpoints = day.get("checkpoints", 0)
        r_step = max(1, min(10, round(reads * 10 / peak))) if peak and reads else 0
        w_step = max(1, min(10, round(writes * 10 / peak))) if peak and writes else 0
        tip = (
            f'{day.get("iso_date", day.get("date", ""))}：查询 {reads} 次 · 写入 {writes} 次 · '
            f'阶段提醒 {checkpoints} 次 · 沉淀提醒 {nudges} 次'
        )
        cols.append(
            f'<div class="trend-col" title="{escape(tip)}">'
            f'<div class="trend-bars">'
            f'<span class="trend-bar trend-bar-read trend-h{r_step}"></span>'
            f'<span class="trend-bar trend-bar-write trend-h{w_step}"></span>'
            f"</div>"
            f'<span class="trend-counts">{writes}写</span>'
            f'<span class="trend-day">{escape(day.get("date", ""))}</span>'
            f"</div>"
        )
    pipeline_html = ""
    if isinstance(pipeline, dict) and (pipeline.get("writes") or pipeline.get("nudges") or pipeline.get("checkpoints")):
        pills = []
        if pipeline.get("writes"):
            append_cnt = pipeline.get("append_writes", 0)
            pills.append(f'<span>增量追加 <strong>{append_cnt}/{pipeline["writes"]}</strong></span>')
        if pipeline.get("checkpoints"):
            pills.append(f'<span>阶段提醒 <strong>{pipeline["checkpoints"]}</strong> 次</span>')
        if pipeline.get("nudges"):
            rate = pipeline.get("nudge_conversion_rate")
            rate_txt = "—" if rate is None else f"{rate * 100:.0f}%"
            pills.append(
                f'<span>提醒转化 <strong>{rate_txt}</strong> ({pipeline.get("nudge_conversions", 0)}/{pipeline["nudges"]})</span>'
            )
        if pipeline.get("write_p95_ms") is not None:
            p95 = pipeline["write_p95_ms"]
            slow_cnt = pipeline.get("slow_writes", 0)
            slow_note = f' · <em class="act-err">{slow_cnt} 次慢写</em>' if slow_cnt else ""
            pills.append(f'<span>写入 P95 <strong>{p95} ms</strong>{slow_note}</span>')
        if pills:
            pipeline_html = f'<div class="pipeline-strip">{"".join(pills)}</div>'
    return (
        '<section class="activity-usage" aria-labelledby="trend-heading">'
        '<div class="usage-head"><h3 id="trend-heading">近 7 天读写趋势</h3>'
        f'<span class="usage-hint">共查 {total_reads} 次 · 共写 {total_writes} 次 '
        '<span class="trend-legend"><i class="trend-dot trend-dot-read"></i>查询 '
        '<i class="trend-dot trend-dot-write"></i>写入</span></span></div>'
        f'<div class="daily-trend">{"".join(cols)}</div>{pipeline_html}</section>'
    )


def _lint_edit_href(ref, key):
    if ref.startswith("user-managed/") and ref.endswith(".md"):
        return query(library=key, entry=Path(ref).stem, view="edit") + "#detail-title"
    if any(ref.startswith(f"{folder}/") for folder in mcp_server.LAYER_DIRS.values()):
        return query(library=key, topic=ref, view="edit_topic") + "#detail-title"
    return ""


def _lint_section(key, root, token="", readonly=False, return_view="", return_meta=False):
    """Library health: the problems a curation pass should fix, worst first."""
    if not root:
        return ("", False) if return_meta else ""
    try:
        report = mcp_server.MemoryStore(root).lint()
    except Exception:
        return ("", False) if return_meta else ""
    findings = report.get("findings") or []
    if not findings:
        empty_html = (
            '<section class="activity-usage"><div class="usage-head"><h3>记忆库体检</h3>'
            f'<span class="usage-hint">{report.get("entries", 0)} 条，没发现问题</span></div></section>'
        )
        return (empty_html, False) if return_meta else empty_html
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 3), f["ref"]))
    counts = report.get("summary", {}).get("by_severity", {})
    urgent = bool(counts.get("error") or counts.get("warning"))
    chips = " · ".join(
        f"{label} {counts[sev]}"
        for sev, label in (("error", "需要修"), ("warning", "该看一眼"), ("info", "可以优化"))
        if counts.get(sev)
    )
    has_index_issue = any(f["check"] in {"index-missing", "index-orphan"} for f in findings)
    rebuild_btn = ""
    if has_index_issue and token and not readonly:
        rebuild_btn = (
            '<form class="inline-form" action="/rebuild_index#feedback" method="post">'
            f'{hidden("csrf", token)}{hidden("library", key)}{hidden("return_view", return_view)}'
            '<button class="btn btn-secondary btn-compact" type="submit">一键重建索引</button></form>'
        )
    rows = []
    for item in findings[:8]:
        cls = "act-err" if item["severity"] == "error" else "act-meta"
        label = _LINT_CHECK_LABELS.get(item["check"], item["check"])
        ref = item["ref"]
        actions = []
        is_topic_ref = any(ref.startswith(f"{folder}/") for folder in mcp_server.LAYER_DIRS.values())
        if (
            token
            and not readonly
            and is_topic_ref
            and item["check"] in {"missing-date", "stale-snapshot", "expired-entry"}
        ):
            actions.append(
                '<form class="inline-form" action="/verify_topic#feedback" method="post">'
                f'{hidden("csrf", token)}{hidden("library", key)}'
                f'{hidden("topic", ref)}{hidden("return_view", return_view)}'
                '<button class="btn btn-ghost btn-compact" type="submit">今日核实</button></form>'
            )
        edit_href = "" if readonly else _lint_edit_href(ref, key)
        if edit_href and item["check"] not in {"index-orphan"}:
            btn_label = "立即处理" if item["severity"] == "error" else "去编辑"
            actions.append(f'<a class="btn btn-ghost btn-compact" href="{escape(edit_href)}">{btn_label}</a>')
        action_html = f'<span class="lint-actions">{"".join(actions)}</span>' if actions else ""
        rows.append(
            f'<li><div class="lint-row-head"><span>{_ref_link(ref, key)} '
            f'<span class="{cls}">{escape(label)}</span></span>{action_html}</div>'
            f'<span class="act-meta">{escape(item["detail"])} → {escape(item["hint"])}</span></li>'
        )
    more = ""
    if len(findings) > 8:
        lint_cmd = "umem lint" if key == "primary" else f"umem lint --library {key}"
        more = f'<li class="act-meta">还有 {len(findings) - 8} 条，运行 <code>{escape(lint_cmd)}</code> 看全部</li>'
    section_cls = "activity-usage lint-urgent" if urgent else "activity-usage"
    html_out = (
        f'<section class="{section_cls}"><div class="usage-head"><h3>记忆库体检</h3>'
        f'<span class="usage-hint">{escape(chips)} {rebuild_btn}</span></div>'
        f'<ul class="lint-list" role="list">{"".join(rows)}{more}</ul></section>'
    )
    return (html_out, urgent) if return_meta else html_out


def render_activity_panel(key, agent_home=None, root=None, token="", readonly=False, return_view=""):
    """First-screen dashboard: what AI clients searched/read/wrote locally (from the audit log)."""
    try:
        summary = audit_log.summarize()
    except Exception:
        return ""
    now = summary["now"]
    seen = {c["client"]: c for c in summary["clients"]}

    client_rows = []
    configured = []
    try:
        import memoryctl

        for aid, spec in memoryctl.agent_specs(home=agent_home).items():
            info = memoryctl.inspect_agent(aid, spec)
            if info["state"] in {"managed", "partial", "drifted"}:
                configured.append(info)
    except Exception:
        configured = []
    names_done = set()
    for info in configured:
        name = info["name"]
        names_done.add(name)
        hooks = info["components"].get("hooks")
        auto = ""
        if hooks is not None:
            auto = (
                '<span class="chip-ok">✓ 自动读写已就绪</span>'
                if hooks["state"] == "managed"
                else '<span class="chip-warn">! 仅工具就绪，自动读写未接管（点「同步」）</span>'
            )
        elif "instructions" in info["components"]:
            auto = (
                '<span class="chip-ok">✓ 规则已注入</span>'
                if info["components"]["instructions"]["state"] == "managed"
                else '<span class="chip-warn">! 规则未就绪</span>'
            )
        c = seen.get(name)
        if c:
            act = (
                f'最近调用 {escape(_ago_zh(c["last_t"], now))} · 今日查 {c["reads_today"]} / 写 {c["writes_today"]}'
                + (f' · <span class="act-err">{c["errors_today"]} 次失败</span>' if c["errors_today"] else "")
            )
        else:
            act = '<span class="act-err">近 7 天没有任何调用：配置了但可能没生效，重载该客户端的 MCP / 重启它</span>'
        client_rows.append(f'<li><strong>{escape(name)}</strong> {auto}<span class="act-meta">{act}</span></li>')
    for name, c in seen.items():
        if name not in names_done:
            client_rows.append(
                f'<li><strong>{escape(name)}</strong><span class="act-meta">最近调用 {escape(_ago_zh(c["last_t"], now))}'
                f' · 今日查 {c["reads_today"]} / 写 {c["writes_today"]}</span></li>'
            )

    write_rows = "".join(
        f'<li><span class="act-meta">{escape(_ago_zh(e["t"], now))} · {escape(str(e.get("client", "")))}</span> '
        f'{_ref_link(e.get("ref") or "", key, e.get("library"))}'
        f'{" <em>(归档)</em>" if e.get("tool") == "memory_archive" else ""}</li>'
        for e in summary["recent_writes"][:5]
    ) or '<li class="act-meta">近 7 天 AI 还没有写入任何记忆</li>'
    query_rows = "".join(
        f'<li><span class="act-meta">{escape(_ago_zh(q["t"], now))} · {escape(str(q.get("client", "")))}</span> '
        f'“{escape(str(q.get("query", "")))}” → {q.get("hits", 0)} 条</li>'
        for q in summary["recent_queries"][:4]
    ) or '<li class="act-meta">近 7 天 AI 还没有查询过记忆</li>'
    hit_rows = "".join(
        f'<li>{_ref_link(h["ref"], key, h.get("library"))} <span class="act-meta">×{h["score"]}</span></li>' for h in summary["top_hits"]
    ) or '<li class="act-meta">暂无高频命中</li>'
    usage_week = summary.get("usage_7d") or {}
    usage_section = _usage_section(summary.get("usage_today") or {}, usage_week)
    trend_section = _daily_trend_section(summary.get("daily") or [], pipeline=usage_week.get("write_pipeline"))
    lint_section, lint_urgent = _lint_section(
        key, root, token=token, readonly=readonly, return_view=return_view, return_meta=True
    )
    checkpoint_note = f' · 阶段提醒 {summary["checkpoints_today"]} 次' if summary.get("checkpoints_today") else ""
    ordered_sections = (
        f"{lint_section}{trend_section}{usage_section}"
        if lint_urgent
        else f"{trend_section}{usage_section}{lint_section}"
    )

    return f"""
      <section class="activity-panel" aria-labelledby="activity-heading">
        <div class="recent-panel-head">
          <div class="recent-panel-title">
            <h2 id="activity-heading">🤖 AI 读写动态</h2>
            <span class="recent-stats">今日查询 {summary["reads_today"]} 次 · 今日写入 {summary["writes_today"]} 次 · 空结果搜索 {summary["empty_searches_today"]} 次{checkpoint_note} · 沉淀提醒 {summary["nudges_today"]} 次</span>
          </div>
        </div>
        <ul class="activity-clients" role="list">{"".join(client_rows) or '<li class="act-meta">尚无已接入客户端</li>'}</ul>
        {ordered_sections}
        <div class="activity-cols">
          <div><h3>最近写入</h3><ul role="list">{write_rows}</ul></div>
          <div><h3>最近查询</h3><ul role="list">{query_rows}</ul></div>
          <div><h3>近 7 天高频命中</h3><ul role="list">{hit_rows}</ul></div>
        </div>
      </section>"""


def render_overview(topics, entries, key, root=None, token="", readonly=False):
    """Render Recent Work timeline + L0-L4 overview dashboard when no topic is selected."""
    now_dt = datetime.now().astimezone()
    now_ep = now_dt.timestamp()
    layer_counts = {lk: 0 for lk in LAYERS}
    recent_pool = []

    for t in topics.values():
        if t.layer in layer_counts:
            layer_counts[t.layer] += 1
        if t.layer != "L4":
            recent_pool.append(("index", t))
    for e in entries:
        if e.status == "active" and e.layer in layer_counts:
            layer_counts[e.layer] += 1
            recent_pool.append(("user", e))

    recent_pool.sort(key=lambda pair: (-pair[1].updated_epoch, -pair[1].created_epoch, pair[1].title.casefold()))
    today_cnt = sum(1 for _, it in recent_pool if now_ep - it.updated_epoch <= 86400)
    week_cnt = sum(1 for _, it in recent_pool if now_ep - it.updated_epoch <= 7 * 86400)

    recent_items_html = []
    for kind, it in recent_pool[:8]:
        href = (
            query(library=key, topic=it.path) + "#detail-title"
            if kind == "index"
            else query(library=key, entry=it.name) + "#detail-title"
        )
        newly = mcp_server.is_newly_created(it.created_at, it.updated_at)
        act_badge = (
            '<span class="act-badge act-new">新写入</span>'
            if newly
            else '<span class="act-badge act-upd">已更新</span>'
        )
        upd_rel, upd_full, upd_iso = mcp_server.format_relative_zh(it.updated_at or it.created_at, now_dt=now_dt)
        if newly:
            time_meta = (
                f'<span class="recent-time-meta">'
                f'<time datetime="{escape(upd_iso)}" data-ts="{escape(upd_iso)}" data-mode="new">{escape(upd_rel)}写入</time>'
                f'<span>· {escape(upd_full)}</span>'
                f"</span>"
            )
        else:
            _, cre_full, _ = mcp_server.format_relative_zh(it.created_at, now_dt=now_dt)
            cre_short = cre_full.split(" ")[0]
            time_meta = (
                f'<span class="recent-time-meta">'
                f'<time datetime="{escape(upd_iso)}" data-ts="{escape(upd_iso)}" data-mode="upd">{escape(upd_rel)}更新</time>'
                f'<span>· 始于 {escape(cre_short)}</span>'
                f"</span>"
            )
        desc_line = f'<p class="recent-desc">{escape(it.description)}</p>' if it.description else ""
        recent_items_html.append(
            f"""
          <li class="recent-item">
            <a class="recent-link" href="{escape(href)}">
              <div class="recent-row-top">
                <span class="layer-badge layer-{it.layer}">{it.layer}</span>
                {act_badge}
                <strong class="recent-title">{escape(it.title)}</strong>
                {time_meta}
              </div>
              {desc_line}
            </a>
          </li>"""
        )

    recent_more_href = query(library=key, window="7d")
    recent_panel = f"""
      <section class="recent-panel" aria-labelledby="recent-work-heading">
        <div class="recent-panel-head">
          <div class="recent-panel-title">
            <h2 id="recent-work-heading">⚡ 最近工作动态</h2>
            <span class="recent-stats">今日活跃 {today_cnt} 条 · 近 7 天活跃 {week_cnt} 条</span>
          </div>
          <a class="btn btn-secondary btn-compact" href="{escape(recent_more_href)}">筛选近 7 天全部 →</a>
        </div>
        <ul class="recent-list" role="list">{"".join(recent_items_html)}</ul>
      </section>"""

    cards = []
    for lk, (lname, _, ldesc) in LAYERS.items():
        href = query(library=key, layer=lk)
        cards.append(
            f"""
          <a class="overview-card" href="{escape(href)}">
            <div class="overview-card-top">
              <span class="layer-badge layer-{lk}">{lk} · {escape(lname)}</span>
              <span class="overview-num">{layer_counts[lk]}</span>
            </div>
            <p>{escape(ldesc)}</p>
          </a>"""
        )

    active_user_cnt = sum(e.status == "active" for e in entries)
    return f"""
      {render_activity_panel(key, root=root, token=token, readonly=readonly)}
      {recent_panel}
      <p class="overview-section-label">按 L0–L4 记忆性质浏览</p>
      <div class="overview-grid">{"".join(cards)}</div>
      <div class="overview-tip">
        <strong>如何高效使用这套统一中枢？</strong>
        <p>1. 左侧列表默认按 <strong>最近更新时间</strong> 倒序排列，顶部可切换「⚡ 最近工作」「最近写入」或「层级顺序」，每条卡片均标明多久前写入或更新。</p>
        <p>2. 当前共有 <strong>{len(topics)} 条</strong> 主知识库记忆和 <strong>{active_user_cnt} 条</strong> 个人手记。点击任意条目右上角「编辑记忆」即可在线修改并自动刷新 <code>updated_at</code> 与 <code>MEMORY.md</code>。</p>
        <p>3. 在已接入的客户端中直接询问“最近做了什么”或“最近工作”，MCP 服务也会按最新更新时间优先返回近期活跃工作。</p>
      </div>"""


def render_skills_overview(skills_matrix, key):
    """Render category overview cards for the unified skills hub."""
    cat_counts = {ck: 0 for ck in SKILL_CATS}
    for sk in skills_matrix:
        if sk["category"] in cat_counts:
            cat_counts[sk["category"]] += 1

    cards = []
    for ck, (cname, badge_layer, cdesc) in SKILL_CATS.items():
        href = query(library=key, tab="skills", cat=ck)
        cards.append(
            f"""
          <a class="overview-card" href="{escape(href)}">
            <div class="overview-card-top">
              <span class="layer-badge layer-{badge_layer}">{escape(cname)}</span>
              <span class="overview-num">{cat_counts[ck]}</span>
            </div>
            <p>{escape(cdesc)}</p>
          </a>"""
        )

    enabled_cnt = sum(sk["enabled"] for sk in skills_matrix)
    return f"""
      <div class="overview-grid">{"".join(cards)}</div>
      <div class="overview-tip">
        <strong>统一 Skills 管理机制</strong>
        <p>1. 所有跨端共享技能统一存放在 <code>{escape(str(data_root() / "skills"))}</code>（共 <strong>{len(skills_matrix)} 个</strong>，当前已启用 <strong>{enabled_cnt} 个</strong>）。</p>
        <p>2. 点击上方「一键同步全部」或「同步」后，系统会自动将原分散在各客户端的独立目录安全备份到 <code>.state/skill-backups/</code>，并统一替换为指向本仓库的软链接。</p>
        <p>3. 在任意技能详情页右上角点击「停用此技能」或「启用此技能」，已接入客户端的挂载状态会实时自动增删同步；客户端专有的内部技能（如 Codex <code>.system</code>）保持原位不受影响。</p>
      </div>"""


def render_skills_tab(
    libraries,
    key,
    token,
    topics,
    q="",
    cat="",
    sk_state="all",
    skill_slug="",
    sub_doc="",
    raw_mode=False,
    notice="",
    error="",
    agent_home=None,
):
    import memoryctl

    library = libraries[key]
    specs = {aid: spec for aid, spec in memoryctl.agent_specs(home=agent_home).items() if "skills_dir" in spec}
    try:
        skills_matrix = memoryctl.inspect_skills_matrix(home=agent_home)
    except OSError:
        skills_matrix = [
            {**s, "agents": dict.fromkeys(specs, "absent")}
            for s in memoryctl.discover_hub_skills()
        ]

    matched = filter_skills(skills_matrix, q, cat, sk_state)
    link_base = dict(library=key, tab="skills", q=q, cat=cat, sk_state="" if sk_state == "all" else sk_state)
    rows = [
        skill_entry_link(
            sk,
            query(**link_base, skill=sk["folder"]),
            sk["folder"] == skill_slug,
        )
        for sk in matched
    ]
    listing = (
        f'<ul class="entry-list" role="list">{"".join(rows)}</ul>'
        if rows
        else '<p class="empty">没有匹配的技能，试试清空搜索或切换分类。</p>'
    )

    base_all_cats = filter_skills(skills_matrix, q, "", sk_state)
    cat_cnt = {ck: 0 for ck in SKILL_CATS}
    for s in base_all_cats:
        if s["category"] in cat_cnt:
            cat_cnt[s["category"]] += 1

    cat_tabs = [
        f'<a class="pill-tab" href="{escape(query(library=key, tab="skills", q=q, sk_state="" if sk_state == "all" else sk_state))}"'
        f'{CUR_ATTR if not cat else ""}>全部 <span class="pill-count">{len(base_all_cats)}</span></a>'
    ]
    for ck, (cname, _, _) in SKILL_CATS.items():
        href = query(library=key, tab="skills", q=q, cat=ck, sk_state="" if sk_state == "all" else sk_state)
        cat_tabs.append(
            f'<a class="pill-tab" href="{escape(href)}"{CUR_ATTR if cat == ck else ""}>'
            f'{escape(cname)} <span class="pill-count">{cat_cnt[ck]}</span></a>'
        )

    en_cnt = sum(s["enabled"] for s in skills_matrix)
    dis_cnt = len(skills_matrix) - en_cnt
    seg_items = [
        (f"全部 ({len(skills_matrix)})", sk_state == "all", query(library=key, tab="skills", q=q, cat=cat)),
        (f"已启用 ({en_cnt})", sk_state == "enabled", query(library=key, tab="skills", q=q, cat=cat, sk_state="enabled")),
        (f"已停用 ({dis_cnt})", sk_state == "disabled", query(library=key, tab="skills", q=q, cat=cat, sk_state="disabled")),
    ]
    source_tabs = "".join(
        f'<a class="seg-link" href="{escape(shref)}"{CUR_ATTR if cur else ""}>{escape(slabel)}</a>'
        for slabel, cur, shref in seg_items
    )

    detail_badges = ""
    detail_submeta = ""
    detail_toolbar = ""
    reader = ""

    by_folder = {s["folder"]: s for s in skills_matrix}
    if skill_slug and skill_slug in by_folder:
        sk = by_folder[skill_slug]
        cat_label, badge_layer, _ = SKILL_CATS.get(sk["category"], ("通用", "L2", ""))
        rel_md = f"skills/{skill_slug}/{sub_doc or 'SKILL.md'}"
        target_file = (library.root / "skills" / skill_slug / (sub_doc or "SKILL.md")).resolve()
        try:
            hub = library.root / "skills"
            resources = memoryctl.resource_catalog(hub).get(skill_slug, [])
            md_content = library.read_markdown(target_file, [(hub / name).resolve() for name in resources])
            heading = sk["name"] if not sub_doc else f"{sk['name']} · {Path(sub_doc).name}"
            ag_chips = []
            for aid, spec in specs.items():
                alabel = escape(spec["name"])
                ast = sk["agents"].get(aid, "absent")
                if ast == "linked":
                    ag_chips.append(f'<span class="meta-chip">✓ {alabel} 已挂载</span>')
                elif ast == "standalone":
                    ag_chips.append(f'<span class="meta-chip">! {alabel} 独立副本待合入</span>')
                else:
                    ag_chips.append(f'<span class="meta-chip">○ {alabel} 未挂载</span>')
            detail_badges = (
                f'<div class="detail-badges">'
                f'<span class="layer-badge layer-{badge_layer}">{escape(cat_label)}</span>'
                + "".join(ag_chips)
                + "</div>"
            )
            detail_submeta = f'<p class="detail-path"><code>skills/{escape(skill_slug)}/{escape(sub_doc or "SKILL.md")}</code></p>'
            toggle_op, toggle_btn_txt, toggle_cls = (
                ("disable", "停用此技能", "btn btn-danger-ghost btn-compact")
                if sk["enabled"]
                else ("enable", "启用此技能", "btn btn-primary btn-compact")
            )
            back_btn = (
                f'<a class="btn btn-secondary btn-compact" href="{escape(query(**link_base, skill=skill_slug))}#detail-title">返回 SKILL.md</a>'
                if sub_doc
                else ""
            )
            raw_href = query(**link_base, skill=skill_slug, doc=sub_doc, raw="" if raw_mode else "1") + "#detail-title"
            detail_toolbar = f"""
              {back_btn}
              <a class="btn btn-ghost btn-compact" href="{escape(raw_href)}">{"阅读视图" if raw_mode else "查看源码"}</a>
              <form method="post" action="/skill#feedback">
                {hidden("csrf", token)}{hidden("library", key)}
                {hidden("folder", skill_slug)}{hidden("op", toggle_op)}
                <button class="{toggle_cls}" type="submit">{toggle_btn_txt}</button>
              </form>"""
            summary_html = (
                f'<div class="doc-summary-banner">{escape(sk["description"])}</div>'
                if sk["description"] and not sub_doc
                else ""
            )
            md_params = {**link_base, "skill": skill_slug}
            body_html = (
                f'<pre class="markdown-raw">{escape(md_content)}</pre>'
                if raw_mode
                else render_markdown_html(
                    md_content,
                    rel_md,
                    library.root,
                    topics,
                    md_params,
                    skip_h1="",
                )
            )
            reader = summary_html + body_html
        except (ValueError, OSError):
            heading = "无法读取技能文档"
            error = "找不到请求的技能 Markdown 文件。"
    else:
        heading = "Skills 统一技能中心"
        detail_badges = '<div class="detail-badges"><span class="meta-chip">已接入的客户端共享</span></div>'
        if dis_cnt > 0:
            detail_toolbar = f"""
              <form method="post" action="/skill#feedback">
                {hidden("csrf", token)}{hidden("library", key)}
                {hidden("folder", "all")}{hidden("op", "enable")}
                <button class="btn btn-secondary btn-compact" type="submit">全部启用</button>
              </form>"""
        reader = "" if DESKTOP else render_skills_overview(skills_matrix, key)

    feedback = ""
    if error:
        feedback = f'<p class="message error" id="feedback" role="alert" tabindex="-1">{escape(error)}</p>'
    elif notice:
        feedback = f'<p class="message" id="feedback" role="status" tabindex="-1">{escape(notice)}</p>'

    mode_tabs = (
        f'<a class="mode-tab" href="{escape(query(library=key))}">记忆知识库 ({len(topics)})</a>'
        f'<a class="mode-tab" href="{escape(query(library=key, tab="skills"))}"{CUR_ATTR}>Skills 技能中心 ({en_cnt}/{len(skills_matrix)})</a>'
    )
    topbar_cta = f'<a class="btn btn-ghost" href="{escape(query(library=key))}">切换到记忆知识库</a>'
    search_hidden = (
        hidden("library", key)
        + hidden("tab", "skills")
        + (hidden("cat", cat) if cat else "")
        + (hidden("sk_state", sk_state) if sk_state != "all" else "")
    )
    clear_search = (
        f'<a class="btn btn-ghost btn-compact" href="{escape(query(library=key, tab="skills", cat=cat, sk_state="" if sk_state == "all" else sk_state))}">清除</a>'
        if q
        else ""
    )

    if DESKTOP and not skill_slug:
        heading = "技能"
        detail_badges = ""
        reader = desktop_results(listing, cat_tabs, source_tabs, label="技能", count=len(rows))

    return load_template().substitute(
        **(desktop_navigation(key, "skills") if DESKTOP else {}),
        page_class="",
        library=escape(library.label),
        home_link=escape(query(library=key, tab="skills")),
        mode_tabs=mode_tabs,
        library_nav=library_nav_html(libraries, key),
        topbar_cta=topbar_cta,
        search_hidden_fields=search_hidden,
        search_placeholder="搜索技能名称、目录或触发场景…",
        clear_search=clear_search,
        filter_caption="技能分类（点击即切换）",
        layer_tabs="".join(cat_tabs),
        segment_class="segment-row-3",
        source_tabs=source_tabs,
        list_title="技能列表",
        sort_controls="",
        index_path=escape(str(library.index)),
        skills_path=escape(str(library.root / "skills")),
        folder=escape(str(library.folder)),
        agent_panel=render_agent_panel(token, key, tab="skills", agent_home=agent_home, library=library),
        detail_badges=detail_badges,
        detail_submeta=detail_submeta,
        detail_toolbar=detail_toolbar,
        heading=escape(heading),
        reader=reader,
        editor="",
        confirm_dialog="",
        feedback=feedback,
        unreadable="",
        listing=listing,
        count=len(rows),
        q=escape(q),
    )


def render(
    libraries,
    key,
    token,
    entry=Entry(),
    topic_draft=None,
    index_draft=None,
    draft_scope="",
    draft_slug="",
    tab="memory",
    q="",
    layer="",
    status="active",
    source="all",
    window="",
    sort_by="updated",
    cat="",
    sk_state="all",
    skill_slug="",
    sub_doc="",
    view="",
    topic_path="",
    raw_mode=False,
    notice="",
    error="",
    agent_home=None,
    import_draft=None,
    import_client="codex",
):
    library = libraries[key]
    entries, unreadable = library.list()
    try:
        markdown, topics = library.read_index()
    except (ValueError, OSError):
        markdown, topics = "", {}
        unreadable.append("MEMORY.md")

    requested_topic_path = topic_path
    if topic_path:
        try:
            topic_path = library._canonical_topic_path(topic_path, topics)
        except InvalidEntry:
            # Keep the original value for a diagnostic error page.  A topic
            # from another library must not be silently redirected.
            pass

    if tab == "skills":
        return render_skills_tab(
            libraries,
            key,
            token,
            topics,
            q=q,
            cat=cat,
            sk_state=sk_state,
            skill_slug=skill_slug,
            sub_doc=sub_doc,
            raw_mode=raw_mode,
            notice=notice,
            error=error,
            agent_home=agent_home,
        )

    import memoryctl

    hub_skills = memoryctl.discover_hub_skills()
    en_sk_cnt = sum(s["enabled"] for s in hub_skills)
    now_dt = datetime.now().astimezone()

    sort_param = "" if sort_by == "updated" else sort_by
    link_params = dict(library=key, q=q, layer=layer, status=status, source=source, window=window, sort=sort_param)
    matched = filter_and_score(library, topics, entries, q, layer, status, source, window=window, sort_by=sort_by)
    rows = []
    for _, kind, item in matched:
        if kind == "index":
            rows.append(
                entry_link(
                    item,
                    query(**link_params, topic=item.path),
                    False,
                    item.path == topic_path,
                    sort_by=sort_by,
                    now_dt=now_dt,
                )
            )
        else:
            rows.append(
                entry_link(
                    item,
                    query(**link_params, entry=item.name),
                    True,
                    item.name == entry.name,
                    sort_by=sort_by,
                    now_dt=now_dt,
                )
            )

    listing = (
        f'<ul class="entry-list" role="list">{"".join(rows)}</ul>'
        if rows
        else '<p class="empty">没有找到匹配的记忆条目，试试清空筛选或换个关键词。</p>'
    )

    base_all_layers = filter_and_score(library, topics, entries, q, "", status, source, window="", sort_by=sort_by)
    recent_7d_rows = filter_and_score(library, topics, entries, q, "", "active", source, window="7d", sort_by="updated")
    per_layer_cnt = {lk: 0 for lk in LAYERS}
    for _, _, it in base_all_layers:
        if it.layer in per_layer_cnt:
            per_layer_cnt[it.layer] += 1

    recent_cur = CUR_ATTR if (window == "7d" and not layer) else ""
    all_cur = CUR_ATTR if (not layer and not window) else ""
    layer_tabs = [
        f'<a class="pill-tab pill-tab-recent" href="{escape(query(library=key, q=q, source=source, window="7d"))}"'
        f'{recent_cur}>⚡ 最近工作 <span class="pill-count">{len(recent_7d_rows)}</span></a>',
        f'<a class="pill-tab" href="{escape(query(library=key, q=q, status=status, source=source, sort=sort_param))}"'
        f'{all_cur}>全部 <span class="pill-count">{len(base_all_layers)}</span></a>',
    ]
    for lk in LAYERS:
        active_attr = CUR_ATTR if layer == lk else ""
        href = query(library=key, q=q, layer=lk, status=status, source=source, sort=sort_param)
        layer_tabs.append(
            f'<a class="pill-tab" href="{escape(href)}"{active_attr}>'
            f'{lk} {LAYER_SHORT[lk]} <span class="pill-count">{per_layer_cnt[lk]}</span></a>'
        )

    sort_options = [
        ("updated", "最近更新"),
        ("created", "最近写入"),
        ("index", "层级顺序"),
    ]
    sort_links = []
    for skey, slabel in sort_options:
        s_cur = CUR_ATTR if sort_by == skey else ""
        s_href = query(
            library=key,
            q=q,
            layer=layer,
            status=status,
            source=source,
            window=window,
            sort="" if skey == "updated" else skey,
            topic=topic_path,
            entry=entry.name,
        )
        sort_links.append(f'<a class="sort-link" href="{escape(s_href)}"{s_cur}>{escape(slabel)}</a>')
    sort_controls_html = f'<nav class="sort-switch" aria-label="排序方式">{"".join(sort_links)}</nav>'

    active_user_cnt = sum(e.status == "active" for e in entries)
    archived_user_cnt = sum(e.status == "archived" for e in entries)
    segments = [
        ("全部", status == "active" and source == "all", query(library=key, q=q, layer=layer)),
        (f"知识库 ({len(topics)})", status == "active" and source == "index", query(library=key, q=q, layer=layer, source="index")),
        (f"手记 ({active_user_cnt})", status == "active" and source == "user", query(library=key, q=q, layer=layer, source="user")),
        (f"归档 ({archived_user_cnt})", status == "archived", query(library=key, q=q, layer=layer, source="user", status="archived")),
    ]
    source_tabs = "".join(
        f'<a class="seg-link" href="{escape(shref)}"{CUR_ATTR if cur else ""}>{escape(slabel)}</a>'
        for slabel, cur, shref in segments
    )

    home_link = query(library=key)
    index_link = query(library=key, view="index") + "#detail-title"
    new_link = query(library=key, layer=layer, source=source if source in {"index", "user"} else "", view="new") + "#detail-title"

    detail_badges = ""
    detail_submeta = ""
    detail_toolbar = ""
    reader = ""
    editor_html = ""
    confirm_dialog_html = ""

    if view in {"new", "edit", "edit_topic", "edit_index"} and library.is_readonly:
        heading = f"{library.label}（只读共享库）"
        detail_badges = '<div class="detail-badges"><span class="meta-chip">只读共享库</span></div>'
        error = error or f"当前库「{library.label}」为只读挂载，不支持在页面中新建或编辑。"
        reader = "" if DESKTOP else render_overview(topics, entries, key, root=library.root)
    elif view == "new":
        heading = "新建记忆"
        default_scope = draft_scope or ("user" if source == "user" else "index")
        default_layer = (
            topic_draft.layer
            if topic_draft
            else (entry.layer if entry.title or entry.body else (layer or "L1"))
        )
        detail_badges = '<div class="detail-badges"><span class="meta-chip">支持写入主知识库或个人手记</span></div>'
        editor_html = render_memory_editor_html(
            mode="new",
            scope=default_scope,
            token=token,
            library_key=key,
            layer=default_layer,
            title=topic_draft.title if topic_draft else entry.title,
            description=topic_draft.description if topic_draft else entry.description,
            body=topic_draft.body if topic_draft else entry.body,
            verified_at=topic_draft.verified_at if topic_draft else entry.verified_at,
            valid_until=topic_draft.valid_until if topic_draft else entry.valid_until,
            slug=draft_slug,
            cancel_link=query(**link_params),
        )
    elif view == "edit" and entry.name:
        heading = f"编辑手记 · {entry.title or entry.name}"
        detail_badges = '<div class="detail-badges"><span class="meta-chip">个人手记区 · user-managed/</span></div>'
        detail_submeta = f'<p class="detail-path"><code>user-managed/{escape(entry.name)}.md</code></p>'
        _, upd_full, _ = mcp_server.format_relative_zh(entry.updated_at, now_dt=now_dt)
        meta_note = (
            f"<span>当前状态：{STATES[entry.status]} · 类型：{escape(entry.resolved_type())} · "
            f"上次修改：{escape(upd_full or entry.updated_at)}</span>"
            f"<code>user-managed/{escape(entry.name)}.md</code>"
        )
        editor_html = render_memory_editor_html(
            mode="edit",
            scope="user",
            token=token,
            library_key=key,
            layer=entry.layer,
            title=entry.title,
            description=entry.description,
            body=entry.body,
            entry_name=entry.name,
            revision=entry.revision,
            entry_type=entry.entry_type,
            verified_at=entry.verified_at,
            valid_until=entry.valid_until,
            cancel_link=query(**link_params, entry=entry.name) + "#detail-title",
            file_meta_note=meta_note,
        )
    elif view == "edit_topic" and (topic_draft or topic_path):
        try:
            t_entry = topic_draft if topic_draft is not None else library.read_topic(topic_path, topics)
            heading = f"编辑记忆 · {t_entry.title}"
            lname = LAYERS.get(t_entry.layer, ("记忆", "reference", ""))[0]
            detail_badges = (
                f'<div class="detail-badges">'
                f'<span class="layer-badge layer-{t_entry.layer}">{t_entry.layer} · {escape(lname)}</span>'
                f'<span class="meta-chip">主知识库索引条目</span>'
                f"</div>"
            )
            detail_submeta = f'<p class="detail-path"><code>{escape(t_entry.path)}</code></p>'
            meta_note = (
                f"<span>类型：{escape(t_entry.entry_type)} · 保存时将自动同步更新 MEMORY.md</span>"
                f"<code>{escape(t_entry.path)}</code>"
            )
            editor_html = render_memory_editor_html(
                mode="edit_topic",
                scope="index",
                token=token,
                library_key=key,
                layer=t_entry.layer,
                title=t_entry.title,
                description=t_entry.description,
                body=t_entry.body,
                orig_topic=t_entry.path,
                revision=t_entry.revision,
                entry_type=t_entry.entry_type,
                verified_at=t_entry.verified_at,
                valid_until=t_entry.valid_until,
                cancel_link=query(**link_params, topic=t_entry.path) + "#detail-title",
                file_meta_note=meta_note,
            )
        except (ValueError, OSError) as exc:
            heading = "无法编辑条目"
            error = error or topic_read_error(
                library,
                requested_topic_path or topic_path,
                exc,
            )
            detail_toolbar = (
                f'<a class="btn btn-ghost btn-compact" href="{escape(home_link)}">'
                "返回当前库概览</a>"
            )
    elif view == "edit_index":
        heading = "编辑 MEMORY.md · 记忆维护规则与总索引"
        detail_badges = '<div class="detail-badges"><span class="meta-chip">权威入口文件</span></div>'
        detail_submeta = f'<p class="detail-path"><code>{escape(str(library.index))}</code></p>'
        try:
            idx_text, idx_rev = index_draft if index_draft is not None else library.read_index_with_revision()
            editor_html = render_index_editor_html(
                token=token,
                library_key=key,
                markdown_text=idx_text,
                revision=idx_rev,
                cancel_link=query(**link_params, view="index") + "#detail-title",
                index_path=library.index,
            )
        except OSError:
            error = error or "无法读取 MEMORY.md。"
    elif entry.name and entry.revision:
        heading = entry.title
        lname = LAYERS[entry.layer][0]
        validity_chips = ""
        if entry.verified_at:
            validity_chips += f'<span class="meta-chip">核实于 {escape(entry.verified_at)}</span>'
        if entry.valid_until:
            exp = mcp_server.is_date_expired(entry.valid_until)
            chip_cls = "meta-chip meta-chip-warn" if exp else "meta-chip"
            chip_txt = f"已过期 · {entry.valid_until}" if exp else f"有效期至 {entry.valid_until}"
            validity_chips += f'<span class="{chip_cls}">{escape(chip_txt)}</span>'
        detail_badges = (
            f'<div class="detail-badges">'
            f'<span class="layer-badge layer-{entry.layer}">{entry.layer} · {escape(lname)}</span>'
            f'<span class="meta-chip">个人手记</span>'
            f'<span class="meta-chip">类型: {escape(entry.resolved_type())}</span>'
            f'<span class="meta-chip">{STATES[entry.status]}</span>'
            f"{validity_chips}"
            f"</div>"
        )
        detail_submeta = render_detail_timestamps_html(
            f"user-managed/{entry.name}.md",
            entry.created_at,
            entry.updated_at,
            now_dt,
        )
        edit_href = query(**link_params, entry=entry.name, view="edit") + "#detail-title"
        toggle_href = query(**link_params, entry=entry.name, raw="" if raw_mode else "1") + "#detail-title"
        toggle_label = "阅读视图" if raw_mode else "查看源码"
        act, act_label = ("archive", "归档") if entry.status == "active" else ("restore", "恢复")
        detail_toolbar = f"""
          <a class="btn btn-primary btn-compact" href="{escape(edit_href)}">编辑手记</a>
          <button class="btn btn-ghost btn-compact" type="button" data-copy-target="copy-markdown-source">复制正文</button>
          <details class="action-menu">
            <summary class="btn btn-ghost btn-compact">更多 ▾</summary>
            <div class="action-menu-pop">
              <a class="action-menu-item" href="{escape(toggle_href)}">{toggle_label}</a>
              <form action="/{act}#feedback" method="post" aria-label="{act_label}当前条目">
                {hidden("csrf", token)}{hidden("name", entry.name)}{hidden("revision", entry.revision)}
                {hidden("library", key)}
                <button class="action-menu-item" type="submit">{act_label}</button>
              </form>
              <button class="action-menu-item action-menu-danger" type="button" commandfor="delete-dialog" command="show-modal">删除</button>
            </div>
          </details>"""
        confirm_dialog_html = render_delete_dialog_html(
            token=token,
            library_key=key,
            scope="user",
            target_id=entry.name,
            display_path=f"user-managed/{entry.name}.md",
            revision=entry.revision,
        )
        summary_html = (
            f'<div class="doc-summary-banner">{escape(entry.description)}</div>'
            if entry.description
            else ""
        )
        body_html = (
            f'<pre class="markdown-raw">{escape(entry.markdown())}</pre>'
            if raw_mode
            else render_markdown_html(
                entry.body,
                f"user-managed/{entry.name}.md",
                library.root,
                topics,
                link_params,
                skip_h1=entry.title,
            )
        )
        copy_source = f'<textarea id="copy-markdown-source" hidden readonly>{escape(entry.body)}</textarea>'
        reader = summary_html + body_html + copy_source
    elif view == "clients":
        heading = "客户端接入"
        reader = render_agent_panel(token, key, agent_home=agent_home, library=library, flat=True)
    elif view == "memory_import":
        heading = f"{memory_import.CLIENTS[import_client]} 待确认记忆"
        detail_toolbar = f'<a class="btn btn-ghost btn-compact" href="{escape(query(view="clients"))}">返回客户端接入</a>'
        reader = render_memory_import(token, library, import_client, topic_path, q, import_draft or {})
    elif view == "sync_hub":
        heading = "共享记忆库与 Git 同步中心"
        detail_badges = '<div class="detail-badges"><span class="meta-chip">个人跨机同步 · 团队共享挂载</span></div>'
        detail_toolbar = f'<a class="btn btn-ghost btn-compact" href="{escape(home_link)}">返回记忆库</a>'
        reader = render_sync_hub_html(token, key, libraries, agent_home=agent_home)
    elif view == "conflicts":
        heading = f"同步冲突对比与合并 · {library.label}"
        detail_badges = '<div class="detail-badges"><span class="meta-chip">行级 Diff · 双方内容完整保留</span></div>'
        detail_toolbar = (
            f'<a class="btn btn-ghost btn-compact" href="{escape(query(library=key, view="sync_hub"))}#detail-title">共享与同步</a>'
            f'<a class="btn btn-ghost btn-compact" href="{escape(home_link)}">返回记忆库</a>'
        )
        reader = render_conflicts_view_html(token, key, library)
    elif view == "index":
        heading = "MEMORY.md · 记忆维护规则与总索引"
        detail_badges = '<div class="detail-badges"><span class="meta-chip">权威入口文件</span></div>'
        detail_submeta = f'<p class="detail-path"><code>{escape(str(library.index))}</code></p>'
        edit_idx_href = query(**link_params, view="edit_index") + "#detail-title"
        toggle_href = query(**link_params, view="index", raw="" if raw_mode else "1") + "#detail-title"
        edit_idx_btn = (
            ""
            if library.is_readonly
            else f'<a class="btn btn-primary btn-compact" href="{escape(edit_idx_href)}">编辑 MEMORY.md</a>'
        )
        detail_toolbar = (
            f'{edit_idx_btn}'
            f'<a class="btn btn-ghost btn-compact" href="{escape(toggle_href)}">'
            f'{"阅读视图" if raw_mode else "查看源码"}</a>'
        )
        reader = (
            f'<pre class="markdown-raw">{escape(markdown)}</pre>'
            if raw_mode
            else render_markdown_html(markdown, "MEMORY.md", library.root, topics, link_params, skip_h1="")
        )
    elif topic_path:
        try:
            t_entry = library.read_topic(topic_path, topics)
            detail = library.read_markdown(library.root / t_entry.path)
            heading = t_entry.title
            lname = LAYERS[t_entry.layer][0]
            validity_chips = ""
            if t_entry.verified_at:
                validity_chips += f'<span class="meta-chip">核实于 {escape(t_entry.verified_at)}</span>'
            if t_entry.valid_until:
                exp = mcp_server.is_date_expired(t_entry.valid_until) and t_entry.layer != "L4"
                chip_cls = "meta-chip meta-chip-warn" if exp else "meta-chip"
                chip_txt = f"已过期 · {t_entry.valid_until}" if exp else f"有效期至 {t_entry.valid_until}"
                validity_chips += f'<span class="{chip_cls}">{escape(chip_txt)}</span>'
            detail_badges = (
                f'<div class="detail-badges">'
                f'<span class="layer-badge layer-{t_entry.layer}">{t_entry.layer} · {escape(lname)}</span>'
                f'<span class="meta-chip">知识库索引</span>'
                f'<span class="meta-chip">type: {escape(t_entry.entry_type)}</span>'
                f"{validity_chips}"
                f"</div>"
            )
            detail_submeta = render_detail_timestamps_html(
                t_entry.path,
                t_entry.created_at,
                t_entry.updated_at,
                now_dt,
            )
            edit_topic_href = query(**link_params, topic=t_entry.path, view="edit_topic") + "#detail-title"
            toggle_href = query(**link_params, topic=t_entry.path, raw="" if raw_mode else "1") + "#detail-title"
            if library.is_readonly:
                detail_toolbar = (
                    '<button class="btn btn-ghost btn-compact" type="button" data-copy-target="copy-markdown-source">复制正文</button>'
                    f'<a class="btn btn-ghost btn-compact" href="{escape(toggle_href)}">'
                    f'{"阅读视图" if raw_mode else "查看源码"}</a>'
                )
            else:
                verify_btn = ""
                if t_entry.layer != "L4":
                    verify_btn = f"""
                      <form action="/verify_topic#feedback" method="post" aria-label="标记当前记忆为今日已核实">
                        {hidden("csrf", token)}{hidden("library", key)}
                        {hidden("topic", t_entry.path)}{hidden("revision", t_entry.revision)}
                        <button class="btn btn-ghost btn-compact" type="submit">今日核实</button>
                      </form>"""
                archive_btn = ""
                if t_entry.layer != "L4" and "L4" in library.allowed_layers:
                    archive_btn = f"""
                      <form action="/archive_topic#feedback" method="post" aria-label="归档当前记忆到 L4">
                        {hidden("csrf", token)}{hidden("library", key)}
                        {hidden("topic", t_entry.path)}{hidden("revision", t_entry.revision)}
                        <button class="action-menu-item" type="submit">归档至 L4</button>
                      </form>"""
                writable_shared = [
                    (lk, lib)
                    for lk, lib in libraries.items()
                    if lib.role == "shared" and not lib.is_readonly
                ]
                share_btn = ""
                share_dialog = ""
                if library.role == "personal" and writable_shared:
                    share_btn = '<button class="btn btn-secondary btn-compact" type="button" commandfor="share-dialog" command="show-modal">分享到共享库</button>'
                    share_dialog = render_share_dialog_html(token, key, t_entry, writable_shared)
                detail_toolbar = f"""
                  <a class="btn btn-primary btn-compact" href="{escape(edit_topic_href)}">编辑记忆</a>
                  {verify_btn}
                  {share_btn}
                  <button class="btn btn-ghost btn-compact" type="button" data-copy-target="copy-markdown-source">复制正文</button>
                  <details class="action-menu">
                    <summary class="btn btn-ghost btn-compact">更多 ▾</summary>
                    <div class="action-menu-pop">
                      <a class="action-menu-item" href="{escape(toggle_href)}">{"阅读视图" if raw_mode else "查看源码"}</a>
                      {archive_btn}
                      <button class="action-menu-item action-menu-danger" type="button" commandfor="delete-dialog" command="show-modal">删除</button>
                    </div>
                  </details>"""
                confirm_dialog_html = render_delete_dialog_html(
                    token=token,
                    library_key=key,
                    scope="index",
                    target_id=t_entry.path,
                    display_path=t_entry.path,
                    revision=t_entry.revision,
                ) + share_dialog
            summary_html = (
                f'<div class="doc-summary-banner">{escape(t_entry.description)}</div>'
                if t_entry.description
                else ""
            )
            body_html = (
                f'<pre class="markdown-raw">{escape(detail)}</pre>'
                if raw_mode
                else render_markdown_html(
                    detail,
                    t_entry.path,
                    library.root,
                    topics,
                    link_params,
                    skip_h1=t_entry.title,
                )
            )
            slug = Path(t_entry.path).stem
            links, backlinks = library.topic_links(t_entry.path, detail)
            copy_source = f'<textarea id="copy-markdown-source" hidden readonly>{escape(detail)}</textarea>'
            reader = (
                summary_html
                + body_html
                + render_topic_links_html(key, links, backlinks)
                + render_topic_versions_html(token, key, slug, library.topic_versions(slug), library.is_readonly)
                + copy_source
            )
        except (ValueError, OSError) as exc:
            heading = "无法读取条目"
            error = error or topic_read_error(
                library,
                requested_topic_path or topic_path,
                exc,
            )
            detail_toolbar = (
                f'<a class="btn btn-ghost btn-compact" href="{escape(home_link)}">'
                "返回当前库概览</a>"
            )
    else:
        heading = "记忆库概览"
        detail_badges = '<div class="detail-badges"><span class="meta-chip">L0–L4 分层记忆工作台</span></div>'
        reader = "" if DESKTOP else render_overview(
            topics, entries, key, root=library.root, token=token, readonly=library.is_readonly
        )

    feedback = ""
    if error:
        feedback = f'<p class="message error" id="feedback" role="alert" tabindex="-1">{escape(error)}</p>'
    elif notice:
        feedback = f'<p class="message" id="feedback" role="status" tabindex="-1">{escape(notice)}</p>'
    unreadable_note = ""
    if unreadable:
        names = "、".join(escape(name) for name in unreadable)
        unreadable_note = f'<p class="message error">无法读取这些文件，请检查格式：{names}</p>'

    search_hidden = (
        hidden("library", key)
        + (hidden("layer", layer) if layer else "")
        + (hidden("source", source) if source != "all" else "")
        + (hidden("status", status) if status != "active" else "")
        + (hidden("window", window) if window else "")
        + (hidden("sort", sort_param) if sort_param else "")
    )
    clear_search = (
        f'<a class="btn btn-ghost btn-compact" href="{escape(query(library=key, layer=layer, source=source, status=status, window=window, sort=sort_param))}">清除</a>'
        if q
        else ""
    )
    mode_tabs = (
        f'<a class="mode-tab" href="{escape(query(library=key))}"{CUR_ATTR}>记忆知识库 ({len(topics)})</a>'
        f'<a class="mode-tab" href="{escape(query(library=key, tab="skills"))}">Skills 技能中心 ({en_sk_cnt}/{len(hub_skills)})</a>'
    )
    new_btn_html = (
        ""
        if library.is_readonly
        else f'<a class="btn btn-primary" href="{escape(new_link)}"><span aria-hidden="true">＋</span> 新建记忆</a>'
    )
    topbar_cta = f'<a class="btn btn-ghost" href="{escape(index_link)}">查看 MEMORY.md 规范</a>{new_btn_html}'

    agent_and_sync_html = "" if DESKTOP or view in {"clients", "memory_import"} else render_agent_panel(token, key, tab="", agent_home=agent_home, library=library)

    selected = "library"
    if DESKTOP:
        if view in {"sync_hub", "conflicts"}:
            selected = "sync"
        elif view in {"clients", "activity", "memory_import"}:
            selected = "clients" if view == "memory_import" else view
            detail_badges = detail_submeta = ""
            if view == "activity":
                heading = "读写动态"
                reader = render_activity_panel(
                    key, root=library.root, token=token, readonly=library.is_readonly, return_view="activity"
                )
        elif not view and not topic_path and not entry.name:
            heading = "搜索结果" if q else "记忆库"
            detail_badges = detail_submeta = detail_toolbar = ""
            stats = (
                ("知识库条目", len(topics), query(library=key, source="index")),
                ("近七天活跃", len(recent_7d_rows), query(library=key, window="7d")),
                ("个人手记", active_user_cnt, query(library=key, source="user")),
            )
            metrics = "".join(
                f'<a class="desk-stat" href="{escape(href)}"><span>{label}</span><strong>{value}</strong></a>'
                for label, value, href in stats
            )
            reader = ("" if q else desktop_status_strip(key, library) + f'<div class="desk-stats">{metrics}</div>') + desktop_results(
                listing, layer_tabs, source_tabs, sort_controls_html, "记忆条目", len(rows))

    return load_template().substitute(
        **(desktop_navigation(key, selected) if DESKTOP else {}),
        page_class="settings-view" if view in {"clients", "memory_import"} else "",
        library=escape(library.label),
        home_link=escape(home_link),
        mode_tabs=mode_tabs,
        library_nav=library_nav_html(libraries, key),
        topbar_cta=topbar_cta,
        search_hidden_fields=search_hidden,
        search_placeholder="搜索标题、摘要、正文或“最近工作”…",
        clear_search=clear_search,
        filter_caption="内容分层与近期动态",
        layer_tabs="".join(layer_tabs),
        segment_class="segment-row",
        source_tabs=source_tabs,
        list_title="近 7 天活跃" if window == "7d" else "记忆列表",
        sort_controls=sort_controls_html,
        index_path=escape(str(library.index)),
        skills_path=escape(str(library.root / "skills")),
        folder=escape(str(library.folder)),
        agent_panel=agent_and_sync_html,
        detail_badges=detail_badges,
        detail_submeta=detail_submeta,
        detail_toolbar=detail_toolbar,
        heading=escape(heading),
        reader=reader,
        editor=editor_html,
        confirm_dialog=confirm_dialog_html,
        feedback=feedback,
        unreadable=unreadable_note,
        listing=listing,
        count=len(rows),
        q=escape(q),
    )


class Handler(BaseHTTPRequestHandler):
    # The current server is single-threaded, but sync workers and future
    # threaded integrations can observe this registry concurrently.
    SYNC_JOB_TTL_SECONDS = 15 * 60

    def _sync_jobs_lock(self):
        lock = getattr(self.server, "sync_jobs_lock", None)
        if lock is None:
            lock = threading.RLock()
            self.server.sync_jobs_lock = lock
        return lock

    def _sync_jobs_snapshot(self):
        jobs = self.server.sync_jobs
        now = time.time()
        with self._sync_jobs_lock():
            # Keep completed jobs long enough for a redirect/poll, then evict
            # them so a long-running manager cannot grow this map forever.
            for key, job in list(jobs.items()):
                if (
                    job.outcome.done()
                    and job.created_at
                    and now - job.created_at > self.SYNC_JOB_TTL_SECONDS
                ):
                    jobs.pop(key, None)
            return list(jobs.items())

    def log_message(self, format_string, *args):
        pass

    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def respond(self, status, content, mime="text/html; charset=utf-8", location=None):
        data = content.encode("utf-8") if isinstance(content, str) else content
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'",
        )
        if location:
            self.send_header("Location", location)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def known_host(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def page(self, code=HTTPStatus.OK, entry=Entry(), key="primary", **kwargs):
        agent_home = getattr(self.server, "agent_home", None)
        self.respond(
            code,
            render(
                self.server.libraries,
                key,
                self.server.token,
                entry=entry,
                agent_home=agent_home,
                **kwargs,
            ),
        )

    def _refresh_mounts(self, force=False):
        loader = getattr(self.server, "reload_mounts", None)
        if loader is not None:
            loader(force=force)

    def do_GET(self):
        if not self.known_host():
            self.respond(HTTPStatus.FORBIDDEN, "地址无效。", "text/plain; charset=utf-8")
            return
        url = urlsplit(self.path)
        if url.path == "/sync_jobs":
            self.show_inline_sync_jobs()
            return
        self._refresh_mounts()
        if url.path == "/quick_search":
            self.handle_quick_search(parse_qs(url.query, max_num_fields=8))
            return
        if url.path == "/sync_progress":
            self.show_sync_progress(parse_qs(url.query).get("job", [""])[0])
            return
        if url.path == "/style.css":
            self.respond(HTTPStatus.OK, load_style(), "text/css; charset=utf-8")
            return
        if url.path == "/app.js":
            self.respond(HTTPStatus.OK, load_script(), "application/javascript; charset=utf-8")
            return
        if url.path != "/":
            self.respond(HTTPStatus.NOT_FOUND, "", "text/plain; charset=utf-8")
            return
        key = "primary"
        try:
            params = parse_qs(url.query, max_num_fields=18)
            requested_key = params.get("library", ["primary"])[0]
            if requested_key not in self.server.libraries:
                raise InvalidEntry("记忆库不存在。")
            key = requested_key
            tab = "skills" if params.get("tab", [""])[0] == "skills" else "memory"
            q = params.get("q", [""])[0].strip()[:120]
            layer = params.get("layer", [""])[0]
            status = params.get("status", ["active"])[0]
            source = params.get("source", ["all"])[0]
            window = params.get("window", [""])[0]
            sort_by = params.get("sort", ["updated"])[0]
            cat = params.get("cat", [""])[0]
            sk_state = params.get("sk_state", ["all"])[0]
            skill_slug = params.get("skill", [""])[0]
            sub_doc = params.get("doc", [""])[0]
            view = params.get("view", [""])[0]
            topic_path = params.get("topic", [""])[0]
            import_client = params.get("client", ["codex"])[0]
            raw_mode = params.get("raw", [""])[0] == "1"
            if (
                (layer and layer not in LAYERS)
                or status not in STATES
                or source not in SOURCES
                or window not in {"", "7d"}
                or sort_by not in {"updated", "created", "index"}
                or (cat and cat not in SKILL_CATS)
                or sk_state not in {"all", "enabled", "disabled"}
                or view not in ({"", "new", "edit", "edit_topic", "index", "edit_index", "sync_hub", "conflicts", "clients", "memory_import"}
                                | ({"activity"} if DESKTOP else set()))
                or (view == "memory_import" and (key != "primary" or tab != "memory" or import_client not in memory_import.CLIENTS))
            ):
                raise InvalidEntry("筛选条件无效。")
            name = params.get("entry", [""])[0]
            library = self.server.libraries[key]
            entry = library.read(name) if (name and tab == "memory") else Entry(layer=layer or "L2")
        except (ValueError, OSError):
            self.page(HTTPStatus.BAD_REQUEST, key=key, error="无法打开条目，请重新选择。")
            return
        notices = {
            "saved": "个人手记已保存。",
            "topic_saved": "记忆已保存并同步更新 MEMORY.md 索引。",
            "topic_verified": "已将该记忆标记为今日核实（若原有效期已过则同步清除过期状态）。",
            "topic_archived": "记忆已移入 L4 事故归档并同步更新索引。",
            "index_saved": "MEMORY.md 规范与索引已保存。",
            "deleted": "记忆条目已移除（已在本机回收站保留安全备份）。",
            "archived": "手记已归档。",
            "restored": "手记已恢复生效。",
            "version_restored": "历史版本已恢复为当前内容（原内容已另存为新备份）。",
            "connected": "客户端配置已保存，可在读写动态查看实际调用。",
            "claude-desktop_connected": "Claude Desktop 配置已保存。请重启 Claude Desktop 并在新会话中试用 Unified Memory。",
            "workbuddy_connected": "WorkBuddy 配置已保存。如果当前会话还看不到记忆工具，请重新打开 WorkBuddy 会话。",
            "deepseek_connected": "DeepSeek Harness 配置已保存。重启 Harness 后，在新会话中试用 Unified Memory。",
            "disconnected": "客户端接入已干净移除，原备份已还原。",
            "doctored": "自检完成，所有可自动修复项已同步对齐。",
            "skill_enabled": "技能已启用并同步到已连通客户端。",
            "skill_disabled": "技能已停用并从已连通客户端卸载。",
            "synced": "Git 同步完成。",
            "synced_conflicts": "已合并远端更新，发现正文冲突副本，请前往冲突对比页合并后再推送。",
            "index_rebuilt": "MEMORY.md 索引已按磁盘条目重新生成。",
            "remote_readable": "读取连接成功。远端仓库可访问，本次测试未上传记忆。",
            "sync_setup_saved": "Git 远端同步配置已保存。",
            "mount_added": "共享记忆库已挂载生效，顶栏可直接切换。",
            "mount_updated": "共享库挂载设置已更新。",
            "mount_removed": "已解除挂载（本地目录与文件已完整保留）。",
            "conflict_resolved": "冲突已解决并清理冲突副本。",
            "topic_shared": "已将该个人记忆分享到目标共享库。",
            "memory_import_approved": "已将确认后的记忆归入个人记忆库。",
        }
        notice = notices.get(params.get("notice", [""])[0], "")
        self.page(
            entry=entry,
            key=key,
            tab=tab,
            q=q,
            layer=layer,
            status=status,
            source=source,
            window=window,
            sort_by=sort_by,
            cat=cat,
            sk_state=sk_state,
            skill_slug=skill_slug,
            sub_doc=sub_doc,
            view=view,
            topic_path=topic_path,
            raw_mode=raw_mode,
            notice=notice,
            import_client=import_client,
        )

    def do_POST(self):
        origin = self.headers.get("Origin")
        expected_origin = f"http://127.0.0.1:{self.server.server_port}"
        if not self.known_host() or (origin and origin != expected_origin):
            self.respond(HTTPStatus.FORBIDDEN, "请求来源无效。", "text/plain; charset=utf-8")
            return
        allowed_routes = {
            "/save",
            "/save_index",
            "/archive_topic",
            "/verify_topic",
            "/delete",
            "/archive",
            "/restore",
            "/agent",
            "/doctor",
            "/skill",
            "/sync",
            "/sync_test",
            "/rebuild_index",
            "/sync_setup",
            "/mount_add",
            "/mount_update",
            "/mount_remove",
            "/resolve_conflict",
            "/restore_version",
            "/share_topic",
            "/memory_import",
        }
        if self.path not in allowed_routes:
            self.respond(HTTPStatus.NOT_FOUND, "", "text/plain; charset=utf-8")
            return
        self._refresh_mounts()
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            self.page(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, error="请使用页面上的表单。")
            return
        try:
            size = int(self.headers.get("Content-Length", ""))
            if not 0 < size <= MAX_POST:
                raise ValueError
            params = parse_qs(
                self.rfile.read(size).decode("utf-8"),
                keep_blank_values=True,
                max_num_fields=32,
                errors="strict",
            )
            if any(len(values) != 1 for values in params.values()):
                raise ValueError
            fields = {k: v[0] for k, v in params.items()}
        except (ValueError, TimeoutError):
            self.page(HTTPStatus.BAD_REQUEST, error="表单无效或超出大小限制。")
            return
        if not secrets.compare_digest(
            fields.get("csrf", "").encode("utf-8"), self.server.token.encode("ascii")
        ):
            self.respond(HTTPStatus.FORBIDDEN, "请刷新页面后重试。", "text/plain; charset=utf-8")
            return

        if self.path == "/memory_import":
            self.handle_memory_import(fields)
            return
        if self.path in {
            "/agent",
            "/doctor",
            "/skill",
            "/sync",
            "/sync_test",
            "/rebuild_index",
            "/sync_setup",
            "/mount_add",
            "/mount_update",
            "/mount_remove",
            "/resolve_conflict",
            "/share_topic",
        }:
            self.handle_control_action(fields)
            return
        if self.path == "/save_index":
            self.handle_save_index(fields)
            return
        if self.path == "/archive_topic":
            self.handle_archive_topic(fields)
            return
        if self.path == "/verify_topic":
            self.handle_verify_topic(fields)
            return
        if self.path == "/restore_version":
            self.handle_restore_version(fields)
            return
        if self.path == "/delete":
            self.handle_delete(fields)
            return
        self.update_entry(fields)

    def handle_memory_import(self, fields):
        library = self.server.libraries["primary"]
        selected = fields.get("import_id", "")
        client = fields.get("client", "codex")
        if client not in memory_import.CLIENTS:
            self.page(HTTPStatus.BAD_REQUEST, view="clients", error="不支持的客户端。")
            return
        draft = None
        op = fields.get("op")
        try:
            if op in {"import", "text"}:
                if op == "import":
                    report = memory_import.import_memories(library.root, client, home=self.server.agent_home)
                else:
                    report = memory_import.import_text(library.root, client, fields.get("title", ""), fields.get("body", ""))
                name = memory_import.CLIENTS[client]
                if report["found"]:
                    notice = f'找到 {report["found"]} 条 {name} 记忆，新增 {report["added"]} 条，更新 {report["updated"]} 条。'
                else:
                    notice = f"未发现可直接导入的 {name} 记忆。可选择文件或粘贴原文。"
                self.page(view="memory_import", import_client=client, notice=notice, error=" ".join(report["warnings"]))
                return
            if op != "approve":
                raise InvalidEntry("未知的记忆导入操作。")
            with memory_import.review_lock(library.root, client):
                item = memory_import.read_candidate(library.root, client, selected)
                if fields.get("revision") != memory_import.revision(item):
                    raise InvalidEntry("导入原文已更新，请重新核对。")
                draft = {name: fields.get(name, "").strip() for name in ("layer", "title", "description", "body")}
                validate_common_fields(**draft)
                original_date = item["source_date"] or "未知"
                session = f'，会话 {item["thread_id"]}' if item["thread_id"] else ""
                provenance = f'> 来源：{item["source"]}，原日期 {original_date}{session}'
                body = f'{draft["body"]}\n\n{provenance}'
                # Recover a completed save if the prior process stopped before recording its receipt.
                _, index = library.read_index()
                saved = next(
                    (path for path in index if Path(path).stem == item["id"]
                     and provenance in library.read_markdown(library.root / path).splitlines()),
                    None,
                )
                if not saved:
                    saved = library.save_topic("", "", draft["layer"], draft["title"], draft["description"], body, slug=item["id"])
                memory_import.mark_approved(library.root, client, item, saved)
            target = query(topic=saved, notice="memory_import_approved") + "#detail-title"
            self.respond(HTTPStatus.SEE_OTHER, "", location=target)
        except (ValueError, OSError) as exc:
            self.page(HTTPStatus.CONFLICT, view="memory_import", topic_path=selected, error=str(exc), import_draft=draft, import_client=client)

    def start_sync_action(self, key, read_only, tab, return_view):
        library = self.server.libraries[key]
        cfg = library.sync_config or {}
        remote = cfg.get("remote") or "origin"
        remote_url = sync_git.git_status(library.root, remote=remote).get("remote_url") or ""
        interactive = sync_git.github_browser_login(remote_url)

        def perform():
            if read_only:
                sync_git.test_remote_access(library.root, remote, browser_login=interactive)
                return query(library=key, view="sync_hub", notice="remote_readable")
            result = sync_git.sync(library.root, remote=remote, branch=cfg.get("branch") or "", mode=library.mode)
            if result.get("status") == "needs_manual_merge":
                return query(library=key, view="conflicts", notice="synced_conflicts")
            return query(library=key, tab=tab, view=return_view, notice="synced")

        self.run_sync_job(key, "验证连接" if read_only else "同步", interactive, perform)

    def wants_inline_sync(self):
        return self.path in {"/sync", "/sync_setup"} and self.headers.get("Accept") == "application/json"

    def respond_json(self, status, data):
        self.respond(status, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")

    def run_sync_job(self, key, action, interactive, perform):
        inline = self.wants_inline_sync()
        jobs = self.server.sync_jobs
        with self._sync_jobs_lock():
            now = time.time()
            for old_key, old_job in list(jobs.items()):
                if (
                    old_job.outcome.done()
                    and old_job.created_at
                    and now - old_job.created_at > self.SYNC_JOB_TTL_SECONDS
                ):
                    jobs.pop(old_key, None)
            previous = jobs.get(key)
            if previous and not previous.outcome.done():
                if previous.action != action:
                    raise InvalidEntry("这个库正在验证连接或同步，请待当前操作完成后重试。")
                job = replace(previous, inline=True) if inline else previous
                jobs[key] = job
                start_thread = False
                run_direct = False
            elif not interactive and not inline:
                job = None
                start_thread = False
                run_direct = True
            else:
                job = SyncJob(secrets.token_hex(16), Future(), action, interactive, inline, now)
                jobs[key] = job
                start_thread = True
                run_direct = False
        if run_direct:
            self.respond(HTTPStatus.SEE_OTHER, "", location=perform() + "#feedback")
            return
        if start_thread:
            def run():
                try:
                    job.outcome.set_result(perform())
                except Exception as exc:
                    job.outcome.set_exception(exc)

            threading.Thread(target=run, daemon=True, name="library-sync").start()
        if inline:
            self.respond_json(HTTPStatus.ACCEPTED, {"id": job.identifier})
        else:
            self.respond(HTTPStatus.SEE_OTHER, "", location="/sync_progress?job=" + job.identifier)

    def show_inline_sync_jobs(self):
        jobs = []
        for key, job in self._sync_jobs_snapshot():
            if not job.inline:
                continue
            library = self.server.libraries.get(key)
            destination = query(library=key if library else "primary", view="sync_hub")
            status = {"id": job.identifier, "label": library.label if library else "记忆库"}
            if not job.outcome.done():
                message = "如果浏览器打开了 GitHub 授权页，请在浏览器中完成。" if job.interactive else "可以继续使用记忆库。"
                status.update(state="running", title="正在同步", message=message)
            elif job.outcome.exception() is not None:
                status.update(state="error", title="同步未完成", message=str(job.outcome.exception()),
                              link=destination, link_text="返回同步")
            else:
                result = job.outcome.result()
                if parse_qs(urlsplit(result).query).get("notice") == ["synced_conflicts"]:
                    status.update(state="conflict", title="同步有冲突", link=result + "#feedback", link_text="处理冲突")
                else:
                    status.update(state="success", title="同步完成", link=destination, link_text="查看同步")
            jobs.append(status)
        self.respond_json(HTTPStatus.OK, jobs)

    def handle_quick_search(self, params):
        q = (params.get("q", [""])[0] or "").strip()[:120]
        active_key = params.get("library", ["primary"])[0]
        if active_key not in self.server.libraries:
            active_key = "primary"
        items = []
        active_lib = self.server.libraries[active_key]
        if not q:
            # Show the most recently updated items across mounted libraries when empty.
            recent = []
            for lib_key, lib in self.server.libraries.items():
                try:
                    store = mcp_server.MemoryStore(lib.root, label=lib.label, allowed_layers=lib.allowed_layers)
                    for entry in store.list_entries(include_archived=False, sort="updated")[:8]:
                        recent.append((entry.get("updated_at") or "", lib_key, lib.label, entry))
                except OSError:
                    continue
            recent.sort(key=lambda row: row[0], reverse=True)
            for _, lib_key, lib_label, entry in recent[:10]:
                ref = entry.get("ref", "")
                if ref.startswith("user-managed/") and ref.endswith(".md"):
                    href = query(library=lib_key, entry=Path(ref).stem) + "#detail-title"
                else:
                    href = query(library=lib_key, topic=ref) + "#detail-title"
                items.append(
                    {
                        "title": entry.get("title") or ref,
                        "description": entry.get("description") or "",
                        "layer": entry.get("layer") or "",
                        "library": lib_label,
                        "ref": ref,
                        "href": href,
                        "expired": bool(entry.get("expired")),
                    }
                )
        else:
            scored = []
            for lib_key, lib in self.server.libraries.items():
                try:
                    store = mcp_server.MemoryStore(lib.root, label=lib.label, allowed_layers=lib.allowed_layers)
                    for hit in store.search(q, limit=8, include_archived=True):
                        scored.append((hit.get("score", 0), hit.get("updated_at") or "", lib_key, lib.label, hit))
                except OSError:
                    continue
            # Sort by descending score, breaking ties by descending updated_at.
            scored.sort(key=lambda row: row[1], reverse=True)
            scored.sort(key=lambda row: row[0], reverse=True)
            for _, _, lib_key, lib_label, hit in scored[:12]:
                ref = hit.get("ref", "")
                if ref.startswith("user-managed/") and ref.endswith(".md"):
                    href = query(library=lib_key, entry=Path(ref).stem) + "#detail-title"
                else:
                    href = query(library=lib_key, topic=ref) + "#detail-title"
                items.append(
                    {
                        "title": hit.get("title") or ref,
                        "description": hit.get("description") or hit.get("snippet") or "",
                        "layer": hit.get("layer") or "",
                        "library": lib_label,
                        "ref": ref,
                        "href": href,
                        "expired": bool(hit.get("expired")),
                    }
                )
        actions = []
        if not active_lib.is_readonly:
            actions.append({"title": "新建记忆", "subtitle": f"写入 {active_lib.label}", "href": query(library=active_key, view="new") + "#detail-title", "shortcut": "⌘N"})
        if DESKTOP:
            actions.append({"title": "读写动态与体检", "subtitle": "查看 AI 调用、7 天趋势与一键修复", "href": query(library=active_key, view="activity")})
        actions.extend(
            [
                {"title": "客户端接入", "subtitle": "管理 Claude / Codex / Cursor / Claude Desktop", "href": query(library=active_key, view="clients")},
                {"title": "共享与 Git 同步", "subtitle": "查看个人同步与团队共享记忆库", "href": query(library=active_key, view="sync_hub")},
                {"title": "Skills 技能中心", "subtitle": "启用或停用跨客户端共享技能", "href": query(library=active_key, tab="skills")},
                {"title": "查看 MEMORY.md 规范与索引", "subtitle": active_lib.label, "href": query(library=active_key, view="index") + "#detail-title"},
            ]
        )
        if q:
            ql = q.casefold()
            actions = [a for a in actions if ql in a["title"].casefold() or ql in a["subtitle"].casefold()]
        self.respond_json(HTTPStatus.OK, {"query": q, "library": active_key, "items": items, "actions": actions})

    def show_sync_progress(self, job_id):
        for key, job in self._sync_jobs_snapshot():
            if job.identifier != job_id:
                continue
            if not job.outcome.done():
                # A POST redirect inherits the form's #feedback. Poll a fragment-free URL
                # so Chromium requests the job result instead of scrolling the same page.
                progress_url = "/sync_progress?" + urlencode({"job": job.identifier})
                title = escape(f"正在{job.action}")
                back_url = escape(query(library=key if key in self.server.libraries else "primary", view="sync_hub"))
                content = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="2;url={progress_url}">
  <link rel="stylesheet" href="/style.css">
  <title>{title} · Unified Memory</title>
</head>
<body>
  <main class="sync-progress">
    <section class="sync-progress-card" aria-labelledby="sync-progress-title">
      <header class="sync-progress-header">
        <span class="sync-progress-icon" aria-hidden="true"><svg viewBox="0 0 24 24">{NAV_ICONS['sync']}</svg></span>
        <div>
          <h1 id="sync-progress-title">{title}</h1>
          <p>完成后会自动显示结果。</p>
        </div>
      </header>
      <div class="sync-progress-track" role="progressbar" aria-labelledby="sync-progress-title"></div>
      <aside class="sync-progress-help" aria-labelledby="sync-auth-title">
        <h2 id="sync-auth-title">GitHub 授权</h2>
        <p>如果浏览器打开了 GitHub 授权页，请完成授权后回到这里。</p>
      </aside>
      <div class="sync-progress-actions">
        <a class="btn btn-ghost" href="{progress_url}">查看最新状态</a>
        <a class="btn btn-secondary" href="{back_url}">返回共享与同步</a>
      </div>
    </section>
  </main>
</body>
</html>"""
                self.respond(HTTPStatus.OK, content)
            else:
                try:
                    location = job.outcome.result()
                except Exception as exc:
                    self.page(HTTPStatus.CONFLICT, key=key if key in self.server.libraries else "primary",
                              view="sync_hub", error=f"操作未完成：{exc}")
                else:
                    self.respond(HTTPStatus.SEE_OTHER, "", location=location + "#feedback")
            return
        self.respond(HTTPStatus.SEE_OTHER, "", location=query(view="sync_hub"))

    def handle_control_action(self, fields):
        import memoryctl

        key = fields.get("library", "primary")
        if key not in self.server.libraries:
            key = "primary"
        return_tab = "skills" if fields.get("tab") == "skills" or self.path == "/skill" else ""
        agent_home = getattr(self.server, "agent_home", None)
        try:
            if self.path in {"/sync_setup", "/mount_add", "/mount_update", "/mount_remove"}:
                target = fields.get("target_library") or fields.get("mount_id")
                target = "primary" if target == "personal" else target
                for job_key, job in self._sync_jobs_snapshot():
                    if not job.outcome.done() and (job.action == "挂载共享库" or job_key == target):
                        raise InvalidEntry("正在连接或同步，请完成后再修改仓库配置。")
            if self.path == "/skill":
                folder = fields.get("folder", "")
                op = fields.get("op", "")
                if op not in {"enable", "disable"}:
                    raise InvalidEntry("不支持的技能操作。")
                memoryctl.set_skill_enabled(folder, op == "enable", home=agent_home)
                notice = "skill_enabled" if op == "enable" else "skill_disabled"
                loc = query(
                    library=key,
                    tab="skills",
                    skill="" if folder == "all" else folder,
                    notice=notice,
                )
                self.respond(HTTPStatus.SEE_OTHER, "", location=loc + "#feedback")
                return
            if self.path == "/rebuild_index":
                lib_obj = self.server.libraries[key]
                lib_obj._ensure_writable()
                sync_git.rebuild_index(lib_obj.root)
                return_view = fields.get("return_view", "")
                loc = query(library=key, tab=return_tab, view=return_view, notice="index_rebuilt")
                self.respond(HTTPStatus.SEE_OTHER, "", location=loc + "#feedback")
                return
            if self.path in {"/sync", "/sync_test"}:
                self.start_sync_action(key, self.path == "/sync_test", return_tab, fields.get("return_view", ""))
                return
            if self.path == "/sync_setup":
                target_lib = fields.get("target_library", "personal").strip() or "personal"
                remote_url = fields.get("remote_url", "").strip()
                if not remote_url:
                    raise InvalidEntry("请填写 Git 仓库地址。")
                reject_masked_remote_url(remote_url)
                branch = fields.get("branch", "main").strip() or "main"
                auto_commit = fields.get("auto_commit") == "1"
                auto_push = fields.get("auto_push") == "1"
                memoryctl.setup_library_sync(
                    target_lib,
                    remote_url=remote_url,
                    branch=branch,
                    auto_commit=auto_commit,
                    auto_push=auto_push,
                    home=agent_home,
                )
                self._refresh_mounts(force=True)
                if fields.get("after_save") == "sync":
                    sync_key = "primary" if target_lib == "personal" else target_lib
                    self.start_sync_action(sync_key, False, return_tab, "sync_hub")
                    return
                loc = query(library=key, view="sync_hub", notice="sync_setup_saved")
                self.respond(HTTPStatus.SEE_OTHER, "", location=loc + "#feedback")
                return
            if self.path == "/mount_add":
                mount_id = fields.get("mount_id", "").strip()
                label = fields.get("label", "").strip()
                path_val = fields.get("path", "").strip()
                mode_val = fields.get("mode", "ro").strip()
                git_url = fields.get("git_url", "").strip()
                branch = fields.get("branch", "").strip()
                if git_url:
                    reject_masked_remote_url(git_url)
                repo_name = git_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
                if not git_url and not (path_val and mount_id):
                    raise InvalidEntry("请填写团队仓库地址。挂载本地库时，请在目录设置中填写库标识和路径。")
                if not mount_id:
                    slug = re.sub(r"[^a-z0-9_-]+", "-", repo_name.lower()).strip("-_")
                    mount_id = "team-" + (slug or "repo")[:35]
                if not path_val:
                    default_dir = Path.home() / "UnifiedMemory-Shared" / memoryctl.validate_mount_id(mount_id)
                    if default_dir.exists() and (not default_dir.is_dir() or any(default_dir.iterdir())):
                        raise InvalidEntry("默认保存目录已被使用。请在目录设置中选择一个空目录。")
                    path_val = str(default_dir)
                auto_commit = fields.get("auto_commit") == "1"
                auto_push = fields.get("auto_push") == "1"
                selected_layers = [lyr for lyr in ALL_LAYERS if fields.get(f"layer_{lyr}") == "1"]
                if not selected_layers:
                    raise InvalidEntry("请至少勾选一个允许生效的记忆层级。")
                def mount():
                    memoryctl.mount_add(mount_id, path_val, label=label or repo_name or mount_id,
                                        mode=mode_val, layers=selected_layers, git_url=git_url, branch=branch,
                                        auto_commit=auto_commit, auto_push=auto_push, home=agent_home)
                    return query(library=key, view="sync_hub", notice="mount_added")

                self.run_sync_job(mount_id, "挂载共享库", sync_git.github_browser_login(git_url), mount)
                return
            if self.path == "/mount_update":
                mount_id = fields.get("mount_id", "").strip()
                label = fields.get("label", "").strip()
                mode_val = fields.get("mode", "ro").strip()
                remote_url = fields.get("remote_url", "").strip()
                if remote_url:
                    reject_masked_remote_url(remote_url)
                branch = fields.get("branch", "main").strip()
                enabled = fields.get("enabled") == "1"
                auto_commit = fields.get("auto_commit") == "1"
                auto_push = fields.get("auto_push") == "1"
                selected_layers = [lyr for lyr in ALL_LAYERS if fields.get(f"layer_{lyr}") == "1"]
                if not selected_layers:
                    raise InvalidEntry("请至少勾选一个允许生效的记忆层级。")
                memoryctl.mount_update(
                    mount_id,
                    label=label,
                    mode=mode_val,
                    layers=selected_layers,
                    enabled=enabled,
                    remote_url=remote_url if remote_url else None,
                    branch=branch if remote_url else None,
                    auto_commit=auto_commit if remote_url else None,
                    auto_push=auto_push if remote_url else None,
                    home=agent_home,
                )
                self._refresh_mounts(force=True)
                dest_key = key if key in self.server.libraries else "primary"
                loc = query(library=dest_key, view="sync_hub", notice="mount_updated")
                self.respond(HTTPStatus.SEE_OTHER, "", location=loc + "#feedback")
                return
            if self.path == "/mount_remove":
                mount_id = fields.get("mount_id", "").strip()
                memoryctl.mount_remove(mount_id, home=agent_home)
                self._refresh_mounts(force=True)
                dest_key = key if key in self.server.libraries else "primary"
                loc = query(library=dest_key, view="sync_hub", notice="mount_removed")
                self.respond(HTTPStatus.SEE_OTHER, "", location=loc + "#feedback")
                return
            if self.path == "/resolve_conflict":
                lib_obj = self.server.libraries[key]
                lib_obj._ensure_writable()
                conflict_file = fields.get("conflict_file", "").strip()
                strategy = fields.get("strategy", "").strip()
                merged_content = fields.get("merged_content", "")
                sync_cfg = lib_obj.sync_config or {}
                res = sync_git.resolve_conflict(
                    lib_obj.root,
                    conflict_file,
                    strategy,
                    merged_content=merged_content,
                    auto_commit_enabled=bool(sync_cfg.get("driver") == "git" and sync_cfg.get("auto_commit", True)),
                    library_id=lib_obj.library_id,
                )
                next_view = "conflicts" if res.get("remaining_conflicts") else ""
                loc = query(library=key, view=next_view, notice="conflict_resolved")
                self.respond(HTTPStatus.SEE_OTHER, "", location=loc + "#feedback")
                return
            if self.path == "/share_topic":
                src_lib = self.server.libraries[key]
                topic_path = fields.get("topic", "").strip()
                target_key = fields.get("target_library", "").strip()
                target_layer = fields.get("target_layer", "").strip()
                overwrite = fields.get("overwrite") == "1"
                if target_key not in self.server.libraries:
                    raise InvalidEntry("目标共享库不存在或未启用。")
                dst_lib = self.server.libraries[target_key]
                dst_lib._ensure_writable(layer=target_layer)
                t_entry = src_lib.read_topic(topic_path)
                dst_folder = mcp_server.LAYER_DIRS[target_layer]
                dst_file = dst_lib.root / dst_folder / f"{t_entry.slug}.md"
                if dst_file.exists() and not overwrite:
                    raise InvalidEntry(
                        f"目标库「{dst_lib.label}」已存在 {dst_folder}/{t_entry.slug}.md；如需覆盖请勾选确认项。"
                    )
                dst_store = mcp_server.MemoryStore(
                    dst_lib.root,
                    library_id=dst_lib.library_id,
                    label=dst_lib.label,
                    role=dst_lib.role,
                    mode=dst_lib.mode,
                    allowed_layers=dst_lib.allowed_layers,
                    allow_user_managed=dst_lib.allow_user_managed,
                    sync_config=dst_lib.sync_config,
                    async_scheduler=getattr(self.server, "async_scheduler", None),
                )
                saved = dst_store.upsert_entry(
                    layer=target_layer,
                    name=t_entry.slug,
                    title=t_entry.title,
                    description=t_entry.description or t_entry.file_description or t_entry.title,
                    body=t_entry.body,
                    topic_type=t_entry.entry_type,
                    user_managed=False,
                )
                loc = query(library=target_key, topic=saved["path"], notice="topic_shared")
                self.respond(HTTPStatus.SEE_OTHER, "", location=loc + "#feedback")
                return
            if self.path == "/doctor":
                report = memoryctl.run_doctor(fix=True, home=agent_home)
                errs = [f["detail"] for f in report["findings"] if f["severity"] == "error"]
                if errs:
                    self.page(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        key=key,
                        tab=return_tab or "memory",
                        error="自检发现异常：" + "；".join(errs),
                    )
                    return
                notice = "doctored"
            else:
                target = fields.get("target", "all")
                op = fields.get("op", "")
                targets = memoryctl.resolve_targets(target, home=agent_home)
                if op == "connect":
                    for aid, spec in targets:
                        memoryctl.connect_agent(aid, spec)
                    notice = f"{target}_connected" if target in {"claude-desktop", "workbuddy", "deepseek"} else "connected"
                elif op == "disconnect":
                    for aid, spec in targets:
                        memoryctl.disconnect_agent(aid, spec)
                    notice = "disconnected"
                else:
                    raise InvalidEntry("不支持的控制操作。")
        except (RuntimeError, ValueError, OSError, SystemExit) as exc:
            if self.wants_inline_sync():
                self.respond_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            err_view = "clients" if self.path == "/agent" and not return_tab else ""
            err_topic = ""
            if self.path in {"/sync", "/sync_test", "/sync_setup", "/mount_add", "/mount_update", "/mount_remove"}:
                err_view = "sync_hub"
            elif self.path == "/resolve_conflict":
                err_view = "conflicts"
            elif self.path == "/share_topic":
                err_topic = fields.get("topic", "").strip()
            self.page(
                HTTPStatus.CONFLICT,
                key=key,
                tab=return_tab or "memory",
                view=err_view,
                topic_path=err_topic,
                error=f"操作未完成：{exc}",
            )
            return
        view = "clients" if self.path == "/agent" and not return_tab else ""
        location = query(library=key, tab=return_tab, view=view, notice=notice)
        self.respond(HTTPStatus.SEE_OTHER, "", location=location + "#feedback")

    def handle_save_index(self, fields):
        key = fields.get("library", "primary")
        if key not in self.server.libraries:
            self.page(HTTPStatus.BAD_REQUEST, error="记忆库不存在。")
            return
        library = self.server.libraries[key]
        body = fields.get("body", "")
        revision = fields.get("revision", "")
        try:
            library.save_index_markdown(body, revision)
        except StaleEntry:
            self.page(
                HTTPStatus.CONFLICT,
                key=key,
                view="edit_index",
                index_draft=(body, revision),
                error="保存冲突：MEMORY.md 已被其他进程更新，请先复制当前内容再刷新重开。",
            )
            return
        except InvalidEntry as exc:
            self.page(
                HTTPStatus.BAD_REQUEST,
                key=key,
                view="edit_index",
                index_draft=(body, revision),
                error=str(exc),
            )
            return
        except OSError:
            self.page(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                key=key,
                view="edit_index",
                index_draft=(body, revision),
                error="写入 MEMORY.md 失败，请检查文件权限。",
            )
            return
        loc = query(library=key, view="index", notice="index_saved")
        self.respond(HTTPStatus.SEE_OTHER, "", location=loc + "#feedback")

    def handle_archive_topic(self, fields):
        key = fields.get("library", "primary")
        if key not in self.server.libraries:
            self.page(HTTPStatus.BAD_REQUEST, error="记忆库不存在。")
            return
        library = self.server.libraries[key]
        topic_path = fields.get("topic", "")
        revision = fields.get("revision", "")
        try:
            new_rel = library.archive_topic(topic_path, revision)
        except StaleEntry:
            self.page(
                HTTPStatus.CONFLICT,
                key=key,
                topic_path=topic_path,
                error="归档冲突：该条目内容或索引刚发生变化，请刷新后再试。",
            )
            return
        except (InvalidEntry, OSError) as exc:
            self.page(HTTPStatus.BAD_REQUEST, key=key, topic_path=topic_path, error=str(exc))
            return
        loc = query(library=key, topic=new_rel, notice="topic_archived")
        self.respond(HTTPStatus.SEE_OTHER, "", location=loc + "#feedback")

    def handle_verify_topic(self, fields):
        key = fields.get("library", "primary")
        if key not in self.server.libraries:
            self.page(HTTPStatus.BAD_REQUEST, error="记忆库不存在。")
            return
        library = self.server.libraries[key]
        topic_path = fields.get("topic", "")
        revision = fields.get("revision", "")
        return_view = fields.get("return_view", "")
        try:
            new_rel = library.verify_topic(topic_path, revision)
        except StaleEntry:
            self.page(
                HTTPStatus.CONFLICT,
                key=key,
                topic_path="" if return_view else topic_path,
                view=return_view,
                error="核实冲突：该条目内容或索引刚发生变化，请刷新后再试。",
            )
            return
        except (InvalidEntry, OSError) as exc:
            self.page(
                HTTPStatus.BAD_REQUEST,
                key=key,
                topic_path="" if return_view else topic_path,
                view=return_view,
                error=str(exc),
            )
            return
        if return_view:
            loc = query(library=key, view=return_view, notice="topic_verified")
        else:
            loc = query(library=key, topic=new_rel, notice="topic_verified")
        self.respond(HTTPStatus.SEE_OTHER, "", location=loc + "#feedback")

    def handle_restore_version(self, fields):
        """Put one saved version back as the current entry."""
        key = fields.get("library", "primary")
        if key not in self.server.libraries:
            self.page(HTTPStatus.BAD_REQUEST, error="记忆库不存在。")
            return
        library = self.server.libraries[key]
        slug = fields.get("slug", "")
        topic_path = ""
        try:
            topic_path = library.restore_version(fields.get("version", ""))
        except (InvalidEntry, ValueError, OSError) as exc:
            self.page(
                HTTPStatus.BAD_REQUEST,
                key=key,
                topic_path=self._slug_topic_path(library, slug),
                error=f"恢复失败：{exc}",
            )
            return
        loc = query(library=key, topic=topic_path, notice="version_restored")
        self.respond(HTTPStatus.SEE_OTHER, "", location=loc + "#feedback")

    @staticmethod
    def _slug_topic_path(library, slug):
        """Best-effort current path of an entry slug, for error redirects."""
        if not mcp_server.NAME_RE.fullmatch(slug or ""):
            return ""
        for folder in mcp_server.LAYER_DIRS.values():
            if (library.root / folder / f"{slug}.md").is_file():
                return f"{folder}/{slug}.md"
        return ""

    def handle_delete(self, fields):
        key = fields.get("library", "primary")
        if key not in self.server.libraries:
            self.page(HTTPStatus.BAD_REQUEST, error="记忆库不存在。")
            return
        library = self.server.libraries[key]
        scope = fields.get("scope", "")
        target = fields.get("target", "")
        revision = fields.get("revision", "")
        try:
            if scope == "index":
                library.delete_topic(target, revision)
            elif scope == "user":
                library.delete_user_entry(target, revision)
            else:
                raise InvalidEntry("未知的删除目标类型。")
        except StaleEntry:
            self.page(
                HTTPStatus.CONFLICT,
                key=key,
                error="删除冲突：该记忆刚刚被修改或已不存在，请刷新后重试。",
            )
            return
        except (InvalidEntry, OSError) as exc:
            self.page(HTTPStatus.BAD_REQUEST, key=key, error=str(exc))
            return
        loc = query(library=key, notice="deleted")
        self.respond(HTTPStatus.SEE_OTHER, "", location=loc + "#feedback")

    def update_entry(self, fields):
        key = fields.get("library", "primary")
        if key not in self.server.libraries:
            self.page(HTTPStatus.BAD_REQUEST, error="记忆库不存在。")
            return
        library = self.server.libraries[key]
        saving = self.path == "/save"
        scope = fields.get("scope", "user") if saving else "user"

        if saving and scope == "index":
            orig_topic = fields.get("orig_topic", "").strip()
            revision = fields.get("revision", "")
            layer = fields.get("layer", "L1")
            title = fields.get("title", "").strip()
            description = fields.get("description", "").strip()
            body = fields.get("body", "").replace("\r\n", "\n").replace("\r", "\n")
            slug = fields.get("slug", "").strip()
            entry_type = fields.get("entry_type", "").strip()
            verified_at = fields.get("verified_at", "").strip()
            valid_until = fields.get("valid_until", "").strip()
            draft = TopicEntry(
                path=orig_topic,
                slug=Path(orig_topic).stem if orig_topic else slug,
                title=title,
                description=description,
                layer=layer if layer in LAYERS else "L1",
                entry_type=entry_type or LAYERS.get(layer, ("", "reference", ""))[1],
                verified_at=verified_at,
                valid_until=valid_until,
                body=body,
                revision=revision,
            )
            err_view = "edit_topic" if orig_topic else "new"
            try:
                saved_rel = library.save_topic(
                    orig_path=orig_topic,
                    expected_revision=revision,
                    layer=layer,
                    title=title,
                    description=description,
                    body=body,
                    slug=slug,
                    entry_type=entry_type,
                    verified_at=verified_at,
                    valid_until=valid_until,
                )
            except StaleEntry:
                self.page(
                    HTTPStatus.CONFLICT,
                    key=key,
                    view=err_view,
                    topic_path=orig_topic,
                    topic_draft=draft,
                    draft_scope="index",
                    draft_slug=slug,
                    error="保存冲突：该记忆或 MEMORY.md 索引刚刚被更新。请先复制你编辑的内容，刷新条目后再粘贴保存。",
                )
                return
            except InvalidEntry as exc:
                self.page(
                    HTTPStatus.BAD_REQUEST,
                    key=key,
                    view=err_view,
                    topic_path=orig_topic,
                    topic_draft=draft,
                    draft_scope="index",
                    draft_slug=slug,
                    error=str(exc),
                )
                return
            except OSError:
                self.page(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    key=key,
                    view=err_view,
                    topic_path=orig_topic,
                    topic_draft=draft,
                    draft_scope="index",
                    draft_slug=slug,
                    error="写入失败，请检查目录权限或磁盘空间。",
                )
                return
            loc = query(library=key, topic=saved_rel, notice="topic_saved")
            self.respond(HTTPStatus.SEE_OTHER, "", location=loc + "#feedback")
            return

        name = fields.get("name", "")
        revision = fields.get("revision", "")
        entry = Entry()
        view = "edit" if (saving and name) else ("new" if saving else "")
        if saving:
            entry = Entry(
                name=name,
                revision=revision,
                title=fields.get("title", "").strip(),
                description=fields.get("description", "").strip(),
                layer=fields.get("layer", ""),
                body=fields.get("body", "").replace("\r\n", "\n").replace("\r", "\n"),
                verified_at=fields.get("verified_at", "").strip(),
                valid_until=fields.get("valid_until", "").strip(),
            )
        try:
            current = library.read(name) if name else Entry()
            if saving:
                preserved_type = (
                    current.entry_type
                    if current.entry_type in ALLOWED_LAYER_TYPES.get(entry.layer, set())
                    else ""
                )
                entry = replace(
                    entry,
                    entry_type=preserved_type,
                    status=current.status,
                    updated_at=current.updated_at,
                )
            if current.revision != revision:
                raise StaleEntry
            if saving:
                validate_common_fields(
                    entry.layer,
                    entry.title,
                    entry.description,
                    entry.body,
                    verified_at=entry.verified_at,
                    valid_until=entry.valid_until,
                )
                new_name = fields.get("create_id", "")
                if not name and not re.fullmatch(r"entry-[a-f0-9]{20}", new_name):
                    raise InvalidEntry("无法识别新条目，请刷新页面后重试。")
                entry = replace(entry, name=name or new_name, created_at=current.created_at)
                notice = "saved"
            else:
                if not name:
                    raise InvalidEntry("请选择一条记忆。")
                state = "archived" if self.path == "/archive" else "active"
                entry = replace(current, status=state)
                notice = "archived" if state == "archived" else "restored"
            library.save(entry)
        except (StaleEntry, FileNotFoundError, FileExistsError):
            self.page(
                HTTPStatus.CONFLICT,
                entry=entry,
                key=key,
                view=view,
                draft_scope="user",
                error="保存冲突：条目已变化或表单已提交。可复制当前输入，再从列表重新打开条目。",
            )
            return
        except InvalidEntry as exc:
            if entry.layer not in LAYERS:
                entry = replace(entry, layer="L2")
            self.page(
                HTTPStatus.BAD_REQUEST,
                entry=entry,
                key=key,
                view=view,
                draft_scope="user",
                error=str(exc),
            )
            return
        except (OSError, ValueError):
            self.page(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                entry=entry,
                key=key,
                view=view,
                draft_scope="user",
                error="写入失败。请检查目录权限或 Markdown 文件格式。",
            )
            return
        location = query(library=key, entry=entry.name, status=entry.status, notice=notice)
        self.respond(HTTPStatus.SEE_OTHER, "", location=location + "#feedback")


def build_libraries_map(primary_root, also_root=None, home=None, issues=None, async_scheduler=None):
    """Build ordered mapping of key -> Library from primary root, mounts.json, and optional --also-root.

    Invalid or unavailable shared mounts are reported through ``issues`` instead of silently
    disappearing from the web console.  The personal library remains fatal because the console
    cannot operate without its primary index.
    """
    mount_issues = issues if issues is not None else []
    descriptors = resolve_libraries(primary_root=primary_root, home=home, issues=mount_issues)
    libraries = {}
    for desc in descriptors:
        key = "primary" if desc["id"] == "personal" else desc["id"]
        try:
            libraries[key] = Library(
                desc["path"],
                desc["label"],
                library_id=desc["id"],
                role=desc["role"],
                mode=desc["mode"],
                allowed_layers=desc["layers"],
                allow_user_managed=desc["allow_user_managed"],
                sync_config=desc.get("sync"),
                async_scheduler=async_scheduler,
            )
        except (InvalidEntry, ValueError, OSError) as exc:
            if key == "primary":
                raise
            reason = library_unavailable_reason(desc["path"]) or str(exc) or exc.__class__.__name__
            mount_issues.append(unavailable_issue(desc, reason))
            continue
    if also_root:
        alternate = Library(also_root, "对照记忆库", library_id="local", async_scheduler=async_scheduler)
        if all(lib.root != alternate.root for lib in libraries.values()):
            libraries["local"] = alternate
    return libraries


def main():
    global DESKTOP, PAGE_TEMPLATE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-root", type=Path, default=data_root())
    parser.add_argument("--also-root", type=Path, help="同一页面可切换到的本机记忆库")
    parser.add_argument("--port", type=int, default=8765, help="端口，0 表示自动选择")
    parser.add_argument("--desktop", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.desktop:
        DESKTOP = True
        PAGE_TEMPLATE = Template((APP / "desktop.html").read_text(encoding="utf-8"))
        os.environ["UNIFIED_MEMORY_ROOT"] = str(data_root(args.memory_root))
        import memoryctl
        memoryctl.initialize_library()
        memoryctl.ensure_stable_shims()  # keep client launchers pointing at this app's runtime
    try:
        primary = data_root(args.memory_root).resolve(strict=True)
        os.environ["UNIFIED_MEMORY_ROOT"] = str(primary)
        initial_config_signature = config_signature()
        mount_issues = []
        async_scheduler = AsyncSyncScheduler()
        libraries = build_libraries_map(
            primary,
            also_root=args.also_root,
            issues=mount_issues,
            async_scheduler=async_scheduler,
        )
        server = HTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            parser.exit(
                2,
                f"端口 127.0.0.1:{args.port} 已在运行服务，可直接访问 http://127.0.0.1:{args.port}/ "
                f"（如需另起实例可加 --port 8766）。\n",
            )
        parser.exit(2, f"{exc}\n")
    except ValueError as exc:
        parser.exit(2, f"{exc}\n")
    with server:
        server.libraries = libraries
        server.async_scheduler = async_scheduler
        server.mount_issues = mount_issues
        server.sync_jobs = {}
        server.sync_jobs_lock = threading.RLock()
        server.token = secrets.token_urlsafe(32)
        server.agent_home = None
        # Capture the pre-build signature so a config rewrite during library construction
        # is observed on the first request instead of being mistaken for the initial state.
        state_holder = {"signature": initial_config_signature}

        def reload_mounts(force=False):
            cur_signature = config_signature()
            if force or cur_signature != state_holder["signature"]:
                issues = []
                server.libraries = build_libraries_map(
                    primary,
                    also_root=args.also_root,
                    issues=issues,
                    async_scheduler=server.async_scheduler,
                )
                server.mount_issues = issues
                state_holder["signature"] = cur_signature

        if DESKTOP:
            # The native window owns the write end. EOF also covers a parent crash.
            def watch_parent():
                sys.stdin.buffer.read()
                server.shutdown()
            threading.Thread(target=watch_parent, daemon=True).start()
        server.reload_mounts = reload_mounts
        print(f"http://127.0.0.1:{server.server_port}/", flush=True)
        for library in libraries.values():
            print(f"{library.label}: {library.index}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.async_scheduler.shutdown()


if __name__ == "__main__":
    main()
