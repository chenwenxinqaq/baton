"""HTML view components and page renderers for the Unified Memory Web & Desktop console."""

from datetime import datetime
import difflib
from html import escape
from pathlib import Path
import secrets
from string import Template
import sys
import time
from types import SimpleNamespace

APP = Path(__file__).resolve().parent
ROOT_DIR = APP.parent
if str(ROOT_DIR / "mcp") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "mcp"))
if str(ROOT_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "scripts"))
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from library_paths import ALL_LAYERS, data_root  # noqa: E402
import audit_log  # noqa: E402
import handoff  # noqa: E402
import l0_guard  # noqa: E402
import memory_import  # noqa: E402
import server as mcp_server  # noqa: E402
import sync_git  # noqa: E402
import upkeep  # noqa: E402
from async_sync import is_stale_active  # noqa: E402
from markdown_render import (  # noqa: E402
    hidden,
    query,
    render_markdown_html,
)

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
STATES = {"active": "生效中", "archived": "已归档"}

MAX_TITLE_CHARS = 120
MAX_DESC_CHARS = 800
MAX_BODY_CHARS = 120_000
CUR_ATTR = ' aria-current="page"'

DESKTOP = False
PAGE_TEMPLATE = Template((APP / "page.html").read_text(encoding="utf-8"))


def configure_ui(*, desktop=False, template=None):
    """Synchronize desktop mode and HTML page template with the controller."""
    global DESKTOP, PAGE_TEMPLATE
    DESKTOP = bool(desktop)
    if template is not None:
        PAGE_TEMPLATE = template


def load_template():
    return PAGE_TEMPLATE


def load_style():
    base = (APP / "style.css").read_bytes()
    return base + (APP / "desktop.css").read_bytes() if DESKTOP else base


def load_script():
    return (APP / "app.js").read_bytes()


_EMPTY_ENTRY = SimpleNamespace(
    name="",
    title="",
    description="",
    layer="L2",
    entry_type="",
    body="",
    status="active",
    created_at="",
    updated_at="",
    verified_at="",
    valid_until="",
    created_epoch=0.0,
    updated_epoch=0.0,
    revision="",
)


def query_plan(q):
    """Split a search box query the way MCP search does.

    Returns ``(has_temporal_cue, pure_temporal, raw_terms, subtokens, procedure_variants)``.
    """
    q_stripped = q.strip()
    has_temporal_cue = bool(q_stripped and mcp_server.TEMPORAL_CUE_RE.search(q_stripped))
    residual_q = mcp_server.TEMPORAL_STOP_RE.sub(" ", q_stripped).strip() if has_temporal_cue else q_stripped
    pure_temporal = bool(has_temporal_cue and not residual_q)
    effective_q = "" if pure_temporal else (residual_q or q_stripped)
    raw_terms, subtokens = mcp_server.build_query_features(effective_q)
    return has_temporal_cue, pure_temporal, raw_terms, subtokens, mcp_server.procedure_phrase_variants(effective_q)


def topic_search_fields(library, topic):
    """Return the name, description and body MCP search would score for an indexed topic."""
    try:
        resolved = (library.root / topic.path).resolve(strict=True)
        if not resolved.is_relative_to(library.root):
            raise ValueError("只能读取当前库中的 Markdown。")
        meta, body = mcp_server.load_entry(resolved)
    except (ValueError, OSError):
        return Path(topic.path).stem, topic.description, ""
    return str(meta.get("name") or Path(topic.path).stem), str(meta.get("description") or topic.description), body


def top_hit_citations(library, topics, rows, q):
    """List the indexed entries the best hit cites that the result list lacks.

    This is the list MCP search returns as ``see_also``.
    """
    _, _, raw_terms, subtokens, variants = query_plan(q)
    if not raw_terms or not rows or rows[0][1] != "index":
        return []
    top = rows[0][2]
    name, description, body = topic_search_fields(library, top)
    _, matched, _ = mcp_server.relevance(raw_terms, subtokens, variants, name, top.title, description, body)
    if mcp_server.match_strength(raw_terms, matched) == "fuzzy":
        return []
    shown = {item.path for _, kind, item in rows if kind == "index"}
    return [topics[path] for path in mcp_server.cited_paths(body, topics, shown)]


