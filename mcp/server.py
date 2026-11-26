#!/usr/bin/env python3
"""Zero-dependency MCP stdio server for the unified L0-L4 Markdown memory library."""

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import threading
import time
from tempfile import NamedTemporaryFile


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from library_paths import (
    ALL_LAYERS,
    config_signature,
    data_root,
    library_state_dir,
    lock_file,
    resolve_libraries,
    unlock_file,
)
import audit_log
import memory_lint
import secret_guard
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
# Chinese has no spaces, so queries are matched by overlapping bigrams. These
# function particles carry no topic, and bigrams that straddle one (的打 in
# 桌面版的打包) match unrelated entries while pushing up the number of bigrams a
# real match has to reach. Only particles that do not appear inside technical
# compounds are listed: 地/得/在/对/过 are excluded because 地址, 得到, 在线,
# 对齐, 过期 are real terms in these libraries.
CJK_PARTICLE_RE = re.compile(r"[的吗呢吧嘛呀啦啊哦哈]+")
PROCEDURE_SUFFIXES = ("步骤", "方法", "流程")
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


def now_iso(previous=""):
    """Stamp a write newer than its previous version, even if the clock repeats."""
    now = datetime.now().astimezone()
    if previous:
        try:
            old, has_clock_time = parse_timestamp(previous)
            if has_clock_time and old >= now:
                now = old + timedelta(microseconds=1)
        except (ValueError, OverflowError):
            pass  # An unparseable legacy timestamp is not a clock floor.
    return now.isoformat(timespec="microseconds")


def local_tz():
    """Return current system local tzinfo."""
    return datetime.now().astimezone().tzinfo


def normalize_iso_date(value, field_name="date"):
    """Validate an optional YYYY-MM-DD date string; return '' when empty."""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        raise ValueError(f"{field_name} must be a YYYY-MM-DD date.")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid YYYY-MM-DD date.") from exc


def is_date_expired(valid_until, today=None):
    """Return True when valid_until is a valid YYYY-MM-DD strictly before today."""
    text = str(valid_until or "").strip()
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        return False
    try:
        until_date = date.fromisoformat(text)
    except ValueError:
        return False
    ref_day = today or datetime.now().astimezone().date()
    return until_date < ref_day


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
FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
# Progressive disclosure: large entries get a section outline so an agent can read
# one section instead of the whole file (MemGPT-style paging over Markdown).
OUTLINE_MIN_CHARS = 1200
# Entries that cite each other turn a flat library into a small graph: reading one
# conclusion shows the neighbours worth reading next (A-MEM, HippoRAG).
ENTRY_LINK_RE = re.compile(r"\b(constraints|methods|preferences|projects|archive|user-managed)/([A-Za-z0-9][A-Za-z0-9._-]*)\.md\b")
MAX_ENTRY_LINKS = 5
SEE_ALSO_LIMIT = 3
MAX_BATCH_READ = 10


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
    in_fence = ""
    for line in body.replace("\r\n", "\n").split("\n"):
        fence = FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)[0]
            if not in_fence:
                in_fence = marker
            elif in_fence == marker:
                in_fence = ""
        match = None if in_fence else SECTION_HEADING_RE.match(line)
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


def patch_markdown_section(existing_body, section_query, new_body, mode="replace"):
    """Replace or append a single '## ' section without disturbing the rest of the entry.

    Returns (patched_body, resolved_section_title, section_action).
    If no existing section matches, appends a new '## <section_query>' section at the end.
    """
    wanted = (section_query or "").strip()
    if not wanted or "\n" in wanted:
        raise ValueError("section must be a single non-empty heading title.")
    if wanted.startswith("#"):
        wanted = wanted.lstrip("#").strip()
    if not wanted:
        raise ValueError("section must be a single non-empty heading title.")

    preamble_lines = []
    sections = []
    current = None
    in_fence = ""
    for line in (existing_body or "").replace("\r\n", "\n").split("\n"):
        fence = FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)[0]
            if not in_fence:
                in_fence = marker
            elif in_fence == marker:
                in_fence = ""
        match = None if in_fence else SECTION_HEADING_RE.match(line)
        if match and len(match.group(1)) == 2:
            if current is not None:
                sections.append(current)
            current = {"title": match.group(2).strip(), "lines": [line]}
        elif current is not None:
            current["lines"].append(line)
        else:
            preamble_lines.append(line)
    if current is not None:
        sections.append(current)

    folded = wanted.casefold()
    exact_idx = [i for i, sec in enumerate(sections) if sec["title"].casefold() == folded]
    if exact_idx:
        match_idx = exact_idx[0]
    else:
        sub_idx = [i for i, sec in enumerate(sections) if folded in sec["title"].casefold()]
        if len(sub_idx) > 1:
            candidates = ", ".join(sections[i]["title"] for i in sub_idx)
            raise ValueError(f"Ambiguous section '{wanted}'. Matches: {candidates}")
        match_idx = sub_idx[0] if len(sub_idx) == 1 else None

    chunk = (new_body or "").strip()
    if match_idx is not None:
        target = sections[match_idx]
        resolved_title = target["title"]
        if mode == "append":
            first_line, _, rest = chunk.partition("\n")
            first_match = SECTION_HEADING_RE.match(first_line)
            if first_match and len(first_match.group(1)) == 2 and first_match.group(2).strip().casefold() == resolved_title.casefold():
                chunk = rest.strip()
            existing_sec_text = "\n".join(target["lines"]).rstrip()
            target["lines"] = (f"{existing_sec_text}\n\n{chunk}" if chunk else existing_sec_text).split("\n")
            action = "appended"
        else:
            first_line = chunk.split("\n", 1)[0]
            first_match = SECTION_HEADING_RE.match(first_line)
            if first_match and len(first_match.group(1)) == 2:
                resolved_title = first_match.group(2).strip()
                replacement = chunk
            else:
                replacement = f"## {resolved_title}\n\n{chunk}"
            target["lines"] = replacement.split("\n")
            action = "replaced"
    else:
        first_line = chunk.split("\n", 1)[0]
        first_match = SECTION_HEADING_RE.match(first_line)
        if first_match and len(first_match.group(1)) == 2:
            resolved_title = first_match.group(2).strip()
            new_block = chunk
        else:
            resolved_title = wanted
            new_block = f"## {wanted}\n\n{chunk}"
        sections.append({"title": resolved_title, "lines": new_block.split("\n")})
        action = "created"

    blocks = []
    preamble_text = "\n".join(preamble_lines).strip()
    if preamble_text:
        blocks.append(preamble_text)
    for sec in sections:
        sec_text = "\n".join(sec["lines"]).strip()
        if sec_text:
            blocks.append(sec_text)
    return "\n\n".join(blocks), resolved_title, action


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


