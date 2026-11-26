"""Safe Markdown-to-HTML renderer and internal link rewriter for Unified Memory."""

from html import escape
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlencode, urlsplit

APP = Path(__file__).resolve().parent
ROOT_DIR = APP.parent
if str(ROOT_DIR / "mcp") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "mcp"))
if str(ROOT_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "scripts"))

import server as mcp_server  # noqa: E402

INLINE_TOKEN_RE = re.compile(
    r"(`[^`]+`)|(\*\*[^*]+\*\*)|(\[(.+?)\]\(([^)\s]+)\))"
)
ORDERED_ITEM_RE = re.compile(r"^\s*\d+\.\s+(.+)$")
UNORDERED_ITEM_RE = re.compile(r"^\s*[-*+]\s+(.+)$")
TABLE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")


def hidden(name, value):
    return f'<input type="hidden" name="{escape(name)}" value="{escape(value)}">'


def query(**fields):
    clean = {k: v for k, v in fields.items() if v != ""}
    return "/?" + urlencode(clean) if clean else "/"


def format_inline_md(text, base_rel_dir, root, topics, link_params):
    """Safely render inline code, bold, and links (rewriting internal .md refs)."""
    parts = []
    last = 0
    for m in INLINE_TOKEN_RE.finditer(text):
        parts.append(escape(text[last : m.start()]))
        code_tok, bold_tok, _, link_label, link_href = m.groups()
        if code_tok:
            code_inner = code_tok[1:-1]
            app_url, tip = _resolve_internal_md_url(code_inner, base_rel_dir, root, topics, link_params)
            if app_url:
                title_attr = f' title="{escape(tip)}"' if tip else ""
                parts.append(
                    f'<a class="inline-code-link" href="{escape(app_url)}"{title_attr}>'
                    f'<code class="inline-code">{escape(code_inner)}</code></a>'
                )
            else:
                parts.append(f'<code class="inline-code">{escape(code_inner)}</code>')
        elif bold_tok:
            parts.append(f"<strong>{escape(bold_tok[2:-2])}</strong>")
        else:
            parts.append(render_md_link(link_label, link_href, base_rel_dir, root, topics, link_params))
        last = m.end()
    parts.append(escape(text[last:]))
    return "".join(parts)


def _resolve_internal_md_url(ref_text, base_rel_dir, root, topics, link_params):
    """Resolve a relative or library-root Markdown ref into an in-app URL and tooltip."""
    raw = (ref_text or "").strip()
    if not raw or any(ch.isspace() for ch in raw):
        return "", ""
    parsed = urlsplit(raw)
    clean_path = unquote(parsed.path)
    if parsed.scheme or clean_path.startswith("/") or not clean_path or not clean_path.lower().endswith(".md"):
        return "", ""
    root_resolved = Path(root).resolve()
    candidates = [root / base_rel_dir / clean_path]
    if str(base_rel_dir) not in ("", "."):
        candidates.append(root / clean_path)
    for unnorm in candidates:
        resolved = unnorm.resolve()
        if resolved.suffix.lower() != ".md" or not resolved.is_file():
            continue
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
                return query(**sk_params) + "#detail-title", f"打开技能文档 {sk_folder}/{sub_doc}"
        if resolved.is_relative_to(root_resolved):
            rel_posix = resolved.relative_to(root_resolved).as_posix()
            if rel_posix == "MEMORY.md":
                return query(library=link_params["library"], view="index") + "#detail-title", "打开 MEMORY.md"
            if rel_posix in topics:
                mem_params = {k: v for k, v in link_params.items() if k != "tab"}
                topic_obj = topics[rel_posix]
                tip = f"打开 {topic_obj.title}" if getattr(topic_obj, "title", "") else f"打开 {rel_posix}"
                return query(**mem_params, topic=rel_posix) + "#detail-title", tip
            if rel_posix.startswith("user-managed/") and len(Path(rel_posix).parts) == 2:
                mem_params = {k: v for k, v in link_params.items() if k != "tab"}
                return query(**mem_params, entry=Path(rel_posix).stem) + "#detail-title", f"打开手记 {rel_posix}"
    return "", ""


def render_md_link(label, href, base_rel_dir, root, topics, link_params):
    """Rewrite intra-library and intra-skill Markdown links into clickable in-app URLs."""
    safe_label = escape(label)
    if href.startswith(("https://", "http://")):
        return f'<a href="{escape(href)}" target="_blank" rel="noopener noreferrer">{safe_label} ↗</a>'
    app_url, _ = _resolve_internal_md_url(href, base_rel_dir, root, topics, link_params)
    if app_url:
        return f'<a href="{escape(app_url)}">{safe_label}</a>'
    return f'<span class="local-ref" title="{escape(href)}">{safe_label}</span>'


def split_table_row(line):
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def render_markdown_html(md_text, rel_doc_path, root, topics, link_params, skip_h1="", include_toc=True):
    """Render safe structured HTML from Markdown (headings, TOC, lists, tables, code blocks)."""
    _, body = mcp_server.parse_frontmatter(md_text)
    base_dir = Path(rel_doc_path).parent
    lines = body.splitlines()
    blocks = []
    headings = []
    idx = 0
    skipped_first_h1 = False

    while idx < len(lines):
        raw = lines[idx]
        stripped = raw.strip()

        if not stripped:
            idx += 1
            continue

        if stripped.startswith("```"):
            fence_lang = stripped[3:].strip().split()[0][:24] if len(stripped) > 3 and stripped[3:].strip() else ""
            idx += 1
            code_lines = []
            while idx < len(lines) and not lines[idx].strip().startswith("```"):
                code_lines.append(lines[idx])
                idx += 1
            if idx < len(lines):
                idx += 1
            lang_html = f'<span class="code-lang">{escape(fence_lang)}</span>' if fence_lang else '<span class="code-lang">code</span>'
            blocks.append(
                '<div class="code-wrap">'
                f'<div class="code-bar">{lang_html}'
                '<button type="button" class="code-copy" data-copy-code>复制</button></div>'
                f'<pre class="code-block"><code>{escape(chr(10).join(code_lines))}</code></pre>'
                "</div>"
            )
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
                sec_id = f"sec-{len(headings) + 1}"
                if tag in ("h2", "h3"):
                    headings.append((tag, sec_id, htext))
                    blocks.append(f'<{tag} id="{sec_id}">{format_inline_md(htext, base_dir, root, topics, link_params)}</{tag}>')
                else:
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

    toc_html = ""
    if include_toc and len(headings) >= 3:
        toc_items = "".join(
            f'<a class="doc-toc-link doc-toc-{tag}" href="#{sec_id}">{escape(htext)}</a>'
            for tag, sec_id, htext in headings[:18]
        )
        toc_html = (
            '<nav class="doc-toc" aria-label="正文目录">'
            '<span class="doc-toc-label">目录</span>'
            f'<div class="doc-toc-links">{toc_items}</div>'
            "</nav>"
        )
    return f'{toc_html}<article class="prose">{"".join(blocks)}</article>'
