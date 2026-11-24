#!/usr/bin/env python3
"""Zero-dependency MCP stdio server for the unified L0-L4 Markdown memory library."""

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import time
from tempfile import NamedTemporaryFile


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from library_paths import (
    ALL_LAYERS,
    config_signature,
    data_root,
    library_state_dir,
    resolve_libraries,
)
import audit_log
import sync_git
from async_sync import AsyncSyncScheduler

DEFAULT_ROOT = data_root()
LAYER_DIRS = {
    "L0": "constraints",
    "L1": "methods",
    "L2": "preferences",
    "L3": "projects",
    "L4": "archive",
}
LAYER_DEFAULT_TYPE = {
    "L0": "feedback",
    "L1": "reference",
    "L2": "feedback",
    "L3": "project",
    "L4": "reference",
}
ALLOWED_LAYER_TYPES = {
    "L0": {"feedback", "reference"},
    "L1": {"reference"},
    "L2": {"feedback"},
    "L3": {"project"},
    "L4": {"reference"},
}
VALID_TOPIC_TYPES = {"user", "feedback", "project", "reference"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
TRASH_FILE_RE = re.compile(r"^(\d{8}-\d{6})-(.+)-([0-9a-f]{4})\.md$")
# Why a backup exists, keyed by the slug prefix _backup_to_trash writes.
TRASH_REASONS = {"": "update", "user": "update", "migrated": "layer-change", "archive": "archive"}
INDEX_LAYER_RE = re.compile(r"^## (L[0-4])(?:\s|$)")
INDEX_LINK_RE = re.compile(
    r"^\s*[-*+]\s+\[(.+?)\]\((?:<([^>]+)>|([^\s()]+))\)(?:\s+[—–-]\s+(.+))?$"
)
CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
ALNUM_RUN_RE = re.compile(r"[a-z0-9_.-]{2,}")
WORD_RUN_RE = re.compile(r"[a-z0-9](?:[a-z0-9_.-]*[a-z0-9])?")
WORD_PART_RE = re.compile(r"[a-z0-9]+")
# BM25F field weights: a match in the entry name or title says more than one in the body.
BM25F_FIELDS = (("name", 3.0), ("title", 3.0), ("description", 2.0), ("body", 1.0))
BM25_K1 = 1.2
HYBRID_BM25_WEIGHT = 3.0
BM25_B = 0.75
# "legacy" (fixed field points) is the default. On a 40-query hand-labeled set
# (2026-11-24) it ranks the right entry first in 39 cases; BM25F adds nothing
# there and demotes the right entry on vague queries, because IDF rewards a
# single rare term and length normalization favors very short entries.
# "hybrid"/"bm25f" stay available for experiments via UNIFIED_MEMORY_SEARCH_RANKER.
SEARCH_RANKERS = ("legacy", "hybrid", "bm25f")
TEMPORAL_CUE_RE = re.compile(
    r"(刚刚|刚才|最近|最新|近期|今天|今日|昨天|本周|这周|近期工作|最近工作|最近进展|recent|latest|today)",
    re.IGNORECASE,
)
TEMPORAL_STOP_RE = re.compile(
    r"(刚刚|刚才|最近|最新|近期|今天|今日|昨天|本周|这周|做了什么|干了什么|改了什么|写了什么|"
    r"有什么|有哪些|进展|动态|工作|项目|记忆|记录|更新|写入|recent|latest|work|today|what)",
    re.IGNORECASE,
)
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2024-11-05"


def now_iso():
    """Return local timezone-aware ISO 8601 timestamp with second precision."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def local_tz():
    """Return current system local tzinfo."""
    return datetime.now().astimezone().tzinfo


def parse_timestamp(ts_str, fallback_epoch=None):
    """Parse ISO 8601 datetime or YYYY-MM-DD date into (aware_datetime, has_clock_time)."""
    tz = local_tz()
    s = (ts_str or "").strip()
    if s:
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            d = date.fromisoformat(s)
            return datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=tz), False
        iso_norm = s[:-1] + "+00:00" if s.endswith("Z") else s
        dt = datetime.fromisoformat(iso_norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        else:
            dt = dt.astimezone(tz)
        return dt, True
    if fallback_epoch is not None:
        return datetime.fromtimestamp(fallback_epoch, tz=tz), True
    return datetime.now().astimezone(), True


def file_timestamps(md_path, meta):
    """Resolve (created_at_str, updated_at_str, created_epoch, updated_epoch) for a file."""
    st = md_path.stat()
    mtime_epoch = st.st_mtime
    btime_epoch = min(getattr(st, "st_birthtime", mtime_epoch), mtime_epoch)

    raw_upd = str(meta.get("updated_at") or "").strip()
    raw_cre = str(meta.get("created_at") or "").strip()

    try:
        upd_dt, upd_has_time = parse_timestamp(raw_upd, fallback_epoch=mtime_epoch)
    except ValueError:
        upd_dt, upd_has_time = parse_timestamp("", fallback_epoch=mtime_epoch)
        raw_upd = ""

    try:
        if raw_cre:
            cre_dt, cre_has_time = parse_timestamp(raw_cre, fallback_epoch=btime_epoch)
        else:
            cre_dt, cre_has_time = parse_timestamp("", fallback_epoch=btime_epoch)
            if cre_dt > upd_dt:
                cre_dt, cre_has_time = upd_dt, upd_has_time
    except ValueError:
        cre_dt, cre_has_time = upd_dt, upd_has_time
        raw_cre = ""

    if cre_dt > upd_dt:
        cre_dt, cre_has_time = upd_dt, upd_has_time

    upd_out = raw_upd if raw_upd else (upd_dt.isoformat(timespec="seconds") if upd_has_time else upd_dt.strftime("%Y-%m-%d"))
    cre_out = raw_cre if raw_cre else (cre_dt.isoformat(timespec="seconds") if cre_has_time else cre_dt.strftime("%Y-%m-%d"))
    return cre_out, upd_out, cre_dt.timestamp(), upd_dt.timestamp()


def is_newly_created(created_at_str, updated_at_str):
    """Return True when updated_at is effectively the initial creation timestamp."""
    if not created_at_str or not updated_at_str:
        return True
    if created_at_str == updated_at_str:
        return True
    try:
        cre_dt, cre_has_time = parse_timestamp(created_at_str)
        upd_dt, upd_has_time = parse_timestamp(updated_at_str)
    except ValueError:
        return False
    if not cre_has_time and not upd_has_time:
        return cre_dt.date() == upd_dt.date()
    return abs((upd_dt - cre_dt).total_seconds()) < 600


def format_relative_zh(ts_str, now_dt=None):
    """Format a timestamp string into (relative_zh, full_display, machine_datetime)."""
    now_dt = now_dt or datetime.now().astimezone()
    try:
        dt, has_time = parse_timestamp(ts_str)
    except ValueError:
        return "未知时间", ts_str or "", ts_str or ""

    days_diff = (now_dt.date() - dt.date()).days
    if has_time:
        diff_sec = max(0.0, (now_dt - dt).total_seconds())
        full_disp = dt.strftime("%Y-%m-%d %H:%M")
        iso_attr = dt.isoformat(timespec="seconds")
        if diff_sec < 60:
            rel = "刚刚"
        elif diff_sec < 3600:
            rel = f"{int(diff_sec // 60)}分钟前"
        elif diff_sec < 86400 and days_diff == 0:
            rel = f"{int(diff_sec // 3600)}小时前"
        elif days_diff == 1 or (diff_sec < 86400 and days_diff <= 1):
            rel = f"昨天 {dt.strftime('%H:%M')}"
        elif days_diff < 7:
            rel = f"{days_diff}天前"
        elif days_diff < 30:
            rel = f"{max(1, days_diff // 7)}周前"
        elif days_diff < 365:
            rel = f"{max(1, days_diff // 30)}个月前"
        else:
            rel = f"{max(1, days_diff // 365)}年前"
        return rel, full_disp, iso_attr

    full_disp = dt.strftime("%Y-%m-%d")
    iso_attr = full_disp
    if days_diff <= 0:
        rel = "今天"
    elif days_diff == 1:
        rel = "昨天"
    elif days_diff < 7:
        rel = f"{days_diff}天前"
    elif days_diff < 30:
        rel = f"{max(1, days_diff // 7)}周前"
    elif days_diff < 365:
        rel = f"{max(1, days_diff // 30)}个月前"
    else:
        rel = f"{max(1, days_diff // 365)}年前"
    return rel, full_disp, iso_attr


def parse_frontmatter(text):
    """Extract simple YAML/JSON-style key: value frontmatter and remaining body."""
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    header, sep, body = normalized[4:].partition("\n---\n")
    if not sep:
        return {}, normalized
    fields = {}
    for line in header.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, colon, raw_val = line.partition(":")
        if not colon:
            continue
        key = key.strip()
        val = raw_val.strip()
        if val.startswith('"') and val.endswith('"'):
            try:
                val = json.loads(val)
            except json.JSONDecodeError:
                val = val[1:-1]
        fields[key] = val
    return fields, body.lstrip("\n")


SECTION_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# Progressive disclosure: large entries get a section outline so an agent can read
# one section instead of the whole file (MemGPT-style paging over Markdown).
OUTLINE_MIN_CHARS = 1200


def heading_line(preamble):
    """Return the leading "# Title" line of a body, if present."""
    for line in preamble.split("\n"):
        if line.startswith("# "):
            return line.strip()
    return ""


def split_sections(body):
    """Split a Markdown body into (preamble, [{"title", "text"}]) on "## " headings."""
    preamble = []
    sections = []
    current = None
    for line in body.replace("\r\n", "\n").split("\n"):
        match = SECTION_HEADING_RE.match(line)
        if match and len(match.group(1)) == 2:
            if current is not None:
                sections.append(current)
            current = {"title": match.group(2).strip(), "lines": [line]}
        elif current is not None:
            current["lines"].append(line)
        else:
            preamble.append(line)
    if current is not None:
        sections.append(current)
    out = []
    for sec in sections:
        text = "\n".join(sec["lines"]).strip("\n")
        out.append({"title": sec["title"], "text": text})
    intro = "\n".join(preamble).strip("\n")
    title_line = heading_line(intro)
    if out and intro and intro != title_line:
        # The text before the first "## " heading (for MEMORY.md: the whole §0 rules)
        # is a readable section too, named after the document title.
        out.insert(0, {"title": title_line[2:].strip() or "intro", "text": intro})
    return title_line, out


def section_outline(sections):
    """Describe each section with its size so agents can budget a targeted read."""
    return [
        {
            "section": sec["title"],
            "chars": len(sec["text"]),
            "tokens": audit_log.estimate_tokens(sec["text"]),
        }
        for sec in sections
    ]


def strip_generated_header(body):
    """Drop frontmatter and the H1 that the server writes itself.

    memory_read returns the whole file, so agents that edit it and pass it back to
    memory_upsert would otherwise nest a second frontmatter block and title.
    """
    text = body.replace("\r\n", "\n").strip()
    while True:
        meta, rest = parse_frontmatter(text + "\n")
        if meta:
            text = rest.strip()
            continue
        first, _, remainder = text.partition("\n")
        if first.startswith("# ") and remainder.lstrip().startswith("---\n"):
            meta, rest = parse_frontmatter(remainder.lstrip() + "\n")
            if meta:
                text = rest.strip()
                continue
        return text


def extract_title(body, fallback):
    """Extract first Markdown H1 heading if present."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return fallback


def first_summary_snippet(body):
    """Extract first informative non-heading line as snippet."""
    for line in body.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("```"):
            return s[:240]
    return ""


def atomic_write(path, content):
    """Atomically write UTF-8 text to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".tmp-", delete=False
        ) as handle:
            staged = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        staged.replace(path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def build_query_features(query):
    """Extract whitespace terms plus CJK bigrams/subtokens for partial phrase recall."""
    q_cf = query.strip().casefold()
    raw_terms = [t for t in q_cf.split() if t]
    subtokens = []
    seen = set()
    for term in raw_terms:
        for run in ALNUM_RUN_RE.findall(term):
            if run not in seen:
                seen.add(run)
                subtokens.append(run)
        for cjk in CJK_RUN_RE.findall(term):
            if len(cjk) == 2:
                if cjk not in seen:
                    seen.add(cjk)
                    subtokens.append(cjk)
            else:
                for idx in range(len(cjk) - 1):
                    bg = cjk[idx : idx + 2]
                    if bg not in seen:
                        seen.add(bg)
                        subtokens.append(bg)
    return raw_terms, subtokens


def search_ranker(ranker=None):
    """Return the ranker name to use; unknown values fall back to the default."""
    chosen = (ranker or os.environ.get("UNIFIED_MEMORY_SEARCH_RANKER") or SEARCH_RANKERS[0]).strip().lower()
    return chosen if chosen in SEARCH_RANKERS else SEARCH_RANKERS[0]


def search_tokens(text_cf):
    """Split casefolded text into BM25 terms: word parts, compound words, and CJK bigrams.

    ``docker-build-cache-fix`` yields the compound plus ``docker``, ``build``...
    so a query for either form matches. Single-letter parts are dropped.
    """
    tokens = []
    for run in WORD_RUN_RE.findall(text_cf):
        parts = WORD_PART_RE.findall(run)
        if len(parts) > 1:
            tokens.append(run)
        tokens.extend(part for part in parts if len(part) > 1)
    for run in CJK_RUN_RE.findall(text_cf):
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def bm25f_scores(docs, query_terms):
    """Score docs (dicts of casefolded field text) with BM25F over this collection.

    Field term frequencies are length-normalised per field, weighted by
    BM25F_FIELDS, summed, then saturated once per term with BM25_K1. IDF uses
    the documents passed in, so scores are comparable only within one call.
    """
    terms = list(dict.fromkeys(query_terms))
    if not docs or not terms:
        return [0.0] * len(docs)
    counted = []
    totals = {field: 0 for field, _ in BM25F_FIELDS}
    df = Counter()
    for doc in docs:
        fields = {}
        present = set()
        for field, _ in BM25F_FIELDS:
            toks = search_tokens(doc.get(field, ""))
            totals[field] += len(toks)
            counts = Counter(toks)
            fields[field] = (counts, len(toks))
            present.update(t for t in terms if t in counts)
        df.update(present)
        counted.append(fields)
    n = len(docs)
    avg = {field: (totals[field] / n) or 1.0 for field in totals}
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in terms}
    scores = []
    for fields in counted:
        score = 0.0
        for t in terms:
            if not df[t]:
                continue
            tf = 0.0
            for field, weight in BM25F_FIELDS:
                counts, length = fields[field]
                if counts[t]:
                    tf += weight * counts[t] / (1 - BM25_B + BM25_B * length / avg[field])
            if tf:
                score += idf[t] * tf / (BM25_K1 + tf)
        scores.append(score)
    return scores


def score_fields(raw_terms, subtokens, name_cf, title_cf, desc_cf, body_cf):
    """Score an entry against exact query terms and fallback CJK bigrams."""
    score = 0
    matched_terms = []
    for term in raw_terms:
        hit = False
        if term in name_cf or term in title_cf:
            score += 8
            hit = True
        if term in desc_cf:
            score += 5
            hit = True
        if term in body_cf:
            score += 2
            hit = True
        if hit:
            matched_terms.append(term)

    if score > 0 or not subtokens:
        return score, matched_terms

    sub_hits = []
    sub_score = 0
    for tok in subtokens:
        hit = False
        if tok in name_cf or tok in title_cf:
            sub_score += 3
            hit = True
        if tok in desc_cf:
            sub_score += 2
            hit = True
        if tok in body_cf:
            sub_score += 1
            hit = True
        if hit:
            sub_hits.append(tok)

    min_required = 1 if len(subtokens) == 1 else max(2, (len(subtokens) + 1) // 2)
    if len(sub_hits) >= min_required:
        return sub_score, sub_hits
    return 0, []


class MemoryStore:
    """Read, search, upsert, and archive entries in one L0-L4 Markdown library."""

    def __init__(
        self,
        root,
        *,
        library_id="personal",
        label="统一本地记忆",
        role="personal",
        mode="rw",
        allowed_layers=None,
        allow_user_managed=True,
        sync_config=None,
        async_scheduler=None,
    ):
        self.root = data_root(root).resolve(strict=True)
        self.index_path = self.root / "MEMORY.md"
        if not self.index_path.is_file():
            raise ValueError(f"MEMORY.md not found under {self.root}")
        self.library_id = library_id
        self.label = label
        self.role = role
        self.mode = mode
        self.allowed_layers = frozenset(allowed_layers if allowed_layers is not None else ALL_LAYERS)
        self.allow_user_managed = bool(allow_user_managed)
        self.sync_config = sync_config
        # The scheduler is supplied by WorkspaceStore so all mounted libraries in
        # this MCP process share one lightweight worker registry.  Keeping this
        # optional preserves the small, directly-testable MemoryStore API.
        self.async_scheduler = async_scheduler
        self.state_dir = library_state_dir(self.root)
        self.lock_path = self.state_dir / "library.lock"
        self.trash_dir = self.state_dir / "trash"

    @contextmanager
    def write_lock(self):
        """Serialize concurrent writes across multiple MCP servers and Web UI."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _similar_entries(self, query, name, limit=3):
        """Find existing entries on the same topic so a new write can merge instead of fork."""
        try:
            hits = self.search(query, limit=limit + 3)
        except Exception:
            return []
        out = []
        for hit in hits:
            if Path(hit.get("path", "")).stem == name:
                continue
            if (hit.get("score") or 0) < 8:
                continue
            out.append({
                "ref": hit.get("ref", ""),
                "layer": hit.get("layer", ""),
                "title": hit.get("title", ""),
                "updated_at": hit.get("updated_at", ""),
            })
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _check_expected_updated(expected_updated_at, old_meta, target):
        """Reject a write whose base version is stale (lost-update protection)."""
        expected = (expected_updated_at or "").strip()
        if not expected:
            return
        actual = str(old_meta.get("updated_at") or "").strip()
        if not actual:
            _, actual, _, _ = file_timestamps(target, old_meta)
        if expected != actual:
            raise ValueError(
                "Entry changed since you read it: expected_updated_at="
                f"{expected} but the entry is at {actual}. Read it again, merge, then write."
            )

    @staticmethod
    def _previous_receipt(old_meta, old_body, backup):
        """Describe the version that a write is about to replace."""
        return {
            "updated_at": str(old_meta.get("updated_at") or "").strip(),
            "chars": len(old_body or ""),
            "backup": backup or "",
        }

    @staticmethod
    def _attach_previous(result, previous, new_body):
        """Report the replaced version and warn when a write drops most of it."""
        if not previous:
            return result
        result["previous"] = previous
        old_chars = previous.get("chars") or 0
        new_chars = len(new_body or "")
        if old_chars >= 400 and new_chars < old_chars * 0.6:
            result["content_warning"] = (
                f"This write replaced {old_chars} chars with {new_chars}. If that was not"
                " intended, the previous version is in the backup path above."
            )
        return result

    def _backup_to_trash(self, src_path, slug_hint):
        """Save a safety backup copy into local state trash before overwriting or removing a file."""
        if not src_path.is_file():
            return None
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_slug = re.sub(r"[^A-Za-z0-9._-]+", "_", slug_hint)[:80] or "entry"
        dest = self.trash_dir / f"{stamp}-{safe_slug}-{secrets.token_hex(2)}.md"
        shutil.copy2(src_path, dest)
        return str(dest)

    def _maybe_auto_commit(self, message):
        """Auto-commit to Git while keeping the primary memory write non-fatal.

        The write API intentionally remains backward compatible: a Git failure is returned as
        status metadata and is surfaced on the write result as ``sync_warning``.  It is also
        recorded separately so a successful memory write cannot hide a failed Git snapshot.
        """
        if not self.sync_config or self.sync_config.get("driver") != "git":
            return None
        if not self.sync_config.get("auto_commit", True):
            return None

        def redacted(value):
            text = str(value or "")
            redact_url = getattr(sync_git, "_redact_remote_urls", None)
            if callable(redact_url):
                text = redact_url(text)
            return text[:320]

        def failure(result, *, error=""):
            if isinstance(result, dict):
                status = dict(result)
            else:
                status = {"committed": False, "reason": "sync_error"}
            status["committed"] = False
            reason = redacted(status.get("reason") or "unknown")
            detail = redacted(error or status.get("error") or "")
            status["reason"] = reason
            if detail:
                status["error"] = detail
            status["warning"] = f"记忆已保存，但 Git 快照未提交（原因：{reason}）。"
            audit_log.record(
                {
                    "source": "sync",
                    "event": "git_auto_commit",
                    "client": CLIENT_NAME,
                    "tool": "git_auto_commit",
                    "ok": False,
                    "library": self.library_id,
                    "message": redacted(message),
                    "reason": reason,
                    "error": detail,
                },
                home=AUDIT_HOME,
            )
            return status

        try:
            result = sync_git.auto_commit(self.root, message)
        except Exception as exc:
            return failure(None, error=exc)
        if not isinstance(result, dict) or not result.get("committed"):
            return failure(result)
        return result

    @staticmethod
    def _with_sync_warning(result, sync_status):
        """Attach a non-fatal Git warning without changing successful write payloads."""
        if isinstance(sync_status, dict) and sync_status.get("warning"):
            result["sync_warning"] = sync_status["warning"]
        return result

    def _enqueue_async_sync(self, message):
        """Queue a debounced remote sync after a successful local write.

        Async push is deliberately best-effort: the memory write and local
        ``auto_commit`` result remain authoritative, while the scheduler persists
        any remote failure in ``.state/sync-status.json`` for later inspection.
        The scheduler itself filters read-only, non-Git, and ``auto_push=false``
        libraries, so callers can invoke this uniformly for every write path.
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
            # Never turn an already successful memory write into an error because
            # a process-local background scheduler is unavailable.
            return None

    def async_sync_status(self):
        """Return the latest background sync status for this library.

        The scheduler keeps an in-memory copy while the MCP process is alive and
        persists the same bounded payload under the library's private ``.state``
        directory.  Reading through the scheduler also makes a status left by a
        previous process visible to the current process.
        """
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

    def resolve_safe(self, rel_path):
        """Resolve a relative path and enforce confinement inside self.root."""
        candidate = (self.root / rel_path).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("Path escapes memory root.")
        return candidate

    def parse_index(self):
        """Parse L0-L4 index entries from MEMORY.md."""
        text = self.index_path.read_text(encoding="utf-8")
        entries = {}
        current_layer = ""
        for line in text.splitlines():
            if line.startswith("#"):
                m = INDEX_LAYER_RE.match(line)
                current_layer = m.group(1) if m else ""
                continue
            if not current_layer:
                continue
            m = INDEX_LINK_RE.match(line)
            if not m:
                continue
            title, angled, plain, desc = m.groups()
            target = (angled or plain or "").strip()
            entries[target] = {
                "layer": current_layer,
                "title": title.strip(),
                "description": (desc or "").strip(),
                "path": target,
            }
        return entries

    def collect_entries(self, include_archived=False):
        """Collect all indexed and user-managed memory entries with metadata and timestamps."""
        indexed = self.parse_index()
        items = []

        for layer, folder in LAYER_DIRS.items():
            if layer not in self.allowed_layers:
                continue
            dir_path = self.root / folder
            for md in self._iter_safe_md_files(dir_path):
                if md.name == "README.md" or sync_git.is_conflict_filename(md.name):
                    continue
                rel = md.relative_to(self.root).as_posix()
                # Match the Web manager: a stray non-UTF-8 file must not break every query.
                raw = md.read_text(encoding="utf-8", errors="replace")
                meta, body = parse_frontmatter(raw)
                cre_at, upd_at, cre_ep, upd_ep = file_timestamps(md, meta)
                idx = indexed.get(rel, {})
                items.append(
                    {
                        "library": self.library_id,
                        "library_role": self.role,
                        "library_mode": self.mode,
                        "path": rel,
                        "source": "index",
                        "layer": idx.get("layer", layer),
                        "name": meta.get("name", md.stem),
                        "title": idx.get("title") or extract_title(body, md.stem),
                        "description": meta.get("description") or idx.get("description", ""),
                        "type": meta.get("type", LAYER_DEFAULT_TYPE[layer]),
                        "status": meta.get("status", "active"),
                        "created_at": cre_at,
                        "updated_at": upd_at,
                        "created_epoch": cre_ep,
                        "updated_epoch": upd_ep,
                        "body": body,
                    }
                )

        user_dir = self.root / "user-managed"
        if self.allow_user_managed and user_dir.is_dir():
            for md in self._iter_safe_md_files(user_dir):
                if md.name == "README.md" or sync_git.is_conflict_filename(md.name):
                    continue
                rel = md.relative_to(self.root).as_posix()
                # Match the Web manager: a stray non-UTF-8 file must not break every query.
                raw = md.read_text(encoding="utf-8", errors="replace")
                meta, body = parse_frontmatter(raw)
                status = meta.get("status", "active")
                if status == "archived" and not include_archived:
                    continue
                layer_val = meta.get("layer", "L2")
                if layer_val not in self.allowed_layers:
                    continue
                cre_at, upd_at, cre_ep, upd_ep = file_timestamps(md, meta)
                items.append(
                    {
                        "library": self.library_id,
                        "library_role": self.role,
                        "library_mode": self.mode,
                        "path": rel,
                        "source": "user-managed",
                        "layer": layer_val,
                        "name": meta.get("name", md.stem),
                        "title": meta.get("title") or extract_title(body, md.stem),
                        "description": meta.get("description", ""),
                        "type": meta.get("type", "feedback"),
                        "status": status,
                        "created_at": cre_at,
                        "updated_at": upd_at,
                        "created_epoch": cre_ep,
                        "updated_epoch": upd_ep,
                        "body": body,
                    }
                )

        if include_archived:
            return items
        return [item for item in items if item.get("status", "active") != "archived"]

    def _entry_ref(self, rel_path):
        return rel_path if self.library_id == "personal" else f"{self.library_id}:{rel_path}"

    def _result_metadata(self):
        """Return routing and trust metadata for every entry-facing result."""
        return {
            "library": self.library_id,
            "library_role": self.role,
            "library_mode": self.mode,
            "source_library": self.library_id,
            "provenance": self.role,
            "trust": "personal" if self.role == "personal" else "shared",
            "read_only": self.mode == "ro",
        }

    def _iter_safe_md_files(self, directory):
        """Yield non-symlink Markdown files directly inside a memory layer directory."""
        if directory.is_symlink():
            raise ValueError(f"Symlinked memory directory is not allowed: {directory}")
        if not directory.exists():
            return
        if not directory.is_dir():
            raise ValueError(f"Memory path is not a directory: {directory}")
        for md in sorted(directory.glob("*.md")):
            if md.is_symlink():
                raise ValueError(f"Symlinked memory file is not allowed: {md}")
            resolved = md.resolve(strict=True)
            if not resolved.is_relative_to(self.root):
                raise ValueError(f"Memory file resolves outside the library root: {md}")
            if not resolved.is_file():
                raise ValueError(f"Memory path is not a regular file: {md}")
            yield md

    def search(self, query, layer="", include_archived=False, limit=10, ranker=None):
        """Search entries by query terms, CJK bigrams, and temporal recency cues.

        ``score_fields`` decides which entries match. ``ranker`` decides their
        order: "legacy" (default, fixed field points), "hybrid" (field points
        plus weighted BM25F), or "bm25f"; overridable with
        UNIFIED_MEMORY_SEARCH_RANKER.
        """
        capped_limit = max(1, min(int(limit), 50))
        ranker = search_ranker(ranker)
        q_stripped = query.strip()
        if not q_stripped:
            return self.list_entries(layer=layer, include_archived=include_archived, sort="updated")[:capped_limit]

        has_temporal_cue = bool(TEMPORAL_CUE_RE.search(q_stripped))
        residual_query = TEMPORAL_STOP_RE.sub(" ", q_stripped).strip() if has_temporal_cue else q_stripped

        all_items = [
            it for it in self.collect_entries(include_archived=include_archived)
            if not layer or it["layer"] == layer
        ]

        # Pure temporal query (e.g. "刚刚做了什么", "最近工作", "最近进展"): return most recently updated active work
        if has_temporal_cue and not residual_query:
            candidates = (
                all_items
                if (include_archived or layer == "L4")
                else [it for it in all_items if it["layer"] != "L4"] or all_items
            )
            candidates.sort(key=lambda it: (-it["updated_epoch"], it["layer"], it["path"]))
            return [
                {
                    "score": 10,
                    **self._result_metadata(),
                    "ref": self._entry_ref(it["path"]),
                    "path": it["path"],
                    "source": it["source"],
                    "layer": it["layer"],
                    "title": it["title"],
                    "description": it["description"],
                    "status": it["status"],
                    "created_at": it["created_at"],
                    "updated_at": it["updated_at"],
                    "snippet": first_summary_snippet(it["body"]),
                }
                for it in candidates[:capped_limit]
            ]

        effective_q = residual_query if (has_temporal_cue and residual_query) else q_stripped
        raw_terms, subtokens = build_query_features(effective_q)
        now_ep = datetime.now().astimezone().timestamp()

        bm25 = []
        if ranker in ("bm25f", "hybrid"):
            bm25 = bm25f_scores(
                [
                    {
                        "name": it["name"].casefold(),
                        "title": it["title"].casefold(),
                        "description": it["description"].casefold(),
                        "body": it["body"].casefold(),
                    }
                    for it in all_items
                ],
                search_tokens(effective_q.casefold()),
            )

        results = []
        for pos, item in enumerate(all_items):
            score, matched = score_fields(
                raw_terms,
                subtokens,
                item["name"].casefold(),
                item["title"].casefold(),
                item["description"].casefold(),
                item["body"].casefold(),
            )
            if score == 0:
                continue
            legacy_score = score
            if bm25:
                # Substring-only matches (e.g. "dock") have no BM25 term hit; keep them last.
                if ranker == "hybrid":
                    score = round(legacy_score + HYBRID_BM25_WEIGHT * bm25[pos], 3)
                else:
                    score = round(bm25[pos], 3) or 0.001

            if has_temporal_cue:
                age_sec = max(0.0, now_ep - item["updated_epoch"])
                if age_sec <= 86400:
                    score += 1.0 if ranker == "bm25f" else 6
                elif age_sec <= 7 * 86400:
                    score += 0.5 if ranker == "bm25f" else 3

            snippet = ""
            for line in item["body"].splitlines():
                line_cf = line.casefold()
                if any(tok in line_cf for tok in matched):
                    snippet = line.strip()[:240]
                    break

            results.append(
                {
                    "score": score,
                    **self._result_metadata(),
                    "ref": self._entry_ref(item["path"]),
                    "path": item["path"],
                    "source": item["source"],
                    "layer": item["layer"],
                    "title": item["title"],
                    "description": item["description"],
                    "status": item["status"],
                    "created_at": item["created_at"],
                    "updated_at": item["updated_at"],
                    "_updated_epoch": item["updated_epoch"],
                    "_legacy_score": legacy_score,
                    "snippet": snippet or first_summary_snippet(item["body"]),
                }
            )

        results.sort(key=lambda r: (-r["score"], -r["_legacy_score"], -r["_updated_epoch"], r["layer"], r["path"]))
        for r in results:
            r.pop("_updated_epoch", None)
            r.pop("_legacy_score", None)
        return results[:capped_limit]

    def list_entries(self, layer="", include_archived=False, sort="updated"):
        """List entries optionally filtered by layer and sorted by updated/created/layer."""
        collected = [
            it for it in self.collect_entries(include_archived=include_archived)
            if not layer or it["layer"] == layer
        ]
        if sort == "created":
            collected.sort(key=lambda it: (-it["created_epoch"], -it["updated_epoch"], it["path"]))
        elif sort == "layer":
            collected.sort(key=lambda it: (it["layer"], -it["updated_epoch"], it["path"]))
        else:
            collected.sort(key=lambda it: (-it["updated_epoch"], it["layer"], it["path"]))

        return [
            {
                **self._result_metadata(),
                "ref": self._entry_ref(item["path"]),
                "path": item["path"],
                "source": item["source"],
                "layer": item["layer"],
                "title": item["title"],
                "description": item["description"],
                "status": item["status"],
                "created_at": item["created_at"],
                "updated_at": item["updated_at"],
            }
            for item in collected
        ]

    def read_entry(self, rel_path, section="", outline=False):
        """Read a Markdown file inside the memory root, optionally one section only."""
        target = self.resolve_safe(rel_path)
        if not target.is_file() or target.suffix.lower() != ".md":
            raise ValueError(f"Not a readable markdown file: {rel_path}")
        rel_posix = target.relative_to(self.root).as_posix()
        top_part = Path(rel_posix).parts[0] if Path(rel_posix).parts else ""
        if top_part == "user-managed" and not self.allow_user_managed:
            raise ValueError(f"user-managed/ is not enabled for library '{self.library_id}'.")
        folder_to_layer = {folder: lyr for lyr, folder in LAYER_DIRS.items()}
        if top_part in folder_to_layer and folder_to_layer[top_part] not in self.allowed_layers:
            raise ValueError(
                f"Layer {folder_to_layer[top_part]} is not enabled for library '{self.library_id}'."
            )
        text = target.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        preamble, sections = split_sections(body)
        base = {
            **self._result_metadata(),
            "ref": self._entry_ref(rel_posix),
            "path": rel_posix,
            "chars": len(text),
        }
        if meta.get("updated_at"):
            # Agents pass this back as expected_updated_at to avoid clobbering a
            # parallel session's edit.
            base["updated_at"] = str(meta["updated_at"]).strip()
        wanted = (section or "").strip()
        if wanted:
            if not sections:
                raise ValueError(f"{rel_posix} has no '## ' sections to read.")
            folded = wanted.casefold()
            picked = next((sec for sec in sections if sec["title"].casefold() == folded), None)
            if picked is None:
                picked = next((sec for sec in sections if folded in sec["title"].casefold()), None)
            if picked is None:
                available = ", ".join(sec["title"] for sec in sections)
                raise ValueError(f"Section '{wanted}' not found in {rel_posix}. Available: {available}")
            title_line = preamble if preamble not in picked["text"] else ""
            parts = [part for part in (title_line, picked["text"]) if part]
            return {
                **base,
                "section": picked["title"],
                "partial": True,
                "content": "\n\n".join(parts) + "\n",
                "sections": section_outline(sections),
            }
        if outline:
            if not sections:
                return {**base, "partial": False, "content": text}
            return {
                **base,
                "partial": True,
                "content": (preamble or f"# {rel_posix}") + "\n",
                "sections": section_outline(sections),
                "notice": "Outline only. Call memory_read again with section=\"<title>\" for one section, or without outline for the whole entry.",
            }
        result = {**base, "partial": False, "content": text}
        if len(sections) >= 2 and len(text) >= OUTLINE_MIN_CHARS:
            result["sections"] = section_outline(sections)
        return result

    def upsert_entry(self, layer, name, title, description, body, topic_type="", user_managed=False,
                     expected_updated_at=""):
        """Create or update a memory entry under lock, backing up previous versions to trash.

        ``expected_updated_at`` turns the write into a compare-and-set: if the entry
        changed since the agent read it, the write is rejected instead of silently
        dropping the other session's edit.
        """
        if self.mode == "ro":
            raise ValueError(f"Library '{self.library_id}' is read-only.")
        if layer not in LAYER_DIRS:
            raise ValueError(f"Invalid layer: {layer}. Expected L0-L4.")
        if layer not in self.allowed_layers:
            allowed_str = ", ".join(sorted(self.allowed_layers))
            raise ValueError(f"Layer {layer} is not allowed in library '{self.library_id}' (allowed: {allowed_str}).")
        if user_managed and not self.allow_user_managed:
            raise ValueError(f"Library '{self.library_id}' does not allow user-managed entries.")
        if not NAME_RE.fullmatch(name) or name.lower() == "readme":
            raise ValueError("name must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$ and not be 'readme'.")
        title = title.strip()
        description = description.strip()
        body = body.strip() if user_managed else strip_generated_header(body)
        if not title or "\n" in title:
            raise ValueError("title must be a single non-empty line.")
        if "\n" in description:
            raise ValueError("description must be a single line.")
        if not body:
            raise ValueError("body must not be empty.")

        resolved_type = topic_type or LAYER_DEFAULT_TYPE[layer]
        allowed_types = ALLOWED_LAYER_TYPES[layer] if user_managed else VALID_TOPIC_TYPES
        if resolved_type not in allowed_types:
            raise ValueError(f"type '{resolved_type}' is invalid for {layer}.")

        related = []
        if not user_managed and not (self.root / LAYER_DIRS[layer] / f"{name}.md").is_file():
            related = self._similar_entries(f"{title} {description}", name)

        ts_now = now_iso()
        with self.write_lock():
            if user_managed:
                target = self.root / "user-managed" / f"{name}.md"
                status = "active"
                cre_at = ts_now
                previous = None
                if target.is_file():
                    old_text = target.read_text(encoding="utf-8")
                    old_meta, old_body = parse_frontmatter(old_text)
                    self._check_expected_updated(expected_updated_at, old_meta, target)
                    backup = self._backup_to_trash(target, f"user-{name}")
                    previous = self._previous_receipt(old_meta, old_body, backup)
                    status = old_meta.get("status", "active")
                    cre_at = old_meta.get("created_at") or old_meta.get("updated_at") or ts_now
                    if not topic_type and old_meta.get("type") in ALLOWED_LAYER_TYPES[layer]:
                        resolved_type = old_meta["type"]
                meta = {
                    "name": name,
                    "title": title,
                    "description": description,
                    "type": resolved_type,
                    "layer": layer,
                    "status": status,
                    "created_at": cre_at,
                    "updated_at": ts_now,
                }
                header = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items())
                atomic_write(target, f"---\n{header}\n---\n\n{body}\n")
                rel_out = target.relative_to(self.root).as_posix()
                message = f"memory({self.library_id}): upsert {rel_out}"
                sync_status = self._maybe_auto_commit(message)
                self._enqueue_async_sync(message)
                res = {
                    **self._result_metadata(),
                    "ref": self._entry_ref(rel_out),
                    "path": rel_out,
                    "indexed": False,
                    "created_at": cre_at,
                    "updated_at": ts_now,
                }
                self._attach_previous(res, previous, body)
                return self._with_sync_warning(res, sync_status)

            folder = LAYER_DIRS[layer]
            target = self.root / folder / f"{name}.md"
            rel_path = f"{folder}/{name}.md"

            # Find any existing entry (in target layer or another layer) to preserve created_at
            stale_files = []
            preserved_created = None
            previous = None
            if target.is_file():
                old_meta, old_body = parse_frontmatter(target.read_text(encoding="utf-8"))
                self._check_expected_updated(expected_updated_at, old_meta, target)
                backup = self._backup_to_trash(target, name)
                previous = self._previous_receipt(old_meta, old_body, backup)
                preserved_created, _, _, _ = file_timestamps(target, old_meta)
            for other_layer, other_folder in LAYER_DIRS.items():
                if other_layer == layer or other_layer not in self.allowed_layers:
                    continue
                stale = self.root / other_folder / f"{name}.md"
                if stale.is_file():
                    if preserved_created is None:
                        s_meta, _ = parse_frontmatter(stale.read_text(encoding="utf-8"))
                        preserved_created, _, _, _ = file_timestamps(stale, s_meta)
                    stale_files.append((stale, f"{other_folder}/{name}.md"))

            cre_at = preserved_created or ts_now
            desc_val = json.dumps(description or title, ensure_ascii=False)
            formatted_body = body if body.lstrip().startswith("# ") else f"# {title}\n\n{body}"
            content = (
                f"---\n"
                f"name: {name}\n"
                f"description: {desc_val}\n"
                f"type: {resolved_type}\n"
                f"created_at: {json.dumps(cre_at)}\n"
                f"updated_at: {json.dumps(ts_now)}\n"
                f"---\n\n"
                f"{formatted_body}\n"
            )

            # Crash-safe multi-file update: snapshot MEMORY.md & target so any error rolls back both cleanly
            prev_target_bytes = target.read_bytes() if target.is_file() else None
            prev_index_text = self.index_path.read_text(encoding="utf-8")
            atomic_write(target, content)
            try:
                for _, stale_rel in stale_files:
                    self._remove_index_line(stale_rel)
                self._upsert_index_line(layer, title, rel_path, description or title)
            except Exception:
                if prev_target_bytes is None:
                    target.unlink(missing_ok=True)
                else:
                    atomic_write(target, prev_target_bytes.decode("utf-8"))
                try:
                    atomic_write(self.index_path, prev_index_text)
                except OSError:
                    pass
                raise

            migrated_from = []
            for stale_path, stale_rel in stale_files:
                self._backup_to_trash(stale_path, f"migrated-{name}")
                stale_path.unlink(missing_ok=True)
                migrated_from.append(stale_rel)

            message = f"memory({self.library_id}): upsert {rel_path}"
            sync_status = self._maybe_auto_commit(message)
            self._enqueue_async_sync(message)
            res = {
                **self._result_metadata(),
                "ref": self._entry_ref(rel_path),
                "path": rel_path,
                "indexed": True,
                "created_at": cre_at,
                "updated_at": ts_now,
            }
            if migrated_from:
                res["migrated_from"] = migrated_from
            if related and previous is None and not migrated_from:
                res["related"] = related
                res["notice"] = (
                    "New entry created. The entries in 'related' look close to it: if one"
                    " covers the same topic, fold this content into that entry and archive"
                    " the duplicate, so the library keeps one current conclusion per topic."
                )
            self._attach_previous(res, previous, formatted_body)
            return self._with_sync_warning(res, sync_status)

    def _remove_index_line(self, rel_path):
        """Remove any index line referencing rel_path from MEMORY.md."""
        marker = f"]({rel_path})"
        lines = [
            line for line in self.index_path.read_text(encoding="utf-8").splitlines()
            if marker not in line
        ]
        atomic_write(self.index_path, "\n".join(lines) + "\n")

    def _upsert_index_line(self, layer, title, rel_path, description):
        """Update or insert a single index bullet under ## <layer> in MEMORY.md."""
        lines = self.index_path.read_text(encoding="utf-8").splitlines()
        new_line = f"- [{title}]({rel_path}) — {description}"
        link_marker = f"]({rel_path})"

        current_layer = ""
        found_same_layer = False
        cleaned_lines = []
        for line in lines:
            if line.startswith("#"):
                m = INDEX_LAYER_RE.match(line)
                current_layer = m.group(1) if m else ""
            if link_marker in line:
                if current_layer == layer and not found_same_layer:
                    cleaned_lines.append(new_line)
                    found_same_layer = True
                continue
            cleaned_lines.append(line)

        if not found_same_layer:
            inserted = []
            in_target = False
            did_insert = False
            for idx, line in enumerate(cleaned_lines):
                if line.startswith("## "):
                    m = INDEX_LAYER_RE.match(line)
                    if in_target and not did_insert:
                        if inserted and inserted[-1].strip() == "":
                            inserted.insert(len(inserted) - 1, new_line)
                        else:
                            inserted.append(new_line)
                        did_insert = True
                    in_target = bool(m and m.group(1) == layer)
                inserted.append(line)
                if idx == len(cleaned_lines) - 1 and in_target and not did_insert:
                    inserted.append(new_line)
                    did_insert = True
            cleaned_lines = inserted

        atomic_write(self.index_path, "\n".join(cleaned_lines) + "\n")

    def _current_entry_path(self, name):
        """Return the relative path of the live entry with this slug, if any."""
        for layer, folder in LAYER_DIRS.items():
            if layer not in self.allowed_layers:
                continue
            if (self.root / folder / f"{name}.md").is_file():
                return f"{folder}/{name}.md", layer, False
        if self.allow_user_managed and (self.root / "user-managed" / f"{name}.md").is_file():
            return f"user-managed/{name}.md", "", True
        return "", "", False

    def entry_history(self, name, limit=10):
        """List the saved previous versions of an entry kept in local trash backups."""
        if not NAME_RE.fullmatch(name):
            raise ValueError("Invalid entry name.")
        wanted = {
            name: "update",
            f"user-{name}": "update",
            f"migrated-{name}": "layer-change",
            f"archive-{name}": "archive",
            f"archive-user-{name}": "archive",
        }
        versions = []
        if self.trash_dir.is_dir():
            for path in sorted(self.trash_dir.iterdir()):
                match = TRASH_FILE_RE.match(path.name)
                if not match or match.group(2) not in wanted:
                    continue
                stamp = match.group(1)
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                meta, body = parse_frontmatter(text)
                versions.append({
                    "version": path.name,
                    "saved_at": (
                        f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T"
                        f"{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}"
                    ),
                    "reason": wanted[match.group(2)],
                    "updated_at": str(meta.get("updated_at") or "").strip(),
                    "title": meta.get("title") or extract_title(body, name),
                    "chars": len(body),
                })
        versions.sort(key=lambda row: row["saved_at"], reverse=True)
        rel_path, layer, user_managed = self._current_entry_path(name)
        result = {
            **self._result_metadata(),
            "name": name,
            "current": {"path": rel_path, "layer": layer, "user_managed": user_managed} if rel_path else None,
            "versions": versions[: max(1, int(limit or 10))],
            "total_versions": len(versions),
        }
        if versions:
            result["notice"] = (
                "Read a version with memory_restore(..., dry_run=true) first; restoring"
                " backs up the current version, so nothing is lost either way."
            )
        return result

    def restore_entry(self, version, name="", layer="", dry_run=False):
        """Put a saved previous version back as the current entry (or just show it)."""
        if self.mode == "ro" and not dry_run:
            raise ValueError(f"Library '{self.library_id}' is read-only.")
        version = (version or "").strip()
        match = TRASH_FILE_RE.match(version)
        if not match or "/" in version or "\\" in version:
            raise ValueError("version must be a backup file name from memory_history.")
        backup = self.trash_dir / version
        if not backup.is_file():
            raise ValueError(f"No saved version named {version} in this library.")
        text = backup.read_text(encoding="utf-8", errors="replace")
        meta, body = parse_frontmatter(text)
        slug = (name or "").strip() or str(meta.get("name") or "").strip()
        if not slug:
            slug = re.sub(r"^(?:user|migrated|archive|archive-user)-", "", match.group(2))
        if not NAME_RE.fullmatch(slug):
            raise ValueError("Could not tell which entry this version belongs to; pass name explicitly.")
        rel_path, cur_layer, user_managed = self._current_entry_path(slug)
        target_layer = (layer or "").strip() or cur_layer or str(meta.get("layer") or "").strip()
        if user_managed and not layer:
            target_layer = target_layer or "L2"
        if target_layer not in LAYER_DIRS:
            raise ValueError("Pass layer=L0..L4: this version does not record which layer it came from.")
        title = meta.get("title") or extract_title(body, slug)
        description = str(meta.get("description") or "").strip() or title
        if dry_run:
            return {
                **self._result_metadata(),
                "version": version,
                "name": slug,
                "layer": target_layer,
                "would_replace": rel_path or None,
                "title": title,
                "chars": len(body),
                "content": text,
                "restored": False,
            }
        res = self.upsert_entry(
            layer=target_layer,
            name=slug,
            title=title,
            description=description,
            body=body,
            topic_type=str(meta.get("type") or "").strip(),
            user_managed=user_managed,
        )
        res["restored"] = True
        res["restored_from"] = version
        res.pop("related", None)
        res.pop("notice", None)
        res.pop("content_warning", None)
        return res

    def archive_entry(self, name, user_managed=False):
        """Archive a user-managed entry or move a standard topic into archive/."""
        if self.mode == "ro":
            raise ValueError(f"Library '{self.library_id}' is read-only.")
        if user_managed and not self.allow_user_managed:
            raise ValueError(f"Library '{self.library_id}' does not allow user-managed entries.")
        if not user_managed and "L4" not in self.allowed_layers:
            raise ValueError(f"Layer L4 (archive) is not enabled in library '{self.library_id}'.")
        if not NAME_RE.fullmatch(name):
            raise ValueError("Invalid entry name.")
        ts_now = now_iso()
        with self.write_lock():
            if user_managed:
                target = self.root / "user-managed" / f"{name}.md"
                if not target.is_file():
                    raise ValueError(f"User-managed entry not found: {name}")
                self._backup_to_trash(target, f"archive-user-{name}")
                meta, body = parse_frontmatter(target.read_text(encoding="utf-8"))
                cre_at = meta.get("created_at") or meta.get("updated_at") or ts_now
                ordered = {
                    "name": meta.get("name", name),
                    "title": meta.get("title", extract_title(body, name)),
                    "description": meta.get("description", ""),
                    "type": meta.get("type", LAYER_DEFAULT_TYPE.get(meta.get("layer", "L2"), "feedback")),
                    "layer": meta.get("layer", "L2"),
                    "status": "archived",
                    "created_at": cre_at,
                    "updated_at": ts_now,
                }
                header = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in ordered.items())
                atomic_write(target, f"---\n{header}\n---\n\n{body.strip()}\n")
                rel_out = target.relative_to(self.root).as_posix()
                message = f"memory({self.library_id}): archive {rel_out}"
                sync_status = self._maybe_auto_commit(message)
                self._enqueue_async_sync(message)
                return self._with_sync_warning({
                    **self._result_metadata(),
                    "path": rel_out,
                    "status": "archived",
                    "updated_at": ts_now,
                }, sync_status)

            indexed = self.parse_index()
            for layer, folder in LAYER_DIRS.items():
                if layer == "L4" or layer not in self.allowed_layers:
                    continue
                candidate = self.root / folder / f"{name}.md"
                if candidate.is_file():
                    dest = self.root / "archive" / f"{name}.md"
                    if dest.exists():
                        raise ValueError(f"Archive file already exists: archive/{name}.md")
                    old_rel = f"{folder}/{name}.md"
                    idx_info = indexed.get(old_rel, {})
                    raw = candidate.read_text(encoding="utf-8")
                    meta, body = parse_frontmatter(raw)
                    cre_at, _, _, _ = file_timestamps(candidate, meta)
                    title = idx_info.get("title") or extract_title(body, name)
                    fm_desc = meta.get("description") or idx_info.get("description") or title
                    idx_desc = idx_info.get("description") or fm_desc
                    fm_name = meta.get("name", name)
                    name_serialized = fm_name if NAME_RE.fullmatch(fm_name) else json.dumps(fm_name, ensure_ascii=False)
                    desc_json = json.dumps(fm_desc, ensure_ascii=False)
                    fm_type = meta.get("type") if meta.get("type") in VALID_TOPIC_TYPES else "reference"
                    archived_raw = (
                        f"---\n"
                        f"name: {name_serialized}\n"
                        f"description: {desc_json}\n"
                        f"type: {fm_type}\n"
                        f"created_at: {json.dumps(cre_at)}\n"
                        f"updated_at: {json.dumps(ts_now)}\n"
                        f"---\n\n"
                        f"{body.strip()}\n"
                    )
                    new_rel = f"archive/{name}.md"
                    prev_index_text = self.index_path.read_text(encoding="utf-8")
                    self._backup_to_trash(candidate, f"archive-{name}")
                    atomic_write(dest, archived_raw)
                    try:
                        self._remove_index_line(old_rel)
                        self._upsert_index_line("L4", title, new_rel, idx_desc)
                    except Exception:
                        dest.unlink(missing_ok=True)
                        try:
                            atomic_write(self.index_path, prev_index_text)
                        except OSError:
                            pass
                        raise
                    candidate.unlink(missing_ok=True)
                    message = f"memory({self.library_id}): archive {old_rel} -> {new_rel}"
                    sync_status = self._maybe_auto_commit(message)
                    self._enqueue_async_sync(message)
                    return self._with_sync_warning({
                        **self._result_metadata(),
                        "from": old_rel,
                        "to": new_rel,
                        "layer": "L4",
                        "updated_at": ts_now,
                    }, sync_status)

        raise ValueError(f"Active entry not found: {name}")