def atomic_create(path, content):
    """Atomically create a new UTF-8 text file, raising FileExistsError if it exists."""
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
        os.link(staged, path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def format_topic_markdown(name, description, entry_type, created_at, updated_at, body,
                          *, verified_at="", valid_until=""):
    """Serialize an indexed L0-L4 Markdown memory entry with canonical frontmatter."""
    name_serialized = name if NAME_RE.fullmatch(name) else json.dumps(name, ensure_ascii=False)
    desc_serialized = json.dumps(description, ensure_ascii=False)
    extra_fm = ""
    if verified_at:
        extra_fm += f"verified_at: {json.dumps(verified_at)}\n"
    if valid_until:
        extra_fm += f"valid_until: {json.dumps(valid_until)}\n"
    return (
        f"---\n"
        f"name: {name_serialized}\n"
        f"description: {desc_serialized}\n"
        f"type: {entry_type}\n"
        f"created_at: {json.dumps(created_at)}\n"
        f"updated_at: {json.dumps(updated_at)}\n"
        f"{extra_fm}"
        f"---\n\n"
        f"{body.strip()}\n"
    )


def format_user_markdown(name, title, description, entry_type, layer, status, created_at, updated_at, body,
                         *, verified_at="", valid_until=""):
    """Serialize a user-managed Markdown entry with canonical frontmatter."""
    meta = {
        "name": name,
        "title": title,
        "description": description,
        "type": entry_type,
        "layer": layer,
        "status": status,
        "created_at": created_at or updated_at,
        "updated_at": updated_at,
    }
    if verified_at:
        meta["verified_at"] = verified_at
    if valid_until:
        meta["valid_until"] = valid_until
    header = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items())
    return f"---\n{header}\n---\n\n{body}\n"


def cjk_chunks(cjk_run):
    """Split a Chinese run at function particles, keeping pieces of 2+ chars."""
    return [part for part in CJK_PARTICLE_RE.split(cjk_run) if len(part) > 1]


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
        for run in CJK_RUN_RE.findall(term):
            for cjk in cjk_chunks(run):
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


def procedure_phrase_variants(query):
    """Keep the intact topic of a standalone Chinese procedure phrase."""
    if len(query) < 4 or not CJK_RUN_RE.fullmatch(query):
        return ()
    topic, suffix = query[:-2], query[-2:]
    if suffix not in PROCEDURE_SUFFIXES:
        return ()
    return tuple(topic + other for other in PROCEDURE_SUFFIXES if other != suffix)


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
        for chunk in cjk_chunks(run):
            tokens.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
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


_ENTRY_CACHE = {}
_ENTRY_CACHE_LOCK = threading.Lock()
ENTRY_CACHE_LIMIT = 20000
# A file changed this recently can change again without its timestamps moving on a
# filesystem with coarse timestamps, so it is read from disk until it settles.
ENTRY_CACHE_SETTLE_NS = 2_000_000_000


def load_entry(path):
    """Read one Markdown entry and return its frontmatter fields and body.

    Search and listing read every entry on each call. The parsed result is kept
    in memory and reused while the file's modification time, change time, size
    and inode all stay the same.
    """
    path = Path(path)
    stat = path.stat()
    stamp = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
    key = str(path)
    with _ENTRY_CACHE_LOCK:
        hit = _ENTRY_CACHE.get(key)
    if hit and hit[0] == stamp:
        return dict(hit[1]), hit[2]
    # Match the Web manager: a stray non-UTF-8 file must not break every query.
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    settled = time.time_ns() - max(stat.st_mtime_ns, stat.st_ctime_ns) > ENTRY_CACHE_SETTLE_NS
    with _ENTRY_CACHE_LOCK:
        if settled:
            if len(_ENTRY_CACHE) >= ENTRY_CACHE_LIMIT:
                _ENTRY_CACHE.clear()
            _ENTRY_CACHE[key] = (stamp, meta, body)
        else:
            _ENTRY_CACHE.pop(key, None)
    return dict(meta), body


def relevance(raw_terms, subtokens, procedure_variants, name, title, description, body):
    """Score one entry for a query; MCP search and the Web manager both use this.

    Returns ``(score, matched, matched_phrase)``. A score of 0 means no match.
    """
    title_cf = title.casefold()
    score, matched = score_fields(
        raw_terms, subtokens, name.casefold(), title_cf, description.casefold(), body.casefold()
    )
    if score:
        return score, matched, ""
    # A complete variant in a title provides better evidence than
    # scattered bigrams, but is weaker than a literal title hit.
    # Do not accept a variant from a passing mention in the body.
    phrase = next((v for v in procedure_variants if v in title_cf), "")
    return (4, [phrase], phrase) if phrase else (0, [], "")


def apply_freshness(score, age_sec, has_temporal_cue, expired, ranker="legacy"):
    """Raise recent entries for a time-related query and lower expired ones."""
    if has_temporal_cue:
        age_sec = max(0.0, age_sec)
        if age_sec <= 86400:
            score += 1.0 if ranker == "bm25f" else 6
        elif age_sec <= 7 * 86400:
            score += 0.5 if ranker == "bm25f" else 3
    if expired:
        # Gently demote expired entries so current facts rank first while keeping history findable.
        score = round(max(0.001, score * 0.7), 3) if ranker == "bm25f" else max(1, score - 3)
    return score


def cited_paths(body, known, shown, limit=SEE_ALSO_LIMIT):
    """List the entry paths a body cites that exist in ``known`` and are not in ``shown``."""
    seen = set(shown)
    found = []
    for folder, stem in ENTRY_LINK_RE.findall(body or ""):
        ref = f"{folder}/{stem}.md"
        if ref not in known or ref in seen:
            continue
        seen.add(ref)
        found.append(ref)
        if len(found) >= limit:
            break
    return found