def filter_and_score(library, topics, entries, q, layer, status, source, window="", sort_by="updated"):
    """Filter topics and user entries by layer/status/source/window, temporal cues, and sort order.

    Matching and scoring come from the MCP server, so the search box and
    ``memory_search`` agree on what matches and how strongly.
    """
    has_temporal_cue, pure_temporal, raw_terms, subtokens, procedure_variants = query_plan(q)

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
                name, description, body_text = topic_search_fields(library, topic)
                score, _, _ = mcp_server.relevance(
                    raw_terms, subtokens, procedure_variants, name, topic.title, description, body_text
                )
                if score == 0:
                    continue
                score = mcp_server.apply_freshness(
                    score, now_ep - topic.updated_epoch, has_temporal_cue,
                    topic.layer != "L4" and mcp_server.is_date_expired(topic.valid_until),
                )
            scored_rows.append((score, "index", topic))

    if source != "index":
        for item in entries:
            if item.status != status or (layer and item.layer != layer):
                continue
            if window_cutoff is not None and item.updated_epoch < window_cutoff:
                continue
            score = 1
            if raw_terms:
                score, _, _ = mcp_server.relevance(
                    raw_terms, subtokens, procedure_variants, item.name, item.title, item.description, item.body
                )
                if score == 0:
                    continue
                score = mcp_server.apply_freshness(
                    score, now_ep - item.updated_epoch, has_temporal_cue,
                    item.layer != "L4" and mcp_server.is_date_expired(getattr(item, "valid_until", "")),
                )
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
        icon = (
            f'<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            f"{NAV_ICONS[slug]}</svg>"
        )
        links.append(f'<a href="{href}"{CUR_ATTR if slug == selected else ""}>'
                     f'<span class="nav-icon reicon" aria-hidden="true">{icon}</span>{label}</a>')
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
        report = library.store.lint()
        by_sev = (report.get("summary") or {}).get("by_severity") or {}
        err_cnt = by_sev.get("error", 0)
        warn_cnt = by_sev.get("warning", 0)
        if err_cnt:
            chips.append(f'<span class="chip-warn">! 体检 {err_cnt} 项需要修复</span>')
        elif warn_cnt:
            chips.append(f'<span class="chip-warn">! 体检 {warn_cnt} 项待复核</span>')
    except Exception:
        pass
    try:
        l0_cnt = l0_guard.count_proposals(library.root)
        if l0_cnt and library.role == "personal":
            l0_href = escape(query(library=key, view="l0_review"))
            chips.append(f'<a class="chip-warn" href="{l0_href}">! {l0_cnt} 条 L0 约束待确认</a>')
    except Exception:
        pass
    try:
        ho_cnt = handoff.count_active(library.root)
        if ho_cnt and library.role == "personal":
            ho_href = escape(query(library=key, view="handoff"))
            chips.append(f'<a class="meta-chip" href="{ho_href}">交接包 {ho_cnt}</a>')
    except Exception:
        pass
    href = escape(query(library=key, view="activity"))
    upkeep_href = escape(query(library=key, view="upkeep"))
    handoff_href = escape(query(library=key, view="handoff"))
    handoff_link = f'<a href="{handoff_href}">会话交接</a>' if key == "primary" else ""
    return (
        f'<div class="desk-status check-chips">{"".join(chips)}'
        f'<span class="desk-status-links">{handoff_link}<a href="{upkeep_href}">整理日报</a><a href="{href}">查看读写动态 →</a></span></div>'
    )


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
        ("index", "主知识库"),
        ("user", "个人手记"),
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
                   placeholder="weekly-report-export">
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
        <form class="edit-form" method="post" action="/save#feedback" aria-labelledby="detail-title">
          {hidden_inputs}
          {scope_html}
          {render_layer_radios(layer if layer in LAYERS else "L1")}
          {title_row}
          <div class="field">
            <label for="description">一句话索引摘要 / 检索线索</label>
            <input id="description" name="description" type="text" value="{escape(description)}" maxlength="{MAX_DESC_CHARS}"
                   placeholder="写明核心结论与触发场景">
          </div>
          <div class="form-row-two">
            <div class="field">
              <label for="verified_at">核实日期（可选）</label>
              <input id="verified_at" name="verified_at" type="date" value="{escape(verified_at)}">
            </div>
            <div class="field">
              <label for="valid_until">有效期至（可选）</label>
              <input id="valid_until" name="valid_until" type="date" value="{escape(valid_until)}">
            </div>
          </div>
          <div class="field">
            <div class="editor-field-head">
              <label for="body">正文（Markdown） <span class="required">必填</span></label>
              <div class="editor-tools">
                <span id="editor-metrics" class="editor-metrics" aria-live="polite"></span>
                <div class="editor-tabs" role="tablist" aria-label="编辑器视图">
                  <button type="button" class="editor-tab" role="tab" aria-selected="true" data-editor-tab="write">编辑</button>
                  <button type="button" class="editor-tab" role="tab" aria-selected="false" data-editor-tab="preview">预览</button>
                </div>
              </div>
            </div>
            <div id="editor-secret-alert" class="editor-secret-alert" role="alert" hidden></div>
            <textarea class="auto-grow-textarea" id="body" name="body" rows="18" maxlength="{MAX_BODY_CHARS}"
                      placeholder="记录已验证结论、适用条件、证据路径与失效边界…" required>{escape(body)}</textarea>
            <div id="editor-preview" class="editor-preview" hidden></div>
          </div>
          <div class="form-actions">
            <button class="btn btn-primary" type="submit">{submit_txt}</button>
            <a class="btn btn-ghost" href="{escape(cancel_link)}">取消</a>
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
        <form class="edit-form" method="post" action="/save_index#feedback" aria-labelledby="detail-title">
          {hidden_inputs}
          <div class="field">
            <label for="index-body">MEMORY.md 全文（Markdown） <span class="required">必填</span></label>
            <textarea class="auto-grow-textarea" id="index-body" name="body" rows="24" maxlength="{MAX_BODY_CHARS}"
                      required autofocus>{escape(markdown_text)}</textarea>
          </div>
          <div class="form-actions">
            <button class="btn btn-primary" type="submit">保存 MEMORY.md</button>
            <a class="btn btn-ghost" href="{escape(cancel_link)}">取消</a>
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
        <p class="confirm-dialog-desc">{escape(note)} <code>{escape(display_path)}</code></p>
        <div class="confirm-dialog-actions">
          <button class="btn btn-secondary" type="button" commandfor="delete-dialog" command="close" autofocus>取消</button>
          <form method="post" action="/delete#feedback">
            {hidden_inputs}
            <button class="btn btn-danger" type="submit">确认删除</button>
          </form>
        </div>
      </dialog>"""


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
        ver = str(row.get("version") or "")
        diff_btn = (
            f'<button class="btn btn-ghost btn-compact" type="button" data-version-diff '
            f'data-library="{escape(library_key)}" data-version="{escape(ver)}">对比差异</button>'
        )
        action = ""
        if not readonly:
            action = f"""
              <form method="post" action="/restore_version#feedback" aria-label="恢复 {escape(saved)} 的版本">
                {hidden("csrf", token)}{hidden("library", library_key)}
                {hidden("version", ver)}{hidden("slug", slug)}
                <button class="btn btn-ghost btn-compact" type="submit">恢复此版本</button>
              </form>"""
        rows.append(
            f'<li class="intent-row">'
            f'<div class="version-row-head">'
            f'<span class="version-when">{escape(saved)}</span>'
            f'<span class="meta-chip">{escape(reason)}</span>'
            f'<span class="version-size">{chars} 字</span>'
            f'<div class="version-actions intent-actions">{diff_btn}{action}</div>'
            f'</div>'
            f'<div class="version-diff-slot" hidden></div>'
            f'</li>'
        )
    return f"""
      <section class="detail-block" aria-labelledby="versions-title">
        <h2 class="block-title" id="versions-title">历史版本</h2>
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
    expired = (
        bool(getattr(item, "valid_until", ""))
        and item.layer != "L4"
        and mcp_server.is_date_expired(item.valid_until, today=now_dt.date())
    )
    expired_tag = '<span class="entry-tag-expired">已过期</span>' if expired else ""
    time_html = render_card_time_html(item.created_at, item.updated_at, sort_by, now_dt)
    summary = f'<p class="entry-desc">{escape(item.description)}</p>' if item.description else ""
    return f"""
      <li><a class="entry-link" href="{escape(href)}#detail-title"{current}>
        <div class="entry-card-head">
          {badge}
          <strong class="entry-title">{escape(item.title)}</strong>
          {user_tag}
          {expired_tag}
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
            if is_stale_active(async_status):
                parts.append('<span class="chip-warn">! 上次后台同步未完成</span>')
            else:
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
        detected_unmanaged = []
        for aid, spec in specs.items():
            info = memoryctl.inspect_agent(aid, spec)
            state = info["state"]
            detected = bool(info.get("detected"))
            pending_count = 0
            if aid in memory_import.CLIENTS and key == "primary" and library is not None:
                try:
                    pending_count = len(memory_import.pending(library.root, aid))
                except (OSError, ValueError):
                    pending_count = 0
            detected_pill = ""
            if state == "managed":
                managed_count += 1
                pill_cls = "status-pill status-pill-managed"
                pill_txt = "● 已配置"
                btn_html = f"""
                  <form class="intent-secondary" method="post" action="/agent#feedback">
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
                  <form class="intent-secondary" method="post" action="/agent#feedback">
                    {hidden("csrf", token)}{hidden("library", key)}{hidden("tab", tab)}
                    {hidden("target", aid)}{hidden("op", "disconnect")}
                    <button class="btn btn-danger-ghost btn-compact" type="submit">断开</button>
                  </form>"""
            else:
                pill_cls = "status-pill"
                pill_txt = "○ 未接入"
                if detected:
                    detected_unmanaged.append(info["name"])
                    detected_pill = '<span class="status-pill status-pill-detected">本机已安装</span>'
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
                    label = f"待确认记忆 ({pending_count})" if pending_count else "待确认记忆"
                elif pending_count:
                    label = f"导入记忆 ({pending_count})"
                sec_cls = "" if pending_count else " intent-secondary"
                btn_html += f'<a class="btn btn-ghost btn-compact{sec_cls}" href="{inbox_link}">{label}</a>'
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
            <li class="agent-item intent-row">
              <div class="agent-info">
                <div class="agent-name-row">
                  <strong>{escape(info["name"])}</strong>
                  <span class="{pill_cls}">{pill_txt}</span>
                  {detected_pill}
                </div>
                <div class="check-chips">{chips}</div>
                {issue_html}
              </div>
              <div class="agent-btns{" intent-actions" if state == "managed" and not pending_count else ""}">{btn_html}</div>
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

        # Imports are filed into the primary library, so only its receipts can clear the list.
        stray_root = library.root if library is not None and key == "primary" else None
        stray_list = memoryctl.scan_stray_agent_memories(home=agent_home, root=stray_root)
        stray_alert = ""
        if stray_list:
            stray_items = "".join(f"<li><code>{escape(p)}</code></li>" for p in stray_list[:20])
            extra_note = f"<li>… 另有 {len(stray_list) - 20} 个文件</li>" if len(stray_list) > 20 else ""
            stray_actions = ""
            if key == "primary":
                claude_inbox = escape(query(view="memory_import", client="claude") + "#detail-title")
                stray_actions = f"""
              <div class="ops-callout-actions">
                <form method="post" action="/memory_import">
                  {hidden("csrf", token)}{hidden("op", "import")}{hidden("client", "claude")}
                  <button class="btn btn-warn btn-compact" type="submit">一键导入散落记忆</button>
                </form>
                <a class="btn btn-ghost btn-compact" href="{claude_inbox}">查看待确认队列 →</a>
              </div>"""
            stray_alert = f"""
          <section class="ops-callout ops-callout-warn" aria-label="发现散落原生记忆">
            <div class="ops-callout-head">
              <div>
                <strong>发现 {len(stray_list)} 份未归集的 Claude Code 散落记忆</strong>
              </div>
              {stray_actions}
            </div>
            <details class="stray-files">
              <summary>查看文件路径（{len(stray_list)}）</summary>
              <ul>{stray_items}{extra_note}</ul>
            </details>
          </section>"""

        detected_banner = ""
        if flat and detected_unmanaged:
            detected_names = "、".join(detected_unmanaged)
            detected_banner = f"""
          <section class="ops-callout ops-callout-info" aria-label="检测到未接管客户端">
            <div class="ops-callout-head">
              <div>
                <strong>检测到本机已安装但尚未接入的客户端：{escape(detected_names)}</strong>
              </div>
              <div class="ops-callout-actions">
                <form method="post" action="/agent#feedback">
                  {hidden("csrf", token)}{hidden("library", key)}{hidden("tab", tab)}
                  {hidden("target", "detected")}{hidden("op", "connect")}
                  <button class="btn btn-primary btn-compact" type="submit">一键接管已安装客户端</button>
                </form>
              </div>
            </div>
          </section>"""
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
          {detected_banner}
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
      <form class="intent-secondary" method="post" action="/memory_import">
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
        description = draft.get("description", item.get("description", ""))
        body = draft.get("body", item["body"])
        origin = " · ".join(part for part in (item["source"], item.get("source_detail"), item["source_date"], item["thread_id"]) if part)
        if item.get("previously_approved"):
            origin += f' · 此前已归入 {item["previously_approved"]}，来源之后有改动；确认后会更新该条目'
        similar_html = ""
        selected_merge = draft.get("merge_target", "")
        if not item.get("previously_approved"):
            try:
                similar = [
                    hit
                    for hit in library.store.similar_entries(f"{title} {description}", exclude_name=item["id"], limit=3)
                    if hit.get("path") and not hit["path"].startswith("user-managed/")
                ]
            except Exception:
                similar = []
            if similar:
                sim_opts = [
                    f'<label class="merge-option">'
                    f'<input type="radio" name="merge_target" value=""{" checked" if not selected_merge else ""}>'
                    f'<span>作为新条目归层（默认）</span></label>'
                ]
                for hit in similar:
                    h_path = hit["path"]
                    h_href = escape(query(topic=h_path) + "#detail-title")
                    h_layer = escape(hit.get("layer", ""))
                    h_title = escape(hit.get("title") or h_path)
                    checked = " checked" if selected_merge == h_path else ""
                    sim_opts.append(
                        f'<label class="merge-option">'
                        f'<input type="radio" name="merge_target" value="{escape(h_path)}"{checked}>'
                        f'<span>合并追加到已有条目：<a href="{h_href}" target="_blank" rel="noopener">{h_title}</a>'
                        f' <code>{escape(h_path)}</code> ({h_layer})</span></label>'
                    )
                similar_html = (
                    '<fieldset class="merge-suggestions">'
                    '<legend>发现近似已有条目（可直接合并，避免同主题重复建篇）</legend>'
                    f'{"".join(sim_opts)}</fieldset>'
                )
        form = f"""
          <form class="edit-form" method="post" action="/memory_import">
            {hidden("csrf", token)}{hidden("op", "approve")}{hidden("client", client)}{hidden("import_id", item["id"])}
            {hidden("revision", memory_import.revision(item))}
            <h2>确认并归层</h2>
            <p>{escape(origin)}</p>
            {similar_html}
            <label>标题 <input type="text" name="title" maxlength="{MAX_TITLE_CHARS}" required value="{escape(title)}"></label>
            <label>检索摘要 <input type="text" name="description" maxlength="{MAX_DESC_CHARS}" value="{escape(description)}"></label>
            <label>正文 <textarea name="body" rows="14" required>{escape(body)}</textarea></label>
            {render_layer_radios(draft.get("layer", ""))}
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
        {automatic}
        <details {"open" if not automatic and not item else ""}>
          <summary>从文件或文本导入</summary>
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