class WorkspaceStore:
    """Multi-library facade uniting the personal memory root with mounted shared libraries."""

    def __init__(self, primary_root, home=None, async_scheduler=None):
        self.primary_root = data_root(primary_root).resolve(strict=True)
        self.home = home
        # MCP normally owns one process-local scheduler shared by all mounted
        # libraries.  Tests/embedders may inject one (and then own its lifecycle).
        self.async_scheduler = (
            async_scheduler if async_scheduler is not None else AsyncSyncScheduler()
        )
        self._owns_async_scheduler = async_scheduler is None
        self._config_signature = None
        self._stores = {}
        self._descriptors = []
        self._load_errors = {}
        self._config_issues = []
        self._load_stores(force=True)

    def shutdown(self):
        """Stop this workspace's background scheduler during process shutdown."""
        if self._owns_async_scheduler and self.async_scheduler is not None:
            self.async_scheduler.shutdown()

    def _load_stores(self, force=False):
        signature = config_signature(home=self.home)
        if not force and signature == self._config_signature and "personal" in self._stores:
            return

        config_issues = []
        descriptors = resolve_libraries(
            primary_root=self.primary_root, home=self.home, issues=config_issues
        )
        stores = {}
        load_errors = {}
        for issue in config_issues:
            issue_id = issue.get("id") or "config"
            diagnostic = issue.get("message") or str(issue)
            previous = load_errors.get(issue_id)
            load_errors[issue_id] = f"{previous}; {diagnostic}" if previous else diagnostic
        for desc in descriptors:
            lib_id = desc["id"]
            try:
                stores[lib_id] = MemoryStore(
                    desc["path"],
                    library_id=lib_id,
                    label=desc["label"],
                    role=desc["role"],
                    mode=desc["mode"],
                    allowed_layers=desc["layers"],
                    allow_user_managed=desc["allow_user_managed"],
                    sync_config=desc.get("sync"),
                    async_scheduler=self.async_scheduler,
                )
            except (ValueError, OSError) as exc:
                if lib_id == "personal":
                    raise
                # Skip temporarily unavailable mounted libraries instead of crashing MCP startup
                load_errors[lib_id] = str(exc) or exc.__class__.__name__
                continue
        self._descriptors = descriptors
        self._stores = stores
        self._load_errors = load_errors
        self._config_issues = config_issues
        self._config_signature = signature

    def _target_stores(self, library="personal"):
        self._load_stores()
        sel = (library or "personal").strip()
        if sel in {"*", "all"}:
            return list(self._stores.values())
        if sel not in self._stores:
            known = ", ".join(self._stores.keys())
            diagnostic = self._load_errors.get(sel)
            if diagnostic:
                raise ValueError(f"Library '{sel}' is unavailable: {diagnostic}")
            raise ValueError(f"Unknown library '{sel}'. Available: {known}")
        return [self._stores[sel]]

    def _resolve_single_store(self, library="personal"):
        self._load_stores()
        sel = (library or "personal").strip()
        if sel not in self._stores:
            known = ", ".join(self._stores.keys())
            diagnostic = self._load_errors.get(sel)
            if diagnostic:
                raise ValueError(f"Library '{sel}' is unavailable: {diagnostic}")
            raise ValueError(f"Unknown library '{sel}'. Available: {known}")
        return self._stores[sel]

    def list_libraries(self):
        self._load_stores()
        out = []
        represented_issue_ids = set()
        for desc in self._descriptors:
            lib_id = desc["id"]
            store = self._stores.get(lib_id)
            sync_cfg = desc.get("sync")
            entry_count = len(store.collect_entries(include_archived=False)) if store else 0
            async_status = store.async_sync_status() if store else {}
            issues = [
                issue
                for issue in self._config_issues
                if issue.get("id") == lib_id
            ]
            if issues:
                represented_issue_ids.add(lib_id)
            out.append(
                {
                    "id": lib_id,
                    "label": desc["label"],
                    "role": desc["role"],
                    "mode": desc["mode"],
                    "allowed_layers": list(desc["layers"]),
                    "allow_user_managed": bool(desc["allow_user_managed"]),
                    "path": str(desc["path"]),
                    "available": store is not None,
                    "error": self._load_errors.get(lib_id, ""),
                    "source_library": lib_id,
                    "provenance": desc["role"],
                    "trust": "personal" if desc["role"] == "personal" else "shared",
                    "read_only": desc["mode"] == "ro",
                    "entry_count": entry_count,
                    "async_sync": async_status,
                    "sync_driver": sync_cfg.get("driver") if sync_cfg else "",
                    "issues": issues,
                }
            )
        # Configuration errors and invalid mount entries may not resolve to a descriptor.
        # Keep them visible instead of silently disappearing from library diagnostics.
        for issue in self._config_issues:
            issue_id = issue.get("id") or "config"
            if issue_id in represented_issue_ids:
                continue
            represented_issue_ids.add(issue_id)
            message = issue.get("message") or str(issue)
            out.append(
                {
                    "id": issue_id,
                    "label": issue_id,
                    "role": "configuration",
                    "mode": "ro",
                    "allowed_layers": [],
                    "allow_user_managed": False,
                    "path": issue.get("path") or "",
                    "available": False,
                    "error": message,
                    "source_library": issue_id,
                    "provenance": "configuration",
                    "trust": "unknown",
                    "read_only": True,
                    "entry_count": 0,
                    "sync_driver": "",
                    "issues": [issue],
                }
            )
        return out

    @staticmethod
    def _per_store(stores, call):
        """Run call on each store; one broken library must not take every library down."""
        results, errors = [], []
        for st in stores:
            try:
                results.append((st, call(st)))
            except (ValueError, OSError) as exc:
                errors.append(f"{st.library_id}: {exc}")
                print(f"unified-memory: skipped library {st.library_id}: {exc}", file=sys.stderr)
        if errors and not results:
            raise ValueError("; ".join(errors))
        return results

    def search(self, query, layer="", include_archived=False, limit=10, library="personal", ranker=None):
        capped_limit = max(1, min(int(limit), 50))
        stores = self._target_stores(library)
        if len(stores) == 1:
            return stores[0].search(
                query, layer=layer, include_archived=include_archived, limit=capped_limit, ranker=ranker
            )

        merged = []
        for st, hits in self._per_store(
            stores,
            lambda st: st.search(
                query, layer=layer, include_archived=include_archived, limit=capped_limit, ranker=ranker
            ),
        ):
            for hit in hits:
                try:
                    dt, _ = parse_timestamp(hit.get("updated_at", ""))
                    upd_ep = dt.timestamp()
                except ValueError:
                    upd_ep = 0.0
                merged.append((hit, upd_ep))
        merged.sort(
            key=lambda pair: (
                -pair[0].get("score", 0),
                -pair[1],
                0 if pair[0].get("library") == "personal" else 1,
                pair[0].get("layer", ""),
                pair[0].get("path", ""),
            )
        )
        return [pair[0] for pair in merged[:capped_limit]]

    def list_entries(self, layer="", include_archived=False, sort="updated", library="personal"):
        stores = self._target_stores(library)
        if len(stores) == 1:
            return stores[0].list_entries(layer=layer, include_archived=include_archived, sort=sort)

        merged = []
        for st, items in self._per_store(
            stores, lambda st: st.list_entries(layer=layer, include_archived=include_archived, sort=sort)
        ):
            for item in items:
                try:
                    upd_ep = parse_timestamp(item.get("updated_at", ""))[0].timestamp()
                except ValueError:
                    upd_ep = 0.0
                try:
                    cre_ep = parse_timestamp(item.get("created_at", ""))[0].timestamp()
                except ValueError:
                    cre_ep = 0.0
                merged.append((item, cre_ep, upd_ep))

        if sort == "created":
            merged.sort(key=lambda t: (-t[1], -t[2], 0 if t[0]["library"] == "personal" else 1, t[0]["path"]))
        elif sort == "layer":
            merged.sort(key=lambda t: (t[0]["layer"], -t[2], 0 if t[0]["library"] == "personal" else 1, t[0]["path"]))
        else:
            merged.sort(key=lambda t: (-t[2], 0 if t[0]["library"] == "personal" else 1, t[0]["layer"], t[0]["path"]))
        return [t[0] for t in merged]

    def read_entry(self, rel_path, library="", section="", outline=False):
        self._load_stores()
        target_lib = (library or "").strip()
        effective_path = rel_path
        if not target_lib and ":" in rel_path:
            prefix, rest = rel_path.split(":", 1)
            if prefix in self._stores and rest:
                target_lib = prefix
                effective_path = rest
        store = self._resolve_single_store(target_lib or "personal")
        return store.read_entry(effective_path, section=section, outline=outline)

    def upsert_entry(self, layer, name, title, description, body, topic_type="", user_managed=False, library="personal",
                     expected_updated_at=""):
        store = self._resolve_single_store(library or "personal")
        return store.upsert_entry(
            layer=layer,
            name=name,
            title=title,
            description=description,
            body=body,
            topic_type=topic_type,
            user_managed=user_managed,
            expected_updated_at=expected_updated_at,
        )

    def archive_entry(self, name, user_managed=False, library="personal"):
        store = self._resolve_single_store(library or "personal")
        return store.archive_entry(name=name, user_managed=user_managed)

    def entry_history(self, name, limit=10, library="personal"):
        store = self._resolve_single_store(library or "personal")
        return store.entry_history(name=name, limit=limit)

    def restore_entry(self, version, name="", layer="", dry_run=False, library="personal"):
        store = self._resolve_single_store(library or "personal")
        return store.restore_entry(version=version, name=name, layer=layer, dry_run=dry_run)