def match_strength(raw_terms, matched):
    """How much of the query an entry covers: "full", "partial" or "fuzzy".

    "full" means every query word appears in the entry. "partial" means only
    some do. "fuzzy" means no whole word matched and the hit came from CJK
    bigrams, alphanumeric fragments, or a related phrase. LongMemEval
    (arXiv 2410.10813) scores abstention: when memory lacks the answer, the
    agent should say so instead of using the closest entry. Weak matches are
    the signal for that.
    """
    terms = set(raw_terms)
    whole = terms & set(matched)
    if not whole:
        return "fuzzy"
    return "full" if whole == terms else "partial"


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
        audit_source="sync",
        audit_client=None,
        log_sync_warnings=False,
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
        self.audit_source = audit_source
        self.audit_client = audit_client
        self.log_sync_warnings = bool(log_sync_warnings)
        self.last_sync_status = None
        self.state_dir = library_state_dir(self.root)
        self.lock_path = self.state_dir / "library.lock"
        self.trash_dir = self.state_dir / "trash"

    @contextmanager
    def write_lock(self):
        """Serialize concurrent writes across multiple MCP servers and Web UI."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as fh:
            lock_file(fh.fileno())
            try:
                yield
            finally:
                unlock_file(fh.fileno())

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
    def _check_expected_updated(expected_updated_at, old_meta=None, target=None):
        """Reject a write whose base version is stale (lost-update protection)."""
        expected = (expected_updated_at or "").strip()
        if not expected:
            return
        if target is None:
            raise ValueError("Entry changed since you read it: no current entry exists. Read it again before writing.")
        meta = old_meta or {}
        actual = str(meta.get("updated_at") or "").strip()
        if not actual:
            _, actual, _, _ = file_timestamps(target, meta)
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
        self.last_sync_status = None
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
            self.last_sync_status = status
            audit_log.record(
                {
                    "source": self.audit_source,
                    "event": "git_auto_commit",
                    "client": self.audit_client or CLIENT_NAME,
                    "tool": "git_auto_commit",
                    "ok": False,
                    "library": self.library_id,
                    "message": redacted(message),
                    "reason": reason,
                    "error": detail,
                },
                home=AUDIT_HOME,
            )
            if self.log_sync_warnings:
                try:
                    print(f"[unified-memory] {status['warning']}", file=sys.stderr)
                except Exception:
                    pass
            return status

        try:
            result = sync_git.auto_commit(self.root, message)
        except Exception as exc:
            return failure(None, error=exc)
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
                meta, body = load_entry(md)
                cre_at, upd_at, cre_ep, upd_ep = file_timestamps(md, meta)
                idx = indexed.get(rel, {})
                verified_at = (meta.get("verified_at") or "").strip()
                valid_until = (meta.get("valid_until") or "").strip()
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
                        "verified_at": verified_at,
                        "valid_until": valid_until,
                        "expired": is_date_expired(valid_until),
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
                meta, body = load_entry(md)
                status = meta.get("status", "active")
                if status == "archived" and not include_archived:
                    continue
                layer_val = meta.get("layer", "L2")
                if layer_val not in self.allowed_layers:
                    continue
                cre_at, upd_at, cre_ep, upd_ep = file_timestamps(md, meta)
                verified_at = (meta.get("verified_at") or "").strip()
                valid_until = (meta.get("valid_until") or "").strip()
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
                        "verified_at": verified_at,
                        "valid_until": valid_until,
                        "expired": is_date_expired(valid_until),
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

        ``score_fields`` decides literal and fragment matches; a related
        procedure title can supply a weak fallback. ``ranker`` decides result
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
                    "match": "recent",
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
        procedure_variants = procedure_phrase_variants(effective_q)
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
            score, matched, matched_phrase = relevance(
                raw_terms, subtokens, procedure_variants,
                item["name"], item["title"], item["description"], item["body"],
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

            score = apply_freshness(
                score, now_ep - item["updated_epoch"], has_temporal_cue,
                bool(item.get("expired")) and item["layer"] != "L4", ranker,
            )

            snippet = ""
            for line in item["body"].splitlines():
                line_cf = line.casefold()
                if any(tok in line_cf for tok in matched):
                    snippet = line.strip()[:240]
                    break

            results.append(
                {
                    "score": score,
                    "match": match_strength(raw_terms, matched),
                    **({"matched_phrase": matched_phrase} if matched_phrase else {}),
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
                    **({"verified_at": item["verified_at"]} if item.get("verified_at") else {}),
                    **({"valid_until": item["valid_until"]} if item.get("valid_until") else {}),
                    **({"expired": True} if item.get("expired") else {}),
                    "_updated_epoch": item["updated_epoch"],
                    "_legacy_score": legacy_score,
                    "snippet": snippet or first_summary_snippet(item["body"]),
                }
            )

        results.sort(key=lambda r: (-r["score"], -r["_legacy_score"], -r["_updated_epoch"], r["layer"], r["path"]))
        for r in results:
            r.pop("_updated_epoch", None)
            r.pop("_legacy_score", None)
        results = results[:capped_limit]
        self._attach_see_also(results, all_items)
        return results

    def _attach_see_also(self, results, items, limit=SEE_ALSO_LIMIT):
        """Give the top hit the entries it cites that the search did not return.

        HippoRAG (arXiv 2405.14831) and "From RAG to Memory" (2502.14802) find
        that following associations recovers facts a single lookup misses. Our
        associations are the ``methods/xxx.md`` citations agents write, so the
        cheapest one-hop step is to name the top hit's citations.
        """
        if not results or results[0].get("match") == "fuzzy":
            return
        by_path = {it["path"]: it for it in items}
        top = by_path.get(results[0]["path"])
        if not top:
            return
        see_also = [
            {"ref": self._entry_ref(ref), "title": by_path[ref].get("title", "")}
            for ref in cited_paths(top.get("body"), by_path, {r["path"] for r in results}, limit)
        ]
        if see_also:
            results[0]["see_also"] = see_also

    def list_entries(self, layer="", include_archived=False, sort="updated", limit=0):
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
        cap = max(0, int(limit or 0))
        if cap:
            collected = collected[:cap]

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
                **({"verified_at": item["verified_at"]} if item.get("verified_at") else {}),
                **({"valid_until": item["valid_until"]} if item.get("valid_until") else {}),
                **({"expired": True} if item.get("expired") else {}),
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
        verified_at = str(meta.get("verified_at") or "").strip()
        valid_until = str(meta.get("valid_until") or "").strip()
        if verified_at:
            base["verified_at"] = verified_at
        if valid_until:
            base["valid_until"] = valid_until
            if is_date_expired(valid_until):
                base["expired"] = True
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
        links, backlinks = self.entry_links(rel_posix, body)
        if links:
            result["links"] = links
        if backlinks:
            result["backlinks"] = backlinks
        return result

    def read_entries(self, rel_paths, section="", outline=False):
        """Read up to MAX_BATCH_READ Markdown entries in a single call."""
        if not isinstance(rel_paths, (list, tuple)):
            raise ValueError("paths must be a list of relative markdown paths.")
        cleaned = []
        for raw in rel_paths:
            text = str(raw or "").strip()
            if text and text not in cleaned:
                cleaned.append(text)
        if not cleaned:
            raise ValueError("paths must contain at least one relative markdown path.")
        if len(cleaned) > MAX_BATCH_READ:
            raise ValueError(f"At most {MAX_BATCH_READ} paths can be read in one batch.")
        entries, errors = [], []
        for path in cleaned:
            try:
                entries.append(self.read_entry(path, section=section, outline=outline))
            except ValueError as exc:
                errors.append({"path": path, "error": str(exc)})
        if not entries and errors:
            raise ValueError(errors[0]["error"])
        result = {"count": len(entries), "entries": entries}
        if errors:
            result["errors"] = errors
        return result

    def entry_links(self, rel_posix, body, limit=MAX_ENTRY_LINKS):
        """Entries this one cites, and entries that cite it.

        The index lists every entry, so links from MEMORY.md say nothing.
        """
        if rel_posix == self.index_path.name:
            return [], []
        wanted = {}
        for folder, stem in ENTRY_LINK_RE.findall(body):
            ref = f"{folder}/{stem}.md"
            if ref != rel_posix:
                wanted.setdefault(ref, True)
        entries = {}
        backlinks = []
        for item in self.collect_entries(include_archived=True):
            entries[item["path"]] = item
            if item["path"] == rel_posix:
                continue
            if rel_posix in (item.get("body") or ""):
                backlinks.append({"ref": self._entry_ref(item["path"]), "title": item.get("title", "")})
        links = [
            {"ref": self._entry_ref(ref), "title": entries[ref].get("title", "")}
            for ref in wanted
            if ref in entries
        ]
        return links[:limit], backlinks[:limit]

    def upsert_entry(self, layer, name, title="", description="", body="", topic_type="", user_managed=False,
                     expected_updated_at="", mode="replace", section="", verified_at=None, valid_until=None):
        """Create or update a memory entry under lock, backing up previous versions to trash.

        ``expected_updated_at`` turns the write into a compare-and-set: if the entry
        changed since the agent read it, the write is rejected instead of silently
        dropping the other session's edit. ``mode="append"`` appends ``body`` to an
        existing entry without needing to read and re-emit the entire document. Passing
        ``section`` targets a single ``## `` section (replace or append, or create if
        absent) without rewriting the rest of the document.
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
        mode = (mode or "replace").strip().lower()
        if mode not in {"replace", "append"}:
            raise ValueError("mode must be 'replace' or 'append'.")
        section = (section or "").strip()
        if "\n" in section:
            raise ValueError("section must be a single heading title.")
        title = (title or "").strip()
        description = (description or "").strip()
        body = (body or "").strip() if user_managed else strip_generated_header(body or "")
        if "\n" in title:
            raise ValueError("title must be a single non-empty line.")
        if "\n" in description:
            raise ValueError("description must be a single line.")
        if not body:
            raise ValueError("body must not be empty.")
        secret_guard.ensure_no_secrets(title, description, body)
        norm_verified = None if verified_at is None else normalize_iso_date(verified_at, "verified_at")
        norm_valid_until = None if valid_until is None else normalize_iso_date(valid_until, "valid_until")

        resolved_type = topic_type or LAYER_DEFAULT_TYPE[layer]
        allowed_types = ALLOWED_LAYER_TYPES[layer] if user_managed else VALID_TOPIC_TYPES
        if resolved_type not in allowed_types:
            raise ValueError(f"type '{resolved_type}' is invalid for {layer}.")

        related = []
        if not user_managed and not (self.root / LAYER_DIRS[layer] / f"{name}.md").is_file():
            if title or description:
                related = self._similar_entries(f"{title} {description}", name)

        with self.write_lock():
            ts_now = now_iso()
            if user_managed:
                target = self.root / "user-managed" / f"{name}.md"
                status = "active"
                cre_at = ts_now
                previous = None
                effective_title = title
                effective_desc = description
                merged_body = body
                resolved_section = ""
                section_action = ""
                old_verified = ""
                old_valid_until = ""
                if target.is_file():
                    old_text = target.read_text(encoding="utf-8")
                    old_meta, old_body = parse_frontmatter(old_text)
                    self._check_expected_updated(expected_updated_at, old_meta, target)
                    ts_now = now_iso(file_timestamps(target, old_meta)[1])
                    backup = self._backup_to_trash(target, f"user-{name}")
                    previous = self._previous_receipt(old_meta, old_body, backup)
                    status = old_meta.get("status", "active")
                    cre_at = old_meta.get("created_at") or old_meta.get("updated_at") or ts_now
                    old_verified = (old_meta.get("verified_at") or "").strip()
                    old_valid_until = (old_meta.get("valid_until") or "").strip()
                    if not topic_type and old_meta.get("type") in ALLOWED_LAYER_TYPES[layer]:
                        resolved_type = old_meta["type"]
                    effective_title = title or old_meta.get("title") or extract_title(old_body, name)
                    effective_desc = description or old_meta.get("description", "")
                    if section:
                        merged_body, resolved_section, section_action = patch_markdown_section(
                            old_body, section, body, mode=mode
                        )
                    elif mode == "append" and old_body.strip():
                        merged_body = f"{old_body.strip()}\n\n{body}"
                else:
                    self._check_expected_updated(expected_updated_at)
                    if section:
                        merged_body, resolved_section, section_action = patch_markdown_section(
                            "", section, body, mode=mode
                        )
                if not effective_title:
                    raise ValueError("title must be a single non-empty line.")
                eff_verified = old_verified if norm_verified is None else norm_verified
                eff_valid_until = old_valid_until if norm_valid_until is None else norm_valid_until
                if eff_verified and eff_valid_until and eff_valid_until < eff_verified:
                    raise ValueError("valid_until must not be earlier than verified_at.")
                content = format_user_markdown(
                    name,
                    effective_title,
                    effective_desc,
                    resolved_type,
                    layer,
                    status,
                    cre_at,
                    ts_now,
                    merged_body,
                    verified_at=eff_verified,
                    valid_until=eff_valid_until,
                )
                atomic_write(target, content)
                rel_out = target.relative_to(self.root).as_posix()
                message = f"memory({self.library_id}): upsert {rel_out}"
                sync_status = self._maybe_auto_commit(message)
                self._enqueue_async_sync(message)
                res = {
                    **self._result_metadata(),
                    "ref": self._entry_ref(rel_out),
                    "path": rel_out,
                    "indexed": False,
                    "mode": mode,
                    "created_at": cre_at,
                    "updated_at": ts_now,
                }
                if resolved_section:
                    res["section"] = resolved_section
                    res["section_action"] = section_action
                if eff_verified:
                    res["verified_at"] = eff_verified
                if eff_valid_until:
                    res["valid_until"] = eff_valid_until
                    if is_date_expired(eff_valid_until):
                        res["expired"] = True
                self._attach_previous(res, previous, merged_body)
                return self._with_sync_warning(res, sync_status)

            folder = LAYER_DIRS[layer]
            target = self.root / folder / f"{name}.md"
            rel_path = f"{folder}/{name}.md"

            # A move replaces the live entry in its source layer, not the
            # (usually absent) destination. Find and validate it under lock.
            stale_files = [
                (self.root / other_folder / f"{name}.md", f"{other_folder}/{name}.md")
                for other_layer, other_folder in LAYER_DIRS.items()
                if other_layer != layer and other_layer in self.allowed_layers
                and (self.root / other_folder / f"{name}.md").is_file()
            ]
            current_paths = ([target] if target.is_file() else []) + [path for path, _ in stale_files]
            if (expected_updated_at or "").strip() and len(current_paths) > 1:
                raise ValueError(
                    "Entry changed since you read it: multiple layers contain this name. "
                    "Reconcile the duplicate entries before writing."
                )
            previous = None
            preserved_created = None
            existing_meta = {}
            existing_body = ""
            existing_is_target = False
            prior_timestamps = []
            for current in current_paths:
                old_meta, old_body = parse_frontmatter(current.read_text(encoding="utf-8"))
                if previous is None:
                    self._check_expected_updated(expected_updated_at, old_meta, current)
                created, updated, _, updated_epoch = file_timestamps(current, old_meta)
                prior_timestamps.append((updated_epoch, updated))
                backup_hint = name if current == target else f"migrated-{name}"
                backup = self._backup_to_trash(current, backup_hint)
                if previous is None:
                    previous = self._previous_receipt(old_meta, old_body, backup)
                    preserved_created = created
                    existing_meta = old_meta
                    existing_body = old_body
                    existing_is_target = (current == target)
            if prior_timestamps:
                ts_now = now_iso(max(prior_timestamps)[1])
            else:
                self._check_expected_updated(expected_updated_at)
            cre_at = preserved_created or ts_now

            existing_title = extract_title(existing_body, name) if previous is not None else ""
            effective_title = title or existing_title
            if not effective_title:
                raise ValueError("title must be a single non-empty line.")
            effective_desc = description or existing_meta.get("description", "") or effective_title
            if mode == "append" and existing_is_target and not topic_type:
                if existing_meta.get("type") in VALID_TOPIC_TYPES:
                    resolved_type = existing_meta["type"]
            old_verified = (existing_meta.get("verified_at") or "").strip()
            old_valid_until = (existing_meta.get("valid_until") or "").strip()
            eff_verified = old_verified if norm_verified is None else norm_verified
            eff_valid_until = old_valid_until if norm_valid_until is None else norm_valid_until
            if eff_verified and eff_valid_until and eff_valid_until < eff_verified:
                raise ValueError("valid_until must not be earlier than verified_at.")

            resolved_section = ""
            section_action = ""
            if section:
                if existing_body.strip():
                    base_body = existing_body.strip()
                    if base_body.startswith("# "):
                        if title:
                            _, _, rest = base_body.partition("\n")
                            base_body = f"# {effective_title}\n{rest}" if rest else f"# {effective_title}"
                    else:
                        base_body = f"# {effective_title}\n\n{base_body}"
                else:
                    base_body = f"# {effective_title}"
                formatted_body, resolved_section, section_action = patch_markdown_section(
                    base_body, section, body, mode=mode
                )
            elif mode == "append" and existing_body.strip():
                base_body = existing_body.strip()
                if base_body.startswith("# "):
                    if title:
                        _, _, rest = base_body.partition("\n")
                        base_body = f"# {effective_title}\n{rest}" if rest else f"# {effective_title}"
                else:
                    base_body = f"# {effective_title}\n\n{base_body}"
                formatted_body = f"{base_body}\n\n{body}"
            else:
                formatted_body = body if body.lstrip().startswith("# ") else f"# {effective_title}\n\n{body}"
            content = format_topic_markdown(
                name,
                effective_desc,
                resolved_type,
                cre_at,
                ts_now,
                formatted_body,
                verified_at=eff_verified,
                valid_until=eff_valid_until,
            )
            self._write_topic_and_index(
                target,
                rel_path,
                layer,
                effective_title,
                effective_desc,
                content,
                remove_rels=[stale_rel for _, stale_rel in stale_files],
            )

            migrated_from = []
            for stale_path, stale_rel in stale_files:
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
                "mode": mode,
                "created_at": cre_at,
                "updated_at": ts_now,
            }
            if resolved_section:
                res["section"] = resolved_section
                res["section_action"] = section_action
            if eff_verified:
                res["verified_at"] = eff_verified
            if eff_valid_until:
                res["valid_until"] = eff_valid_until
                if is_date_expired(eff_valid_until):
                    res["expired"] = True
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

    def _write_topic_and_index(self, target, rel_path, layer, title, idx_desc, content, *, remove_rels=()):
        """Atomically write a topic file and update MEMORY.md, rolling back both on error."""
        prev_target_bytes = target.read_bytes() if target.is_file() else None
        prev_index_text = self.index_path.read_text(encoding="utf-8")
        atomic_write(target, content)
        try:
            for old_rel in remove_rels:
                self._remove_index_line(old_rel)
            self._upsert_index_line(layer, title, rel_path, idx_desc)
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

    def save_topic_entry(
        self,
        *,
        layer,
        title,
        description,
        body,
        slug="",
        orig_rel="",
        entry_type="",
        frontmatter_name="",
        file_description=None,
        created_at="",
        previous_updated_at="",
        verified_at="",
        valid_until="",
        before_write=None,
    ):
        """Create or update an indexed topic via the shared write, backup, index, and sync kernel.

        Used by the Web/Desktop manager so UI edits share the exact same atomic
        write, trash version backup, MEMORY.md update, rollback, auto-commit,
        and async Git push path as MCP writes.
        """
        if self.mode == "ro":
            raise ValueError(f"Library '{self.library_id}' is read-only.")
        if layer not in LAYER_DIRS or layer not in self.allowed_layers:
            raise ValueError(f"Layer {layer} is not allowed in library '{self.library_id}'.")
        with self.write_lock():
            if before_write is not None:
                overrides = before_write()
                if isinstance(overrides, dict):
                    orig_rel = overrides.get("orig_rel", orig_rel)
                    slug = overrides.get("slug", slug)
                    entry_type = overrides.get("entry_type", entry_type)
                    frontmatter_name = overrides.get("frontmatter_name", frontmatter_name)
                    file_description = overrides.get("file_description", file_description)
                    created_at = overrides.get("created_at", created_at)
                    previous_updated_at = overrides.get("previous_updated_at", previous_updated_at)
                    verified_at = overrides.get("verified_at", verified_at)
                    valid_until = overrides.get("valid_until", valid_until)
            if not NAME_RE.fullmatch(slug or "") or (slug or "").lower() == "readme":
                raise ValueError("Invalid entry name.")
            new_folder = LAYER_DIRS[layer]
            new_rel = f"{new_folder}/{slug}.md"
            new_file = self.root / new_rel
            if orig_rel:
                if new_rel != orig_rel and new_file.exists():
                    raise ValueError(f"目标层级目录已存在同名文件：{new_rel}")
                orig_file = self.root / orig_rel
                backup_hint = slug if new_rel == orig_rel else f"migrated-{slug}"
                self._backup_to_trash(orig_file, backup_hint)
                ts_now = now_iso(previous_updated_at)
                cre_at = created_at or ts_now
                action = "update"
            else:
                ts_now = now_iso()
                cre_at = ts_now
                action = "create"

            fm_name = frontmatter_name or slug
            idx_desc = description.strip() or title
            fm_desc = idx_desc if file_description is None else file_description
            fm_type = entry_type if entry_type in VALID_TOPIC_TYPES else LAYER_DEFAULT_TYPE[layer]
            content = format_topic_markdown(
                fm_name,
                fm_desc,
                fm_type,
                cre_at,
                ts_now,
                body,
                verified_at=verified_at,
                valid_until=valid_until,
            )
            remove_rels = (orig_rel,) if (orig_rel and new_rel != orig_rel) else ()
            self._write_topic_and_index(new_file, new_rel, layer, title, idx_desc, content, remove_rels=remove_rels)
            if orig_rel and new_rel != orig_rel:
                (self.root / orig_rel).unlink(missing_ok=True)
            message = f"memory({self.library_id}): {action} {new_rel}"
            self._maybe_auto_commit(message)
            self._enqueue_async_sync(message)
            return new_rel

    def save_user_entry(
        self,
        *,
        name,
        title,
        description,
        body,
        layer="L2",
        entry_type="",
        status="active",
        created_at="",
        verified_at="",
        valid_until="",
        exclusive_create=False,
        before_write=None,
    ):
        """Create or update a user-managed entry through the unified locked write path."""
        if self.mode == "ro":
            raise ValueError(f"Library '{self.library_id}' is read-only.")
        if not self.allow_user_managed:
            raise ValueError(f"Library '{self.library_id}' does not allow user-managed entries.")
        if layer not in LAYER_DIRS or layer not in self.allowed_layers:
            raise ValueError(f"Layer {layer} is not allowed in library '{self.library_id}'.")
        if not NAME_RE.fullmatch(name or "") or (name or "").lower() == "readme":
            raise ValueError("Invalid entry name.")
        target = self.root / "user-managed" / f"{name}.md"
        with self.write_lock():
            prev_updated = ""
            if before_write is not None:
                overrides = before_write()
                if isinstance(overrides, dict):
                    prev_updated = overrides.get("previous_updated_at", prev_updated)
                    created_at = overrides.get("created_at", created_at)
            if not exclusive_create and target.is_file():
                self._backup_to_trash(target, f"user-{name}")
            ts_now = now_iso(prev_updated)
            cre_at = created_at or ts_now
            allowed = ALLOWED_LAYER_TYPES.get(layer, {"feedback"})
            resolved_type = entry_type if entry_type in allowed else LAYER_DEFAULT_TYPE[layer]
            content = format_user_markdown(
                name,
                title,
                description,
                resolved_type,
                layer,
                status,
                cre_at,
                ts_now,
                body,
                verified_at=verified_at,
                valid_until=valid_until,
            )
            if exclusive_create:
                atomic_create(target, content)
            else:
                atomic_write(target, content)
        message = f"memory({self.library_id}): save user-managed/{name}.md"
        self._maybe_auto_commit(message)
        self._enqueue_async_sync(message)
        return f"user-managed/{name}.md"

    def delete_entry(self, rel_path="", *, user_managed=False, slug_hint="", before_delete=None):
        """Delete an indexed topic or user-managed entry after backing it up to trash."""
        if self.mode == "ro":
            raise ValueError(f"Library '{self.library_id}' is read-only.")
        if user_managed and not self.allow_user_managed:
            raise ValueError(f"Library '{self.library_id}' does not allow user-managed entries.")
        with self.write_lock():
            if before_delete is not None:
                overrides = before_delete()
                if isinstance(overrides, dict):
                    rel_path = overrides.get("rel_path", rel_path)
                    slug_hint = overrides.get("slug_hint", slug_hint)
            target = self.resolve_safe(rel_path)
            self._backup_to_trash(target, slug_hint or target.stem)
            target.unlink()
            if not user_managed:
                self._remove_index_line(rel_path)
        message = f"memory({self.library_id}): delete {rel_path}"
        self._maybe_auto_commit(message)
        self._enqueue_async_sync(message)
        return rel_path

    def save_index_text(self, normalized_text, *, before_write=None):
        """Atomically overwrite MEMORY.md under the library write lock and trigger sync."""
        if self.mode == "ro":
            raise ValueError(f"Library '{self.library_id}' is read-only.")
        with self.write_lock():
            if before_write is not None:
                before_write()
            atomic_write(self.index_path, normalized_text + "\n")
        message = f"memory({self.library_id}): update MEMORY.md"
        self._maybe_auto_commit(message)
        self._enqueue_async_sync(message)

    def discard_unindexed_topic(self, slug):
        """Move aside a topic file that a stopped save left on disk without an index line."""
        if not NAME_RE.fullmatch(slug or ""):
            return []
        removed = []
        with self.write_lock():
            index = self.parse_index()
            for folder in LAYER_DIRS.values():
                rel = f"{folder}/{slug}.md"
                path = self.root / rel
                if rel in index or path.is_symlink() or not path.is_file():
                    continue
                self._backup_to_trash(path, slug)
                path.unlink()
                removed.append(rel)
        return removed

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

    def lint(self, stale_days=memory_lint.STALE_DAYS, include_archived=True):
        """Report consolidation problems: index drift, stale snapshots, duplicate topics."""
        entries = self.collect_entries(include_archived=include_archived)
        # A mount that only allows some layers still indexes the rest; those
        # files exist on disk and must not be reported as missing.
        index = {path: meta for path, meta in self.parse_index().items()
                 if meta.get("layer", "") in self.allowed_layers}
        findings = memory_lint.lint_entries(entries, index=index, stale_days=stale_days)
        result = {
            "library": self.library_id,
            "entries": len(entries),
            "stale_days": stale_days,
            "summary": memory_lint.summarize(findings),
            "findings": findings,
        }
        result.update(self._result_metadata())
        if not findings:
            result["notice"] = "No consolidation problems found."
        return result

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
        restored_verified_at = str(meta.get("verified_at") or "").strip()
        restored_valid_until = str(meta.get("valid_until") or "").strip()
        if dry_run:
            preview = {
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
            if restored_verified_at:
                preview["verified_at"] = restored_verified_at
            if restored_valid_until:
                preview["valid_until"] = restored_valid_until
            return preview
        res = self.upsert_entry(
            layer=target_layer,
            name=slug,
            title=title,
            description=description,
            body=body,
            topic_type=str(meta.get("type") or "").strip(),
            user_managed=user_managed,
            verified_at=restored_verified_at,
            valid_until=restored_valid_until,
        )
        res["restored"] = True
        res["restored_from"] = version
        res.pop("related", None)
        res.pop("notice", None)
        res.pop("content_warning", None)
        return res

    def archive_entry(self, name="", user_managed=False, *, orig_rel="", before_archive=None):
        """Archive a user-managed entry or move a standard topic into archive/."""
        if self.mode == "ro":
            raise ValueError(f"Library '{self.library_id}' is read-only.")
        if user_managed and not self.allow_user_managed:
            raise ValueError(f"Library '{self.library_id}' does not allow user-managed entries.")
        if not user_managed and "L4" not in self.allowed_layers:
            raise ValueError(f"Layer L4 (archive) is not enabled in library '{self.library_id}'.")
        if not orig_rel and before_archive is None and not NAME_RE.fullmatch(name):
            raise ValueError("Invalid entry name.")
        with self.write_lock():
            if user_managed:
                target = self.root / "user-managed" / f"{name}.md"
                if not target.is_file():
                    raise ValueError(f"User-managed entry not found: {name}")
                self._backup_to_trash(target, f"archive-user-{name}")
                meta, body = parse_frontmatter(target.read_text(encoding="utf-8"))
                ts_now = now_iso(file_timestamps(target, meta)[1])
                cre_at = meta.get("created_at") or meta.get("updated_at") or ts_now
                archived_user = format_user_markdown(
                    meta.get("name", name),
                    meta.get("title", extract_title(body, name)),
                    meta.get("description", ""),
                    meta.get("type", LAYER_DEFAULT_TYPE.get(meta.get("layer", "L2"), "feedback")),
                    meta.get("layer", "L2"),
                    "archived",
                    cre_at,
                    ts_now,
                    body.strip(),
                    verified_at=str(meta.get("verified_at") or "").strip(),
                    valid_until=str(meta.get("valid_until") or "").strip(),
                )
                atomic_write(target, archived_user)
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

            if before_archive is not None:
                overrides = before_archive()
                if isinstance(overrides, dict):
                    if overrides.get("already_archived"):
                        return {
                            **self._result_metadata(),
                            "from": overrides["orig_rel"],
                            "to": overrides["orig_rel"],
                            "layer": "L4",
                        }
                    orig_rel = overrides.get("orig_rel", orig_rel)
                    name = overrides.get("slug", name)

            indexed = self.parse_index()
            if orig_rel:
                candidate = self.resolve_safe(orig_rel)
                candidates = [(candidate, orig_rel)] if candidate.is_file() else []
                if not name:
                    name = candidate.stem
            else:
                candidates = [
                    (self.root / folder / f"{name}.md", f"{folder}/{name}.md")
                    for layer, folder in LAYER_DIRS.items()
                    if layer != "L4" and layer in self.allowed_layers
                    and (self.root / folder / f"{name}.md").is_file()
                ]
            for candidate, old_rel in candidates:
                dest = self.root / "archive" / f"{name}.md"
                if dest.exists():
                    raise ValueError(f"Archive file already exists: archive/{name}.md")
                idx_info = indexed.get(old_rel, {})
                raw = candidate.read_text(encoding="utf-8")
                meta, body = parse_frontmatter(raw)
                cre_at, old_updated, _, _ = file_timestamps(candidate, meta)
                ts_now = now_iso(old_updated)
                title = idx_info.get("title") or extract_title(body, name)
                fm_desc = meta.get("description") or idx_info.get("description") or title
                idx_desc = idx_info.get("description") or fm_desc
                fm_name = meta.get("name", name)
                fm_type = meta.get("type") if meta.get("type") in VALID_TOPIC_TYPES else "reference"
                archived_raw = format_topic_markdown(
                    fm_name,
                    fm_desc,
                    fm_type,
                    cre_at,
                    ts_now,
                    body,
                    verified_at=str(meta.get("verified_at") or "").strip(),
                    valid_until=str(meta.get("valid_until") or "").strip(),
                )
                new_rel = f"archive/{name}.md"
                self._backup_to_trash(candidate, f"archive-{name}")
                self._write_topic_and_index(
                    dest, new_rel, "L4", title, idx_desc, archived_raw, remove_rels=(old_rel,)
                )
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
        hits = [pair[0] for pair in merged[:capped_limit]]
        # Each library attached see_also to its own best hit; only the overall
        # top hit keeps it, so the agent gets one expansion, not one per library.
        for hit in hits[1:]:
            hit.pop("see_also", None)
        return hits

    def list_entries(self, layer="", include_archived=False, sort="updated", library="personal", limit=0):
        stores = self._target_stores(library)
        cap = max(0, int(limit or 0))
        if len(stores) == 1:
            return stores[0].list_entries(layer=layer, include_archived=include_archived, sort=sort, limit=cap)

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
        out = [t[0] for t in merged]
        return out[:cap] if cap else out

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

    def read_entries(self, rel_paths, library="", section="", outline=False):
        """Read up to MAX_BATCH_READ Markdown entries across personal or mounted libraries."""
        if not isinstance(rel_paths, (list, tuple)):
            raise ValueError("paths must be a list of relative markdown paths.")
        cleaned = []
        for raw in rel_paths:
            text = str(raw or "").strip()
            if text and text not in cleaned:
                cleaned.append(text)
        if not cleaned:
            raise ValueError("paths must contain at least one relative markdown path.")
        if len(cleaned) > MAX_BATCH_READ:
            raise ValueError(f"At most {MAX_BATCH_READ} paths can be read in one batch.")
        entries, errors = [], []
        for path in cleaned:
            try:
                entries.append(self.read_entry(path, library=library, section=section, outline=outline))
            except ValueError as exc:
                errors.append({"path": path, "error": str(exc)})
        if not entries and errors:
            raise ValueError(errors[0]["error"])
        result = {"count": len(entries), "entries": entries}
        if errors:
            result["errors"] = errors
        return result

    def upsert_entry(self, layer, name, title="", description="", body="", topic_type="", user_managed=False, library="personal",
                     expected_updated_at="", mode="replace", section="", verified_at=None, valid_until=None):
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
            mode=mode,
            section=section,
            verified_at=verified_at,
            valid_until=valid_until,
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

    def lint(self, stale_days=memory_lint.STALE_DAYS, library="personal"):
        stores = self._target_stores(library)
        results = self._per_store(stores, lambda st: st.lint(stale_days=stale_days))
        if len(results) == 1:
            return results[0][1]
        reports = [report for _, report in results]
        merged = []
        for report in reports:
            for item in report["findings"]:
                merged.append(dict(item, library=report["library"]))
        return {
            "libraries": [report["library"] for report in reports],
            "entries": sum(report["entries"] for report in reports),
            "stale_days": stale_days,
            "summary": memory_lint.summarize(merged),
            "findings": merged,
            "reports": reports,
        }


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
        "description": "Search the personal library by default; pass library='*' or 'all' to search personal plus mounted shared libraries, or pass a mount id explicitly. Each hit has match=full (every space-separated query word found), partial (some words) or fuzzy (fragments or a title variant shown as matched_phrase). Check the snippet before relying on partial or fuzzy hits, and say memory has no answer when none fits instead of using the closest entry. The best hit may carry see_also: entries it cites that this search did not return; read one when the answer needs the cited detail.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords, phrase, or temporal cue."},
                "layer": {"type": "string", "enum": ["L0", "L1", "L2", "L3", "L4"]},
                "include_archived": {"type": "boolean", "default": False},
                "compact": {"type": "boolean", "default": False, "description": "For initial lookup, omit duplicate routing and bookkeeping fields. Keeps qualified refs, trust, read-only flag, match evidence, updated_at and citation hints."},
                "limit": {"type": "integer", "default": 10},
                "library": {"type": "string", "default": "personal", "description": "Target library id (defaults to 'personal'; use '*' or 'all' for all mounted libraries, or a shared mount id)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_list",
        "description": "List indexed L0-L4 and user-managed memory entries from the personal library by default; pass library='*' or 'all' to include mounted shared libraries. Use compact=true and limit on large libraries to save tokens.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "layer": {"type": "string", "enum": ["L0", "L1", "L2", "L3", "L4"]},
                "include_archived": {"type": "boolean", "default": False},
                "sort": {"type": "string", "enum": ["updated", "created", "layer"], "default": "updated"},
                "limit": {"type": "integer", "default": 0, "description": "Optional max entries to return (0 = all)."},
                "compact": {"type": "boolean", "default": False, "description": "Omit duplicate routing/bookkeeping fields while keeping ref, layer, status, updated_at, validity, title, and description."},
                "library": {"type": "string", "default": "personal", "description": "Target library id (defaults to 'personal'; use '*' or 'all' for all mounted libraries, or a shared mount id)."},
            },
        },
    },
    {
        "name": "memory_read",
        "description": "Read one memory Markdown file via 'path' (e.g. 'MEMORY.md', 'projects/example.md', or 'team-kb:methods/guide.md'), or batch-read up to 10 files in one round-trip via 'paths'. Full reads of large entries also return a section outline; pass outline=true to get only that outline, or section='<title>' to read a single section instead of the whole file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path inside the memory library, optionally prefixed with '<library>:'."},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"Batch read up to {MAX_BATCH_READ} relative paths (each optionally prefixed with '<library>:') in one call instead of 'path'.",
                },
                "library": {"type": "string", "default": "", "description": "Optional library id (defaults to 'personal' unless path has '<library>:' prefix)."},
                "section": {"type": "string", "default": "", "description": "Read only this '## ' section (exact title, or a substring of it)."},
                "outline": {"type": "boolean", "default": False, "description": "Return the section outline and the intro only, so you can pick one section to read."},
            },
        },
    },
    {
        "name": "memory_upsert",
        "description": "Create or update a memory entry following MEMORY.md §0. Search for the topic first and reuse its existing name. Use mode='append' to quickly add a new verified note or dated section to an existing entry without rewriting the whole file (title and description are optional when updating an existing entry). Defaults to the personal library; pass 'library' explicitly to write to a writable shared library.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "layer": {"type": "string", "enum": ["L0", "L1", "L2", "L3", "L4"]},
                "name": {"type": "string", "description": "Kebab-case slug matching ^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$."},
                "title": {"type": "string", "description": "Single-line human-readable title. Required when creating a new entry; optional when updating or appending to an existing entry."},
                "description": {"type": "string", "description": "Concise single-line retrieval cue. Optional when updating or appending to an existing entry."},
                "body": {"type": "string", "description": "Markdown body (or incremental note/section when mode='append') with conditions, evidence, and dates. Do not include frontmatter or the H1 title; the server writes both (a leading frontmatter block is stripped)."},
                "mode": {"type": "string", "enum": ["replace", "append"], "default": "replace", "description": "Use 'replace' (default) to write the full entry body, or 'append' to append an incremental update to an existing entry without reading and re-emitting the entire file."},
                "section": {"type": "string", "default": "", "description": "Optional '## ' heading title to update or append in place (creates the section if absent) without rewriting the rest of a long entry."},
                "topic_type": {"type": "string", "enum": ["user", "feedback", "project", "reference"]},
                "user_managed": {"type": "boolean", "default": False, "description": "Only set true when the user explicitly requests changing user-managed/ in the personal library."},
                "library": {"type": "string", "default": "personal", "description": "Target library id (defaults to 'personal'). Shared libraries must be mounted rw and allow the chosen layer."},
                "expected_updated_at": {"type": "string", "default": "", "description": "The updated_at you based your edit on. The write is rejected if the entry changed since then, so a parallel session's edit is not lost."},
                "verified_at": {"type": "string", "description": "Optional YYYY-MM-DD date when the facts in this entry were last verified. Omit to preserve existing value on updates; pass empty string to clear."},
                "valid_until": {"type": "string", "description": "Optional YYYY-MM-DD expiration date for temporary rules, deadlines, or authorizations. Omit to preserve existing value on updates; pass empty string to clear."},
            },
            "required": ["layer", "name", "body"],
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
                "layer": {"type": "string", "enum": ["L0", "L1", "L2", "L3", "L4"], "description": "Target layer, only needed when the entry no longer exists."},
                "dry_run": {"type": "boolean", "default": False, "description": "Return the saved content without writing it back."},
                "library": {"type": "string", "default": "personal", "description": "Target library id (defaults to 'personal')."},
            },
            "required": ["version"],
        },
    },
    {
        "name": "memory_lint",
        "description": "Check a library for consolidation problems before or after a curation pass: entries missing from the index, index lines without a file, leftover frontmatter in a body, missing summaries or verification dates, L3 snapshots nobody refreshed, oversized entries, and pairs of entries that cover one topic. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "library": {"type": "string", "default": "personal", "description": "Target library id (defaults to 'personal'; use '*' or 'all' for every mounted library)."},
                "stale_days": {"type": "integer", "default": memory_lint.STALE_DAYS, "description": "Flag L3 snapshots older than this many days."},
            },
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
        if name == "memory_read" and isinstance(data.get("entries"), list):
            refs = [
                e.get("ref") or e.get("path")
                for e in data["entries"]
                if isinstance(e, dict) and (e.get("ref") or e.get("path"))
            ]
            ev["refs"] = refs[: audit_log.MAX_AUDIT_REFS]
            ev["hits"] = len(refs)
            libs = {e.get("library") for e in data["entries"] if isinstance(e, dict) and e.get("library")}
            if len(libs) == 1:
                ev["library"] = next(iter(libs))
        else:
            ev["ref"] = data.get("ref") or data.get("path") or data.get("to")
            ev["library"] = data.get("library") or ev.get("library")
            if name in ("memory_upsert", "memory_archive", "memory_restore"):
                ev["action"] = data.get("status") or data.get("action")
            if name == "memory_upsert" and data.get("mode"):
                ev["mode"] = data.get("mode")
            if name == "memory_upsert" and data.get("section_action"):
                ev["section_action"] = data.get("section_action")
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