def _render_diff_box(diff_text, empty_msg="与当前生效版本无差异。"):
    lines = diff_text.splitlines() if diff_text else []
    if not lines:
        return f'<p class="field-hint">{escape(empty_msg)}</p>'
    rows = []
    for line in lines:
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
        rows.append(f'<div class="diff-line {cls}">{escape(line)}</div>')
    return f'<div class="diff-view">{"".join(rows)}</div>'


def render_l0_review(token, key, library):
    """Render the pending L0 constraint approval queue to guard against unintended agent writes."""
    proposals = l0_guard.list_proposals(library.root)
    if not proposals:
        return (
            '<section class="activity-usage">'
            '<div class="usage-head"><h3>L0 操作约束待确认区</h3>'
            '<span class="usage-hint">当前没有待确认的 L0 约束修改</span></div>'
            '</section>'
        )
    cards = []
    for idx, item in enumerate(proposals, 1):
        diff_html = _render_diff_box(l0_guard.proposal_diff(library.root, item))
        action_label = "更新已有约束" if item.get("action") == "update" else "新建 L0 约束"
        meta_parts = [
            f"来源客户端：{item.get('client') or 'AI 客户端'}",
            action_label,
            f"目标：{item.get('target_path') or ('constraints/' + item['name'] + '.md')}",
            f"提议时间：{(item.get('updated_at') or '')[:19].replace('T', ' ')}",
        ]
        cards.append(
            f"""
        <article class="conflict-card intent-row">
          <div class="conflict-card-head">
            <div>
              <strong>提案 #{idx}：{escape(item.get("title") or item["name"])}</strong>
              <p class="hub-card-path">{escape(" · ".join(meta_parts))}</p>
            </div>
            <div class="conflict-quick-actions">
              <form method="post" action="/l0_review#feedback">
                {hidden("csrf", token)}{hidden("library", key)}
                {hidden("proposal_id", item["id"])}{hidden("revision", item.get("revision", ""))}
                {hidden("op", "approve")}
                <button class="btn btn-primary btn-compact" type="submit">确认生效</button>
              </form>
              <form method="post" action="/l0_review#feedback">
                {hidden("csrf", token)}{hidden("library", key)}
                {hidden("proposal_id", item["id"])}{hidden("revision", item.get("revision", ""))}
                {hidden("op", "reject")}
                <button class="btn btn-ghost btn-compact" type="submit">拒绝丢弃</button>
              </form>
            </div>
          </div>
          {diff_html}
          <details class="l0-edit-details">
            <summary>修订后再确认生效</summary>
            <form class="edit-form" method="post" action="/l0_review#feedback">
              {hidden("csrf", token)}{hidden("library", key)}
              {hidden("proposal_id", item["id"])}{hidden("revision", item.get("revision", ""))}
              {hidden("op", "approve")}
              <label>标题 <input type="text" name="title" maxlength="{MAX_TITLE_CHARS}" required value="{escape(item.get('title') or '')}"></label>
              <label>检索摘要 <input type="text" name="description" maxlength="{MAX_DESC_CHARS}" value="{escape(item.get('description') or '')}"></label>
              <label>正文 <textarea name="body" rows="8" required>{escape(item.get('body') or '')}</textarea></label>
              <button class="btn btn-primary btn-compact" type="submit">保存修订并生效</button>
            </form>
          </details>
        </article>"""
        )
    return (
        '<section class="l0-review-panel">'
        f'{"".join(cards)}'
        '</section>'
    )