TOOLS = [
    {
        "name": "memory_libraries",
        "description": "List the personal memory library and all mounted shared libraries with availability diagnostics, read/write mode, allowed layers, and sync driver.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "memory_search",
        "description": "Search the personal library by default; pass library='*' or 'all' to search personal plus mounted shared libraries, or pass a mount id explicitly.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords, phrase, or temporal cue."},
                "layer": {"type": "string", "enum": ["", "L0", "L1", "L2", "L3", "L4"], "default": ""},
                "include_archived": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 10},
                "library": {"type": "string", "default": "personal", "description": "Target library id (defaults to 'personal'; use '*' or 'all' for all mounted libraries, or a shared mount id)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_list",
        "description": "List indexed L0-L4 and user-managed memory entries from the personal library by default; pass library='*' or 'all' to include mounted shared libraries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "layer": {"type": "string", "enum": ["", "L0", "L1", "L2", "L3", "L4"], "default": ""},
                "include_archived": {"type": "boolean", "default": False},
                "sort": {"type": "string", "enum": ["updated", "created", "layer"], "default": "updated"},
                "library": {"type": "string", "default": "personal", "description": "Target library id (defaults to 'personal'; use '*' or 'all' for all mounted libraries, or a shared mount id)."},
            },
        },
    },
    {
        "name": "memory_read",
        "description": "Read a specific memory Markdown file by relative path (e.g. 'MEMORY.md', 'projects/example.md', or 'team-kb:methods/guide.md'). Large entries also return a section outline; pass outline=true to get only that outline, or section='<title>' to read a single section instead of the whole file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path inside the memory library, optionally prefixed with '<library>:'."},
                "library": {"type": "string", "default": "", "description": "Optional library id (defaults to 'personal' unless path has '<library>:' prefix)."},
                "section": {"type": "string", "default": "", "description": "Read only this '## ' section (exact title, or a substring of it)."},
                "outline": {"type": "boolean", "default": False, "description": "Return the section outline and the intro only, so you can pick one section to read."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "memory_upsert",
        "description": "Apply the writing rules in MEMORY.md §0. Search for the topic first. Use its existing name and supply the complete revised body when updating. Defaults to writing into the personal library; specify 'library' explicitly to write to a writable shared library.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "layer": {"type": "string", "enum": ["L0", "L1", "L2", "L3", "L4"]},
                "name": {"type": "string", "description": "Kebab-case slug matching ^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$."},
                "title": {"type": "string", "description": "Single-line human-readable title."},
                "description": {"type": "string", "description": "Concise single-line retrieval cue."},
                "body": {"type": "string", "description": "Markdown body with conditions, evidence, and dates. Do not include frontmatter or the H1 title; the server writes both (a leading frontmatter block is stripped)."},
                "topic_type": {"type": "string", "enum": ["", "user", "feedback", "project", "reference"], "default": ""},
                "user_managed": {"type": "boolean", "default": False, "description": "Only set true when the user explicitly requests changing user-managed/ in the personal library."},
                "library": {"type": "string", "default": "personal", "description": "Target library id (defaults to 'personal'). Shared libraries must be mounted rw and allow the chosen layer."},
                "expected_updated_at": {"type": "string", "default": "", "description": "The updated_at you based your edit on. The write is rejected if the entry changed since then, so a parallel session's edit is not lost."},
            },
            "required": ["layer", "name", "title", "description", "body"],
        },
    },
    {
        "name": "memory_history",
        "description": "List the saved previous versions of an entry, so a wrong or lossy write can be inspected and put back. Defaults to the personal library.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Entry slug name (the file stem, without .md)."},
                "limit": {"type": "integer", "default": 10},
                "library": {"type": "string", "default": "personal", "description": "Target library id (defaults to 'personal')."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "memory_restore",
        "description": "Put a previous version from memory_history back as the current entry; the version being replaced is backed up first. Use dry_run=true to read a version without changing anything.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "version": {"type": "string", "description": "A 'version' value returned by memory_history."},
                "name": {"type": "string", "default": "", "description": "Entry slug, only needed when the version cannot tell."},
                "layer": {"type": "string", "enum": ["", "L0", "L1", "L2", "L3", "L4"], "default": "", "description": "Target layer, only needed when the entry no longer exists."},
                "dry_run": {"type": "boolean", "default": False, "description": "Return the saved content without writing it back."},
                "library": {"type": "string", "default": "personal", "description": "Target library id (defaults to 'personal')."},
            },
            "required": ["version"],
        },
    },
    {
        "name": "memory_archive",
        "description": "Archive a user-managed entry or move an L0-L3 entry to archive/ (L4). Defaults to the personal library.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Entry slug name."},
                "user_managed": {"type": "boolean", "default": False},
                "library": {"type": "string", "default": "personal", "description": "Target library id (defaults to 'personal')."},
            },
            "required": ["name"],
        },
    },
]