def compact_search_hit(hit):
    """Project only after final ranking/merge: qualified ref is the read address."""
    fields = ("ref", "trust", "read_only", "layer", "status", "updated_at",
              "verified_at", "valid_until", "expired", "match",
              "matched_phrase", "title", "description", "snippet", "see_also")
    return {key: hit[key] for key in fields if key in hit}


def compact_list_entry(item):
    """Project a list_entries item to its high-signal fields for low-token browsing."""
    fields = ("ref", "layer", "status", "updated_at",
              "verified_at", "valid_until", "expired", "title", "description")
    return {key: item[key] for key in fields if key in item}


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
        if args.get("compact") is True:
            data = [compact_search_hit(hit) for hit in data]
    elif name == "memory_list":
        kwargs = dict(
            layer=args.get("layer", ""),
            include_archived=bool(args.get("include_archived", False)),
            sort=args.get("sort", "updated"),
            limit=int(args.get("limit", 0) or 0),
        )
        if isinstance(store, WorkspaceStore):
            kwargs["library"] = args.get("library", "personal")
        data = store.list_entries(**kwargs)
        if args.get("compact") is True:
            data = [compact_list_entry(item) for item in data]
    elif name == "memory_read":
        kwargs = dict(
            section=args.get("section", "") or "",
            outline=bool(args.get("outline", False)),
        )
        if isinstance(store, WorkspaceStore):
            kwargs["library"] = args.get("library", "")
        has_path = bool(str(args.get("path") or "").strip())
        has_paths = args.get("paths") is not None
        if has_path and has_paths:
            raise ValueError("Pass either 'path' or 'paths' to memory_read, not both.")
        if has_paths:
            data = store.read_entries(args["paths"], **kwargs)
        elif has_path:
            data = store.read_entry(args["path"], **kwargs)
        else:
            raise ValueError("memory_read requires 'path' or a non-empty 'paths' list.")
    elif name == "memory_upsert":
        kwargs = dict(
            layer=args["layer"],
            name=args["name"],
            title=args.get("title", "") or "",
            description=args.get("description", "") or "",
            body=args["body"],
            topic_type=args.get("topic_type", ""),
            user_managed=bool(args.get("user_managed", False)),
            expected_updated_at=args.get("expected_updated_at", "") or "",
            mode=args.get("mode", "replace") or "replace",
            section=args.get("section", "") or "",
        )
        if "verified_at" in args and args["verified_at"] is not None:
            kwargs["verified_at"] = args["verified_at"]
        if "valid_until" in args and args["valid_until"] is not None:
            kwargs["valid_until"] = args["valid_until"]
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
    elif name == "memory_lint":
        kwargs = dict(stale_days=int(args.get("stale_days", memory_lint.STALE_DAYS)))
        if isinstance(store, WorkspaceStore):
            kwargs["library"] = args.get("library", "personal")
        data = store.lint(**kwargs)
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
                "instructions": (
                    "Unified Memory stores persistent personal and shared knowledge across sessions. "
                    "At task start, call memory_search(compact=true) for relevant constraints, methods, preferences, and project context, then use memory_read (paths, outline, or section) to inspect details. "
                    "Capture verified root causes, non-obvious fixes, reusable workflows, key decisions, and user preferences or corrections as soon as they are confirmed—do not wait to be reminded. "
                    "Prefer memory_upsert(mode='append') or memory_upsert(section='...') to update an existing entry incrementally without rewriting the whole file, and do a quick capture check before ending substantial work. "
                    "Follow MEMORY.md §0 for layering and never store plaintext secrets or credentials."
                ),
            }
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
