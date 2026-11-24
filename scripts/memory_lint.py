"""Consolidation checks for a memory library.

Agents and users both forget to curate: index lines drift from the entries, old
snapshots keep claiming the present, two entries cover one topic, and a stale
write can leave a second frontmatter block inside the body. The literature calls
this step consolidation (Letta's sleep-time compute, reflection in Generative
Agents). These checks find the mess; a human or an agent decides what to do.

Every check has to be worth reading. Comparing the index summary with the entry
summary was dropped again: on a real 121-entry shared library it fired on 94
entries, because an index line is written as a short human cue and is allowed to
read differently from the entry's own summary.
"""

import re
import time

DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
FRONTMATTER_LEFTOVER_RE = re.compile(r"^---\s*$", re.MULTILINE)
WORD_RE = re.compile(r"[a-z0-9]{3,}")
CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")

STALE_DAYS = 90
LONG_CHARS = 6000
DUPLICATE_OVERLAP = 0.6
DATED_LAYERS = ("L1", "L3", "L4")


def topic_tokens(text):
    """Tokens used to spot two entries about the same topic."""
    low = (text or "").casefold()
    tokens = set(WORD_RE.findall(low))
    for run in CJK_RUN_RE.findall(low):
        for i in range(len(run) - 1):
            tokens.add(run[i:i + 2])
    return tokens


def _finding(check, severity, ref, detail, hint):
    return {"check": check, "severity": severity, "ref": ref, "detail": detail, "hint": hint}


def lint_entries(entries, index=None, now=None, stale_days=STALE_DAYS):
    """Return consolidation findings for one library's entries and index."""
    index = index or {}
    now = now or time.time()
    findings = []
    stale_cutoff = now - stale_days * 86400

    paths = {e["path"] for e in entries}
    for path, meta in sorted(index.items()):
        if path not in paths:
            findings.append(_finding(
                "index-orphan", "warning", path,
                f"MEMORY.md 索引里有「{meta.get('title') or path}」，但文件不在库里",
                "删掉这行索引，或用 rebuild-index 重建索引",
            ))

    for entry in sorted(entries, key=lambda e: e["path"]):
        ref = entry["path"]
        body = entry.get("body") or ""
        layer = entry.get("layer", "")
        if entry.get("source") == "index" and ref not in index:
            findings.append(_finding(
                "index-missing", "warning", ref,
                "条目不在 MEMORY.md 索引里，只能靠全文搜索命中",
                "重新 upsert 这篇，或运行 rebuild-index",
            ))
        if FRONTMATTER_LEFTOVER_RE.search(body):
            findings.append(_finding(
                "duplicate-frontmatter", "error", ref,
                "正文里还留着一段 frontmatter（多余的 --- 分隔块）",
                "重新 upsert 这篇，body 只写正文",
            ))
        if not (entry.get("description") or "").strip():
            findings.append(_finding(
                "missing-description", "warning", ref,
                "没有摘要，搜索结果里看不出结论",
                "补一行给出结论的 description",
            ))
        if layer in DATED_LAYERS and not DATE_RE.search(body):
            findings.append(_finding(
                "missing-date", "info", ref,
                "正文里没有 YYYY-MM-DD 的验证日期",
                "写清这个结论是哪天验证的",
            ))
        if layer == "L3" and (entry.get("updated_epoch") or 0) < stale_cutoff:
            findings.append(_finding(
                "stale-snapshot", "warning", ref,
                f"项目快照超过 {stale_days} 天没更新：{entry.get('updated_at', '')}",
                "核实现状后更新，或归档成历史快照",
            ))
        if len(body) > LONG_CHARS:
            findings.append(_finding(
                "oversized", "info", ref,
                f"正文 {len(body)} 字，读一次就很贵",
                "拆成几篇，或者整理成小节让 agent 只读需要的那节",
            ))

    findings.extend(_duplicate_findings(entries))
    return findings


def _duplicate_findings(entries):
    """Pairs of active entries whose title and summary describe one topic."""
    out = []
    profiles = []
    for entry in entries:
        if entry.get("layer") == "L4" or entry.get("status") == "archived":
            continue
        tokens = topic_tokens(f"{entry.get('title', '')} {entry.get('description', '')}")
        if len(tokens) >= 4:
            profiles.append((entry["path"], entry.get("title", ""), tokens))
    for i in range(len(profiles)):
        for j in range(i + 1, len(profiles)):
            a_path, a_title, a_tok = profiles[i]
            b_path, b_title, b_tok = profiles[j]
            shared = len(a_tok & b_tok)
            overlap = shared / min(len(a_tok), len(b_tok))
            if overlap >= DUPLICATE_OVERLAP:
                out.append(_finding(
                    "duplicate-topic", "warning", a_path,
                    f"和「{b_title or b_path}」（{b_path}）讲的像是同一个主题（重合度 {overlap:.0%}）",
                    "合成一篇，旧的那篇归档",
                ))
    return sorted(out, key=lambda f: f["ref"])


def summarize(findings):
    """Group findings by check and severity for a short report."""
    by_check = {}
    by_severity = {}
    for item in findings:
        by_check[item["check"]] = by_check.get(item["check"], 0) + 1
        by_severity[item["severity"]] = by_severity.get(item["severity"], 0) + 1
    return {"total": len(findings), "by_check": by_check, "by_severity": by_severity}