CONFIGURED_CLIENT = os.environ.get("UNIFIED_MEMORY_CLIENT")
CLIENT_NAME = audit_log.normalize_client(CONFIGURED_CLIENT)
AUDIT_HOME = None


def _audit_event(name, args, data, ok, ms, error="", result=None):
    """Build a body-free audit event for one tool call."""
    ev = {
        "source": "mcp",
        "client": CLIENT_NAME,
        "tool": name,
        "ok": ok,
        "ms": ms,
        "library": args.get("library") if isinstance(args, dict) else None,
    }
    if isinstance(args, dict):
        if name == "memory_search":
            ev["query"] = str(args.get("query", ""))[:120]
        if args.get("layer"):
            ev["layer"] = args.get("layer")
    if not ok:
        ev["error"] = error[:160]
        return ev
    text = _result_text(result)
    if text:
        # Size of what the client actually received, so token cost can be measured.
        ev["bytes"] = len(text.encode("utf-8"))
        ev["tokens"] = audit_log.estimate_tokens(text)
    if name == "memory_search" and isinstance(data, list):
        ev["hits"] = len(data)
        ev["refs"] = [h.get("ref") for h in data[: audit_log.MAX_AUDIT_REFS] if isinstance(h, dict) and h.get("ref")]
    elif name == "memory_list" and isinstance(data, list):
        ev["hits"] = len(data)
    elif isinstance(data, dict):
        ev["ref"] = data.get("ref") or data.get("path") or data.get("to")
        ev["library"] = data.get("library") or ev.get("library")
        if name in ("memory_upsert", "memory_archive", "memory_restore"):
            ev["action"] = data.get("status") or data.get("action")
    return ev


