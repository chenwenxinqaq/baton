#!/usr/bin/env python3
"""Unified Memory & Skills Web Console: browse, create, and edit memories, manage skills, and control agents."""

import argparse
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime
import difflib
import errno
from hashlib import sha256
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import stat
from string import Template
import sys
import time
import threading
from urllib.parse import parse_qs, unquote, urlencode, urlsplit


APP = Path(__file__).resolve().parent
ROOT_DIR = APP.parent
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
if str(ROOT_DIR / "mcp") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "mcp"))
if str(ROOT_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "scripts"))

from library_paths import (  # noqa: E402
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
import upkeep  # noqa: E402
from async_sync import AsyncSyncScheduler, record_external_result  # noqa: E402
from markdown_render import (  # noqa: E402
    INLINE_TOKEN_RE,
    ORDERED_ITEM_RE,
    TABLE_SEP_RE,
    UNORDERED_ITEM_RE,
    _resolve_internal_md_url,
    format_inline_md,
    hidden,
    query,
    render_markdown_html,
    render_md_link,
    split_table_row,
)
import views as manager_views  # noqa: E402
from views import (  # noqa: E402
    CUR_ATTR,
    IMPORT_INSTRUCTIONS,
    LAYERS,
    LAYER_SHORT,
    MAX_BODY_CHARS,
    MAX_DESC_CHARS,
    MAX_TITLE_CHARS,
    NAV_ICONS,
    SKILL_CATS,
    STATES,
    VERSION_REASONS,
    _LINT_CHECK_LABELS,
    _TOOL_LABELS,
    _ago_zh,
    _daily_trend_section,
    _lint_edit_href,
    _lint_section,
    _ref_link,
    _usage_section,
    configure_ui,
    desktop_navigation,
    desktop_results,
    desktop_status_strip,
    display_remote_url,
    entry_link,
    filter_and_score,
    filter_skills,
    format_comp_chip,
    library_nav_html,
    load_script,
    load_style,
    load_template,
    query_plan,
    render,
    render_activity_panel,
    render_agent_panel,
    render_card_time_html,
    render_conflicts_view_html,
    render_delete_dialog_html,
    render_detail_timestamps_html,
    render_index_editor_html,
    render_l0_review,
    render_layer_checkboxes,
    render_layer_radios,
    render_library_sync_bar,
    render_memory_editor_html,
    render_memory_import,
    render_native_import_button,
    render_overview,
    render_scope_radios,
    render_share_dialog_html,
    render_skills_overview,
    render_skills_tab,
    render_sync_hub_html,
    render_topic_links_html,
    render_topic_versions_html,
    render_upkeep_panel,
    skill_entry_link,
    top_hit_citations,
    topic_read_error,
    topic_search_fields,
)

ALLOWED_LAYER_TYPES = {
    "L0": {"feedback", "reference"},
    "L1": {"reference"},
    "L2": {"feedback"},
    "L3": {"project"},
    "L4": {"reference"},
}
VALID_TOPIC_TYPES = {"user", "feedback", "project", "reference"}
SOURCES = {"all": "全部来源", "index": "现有记忆", "user": "用户管理"}
ENTRY_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}")
TOPIC_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
MASKED_REMOTE_URL_RE = re.compile(r"^(?:https?|ssh)://\*\*\*@", re.IGNORECASE)
ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+")
INDEX_LAYER = re.compile(r"^## (L[0-4])(?:\s|$)")
INDEX_LINK = re.compile(
    r"^\s*[-*+]\s+\[(.+?)\]\((?:<([^>]+)>|([^\s()]+))\)(?:\s+[—–-]\s+(.+))?$"
)
IMPORT_PROVENANCE_RE = re.compile(r"^> 来源：.+，原日期 .+$")

MAX_POST = 1_200_000

DESKTOP = False
PAGE_TEMPLATE = manager_views.PAGE_TEMPLATE