UPKEEP_KIND_LABELS = {
    "merge_duplicate": "合并重复",
    "archive_stale": "归档过期",
    "trim_oversized": "精简超长",
    "rebuild_index": "修复索引",
}


def render_upkeep_panel(token, key, library, agent_home=None):
    """Render the Sleep-time Upkeep / Morning Note review queue with apply/dismiss actions."""
    note = upkeep.public_morning_note(library.store, audit_home=agent_home)
    suggestions = note.get("suggestions") or []
    dismissed_cnt = note.get("dismissed_count", 0)
    pending_l0 = note.get("pending_l0_count", 0)

    header_chips = [
        f'<span class="meta-chip">条目 {note.get("entries_count", 0)} 篇</span>',
        f'<span class="meta-chip">近 7 天查询 {note.get("recent_reads_7d", 0)} · 写入 {note.get("recent_writes_7d", 0)}</span>',
    ]
    if pending_l0 and key == "primary":
        l0_href = escape(query(library=key, view="l0_review"))
        header_chips.append(f'<a class="chip-warn" href="{l0_href}">! {pending_l0} 条 L0 约束待确认</a>')

    reset_btn = ""
    if dismissed_cnt:
        reset_btn = (
            '<form class="inline-form" method="post" action="/upkeep#feedback">'
            f'{hidden("csrf", token)}{hidden("library", key)}{hidden("op", "reset_dismissed")}'
            f'<button class="btn btn-ghost btn-compact" type="submit">恢复已忽略（{dismissed_cnt}）</button>'
            "</form>"
        )

    if not suggestions:
        return (
            '<section class="activity-usage">'
            '<div class="usage-head"><h3>记忆整理日报</h3>'
            f'<span class="usage-hint">当前库状态整洁，暂无待整理项 {reset_btn}</span></div>'
            f'<div class="check-chips">{"".join(header_chips)}</div>'
            "</section>"
        )

    cards = []
    for idx, item in enumerate(suggestions, 1):
        kind = item["kind"]
        kind_label = UPKEEP_KIND_LABELS.get(kind, kind)
        diff_html = _render_diff_box(item.get("diff", ""), empty_msg="") if item.get("diff") else ""
        links = []
        if item.get("target_path"):
            links.append(_ref_link(item["target_path"], key))
        if item.get("secondary_path") and kind == "merge_duplicate":
            links.append(f'← {_ref_link(item["secondary_path"], key)}')
        links_html = f'<p class="hub-card-path">{" ".join(links)}</p>' if links else ""
        apply_btn = ""
        if item.get("can_apply") and not library.is_readonly:
            apply_btn = (
                '<form method="post" action="/upkeep#feedback">'
                f'{hidden("csrf", token)}{hidden("library", key)}{hidden("op", "apply")}'
                f'{hidden("suggestion_id", item["id"])}{hidden("fingerprint", item.get("fingerprint", ""))}'
                '<button class="btn btn-primary btn-compact" type="submit">应用</button>'
                "</form>"
            )
        dismiss_btn = (
            '<form method="post" action="/upkeep#feedback">'
            f'{hidden("csrf", token)}{hidden("library", key)}{hidden("op", "dismiss")}'
            f'{hidden("suggestion_id", item["id"])}{hidden("fingerprint", item.get("fingerprint", ""))}'
            '<button class="btn btn-ghost btn-compact" type="submit">忽略</button>'
            "</form>"
        )
        cards.append(
            f"""
        <article class="conflict-card intent-row">
          <div class="conflict-card-head">
            <div>
              <strong><span class="meta-chip">{escape(kind_label)}</span> #{idx} {escape(item["title"])}</strong>
              {links_html}
              <p class="act-meta">{escape(item["reason"])}</p>
            </div>
            <div class="conflict-quick-actions">
              {apply_btn}
              {dismiss_btn}
            </div>
          </div>
          {diff_html}
        </article>"""
        )
    return (
        '<section class="l0-review-panel">'
        f'<div class="usage-head"><div class="check-chips">{"".join(header_chips)}</div>{reset_btn}</div>'
        f'{"".join(cards)}'
        "</section>"
    )