def _result_text(result):
    if not isinstance(result, dict):
        return ""
    return "".join(
        str(item.get("text", "")) for item in result.get("content") or [] if isinstance(item, dict)
    )


def handle_tool_call(store, name, args):
    """Dispatch a tools/call invocation and record a local audit event."""
    started = time.monotonic()
    try:
        result, data = _handle_tool_call(store, name, args)
    except Exception as exc:
        audit_log.record(_audit_event(name, args, None, False, int((time.monotonic() - started) * 1000), str(exc)), home=AUDIT_HOME)
        raise
    audit_log.record(
        _audit_event(name, args, data, True, int((time.monotonic() - started) * 1000), result=result),
        home=AUDIT_HOME,
    )
    return result


def _handle_tool_call(store, name, args):
    """Dispatch an MCP tools/call invocation; return (mcp_result, raw_data)."""
    if name == "memory_libraries":
        data = store.list_libraries() if hasattr(store, "list_libraries") else []
    elif name == "memory_search":
        kwargs = dict(
            query=args.get("query", ""),
            layer=args.get("layer", ""),
            include_archived=bool(args.get("include_archived", False)),
            limit=args.get("limit", 10),
        )
        if isinstance(store, WorkspaceStore):
            kwargs["library"] = args.get("library", "personal")
        data = store.search(**kwargs)
    elif name == "memory_list":
        kwargs = dict(
            layer=args.get("layer", ""),
            include_archived=bool(args.get("include_archived", False)),
            sort=args.get("sort", "updated"),
        )
        if isinstance(store, WorkspaceStore):
            kwargs["library"] = args.get("library", "personal")
        data = store.list_entries(**kwargs)
    elif name == "memory_read":
        kwargs = dict(
            section=args.get("section", "") or "",
            outline=bool(args.get("outline", False)),
        )
        if isinstance(store, WorkspaceStore):
            kwargs["library"] = args.get("library", "")
        data = store.read_entry(args["path"], **kwargs)
    elif name == "memory_upsert":
        kwargs = dict(
            layer=args["layer"],
            name=args["name"],
            title=args["title"],
            description=args["description"],
            body=args["body"],
            topic_type=args.get("topic_type", ""),
            user_managed=bool(args.get("user_managed", False)),
            expected_updated_at=args.get("expected_updated_at", "") or "",
        )
        if isinstance(store, WorkspaceStore):
            kwargs["library"] = args.get("library", "personal")
        data = store.upsert_entry(**kwargs)
    elif name == "memory_history":
        kwargs = dict(name=args["name"], limit=int(args.get("limit", 10) or 10))
        if isinstance(store, WorkspaceStore):
            kwargs["library"] = args.get("library", "personal")
        data = store.entry_history(**kwargs)
    elif name == "memory_restore":
        kwargs = dict(
            version=args["version"],
            name=args.get("name", "") or "",
            layer=args.get("layer", "") or "",
            dry_run=bool(args.get("dry_run", False)),
        )
        if isinstance(store, WorkspaceStore):
            kwargs["library"] = args.get("library", "personal")
        data = store.restore_entry(**kwargs)
    elif name == "memory_archive":
        kwargs = dict(
            name=args["name"],
            user_managed=bool(args.get("user_managed", False)),
        )
        if isinstance(store, WorkspaceStore):
            kwargs["library"] = args.get("library", "personal")
        data = store.archive_entry(**kwargs)
    else:
        raise ValueError(f"Unknown tool: {name}")
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}]}, data