@dataclass(frozen=True)
class SyncJob:
    identifier: str
    outcome: Future
    action: str
    interactive: bool
    inline: bool
    created_at: float = 0.0


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
    verified_at: str = ""
    valid_until: str = ""


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
        return mcp_server.format_user_markdown(
            self.name,
            self.title,
            self.description,
            self.resolved_type(),
            self.layer,
            self.status,
            self.created_at or self.updated_at,
            self.updated_at,
            self.body,
            verified_at=self.verified_at,
            valid_until=self.valid_until,
        )

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
        self.store = mcp_server.MemoryStore(
            root,
            library_id=library_id,
            label=label,
            role=role,
            mode=mode,
            allowed_layers=self.allowed_layers,
            allow_user_managed=self.allow_user_managed,
            sync_config=sync_config,
            async_scheduler=async_scheduler,
            audit_source="manager",
            audit_client="Unified Memory Web",
            log_sync_warnings=True,
        )
        # The web UI keeps the primary write API stable (save_topic still returns a path), while
        # retaining the latest non-fatal Git snapshot outcome for notices and diagnostics.

    @property
    def last_sync_status(self):
        return self.store.last_sync_status

    @last_sync_status.setter
    def last_sync_status(self, value):
        self.store.last_sync_status = value

    def _enqueue_async_sync(self, message):
        """Queue a debounced remote Git sync via the shared MemoryStore."""
        self.store.sync_config = self.sync_config
        self.store.async_scheduler = self.async_scheduler
        return self.store._enqueue_async_sync(message)

    def async_sync_status(self):
        """Return the latest persisted/in-process asynchronous sync status."""
        self.store.async_scheduler = self.async_scheduler
        return self.store.async_sync_status()

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
        """Auto-commit to Git via the shared MemoryStore."""
        self.store.sync_config = self.sync_config
        return self.store._maybe_auto_commit(message)

    @contextmanager
    def write_lock(self):
        """Serialize concurrent writes with MCP servers."""
        with self.store.write_lock():
            yield

    def _backup_to_trash(self, src_path, slug_hint):
        """Save a safety backup copy into .state/trash/ before deleting a memory."""
        return self.store._backup_to_trash(src_path, slug_hint)

    def topic_versions(self, slug, limit=8):
        """List saved previous versions of one entry.

        The MCP server and this UI share one trash directory per library
        (``library_state_dir``), so both see every backup the other made.
        """
        if not mcp_server.NAME_RE.fullmatch(slug or ""):
            return []
        try:
            return self.store.entry_history(slug, limit=limit).get("versions", [])
        except (ValueError, OSError):
            return []

    def topic_links(self, topic_path, body):
        """Return (outgoing links, incoming backlinks) for one topic."""
        try:
            return self.store.entry_links(topic_path, body)
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
        self.store.sync_config = self.sync_config
        self.store.async_scheduler = self.async_scheduler
        # restore_entry takes the shared write lock itself, so do not wrap it.
        result = self.store.restore_entry(version)
        return result.get("path", "")

    def version_diff(self, version):
        """Return a unified diff between one saved backup version and the live entry."""
        preview = self.store.restore_entry(version, dry_run=True)
        _, backup_body = mcp_server.parse_frontmatter(preview.get("content") or "")
        backup_lines = backup_body.strip().splitlines()
        current_rel = preview.get("would_replace") or ""
        current_body = ""
        if current_rel:
            cur_path = (self.root / current_rel).resolve()
            if cur_path.is_relative_to(self.root) and cur_path.is_file():
                _, current_body = mcp_server.parse_frontmatter(cur_path.read_text(encoding="utf-8", errors="replace"))
        current_lines = current_body.strip().splitlines()
        diff_lines = list(
            difflib.unified_diff(
                backup_lines,
                current_lines,
                fromfile=f"历史备份 ({preview.get('version') or version})",
                tofile=f"当前版本 ({current_rel or preview.get('name') or ''})",
                lineterm="",
                n=3,
            )
        )
        added = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))
        max_lines = 300
        return {
            "version": preview.get("version") or version,
            "title": preview.get("title") or "",
            "layer": preview.get("layer") or "",
            "chars": preview.get("chars") or len(backup_body),
            "verified_at": preview.get("verified_at") or "",
            "valid_until": preview.get("valid_until") or "",
            "added": added,
            "removed": removed,
            "identical": not diff_lines,
            "diff_lines": diff_lines[:max_lines],
            "truncated": len(diff_lines) > max_lines,
        }

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
                    meta, _ = mcp_server.load_entry(md_file)
                    cre_at, upd_at, cre_ep, upd_ep = mcp_server.file_timestamps(md_file, meta)
                    enriched[rel_path] = replace(
                        t,
                        created_at=cre_at,
                        updated_at=upd_at,
                        created_epoch=cre_ep,
                        updated_epoch=upd_ep,
                        verified_at=str(meta.get("verified_at") or "").strip(),
                        valid_until=str(meta.get("valid_until") or "").strip(),
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

        def verify_revision():
            cur_rev = sha256(self.index.read_bytes()).hexdigest()
            if cur_rev != expected_revision:
                raise StaleEntry

        self.store.sync_config = self.sync_config
        self.store.async_scheduler = self.async_scheduler
        self.store.save_index_text(normalized, before_write=verify_revision)

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

    def discard_unindexed_topic(self, slug):
        """Move aside a topic file that a stopped save left on disk without an index line.

        ``save_topic`` writes the file before the index, so a process that dies in
        between leaves a file that every later save with the same slug collides with.
        The file goes to the trash backups first, so nothing is lost.
        """
        return self.store.discard_unindexed_topic(slug)

    def save_topic(self, orig_path, expected_revision, layer, title, description, body, slug="", entry_type="",
                   verified_at=None, valid_until=None, body_limit=MAX_BODY_CHARS):
        self._ensure_writable(layer=layer)
        validate_common_fields(layer, title, description, body, verified_at=verified_at, valid_until=valid_until,
                               body_limit=body_limit)
        norm_verified = None if verified_at is None else str(verified_at).strip()
        norm_valid_until = None if valid_until is None else str(valid_until).strip()

        def prepare_topic():
            if orig_path:
                _, current_topics = self.read_index()
                try:
                    cur = self.read_topic(orig_path, current_topics)
                except (FileNotFoundError, InvalidEntry) as exc:
                    raise StaleEntry from exc
                if cur.revision != expected_revision:
                    raise StaleEntry

                stem = cur.slug
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

                eff_verified = cur.verified_at if norm_verified is None else norm_verified
                eff_valid_until = cur.valid_until if norm_valid_until is None else norm_valid_until
                if eff_verified and eff_valid_until and eff_valid_until < eff_verified:
                    raise InvalidEntry("有效期截止日期不能早于核实日期。")
                return {
                    "orig_rel": cur.path,
                    "slug": stem,
                    "entry_type": fm_type,
                    "frontmatter_name": fm_name,
                    "file_description": fm_desc,
                    "created_at": cur.created_at,
                    "previous_updated_at": cur.updated_at,
                    "verified_at": eff_verified,
                    "valid_until": eff_valid_until,
                }

            final_slug = self._generate_unique_slug(title, slug)
            fm_type = entry_type if entry_type in VALID_TOPIC_TYPES else LAYERS[layer][1]
            return {
                "orig_rel": "",
                "slug": final_slug,
                "entry_type": fm_type,
                "frontmatter_name": final_slug,
                "file_description": None,
                "created_at": "",
                "previous_updated_at": "",
                "verified_at": norm_verified or "",
                "valid_until": norm_valid_until or "",
            }

        self.store.sync_config = self.sync_config
        self.store.async_scheduler = self.async_scheduler
        try:
            return self.store.save_topic_entry(
                layer=layer,
                title=title,
                description=description,
                body=body,
                before_write=prepare_topic,
            )
        except InvalidEntry:
            raise
        except ValueError as exc:
            raise InvalidEntry(str(exc)) from exc

    def archive_topic(self, topic_path, expected_revision):
        self._ensure_writable(layer="L4")

        def prepare_archive():
            _, current_topics = self.read_index()
            try:
                cur = self.read_topic(topic_path, current_topics)
            except (FileNotFoundError, InvalidEntry) as exc:
                raise StaleEntry from exc
            if cur.revision != expected_revision:
                raise StaleEntry
            if cur.layer == "L4":
                return {"already_archived": True, "orig_rel": cur.path, "slug": cur.slug}
            return {"orig_rel": cur.path, "slug": cur.slug}

        self.store.sync_config = self.sync_config
        self.store.async_scheduler = self.async_scheduler
        try:
            result = self.store.archive_entry(before_archive=prepare_archive)
        except InvalidEntry:
            raise
        except ValueError as exc:
            msg = str(exc)
            if msg.startswith("Archive file already exists: "):
                raise InvalidEntry(f"归档目录已存在同名文件：{msg.split(': ', 1)[1]}") from exc
            raise InvalidEntry(msg) from exc
        return result.get("to", "")

    def delete_topic(self, topic_path, expected_revision):
        self._ensure_writable()

        def prepare_delete():
            _, current_topics = self.read_index()
            try:
                cur = self.read_topic(topic_path, current_topics)
            except (FileNotFoundError, InvalidEntry) as exc:
                raise StaleEntry from exc
            if cur.revision != expected_revision:
                raise StaleEntry
            return {"rel_path": cur.path, "slug_hint": cur.slug}

        self.store.sync_config = self.sync_config
        self.store.async_scheduler = self.async_scheduler
        try:
            self.store.delete_entry(user_managed=False, before_delete=prepare_delete)
        except InvalidEntry:
            raise
        except ValueError as exc:
            raise InvalidEntry(str(exc)) from exc

    def path(self, name):
        if not self.allow_user_managed:
            raise InvalidEntry("共享记忆库不支持个人手记。")
        if not ENTRY_NAME.fullmatch(name) or name == "readme":
            raise InvalidEntry("条目名称无效。")
        return self.folder / f"{name}.md"

    def read(self, name):
        path = self.path(name)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
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
        self.path(entry.name)

        def prepare_user():
            if entry.revision:
                try:
                    current = self.read(entry.name)
                except (FileNotFoundError, ValueError) as exc:
                    raise StaleEntry from exc
                if entry.revision != current.revision:
                    raise StaleEntry
                return {
                    "previous_updated_at": current.updated_at,
                    "created_at": entry.created_at or current.created_at,
                }
            return {"previous_updated_at": "", "created_at": entry.created_at}

        self.store.sync_config = self.sync_config
        self.store.async_scheduler = self.async_scheduler
        try:
            self.store.save_user_entry(
                name=entry.name,
                title=entry.title,
                description=entry.description,
                body=entry.body,
                layer=entry.layer,
                entry_type=entry.resolved_type(),
                status=entry.status,
                created_at=entry.created_at,
                verified_at=entry.verified_at,
                valid_until=entry.valid_until,
                exclusive_create=not entry.revision,
                before_write=prepare_user,
            )
        except InvalidEntry:
            raise
        except ValueError as exc:
            raise InvalidEntry(str(exc)) from exc

    def delete_user_entry(self, name, expected_revision):
        self._ensure_writable(user_managed=True)
        self.path(name)

        def prepare_delete():
            try:
                current = self.read(name)
            except (FileNotFoundError, ValueError) as exc:
                raise StaleEntry from exc
            if current.revision != expected_revision:
                raise StaleEntry
            return {"rel_path": f"user-managed/{name}.md", "slug_hint": name}

        self.store.sync_config = self.sync_config
        self.store.async_scheduler = self.async_scheduler
        try:
            self.store.delete_entry(
                rel_path=f"user-managed/{name}.md",
                user_managed=True,
                slug_hint=name,
                before_delete=prepare_delete,
            )
        except InvalidEntry:
            raise
        except ValueError as exc:
            raise InvalidEntry(str(exc)) from exc


def validate_common_fields(layer, title, description, body, verified_at=None, valid_until=None,
                           body_limit=MAX_BODY_CHARS):
    if layer not in LAYERS:
        raise InvalidEntry("请选择 L0–L4 中的一层。")
    if not title or len(title) > MAX_TITLE_CHARS or len(title.splitlines()) != 1:
        raise InvalidEntry(f"标题须为单行且长度为 1–{MAX_TITLE_CHARS} 个字符。")
    if "](" in title:
        raise InvalidEntry("标题中不能包含 Markdown 链接分隔符 ']('。")
    if len(description) > MAX_DESC_CHARS or len(description.splitlines()) > 1:
        raise InvalidEntry(f"检索摘要须为单行且不超过 {MAX_DESC_CHARS} 个字符。")
    if not body.strip() or len(body) > body_limit:
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


def _strip_import_provenance(body):
    lines = body.rstrip().split("\n")
    while lines and IMPORT_PROVENANCE_RE.match(lines[-1].strip()):
        lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()
    cleaned = "\n".join(lines)
    return cleaned if cleaned.strip() else body.strip()


def approve_import(library, client, import_id, revision, draft, merge_target=""):
    """File one reviewed candidate into the library, record its receipt, return the topic path."""
    with memory_import.review_lock(library.root, client):
        item = memory_import.read_candidate(library.root, client, import_id)
        if revision != memory_import.revision(item):
            raise InvalidEntry("导入原文已更新，请重新核对。")
        # Browsers submit textarea content with CRLF line endings.
        normalized_body = _strip_import_provenance(draft["body"].replace("\r\n", "\n").replace("\r", "\n"))
        merge_target = str(merge_target or "").strip()
        original_date = item["source_date"] or "未知"
        session = f'，会话 {item["thread_id"]}' if item["thread_id"] else ""
        provenance = f'> 来源：{item["source"]}，原日期 {original_date}{session}'
        # Recover a completed save if the prior process stopped before recording its receipt.
        _, index = library.read_index()
        if merge_target:
            if merge_target not in index:
                raise InvalidEntry("所选合并目标已不在索引中，请刷新后重试。")
            current = library.read_topic(merge_target, index)
            validate_common_fields(
                current.layer,
                current.title,
                current.description,
                normalized_body,
                body_limit=MAX_BODY_CHARS,
            )
            merged_body = current.body
            if provenance not in merged_body.splitlines():
                addition = f"## 导入补充 · {draft['title'] or item['title']}\n\n{normalized_body}\n\n{provenance}"
                merged_body = f"{merged_body.rstrip()}\n\n{addition}"
            saved = library.save_topic(
                current.path,
                current.revision,
                current.layer,
                current.title,
                current.description,
                merged_body,
                entry_type=current.entry_type,
                verified_at=current.verified_at,
                valid_until=current.valid_until,
                body_limit=max(MAX_BODY_CHARS, len(merged_body) + 16),
            )
            memory_import.mark_approved(library.root, client, item, saved)
            return saved
        draft = dict(draft, body=normalized_body)
        validate_common_fields(**draft)
        body = f'{draft["body"]}\n\n{provenance}'
        saved = next(
            (path for path in index if Path(path).stem == item["id"]
             and provenance in library.read_markdown(library.root / path).splitlines()),
            None,
        )
        prior = item.get("previously_approved")
        if not saved and prior in index:
            # The source changed after it was filed: update that entry instead of forking it.
            current = library.read_topic(prior, index)
            saved = library.save_topic(prior, current.revision, draft["layer"], draft["title"], draft["description"],
                                       body, body_limit=MAX_BODY_CHARS + len(provenance) + 2)
        if not saved:
            # The same stop can also leave the file without its index line.
            library.discard_unindexed_topic(item["id"])
            # The draft was checked against the normal limit; the source line is added on top of it.
            saved = library.save_topic("", "", draft["layer"], draft["title"], draft["description"], body,
                                       slug=item["id"], body_limit=MAX_BODY_CHARS + len(provenance) + 2)
        memory_import.mark_approved(library.root, client, item, saved)
    return saved


def reject_masked_remote_url(remote_url):
    """Do not persist the UI's redacted placeholder as a real Git remote."""
    if MASKED_REMOTE_URL_RE.match(str(remote_url or "").strip()):
        raise InvalidEntry("远端地址中的凭据已隐藏，请重新填写不带 token 的仓库地址。")


class ManagerServer(ThreadingHTTPServer):
    """Serve each request on its own thread so a slow one cannot stall the rest.

    Library writes are serialized by the per-library file lock, the sync job
    registry and mount reloads by their own locks.
    """

    # Let requests that are still writing finish before the process exits.
    daemon_threads = False
    block_on_close = True


class Handler(BaseHTTPRequestHandler):
    # Request threads and sync workers observe this registry concurrently.
    SYNC_JOB_TTL_SECONDS = 15 * 60

    @property
    def libraries(self):
        """The mounted libraries as this request first saw them.

        Another request may reload the mounts at any time; one request must not
        see two different maps.
        """
        snapshot = self.__dict__.get("_libraries")
        if snapshot is None:
            snapshot = self.__dict__["_libraries"] = self.server.libraries
        return snapshot

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
                self.libraries,
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
        self.__dict__.pop("_libraries", None)

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
        if url.path == "/version_diff":
            self.handle_version_diff(parse_qs(url.query, max_num_fields=6))
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
            if requested_key not in self.libraries:
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
                or view not in ({"", "new", "edit", "edit_topic", "index", "edit_index", "sync_hub", "conflicts", "clients", "memory_import", "l0_review", "upkeep"}
                                | ({"activity"} if DESKTOP else set()))
                or (view == "memory_import" and (key != "primary" or tab != "memory" or import_client not in memory_import.CLIENTS))
                or (view == "l0_review" and (key != "primary" or tab != "memory"))
            ):
                raise InvalidEntry("筛选条件无效。")
            name = params.get("entry", [""])[0]
            library = self.libraries[key]
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
            "l0_approved": "L0 操作约束已确认生效并写入 constraints/。",
            "l0_rejected": "已拒绝并丢弃该条 L0 约束提案，现有约束未改动。",
            "upkeep_applied": "整理建议已应用（原内容已在本机回收站保留历史版本备份）。",
            "upkeep_dismissed": "已忽略该条整理建议（条目内容变化后会重新评估）。",
            "upkeep_reset": "已恢复所有被忽略的整理建议。",
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
            "/preview_markdown",
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
            "/l0_review",
            "/upkeep",
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
        if self.path == "/l0_review":
            self.handle_l0_review(fields)
            return
        if self.path == "/upkeep":
            self.handle_upkeep(fields)
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
        if self.path == "/preview_markdown":
            self.handle_preview_markdown(fields)
            return
        if self.path == "/restore_version":
            self.handle_restore_version(fields)
            return
        if self.path == "/delete":
            self.handle_delete(fields)
            return
        self.update_entry(fields)

    def handle_memory_import(self, fields):
        library = self.libraries["primary"]
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
            draft = {name: fields.get(name, "").strip() for name in ("layer", "title", "description", "body", "merge_target")}
            saved = approve_import(
                library,
                client,
                selected,
                fields.get("revision", ""),
                {k: v for k, v in draft.items() if k != "merge_target"},
                merge_target=draft.get("merge_target", ""),
            )
            target = query(topic=saved, notice="memory_import_approved") + "#detail-title"
            self.respond(HTTPStatus.SEE_OTHER, "", location=target)
        except (ValueError, OSError) as exc:
            self.page(HTTPStatus.CONFLICT, view="memory_import", topic_path=selected, error=str(exc), import_draft=draft, import_client=client)

    def handle_l0_review(self, fields):
        key = fields.get("library", "primary")
        if key not in self.libraries or key != "primary":
            self.page(HTTPStatus.BAD_REQUEST, error="L0 待确认区仅支持个人记忆库。")
            return
        library = self.libraries[key]
        proposal_id = fields.get("proposal_id", "").strip()
        revision = fields.get("revision", "").strip()
        op = fields.get("op", "").strip()
        try:
            if op == "approve":
                title = fields["title"].strip() if "title" in fields else None
                description = fields["description"].strip() if "description" in fields else None
                body = (
                    fields["body"].replace("\r\n", "\n").replace("\r", "\n").strip()
                    if "body" in fields
                    else None
                )
                res = library.store.approve_l0_proposal(
                    proposal_id,
                    revision=revision,
                    title=title,
                    description=description,
                    body=body,
                )
                target = query(library=key, topic=res["path"], notice="l0_approved") + "#detail-title"
                self.respond(HTTPStatus.SEE_OTHER, "", location=target)
                return
            if op == "reject":
                library.store.reject_l0_proposal(proposal_id)
                target = query(library=key, view="l0_review", notice="l0_rejected") + "#feedback"
                self.respond(HTTPStatus.SEE_OTHER, "", location=target)
                return
            raise InvalidEntry("未知的 L0 审核操作。")
        except (ValueError, OSError) as exc:
            self.page(HTTPStatus.CONFLICT, key=key, view="l0_review", error=f"操作未完成：{exc}")

    def handle_upkeep(self, fields):
        key = fields.get("library", "primary")
        if key not in self.libraries:
            self.page(HTTPStatus.BAD_REQUEST, error="记忆库不存在。")
            return
        library = self.libraries[key]
        op = fields.get("op", "").strip()
        suggestion_id = fields.get("suggestion_id", "").strip()
        fingerprint = fields.get("fingerprint", "").strip()
        agent_home = getattr(self.server, "agent_home", None)
        try:
            if op == "apply":
                upkeep.apply_suggestion(
                    library.store,
                    suggestion_id,
                    expected_fingerprint=fingerprint,
                    audit_home=agent_home,
                )
                notice = "upkeep_applied"
            elif op == "dismiss":
                upkeep.dismiss_suggestion(library.root, suggestion_id, fingerprint=fingerprint)
                notice = "upkeep_dismissed"
            elif op == "reset_dismissed":
                upkeep.reset_dismissed(library.root, suggestion_id)
                notice = "upkeep_reset"
            else:
                raise InvalidEntry("未知的整理日报操作。")
            target = query(library=key, view="upkeep", notice=notice) + "#feedback"
            self.respond(HTTPStatus.SEE_OTHER, "", location=target)
        except (ValueError, OSError) as exc:
            self.page(HTTPStatus.CONFLICT, key=key, view="upkeep", error=f"操作未完成：{exc}")

    def start_sync_action(self, key, read_only, tab, return_view):
        library = self.libraries[key]
        cfg = library.sync_config or {}
        remote = cfg.get("remote") or "origin"
        remote_url = sync_git.git_status(library.root, remote=remote).get("remote_url") or ""
        interactive = sync_git.github_browser_login(remote_url)

        def perform():
            if read_only:
                sync_git.test_remote_access(library.root, remote, browser_login=interactive)
                return query(library=key, view="sync_hub", notice="remote_readable")
            result = sync_git.sync(library.root, remote=remote, branch=cfg.get("branch") or "", mode=library.mode)
            if cfg.get("auto_push") and library.mode == "rw":
                record_external_result(library.root, result=result, reason="manual sync")
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
            library = self.libraries.get(key)
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
        if active_key not in self.libraries:
            active_key = "primary"
        items = []
        active_lib = self.libraries[active_key]
        if not q:
            # Show the most recently updated items across mounted libraries when empty.
            recent = []
            for lib_key, lib in self.libraries.items():
                try:
                    for entry in lib.store.list_entries(include_archived=False, sort="updated")[:8]:
                        recent.append((entry.get("updated_at") or "", lib_key, lib.label, entry))
                except OSError:
                    continue
            recent.sort(key=lambda row: row[0], reverse=True)
            for _, lib_key, lib_label, entry in recent[:10]:
                rel_path = entry.get("path") or entry.get("ref", "")
                if rel_path.startswith("user-managed/") and rel_path.endswith(".md"):
                    href = query(library=lib_key, entry=Path(rel_path).stem) + "#detail-title"
                else:
                    href = query(library=lib_key, topic=rel_path) + "#detail-title"
                items.append(
                    {
                        "title": entry.get("title") or rel_path,
                        "description": entry.get("description") or "",
                        "layer": entry.get("layer") or "",
                        "library": lib_label,
                        "ref": rel_path,
                        "href": href,
                        "expired": bool(entry.get("expired")),
                    }
                )
        else:
            scored = []
            for lib_key, lib in self.libraries.items():
                try:
                    for hit in lib.store.search(q, limit=8, include_archived=True):
                        scored.append((hit.get("score", 0), hit.get("updated_at") or "", lib_key, lib.label, hit))
                except OSError:
                    continue
            # Sort by descending score, breaking ties by descending updated_at.
            scored.sort(key=lambda row: row[1], reverse=True)
            scored.sort(key=lambda row: row[0], reverse=True)
            for _, _, lib_key, lib_label, hit in scored[:12]:
                rel_path = hit.get("path") or hit.get("ref", "")
                if rel_path.startswith("user-managed/") and rel_path.endswith(".md"):
                    href = query(library=lib_key, entry=Path(rel_path).stem) + "#detail-title"
                else:
                    href = query(library=lib_key, topic=rel_path) + "#detail-title"
                items.append(
                    {
                        "title": hit.get("title") or rel_path,
                        "description": hit.get("description") or hit.get("snippet") or "",
                        "layer": hit.get("layer") or "",
                        "library": lib_label,
                        "ref": rel_path,
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

    def handle_version_diff(self, params):
        key = params.get("library", ["primary"])[0]
        version = (params.get("version", [""])[0] or "").strip()
        library = self.libraries.get(key)
        if library is None:
            self.respond_json(HTTPStatus.NOT_FOUND, {"error": "记忆库不存在。"})
            return
        try:
            payload = library.version_diff(version)
        except (ValueError, OSError) as exc:
            self.respond_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self.respond_json(HTTPStatus.OK, payload)

    def show_sync_progress(self, job_id):
        for key, job in self._sync_jobs_snapshot():
            if job.identifier != job_id:
                continue
            if not job.outcome.done():
                # A POST redirect inherits the form's #feedback. Poll a fragment-free URL
                # so Chromium requests the job result instead of scrolling the same page.
                progress_url = "/sync_progress?" + urlencode({"job": job.identifier})
                title = escape(f"正在{job.action}")
                back_url = escape(query(library=key if key in self.libraries else "primary", view="sync_hub"))
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
                    self.page(HTTPStatus.CONFLICT, key=key if key in self.libraries else "primary",
                              view="sync_hub", error=f"操作未完成：{exc}")
                else:
                    self.respond(HTTPStatus.SEE_OTHER, "", location=location + "#feedback")
            return
        self.respond(HTTPStatus.SEE_OTHER, "", location=query(view="sync_hub"))

    def handle_control_action(self, fields):
        import memoryctl

        key = fields.get("library", "primary")
        if key not in self.libraries:
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
                lib_obj = self.libraries[key]
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
                dest_key = key if key in self.libraries else "primary"
                loc = query(library=dest_key, view="sync_hub", notice="mount_updated")
                self.respond(HTTPStatus.SEE_OTHER, "", location=loc + "#feedback")
                return
            if self.path == "/mount_remove":
                mount_id = fields.get("mount_id", "").strip()
                memoryctl.mount_remove(mount_id, home=agent_home)
                self._refresh_mounts(force=True)
                dest_key = key if key in self.libraries else "primary"
                loc = query(library=dest_key, view="sync_hub", notice="mount_removed")
                self.respond(HTTPStatus.SEE_OTHER, "", location=loc + "#feedback")
                return
            if self.path == "/resolve_conflict":
                lib_obj = self.libraries[key]
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
                src_lib = self.libraries[key]
                topic_path = fields.get("topic", "").strip()
                target_key = fields.get("target_library", "").strip()
                target_layer = fields.get("target_layer", "").strip()
                overwrite = fields.get("overwrite") == "1"
                if target_key not in self.libraries:
                    raise InvalidEntry("目标共享库不存在或未启用。")
                dst_lib = self.libraries[target_key]
                dst_lib._ensure_writable(layer=target_layer)
                t_entry = src_lib.read_topic(topic_path)
                dst_folder = mcp_server.LAYER_DIRS[target_layer]
                dst_file = dst_lib.root / dst_folder / f"{t_entry.slug}.md"
                if dst_file.exists() and not overwrite:
                    raise InvalidEntry(
                        f"目标库「{dst_lib.label}」已存在 {dst_folder}/{t_entry.slug}.md；如需覆盖请勾选确认项。"
                    )
                dst_lib.store.sync_config = dst_lib.sync_config
                dst_lib.store.async_scheduler = getattr(self.server, "async_scheduler", dst_lib.async_scheduler)
                saved = dst_lib.store.upsert_entry(
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
        if key not in self.libraries:
            self.page(HTTPStatus.BAD_REQUEST, error="记忆库不存在。")
            return
        library = self.libraries[key]
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
        if key not in self.libraries:
            self.page(HTTPStatus.BAD_REQUEST, error="记忆库不存在。")
            return
        library = self.libraries[key]
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
        if key not in self.libraries:
            self.page(HTTPStatus.BAD_REQUEST, error="记忆库不存在。")
            return
        library = self.libraries[key]
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

    def handle_preview_markdown(self, fields):
        """Render Markdown editor draft safely and flag suspected plaintext secrets before save."""
        key = fields.get("library", "primary")
        if key not in self.libraries:
            key = "primary"
        library = self.libraries[key]
        body = fields.get("body", "")
        title = fields.get("title", "").strip()
        description = fields.get("description", "").strip()
        try:
            _, topics = library.read_index()
        except (InvalidEntry, OSError):
            topics = {}
        html_out = (
            render_markdown_html(
                body,
                fields.get("orig_topic", "") or "MEMORY.md",
                library.root,
                topics,
                dict(library=key),
                skip_h1=title,
                include_toc=False,
            )
            if body.strip()
            else '<p class="empty">正文为空，输入 Markdown 内容后即可实时预览。</p>'
        )
        findings = secret_guard.find_secrets(title, description, body)
        secret_warning = (
            f"检测到疑似明文凭据（{secret_guard.format_findings(findings)}），保存前请先脱敏或改写为凭据位置说明。"
            if findings
            else ""
        )
        self.respond_json(
            HTTPStatus.OK,
            {
                "html": html_out,
                "chars": len(body),
                "tokens": audit_log.estimate_tokens(body),
                "secret_warning": secret_warning,
            },
        )

    def handle_restore_version(self, fields):
        """Put one saved version back as the current entry."""
        key = fields.get("library", "primary")
        if key not in self.libraries:
            self.page(HTTPStatus.BAD_REQUEST, error="记忆库不存在。")
            return
        library = self.libraries[key]
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
        if key not in self.libraries:
            self.page(HTTPStatus.BAD_REQUEST, error="记忆库不存在。")
            return
        library = self.libraries[key]
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
        if key not in self.libraries:
            self.page(HTTPStatus.BAD_REQUEST, error="记忆库不存在。")
            return
        library = self.libraries[key]
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
        configure_ui(desktop=True, template=PAGE_TEMPLATE)
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
        for lib in libraries.values():
            if lib.mode == "rw" and (lib.sync_config or {}).get("auto_push"):
                async_scheduler.recover_stale(lib.root, sync_config=lib.sync_config, mode=lib.mode)
        server = ManagerServer(("127.0.0.1", args.port), Handler)
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
        reload_lock = threading.Lock()

        def reload_mounts(force=False):
            with reload_lock:
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