_HANDOFF_STATUS_LABELS = {"open": "待接力", "claimed": "已接手", "done": "已结单"}


def render_handoff_panel(token, key, library):
    """Render the Cross-client Session Handoff hub with one-click copyable briefings."""
    items = handoff.list_handoffs(library.root, include_done=True, limit=20)
    active_cnt = sum(1 for it in items if it.get("status") != "done")
    cards = []
    for idx, item in enumerate(items, 1):
        hid = item["id"]
        status = item.get("status", "open")
        status_label = _HANDOFF_STATUS_LABELS.get(status, status)
        briefing = handoff.render_briefing(item, store=library.store)
        copy_id = f"handoff-briefing-{idx}"
        meta_parts = [
            f"{item.get('source_client') or '当前客户端'} → {item.get('target_client') or '任意客户端'}",
            f"编号 {hid}",
            f"更新于 {(item.get('updated_at') or '')[:19].replace('T', ' ')}",
        ]
        if item.get("claimed_by"):
            meta_parts.append(f"接手方：{item['claimed_by']}")
        actions = [
            f'<button class="btn btn-primary btn-compact" type="button" data-copy-target="{copy_id}">复制交接包</button>'
        ]
        if status != "done":
            actions.append(
                '<form method="post" action="/handoff#feedback">'
                f'{hidden("csrf", token)}{hidden("library", key)}{hidden("op", "complete")}{hidden("handoff_id", hid)}'
                '<button class="btn btn-secondary btn-compact" type="submit">标记结单</button>'
                "</form>"
            )
        actions.append(
            '<form method="post" action="/handoff#feedback">'
            f'{hidden("csrf", token)}{hidden("library", key)}{hidden("op", "delete")}{hidden("handoff_id", hid)}'
            '<button class="btn btn-ghost btn-compact" type="submit">删除</button>'
            "</form>"
        )
        cards.append(
            f"""
        <article class="conflict-card intent-row">
          <div class="conflict-card-head">
            <div>
              <strong><span class="meta-chip">{escape(status_label)}</span> {escape(item.get("title") or hid)}</strong>
              <p class="hub-card-path">{escape(" · ".join(meta_parts))}</p>
            </div>
            <div class="conflict-quick-actions">{"".join(actions)}</div>
          </div>
          <pre class="markdown-raw">{escape(briefing)}</pre>
          <textarea id="{copy_id}" hidden readonly>{escape(briefing)}</textarea>
        </article>"""
        )
    cards_html = (
        "".join(cards)
        if cards
        else '<p class="act-meta">还没有会话交接包。可在下方新建，或在 Codex / Claude Code 中直接让 AI 调用 <code>memory_handoff(action="create", ...)</code> 导出当前任务快照。</p>'
    )
    create_form = f"""
      <details class="activity-usage" {"open" if not active_cnt else ""}>
        <summary><strong>新建跨客户端会话交接包</strong></summary>
        <form class="edit-form" method="post" action="/handoff#feedback">
          {hidden("csrf", token)}{hidden("library", key)}{hidden("op", "create")}
          <label>任务标题 <input type="text" name="title" maxlength="{handoff.MAX_TITLE_CHARS}" required placeholder="例如：修复自动更新器签名校验边界"></label>
          <label>来源客户端 <input type="text" name="source_client" value="Codex" placeholder="Codex / Claude Code / Cursor"></label>
          <label>目标客户端 <input type="text" name="target_client" value="Claude Code" placeholder="Claude Code / Codex / Cursor（可留空）"></label>
          <label>工作目录（可选） <input type="text" name="workspace" placeholder="/path/to/repo"></label>
          <label>已验证结论与当前上下文 <textarea name="summary" rows="6" required placeholder="写清已确认根因、已做改动、未解决现象与约束边界"></textarea></label>
          <label>待完成步骤（每行一条，可选） <textarea name="next_steps" rows="4" placeholder="补齐回归单测&#10;运行打包冒烟并提交"></textarea></label>
          <label>关键文件路径（每行一条，可选） <textarea name="files" rows="3" placeholder="/path/to/file.py"></textarea></label>
          <label>关联记忆路径（每行一条，可选） <textarea name="memory_refs" rows="2" placeholder="projects/example.md"></textarea></label>
          <button class="btn btn-primary" type="submit">生成交接包</button>
        </form>
      </details>"""
    return (
        '<section class="l0-review-panel">'
        f'{create_form}{cards_html}'
        "</section>"
    )


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
          <article class="hub-card intent-row">
            <div class="hub-card-head">
              <div>
                <strong>{escape(item["label"])}</strong>
                <span class="hub-card-id"><code>{escape(lid)}</code></span>
              </div>
              <div class="hub-card-actions intent-actions">{"".join(act_btns)}</div>
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
        <div class="hub-card-list">{"".join(cards)}</div>
      </section>

      <section class="hub-section">
        <h2 class="hub-section-title">个人主库跨设备同步</h2>
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
                "memory_restore": "恢复版本",
                "memory_handoff": "会话交接"}


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
              {rank_html}</div>
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
        action_html = f'<span class="lint-actions intent-actions">{"".join(actions)}</span>' if actions else ""
        rows.append(
            f'<li class="intent-row"><div class="lint-row-head"><span>{_ref_link(ref, key)} '
            f'<span class="{cls}">{escape(label)}</span></span>{action_html}</div>'
            f'<span class="act-meta">{escape(item["detail"])} → {escape(item["hint"])}</span></li>'
        )
    more = ""
    if len(findings) > 8:
        lint_cmd = "umem lint" if key == "primary" else f"umem lint --library {key}"
        more = f'<li class="act-meta">还有 {len(findings) - 8} 条，运行 <code>{escape(lint_cmd)}</code> 看全部</li>'
    section_cls = "activity-usage lint-urgent" if urgent else "activity-usage"
    upkeep_link = (
        f'<a class="btn btn-ghost btn-compact" href="{escape(query(library=key, view="upkeep"))}">整理日报 →</a>'
        if not readonly
        else ""
    )
    html_out = (
        f'<section class="{section_cls}"><div class="usage-head"><h3>记忆库体检</h3>'
        f'<span class="usage-hint">{escape(chips)} {rebuild_btn} {upkeep_link}</span></div>'
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
    l0_banner = ""
    if root and not readonly:
        try:
            l0_proposals = l0_guard.list_proposals(root)
        except Exception:
            l0_proposals = []
        if l0_proposals:
            l0_href = escape(query(library=key, view="l0_review"))
            names = "、".join(escape(p.get("title") or p["name"]) for p in l0_proposals[:3])
            l0_banner = (
                '<section class="activity-usage lint-urgent">'
                '<div class="usage-head"><h3>L0 约束待确认</h3>'
                f'<span class="usage-hint">{len(l0_proposals)} 条 AI 提议尚未生效 '
                f'<a class="btn btn-primary btn-compact" href="{l0_href}">立即核对 Diff →</a></span></div>'
                f'<p class="act-meta">待审条目：{names}</p></section>'
            )
    checkpoint_note = f' · 阶段提醒 {summary["checkpoints_today"]} 次' if summary.get("checkpoints_today") else ""
    ordered_sections = (
        f"{l0_banner}{lint_section}{trend_section}{usage_section}"
        if lint_urgent
        else f"{l0_banner}{trend_section}{usage_section}{lint_section}"
    )

    return f"""
      <section class="activity-panel" aria-labelledby="activity-heading">
        <div class="recent-panel-head">
          <div class="recent-panel-title">
            <h2 id="activity-heading">AI 读写动态</h2>
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
            <h2 id="recent-work-heading">最近工作动态</h2>
            <span class="recent-stats">今日活跃 {today_cnt} 条 · 近 7 天活跃 {week_cnt} 条</span>
          </div>
          <a class="btn btn-secondary btn-compact" href="{escape(recent_more_href)}">筛选近 7 天全部 →</a>
        </div>
        <ul class="recent-list" role="list">{"".join(recent_items_html)}</ul>
      </section>"""

    cards = []
    for lk, (lname, _, _) in LAYERS.items():
        href = query(library=key, layer=lk)
        cards.append(
            f"""
          <a class="overview-card" href="{escape(href)}">
            <div class="overview-card-top">
              <span class="layer-badge layer-{lk}">{lk} · {escape(lname)}</span>
              <span class="overview-num">{layer_counts[lk]}</span>
            </div>
          </a>"""
        )

    return f"""
      {render_activity_panel(key, root=root, token=token, readonly=readonly)}
      {recent_panel}
      <div class="overview-grid">{"".join(cards)}</div>"""


def render_skills_overview(skills_matrix, key):
    """Render category overview cards for the unified skills hub."""
    cat_counts = {ck: 0 for ck in SKILL_CATS}
    for sk in skills_matrix:
        if sk["category"] in cat_counts:
            cat_counts[sk["category"]] += 1

    cards = []
    for ck, (cname, badge_layer, _) in SKILL_CATS.items():
        href = query(library=key, tab="skills", cat=ck)
        cards.append(
            f"""
          <a class="overview-card" href="{escape(href)}">
            <div class="overview-card-top">
              <span class="layer-badge layer-{badge_layer}">{escape(cname)}</span>
              <span class="overview-num">{cat_counts[ck]}</span>
            </div>
          </a>"""
        )

    return f'<div class="overview-grid">{"".join(cards)}</div>'


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
    entry=None,
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
    if entry is None:
        entry = _EMPTY_ENTRY
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
        except ValueError:
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
    cited = top_hit_citations(library, topics, matched, q) if status == "active" and source != "user" else []
    if cited:
        cited_links = "、".join(
            f'<a href="{escape(query(**link_params, topic=t.path))}">{escape(t.title)}</a>' for t in cited
        )
        listing += f'<p class="see-also">首条结果还引用了：{cited_links}</p>'

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
        f'{recent_cur}>最近工作 <span class="pill-count">{len(recent_7d_rows)}</span></a>',
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
          <details class="action-menu" data-safe-menu>
            <summary class="btn btn-ghost btn-compact"><span>更多</span><span class="reicon" aria-hidden="true"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg></span></summary>
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
    elif view == "l0_review":
        heading = "L0 操作约束待确认区"
        detail_toolbar = f'<a class="btn btn-ghost btn-compact" href="{escape(home_link)}">返回记忆库</a>'
        reader = render_l0_review(token, key, library)
    elif view == "upkeep":
        heading = f"记忆整理日报 · {library.label}"
        detail_toolbar = f'<a class="btn btn-ghost btn-compact" href="{escape(home_link)}">返回记忆库</a>'
        reader = render_upkeep_panel(token, key, library, agent_home=agent_home)
    elif view == "handoff":
        heading = "跨客户端会话交接包"
        detail_toolbar = f'<a class="btn btn-ghost btn-compact" href="{escape(home_link)}">返回记忆库</a>'
        reader = render_handoff_panel(token, key, library)
    elif view == "sync_hub":
        heading = "共享记忆库与 Git 同步中心"
        detail_toolbar = f'<a class="btn btn-ghost btn-compact" href="{escape(home_link)}">返回记忆库</a>'
        reader = render_sync_hub_html(token, key, libraries, agent_home=agent_home)
    elif view == "conflicts":
        heading = f"同步冲突对比与合并 · {library.label}"
        detail_toolbar = (
            f'<a class="btn btn-ghost btn-compact" href="{escape(query(library=key, view="sync_hub"))}#detail-title">共享与同步</a>'
            f'<a class="btn btn-ghost btn-compact" href="{escape(home_link)}">返回记忆库</a>'
        )
        reader = render_conflicts_view_html(token, key, library)
    elif view == "index":
        heading = "MEMORY.md · 记忆维护规则与总索引"
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
                  <details class="action-menu" data-safe-menu>
                    <summary class="btn btn-ghost btn-compact"><span>更多</span><span class="reicon" aria-hidden="true"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg></span></summary>
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
        else f'<a class="btn btn-primary" href="{escape(new_link)}">新建记忆</a>'
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