def process_one_message(store, msg):
    """Process one JSON-RPC request dict; return response dict or None for notifications."""
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}

    msg_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}

    if msg_id is None:
        return None

    try:
        if method == "initialize":
            global CLIENT_NAME
            CLIENT_NAME = audit_log.normalize_client(CONFIGURED_CLIENT or (params.get("clientInfo") or {}).get("name", ""))
            requested = params.get("protocolVersion", "")
            negotiated = requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
            result = {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "unified-memory", "version": "1.2.0"},
            }
            if CONFIGURED_CLIENT == "DeepSeek Harness":
                result["instructions"] = (
                    "When past decisions or preferences may be relevant, search Unified Memory. "
                    "When asked to remember something, read MEMORY.md §0 and follow its writing rules. "
                    "Do not save ordinary conversation automatically."
                )
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = handle_tool_call(store, params.get("name", ""), params.get("arguments") or {})
        else:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    except Exception as exc:
        if method == "tools/call":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True},
            }
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": str(exc)}}


def send_response(payload):
    """Write a single newline-delimited JSON-RPC message to stdout."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve_stdio(store):
    """Run JSON-RPC 2.0 MCP server loop over stdin/stdout (single & batch)."""
    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                send_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}})
                continue

            if isinstance(parsed, list):
                if not parsed:
                    send_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Empty batch"}})
                    continue
                responses = [resp for item in parsed for resp in [process_one_message(store, item)] if resp is not None]
                if responses:
                    send_response(responses)
            else:
                resp = process_one_message(store, parsed)
                if resp is not None:
                    send_response(resp)
    finally:
        shutdown = getattr(store, "shutdown", None)
        if callable(shutdown):
            shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-root", type=Path, default=DEFAULT_ROOT, help="Path to unified memory root.")
    args = parser.parse_args()
    store = WorkspaceStore(args.memory_root)
    serve_stdio(store)


if __name__ == "__main__":
    main()
