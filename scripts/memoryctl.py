#!/usr/bin/env python3
"""Unified memory & skills control CLI."""

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from tempfile import NamedTemporaryFile
import time


from library_paths import (
    ALL_LAYERS,
    DEFAULT_SHARED_LAYERS,
    FRAMEWORK_ROOT,
    data_root,
    global_state_dir,
    library_unavailable_reason,
    load_mounts_config,
    library_seed_files,
    normalize_layers,
    resolve_libraries,
    update_mounts_config,
    validate_library_path,
    validate_mount_id,
)
import audit_log
import async_sync
import sync_git


def resolve_python_cmd():
    executable = Path(sys.executable)
    resources = executable.parent.parent.parent
    wrapper = resources / "umem-python"
    if resources.name == "Resources" and wrapper.is_file() and executable.parent.parent.name == "python":
        return str(wrapper)
    return sys.executable


SHIM_TARGETS = {
    "umem-mcp": "mcp/server.py",
    "umem-hook": "scripts/cursor_hook.py",
    "umem": "scripts/memoryctl.py",
}


def stable_bin_dir(home=None):
    """Stable launcher directory that client configs point at (survives app moves/updates)."""
    home = Path(home) if home else Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Unified Memory" / "bin"
    return home / ".local" / "share" / "unified-memory" / "bin"


def shim_path(name, home=None):
    return stable_bin_dir(home) / name


def render_shim(rel_script):
    """Return a POSIX launcher that runs `rel_script` with the current runtime.

    If the recorded runtime vanished (app moved or replaced), fall back to the app bundles in the
    usual install locations so configured clients keep working until the app regenerates the shim.
    """
    py, framework = shlex.quote(resolve_python_cmd()), shlex.quote(str(FRAMEWORK_ROOT))
    return f"""#!/bin/sh
# Managed by Unified Memory; regenerated whenever the app or memoryctl runs. Do not edit.
PY={py}
FW={framework}
if [ ! -x "$PY" ] || [ ! -f "$FW/{rel_script}" ]; then
  # Tests and sandboxed callers can disable host-level app discovery.  The
  # default keeps the recovery path for a moved app without weakening the
  # normal install behavior.
  if [ "${{UNIFIED_MEMORY_DISABLE_APP_FALLBACK:-0}}" != "1" ]; then
    for APP in "$HOME/Applications/Unified Memory.app" "/Applications/Unified Memory.app"; do
      R="$APP/Contents/Resources"
      if [ -x "$R/umem-python" ] && [ -f "$R/framework/{rel_script}" ]; then
        PY="$R/umem-python"; FW="$R/framework"; break
      fi
    done
  fi
fi
if [ ! -x "$PY" ] || [ ! -f "$FW/{rel_script}" ]; then
  echo "Unified Memory runtime not found. Open the Unified Memory app once, or run: memoryctl connect all" >&2
  exit 127
fi
exec "$PY" -B "$FW/{rel_script}" "$@"
"""


def ensure_stable_shims(home=None):
    """Create or refresh the stable launchers. Returns the list of paths that changed."""
    directory = stable_bin_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    changed = []
    for name, rel in SHIM_TARGETS.items():
        target = directory / name
        content = render_shim(rel)
        if target.is_file() and not target.is_symlink() and target.read_text(encoding="utf-8") == content \
                and os.access(target, os.X_OK):
            continue
        with NamedTemporaryFile("w", dir=directory, delete=False, encoding="utf-8") as tmp:
            tmp.write(content)
            staged = Path(tmp.name)
        staged.chmod(0o755)
        os.replace(staged, target)
        changed.append(str(target))
    return changed


MEMORY_ROOT = data_root()
STATE_DIR = global_state_dir()
LEGACY_STATE_DIR = MEMORY_ROOT / ".state"
RESTORE_DIR = STATE_DIR / "restore"
SKILL_BACKUP_DIR = STATE_DIR / "skill-backups"
LOCK_FILE = STATE_DIR / "integration.lock"
MCP_SERVER = FRAMEWORK_ROOT / "mcp" / "server.py"
CURSOR_HOOK_SCRIPT = FRAMEWORK_ROOT / "scripts" / "cursor_hook.py"
CURSOR_HOOK_MARKERS = ("umem-hook", "cursor_hook.py")  # second marker migrates legacy entries
CURSOR_HOOK_EVENTS = ("sessionStart", "beforeSubmitPrompt", "stop")
NESTED_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop")
SKILLS_DIR = MEMORY_ROOT / "skills"

MD_START = "<!-- unified-memory:managed:start -->"
MD_END = "<!-- unified-memory:managed:end -->"
TOML_START = "# BEGIN unified-memory managed"
TOML_END = "# END unified-memory managed"
TOML_SECTION_HEADER = "[mcp_servers.unified_memory]"

INDEX_LAYER_RE = re.compile(r"^## (L[0-4])(?:\s|$)")
BULLET_LINK_PREFIX_RE = re.compile(r"^\s*[-*+]\s+\[")

LIBRARY_TEMPLATE = FRAMEWORK_ROOT / "templates" / "library.md"
# The first L0 heading starts the index. Only the public rules belong in client instructions.
WRITING_POLICY, index_start, _ = LIBRARY_TEMPLATE.read_text(encoding="utf-8").partition("\n## L0 ")
if not index_start:
    raise ValueError("Missing L0 index heading in the public library template.")

MANAGED_MD_BODY = f"""{MD_START}
## 统一本地记忆

当前用户的权威记忆库位于 `{MEMORY_ROOT}`。所有记忆读写都使用这个目录。客户端默认记忆目录不用于本库。MCP 不可用时，可运行 `UNIFIED_MEMORY_ROOT={shlex.quote(str(MEMORY_ROOT))} {shlex.quote(str(shim_path("umem")))} doctor` 检查接入状态，并按 `{MEMORY_ROOT}/MEMORY.md` 的 §0 手工维护正文与索引。

{WRITING_POLICY.rstrip()}
{MD_END}"""

AGY_RULE_CONTENT = f"""---
trigger: always_on
---

{MANAGED_MD_BODY}
"""


def initialize_library(target_root=None, shared=False):
    """Create missing directories and seed files while preserving existing data."""
    root = validate_library_path(target_root) if target_root is not None else MEMORY_ROOT
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    folders = ("constraints", "methods", "preferences", "projects", "archive")
    if not shared:
        folders = folders + ("user-managed",)
    for folder in folders:
        (root / folder).mkdir(exist_ok=True, mode=0o700)
    for relative, source in library_seed_files(shared=shared).items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=destination.parent) as staged:
            staged.write(source.read_bytes())
            staged.flush()
            try:
                os.link(staged.name, destination)
            except FileExistsError:
                pass
    return {"memory_root": str(root), "shared": shared}


def sha256_text(text):
    """Return hex SHA-256 of UTF-8 text."""
    return sha256(text.encode("utf-8")).hexdigest()


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


@contextmanager
def integration_lock():
    """Serialize config mutations using an advisory flock."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def parse_skill_frontmatter(skill_md_path):
    """Parse name and single-line description summary from SKILL.md frontmatter."""
    text = skill_md_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return skill_md_path.parent.name, ""
    header, sep, _ = text[4:].partition("\n---\n")
    if not sep:
        return skill_md_path.parent.name, ""

    name = skill_md_path.parent.name
    desc_lines = []
    in_desc = False
    for line in header.splitlines():
        if in_desc:
            if line.startswith((" ", "\t")):
                desc_lines.append(line.strip())
                continue
            in_desc = False
        key, colon, val = line.partition(":")
        if not colon:
            continue
        k = key.strip()
        v = val.strip()
        if k == "name" and v:
            name = v.strip("'\"")
        elif k == "description":
            if v in {"|", ">", "|-", ">-"} or not v:
                in_desc = True
            else:
                desc_lines.append(v.strip("'\""))
    desc = " ".join(desc_lines).strip()
    return name, desc


def load_disabled_skills(skills_dir=None):
    """Return set of disabled skill folder names."""
    sdir = skills_dir or SKILLS_DIR
    cfg = sdir / "disabled.json"
    if not cfg.is_file():
        return set()
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        return {str(x) for x in data.get("disabled", [])}
    except (json.JSONDecodeError, OSError):
        return set()


def save_disabled_skills(disabled_set, skills_dir=None):
    """Persist sorted disabled skill folder names."""
    sdir = skills_dir or SKILLS_DIR
    cfg = sdir / "disabled.json"
    payload = {"disabled": sorted(disabled_set)}
    atomic_write(cfg, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def skill_category(folder_name):
    """Classify a skill folder into a clean UI group."""
    if folder_name == "unified-memory":
        return "infra"
    return "general"


def discover_hub_skills(skills_dir=None):
    """Discover all skills with SKILL.md inside MEMORY/skills/."""
    sdir = skills_dir or SKILLS_DIR
    disabled = load_disabled_skills(sdir)
    items = []
    if not sdir.is_dir():
        return items
    for entry in sorted(sdir.iterdir()):
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        name, desc = parse_skill_frontmatter(skill_md)
        items.append(
            {
                "folder": entry.name,
                "name": name,
                "category": skill_category(entry.name),
                "enabled": entry.name not in disabled,
                "description": desc,
                "path": str(entry),
            }
        )
    return items


def resource_catalog(skills_dir):
    """Read optional, locally configured resource folders for each skill."""
    path = skills_dir / "resources.json"
    if not path.is_file():
        return {}
    catalog = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict):
        raise ValueError("skills/resources.json must map skill names to folder lists.")
    for owner, folders in catalog.items():
        if not isinstance(folders, list) or not all(
            isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)
            for name in [owner, *folders]
        ):
            raise ValueError("Invalid skill or resource folder in skills/resources.json.")
    return catalog


def desired_skill_entries(skills_dir=None):
    """Return (enabled_folders, disabled_folders)."""
    sdir = skills_dir or SKILLS_DIR
    hub = discover_hub_skills(sdir)
    enabled = [item["folder"] for item in hub if item["enabled"]]
    disabled = [item["folder"] for item in hub if not item["enabled"]]
    catalog = resource_catalog(sdir)
    skill_names = set(enabled + disabled)
    all_resources = {name for folders in catalog.values() for name in folders}
    active_resources = {name for owner in enabled for name in catalog.get(owner, [])}
    if all_resources & skill_names:
        raise ValueError("Resource folders must be separate from skill folders.")
    for name in sorted(all_resources):
        if name in active_resources:
            if not (sdir / name).is_dir():
                raise ValueError(f"Missing skill resource folder: {name}")
            enabled.append(name)
        else:
            disabled.append(name)
    return enabled, disabled


def extract_block(text, start_marker, end_marker):
    """Extract (before, block, after) if both markers exist in order."""
    s_idx = text.find(start_marker)
    if s_idx == -1:
        return None
    e_idx = text.find(end_marker, s_idx)
    if e_idx == -1:
        return None
    e_end = e_idx + len(end_marker)
    return text[:s_idx], text[s_idx:e_end], text[e_end:]


def strip_block(before, after):
    """Join remaining text cleanly after removing a managed block."""
    combined = before.rstrip("\n")
    tail = after.lstrip("\n")
    if combined and tail:
        return combined + "\n\n" + tail.rstrip("\n") + "\n"
    if combined:
        return combined + "\n"
    if tail:
        return tail.rstrip("\n") + "\n"
    return ""


def extract_managed_md(text, path):
    """Return (before, block, after) for exactly one ordered managed Markdown block.

    Unbalanced markers (an orphan start or end left behind by an editor or an
    interrupted write) would otherwise make the block span user text, so refuse.
    """
    starts, ends = text.count(MD_START), text.count(MD_END)
    if starts == 0 and ends == 0:
        return None
    if starts == 1 and ends == 1 and text.find(MD_START) < text.find(MD_END):
        return extract_block(text, MD_START, MD_END)
    raise RuntimeError(
        f"{path} 里的 Unified Memory 受管标记不成对（开始 {starts} 个，结束 {ends} 个）。"
        "请手工删掉多余的标记后重试，以免误删你的内容。"
    )


_TOML_KEY = r"""(?:unified_memory|"unified_memory"|'unified_memory')"""
TOML_OUR_TABLE_RE = re.compile(rf"^\s*\[\s*mcp_servers\s*\.\s*{_TOML_KEY}\s*(\.[^\]]*)?\]\s*(?:#.*)?$")
TOML_MCP_PARENT_RE = re.compile(r"^\s*\[\s*mcp_servers\s*\]\s*(?:#.*)?$")
TOML_TOP_DOTTED_RE = re.compile(rf"^\s*mcp_servers\s*\.\s*{_TOML_KEY}\s*[.=]")
TOML_PARENT_KEY_RE = re.compile(rf"^\s*{_TOML_KEY}\s*[.=]")
TOML_ASSIGN_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")


def without_toml_markers(text):
    """Drop legacy wrapper comments; they are cosmetic and unstable under Codex rewrites."""
    lines = text.splitlines(keepends=True)
    kept = [line for line in lines if line.strip() not in (TOML_START, TOML_END)]
    return "".join(kept)


def locate_toml_mcp(text, path="config.toml"):
    """Find our own [mcp_servers.unified_memory] table and its subtables by line range.

    Only these tables belong to Unified Memory. Anything else, including tables
    Codex inserts next to ours, is left alone.
    """
    lines = text.splitlines(keepends=True)
    main, subs = None, []
    current, kind = None, "top"

    def close(idx):
        start, _ = current
        end = idx
        while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
            end -= 1
        return start, end

    for idx, raw in enumerate(lines):
        if raw.lstrip().startswith("["):
            if current is not None:
                rng = close(idx)
                if current[1] == "main":
                    main = rng
                else:
                    subs.append(rng)
                current = None
            m = TOML_OUR_TABLE_RE.match(raw)
            if m:
                kind = "sub" if m.group(1) else "main"
                if kind == "main" and main is not None:
                    raise RuntimeError(f"{path} 里有多个 {TOML_SECTION_HEADER} 表，请先手工合并成一个再重试。")
                current = (idx, kind)
            elif TOML_MCP_PARENT_RE.match(raw):
                kind = "parent"
            else:
                kind = "other"
            continue
        if (kind == "top" and TOML_TOP_DOTTED_RE.match(raw)) or (kind == "parent" and TOML_PARENT_KEY_RE.match(raw)):
            raise RuntimeError(
                f"{path} 用行内表或点号键写了 unified_memory，Unified Memory 不会改写这种写法。"
                f"请改成独立的 {TOML_SECTION_HEADER} 表或删掉它后重试。"
            )
    if current is not None:
        rng = close(len(lines))
        if current[1] == "main":
            main = rng
        else:
            subs.append(rng)
    if main is None and not subs:
        return None
    return {"lines": lines, "main": main, "subs": subs}


def toml_range_text(loc, rng):
    return "".join(line if line.endswith("\n") else line + "\n" for line in loc["lines"][rng[0]:rng[1]])


def remove_toml_ranges(loc, ranges):
    """Remove line ranges, joining the remaining text the same way strip_block does."""
    text = "".join(loc["lines"])
    lines = loc["lines"]
    for start, end in sorted(ranges, reverse=True):
        before = "".join(lines[:start])
        after = "".join(lines[end:])
        text = strip_block(before, after)
        lines = text.splitlines(keepends=True)
    return text


def replace_toml_main(loc, new_main):
    """Replace only our main table, keeping its unknown keys and every other table."""
    start, end = loc["main"]
    lines = loc["lines"]
    before = "".join(lines[:start])
    after = "".join(lines[end:])
    if before and not before.endswith("\n"):
        before += "\n"
    tail = after if not after or after.startswith("\n") else "\n" + after
    return before + new_main.rstrip("\n") + "\n" + tail


def updated_toml_main(main_text, command, args):
    """Rewrite command/args in our table while keeping other keys (env, timeouts...)."""
    body = main_text.splitlines()[1:]
    extra = []
    for line in body:
        key = TOML_ASSIGN_KEY_RE.match(line)
        if line.strip() in (TOML_START, TOML_END) or (key and key.group(1) in ("command", "args")):
            continue
        extra.append(line)
    while extra and not extra[-1].strip():
        extra.pop()
    head = desired_toml_block(command, args)
    return head + ("\n" + "\n".join(extra) if extra else "")


def validate_toml_change(old_text, new_text, path):
    """Refuse to write a config Codex could no longer parse (when tomllib is available)."""
    try:
        import tomllib
    except ImportError:
        return
    try:
        tomllib.loads(old_text)
    except tomllib.TOMLDecodeError:
        return
    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"改写后 {path} 将无法解析（{exc}），已停止修改。") from exc


def parse_toml_mcp_section(section_text):
    """Parse command and args from a simple [mcp_servers.unified_memory] TOML section."""
    cmd = None
    args = None
    for raw in section_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        key, sep, val = line.partition("=")
        if not sep:
            continue
        k = key.strip()
        v = val.strip()
        try:
            parsed_val = json.loads(v)
        except json.JSONDecodeError:
            parsed_val = v.strip("'\"")
        if k == "command":
            cmd = parsed_val
        elif k == "args":
            args = parsed_val
    return cmd, args


def restore_legacy_codex_memories(toml_text, restore):
    """Undo the exact setting older versions wrote, if it is still in place."""
    previous = restore.get("previous_codex_features_memories")
    if previous not in {"true", "absent"}:
        return toml_text
    lines = toml_text.splitlines(keepends=True)
    in_features = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith("["):
            in_features = line.split("#", 1)[0].strip() == "[features]"
        elif in_features and line.rstrip("\r\n") == "memories = false":
            if previous == "true":
                lines[index] = line.replace("false", "true")
            else:
                del lines[index]
            return "".join(lines)
    return toml_text


def scan_stray_agent_memories(home=None):
    """Find any stray .md memory files written into hidden per-client default directories."""
    home = home or Path.home()
    stray = []
    claude_projects = home / ".claude" / "projects"
    if claude_projects.is_dir():
        for mem_dir in sorted(claude_projects.glob("*/memory")):
            if not mem_dir.is_dir() or mem_dir.is_symlink():
                continue
            for md in sorted(mem_dir.glob("*.md")):
                if md.name == "MEMORY.md" and md.stat().st_size == 0:
                    continue
                stray.append(str(md))
    return stray


def _claude_desktop_config_path(home, *, use_env=True):
    """Resolve Claude Desktop's claude_desktop_config.json (standard or 3p build)."""
    if use_env:
        override = os.environ.get("CLAUDE_DESKTOP_CONFIG")
        if override:
            return Path(override).expanduser()
    if sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        standard_dir = support / "Claude"
        third_party_dir = support / "Claude-3p"
        standard_cfg = standard_dir / "claude_desktop_config.json"
        third_party_cfg = third_party_dir / "claude_desktop_config.json"
        if third_party_cfg.is_file() and not standard_cfg.is_file():
            return third_party_cfg
        if standard_cfg.is_file() and not third_party_cfg.is_file():
            return standard_cfg
        if standard_cfg.is_file() and third_party_cfg.is_file():
            try:
                if third_party_cfg.stat().st_mtime > standard_cfg.stat().st_mtime:
                    return third_party_cfg
            except OSError:
                pass
            return standard_cfg
        if third_party_dir.is_dir() and not standard_dir.is_dir():
            return third_party_cfg
        if standard_dir.is_dir() and third_party_dir.is_dir():
            try:
                if third_party_dir.stat().st_mtime > standard_dir.stat().st_mtime:
                    return third_party_cfg
            except OSError:
                pass
        return standard_cfg
    xdg_env = os.environ.get("XDG_CONFIG_HOME") if use_env else None
    xdg_config = Path(xdg_env or home / ".config").expanduser()
    return xdg_config / "Claude" / "claude_desktop_config.json"


def _agent_mcp_path(agent_id, spec, restore):
    """Keep Claude Desktop bound to the connected config path unless overridden via env."""
    if agent_id == "claude-desktop" and not spec.get("mcp_path_override"):
        saved = restore.get("mcp_path")
        if saved:
            return Path(saved)
    return spec["mcp_path"]


def agent_specs(home=None):
    """Return per-agent paths and detection rules."""
    use_env = home is None
    home = home or Path.home()
    dsh_home = Path(os.environ.get("DSH_HOME") or home / ".dsh").expanduser()
    claude_desktop_mcp = _claude_desktop_config_path(home, use_env=use_env)
    claude_desktop_detect = [claude_desktop_mcp.parent]
    if sys.platform == "darwin":
        claude_desktop_detect.extend([
            home / "Library" / "Application Support" / "Claude",
            home / "Library" / "Application Support" / "Claude-3p",
            home / "Applications" / "Claude.app",
            Path("/Applications/Claude.app"),
        ])
    return {
        "claude": {
            "name": "Claude Code",
            "mode": "files",
            "detect_paths": [home / ".claude", home / ".local" / "bin" / "claude"],
            "instructions": home / ".claude" / "CLAUDE.md",
            "mcp_type": "json",
            "mcp_path": home / ".claude.json",
            "mcp_key": "unified-memory",
            "skills_dir": home / ".claude" / "skills",
            "hooks_path": home / ".claude" / "settings.json",
            "hooks_style": "nested",
        },
        "claude-desktop": {
            "name": "Claude Desktop",
            "mode": "files",
            "detect_paths": claude_desktop_detect,
            "mcp_type": "json",
            "mcp_path": claude_desktop_mcp,
            "mcp_path_override": bool(use_env and os.environ.get("CLAUDE_DESKTOP_CONFIG")),
            "mcp_env": {"UNIFIED_MEMORY_CLIENT": "Claude Desktop"},
            "mcp_key": "unified-memory",
        },
        "codex": {
            "name": "Codex",
            "mode": "files",
            "detect_paths": [home / ".codex", home / ".local" / "bin" / "codex"],
            "instructions": home / ".codex" / "AGENTS.md",
            "mcp_type": "toml",
            "mcp_path": home / ".codex" / "config.toml",
            "mcp_key": "unified_memory",
            "skills_dir": home / ".codex" / "skills",
            "hooks_path": home / ".codex" / "hooks.json",
            "hooks_style": "nested",
        },
        "cursor": {
            "name": "Cursor",
            "mode": "files",
            "detect_paths": [home / ".cursor", home / ".local" / "bin" / "cursor-agent"],
            "mcp_type": "json",
            "mcp_path": home / ".cursor" / "mcp.json",
            "mcp_key": "unified_memory",
            "skills_dir": home / ".cursor" / "skills",
            "hooks_path": home / ".cursor" / "hooks.json",
            "hooks_style": "cursor",
        },
        "workbuddy": {
            "name": "WorkBuddy",
            "mode": "files",
            "detect_paths": [home / ".workbuddy", home / "Applications/WorkBuddy.app", Path("/Applications/WorkBuddy.app")],
            "mcp_type": "json",
            "mcp_path": home / ".workbuddy" / "mcp.json",
            "mcp_env": {"UNIFIED_MEMORY_CLIENT": "WorkBuddy"},
            "mcp_key": "unified_memory",
        },
        "antigravity": {
            "name": "Antigravity",
            "mode": "agy_plugin",
            "detect_paths": [home / ".gemini" / "antigravity", home / ".gemini" / "config"],
            "plugin_dir": home / ".gemini" / "config" / "plugins" / "unified-memory",
            "skills_dir": home / ".gemini" / "config" / "plugins" / "unified-memory" / "skills",
            "legacy_gemini_md": home / ".gemini" / "GEMINI.md",
            "legacy_mcp_json": home / ".gemini" / "config" / "mcp_config.json",
            "legacy_skill_link": home / ".gemini" / "config" / "skills" / "unified-memory",
        },
        "deepseek": {
            "name": "DeepSeek Harness",
            "mode": "dsh_patch",
            "detect_paths": [dsh_home],
            "mcp_path": dsh_home / "cordis.patch.yml",
        },
    }


def load_restore(agent_id):
    """Load saved pre-integration metadata for an agent (with legacy .state fallback)."""
    path = RESTORE_DIR / f"{agent_id}.json"
    if not path.is_file():
        legacy = LEGACY_STATE_DIR / "restore" / f"{agent_id}.json"
        if legacy.is_file():
            return json.loads(legacy.read_text(encoding="utf-8"))
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_restore(agent_id, payload):
    """Persist pre-integration metadata for clean disconnect."""
    RESTORE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write(RESTORE_DIR / f"{agent_id}.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def desired_mcp_args():
    """Return (command, args) for the MCP server: always the stable launcher, never a bundle path."""
    return str(shim_path("umem-mcp")), ["--memory-root", str(MEMORY_ROOT)]


DSH_START = "# unified-memory:managed:start"
DSH_END = "# unified-memory:managed:end"
DSH_EMPTY = "# unified-memory:original-empty-sequence "
DSH_BLOCK = re.compile(
    rf"^(?P<indent> *){DSH_START}\r?\n(?:.*\n)*?^(?P=indent){DSH_END}(?:\r?\n|$)", re.MULTILINE
)
DSH_OTHER_SERVER = re.compile(r"\b(?:id|serverName)['\"]?\s*:\s*['\"]?(?:umem-unified-memory|unified_memory)(?=['\"\s,#}\]]|$)")


def dsh_mcp_block(indent, newline, empty_sequence=""):
    command, args = desired_mcp_args()
    lines = [DSH_START]
    if empty_sequence:
        lines.append(DSH_EMPTY + json.dumps(empty_sequence, ensure_ascii=False))
    lines.extend([
        "- insert:",
        "    - id: umem-unified-memory",
        "      name: '@deepseek-ai/dsh-mcp-client'",
        "      config:",
        "        serverName: unified_memory",
        "        transport: stdio",
        f"        command: {json.dumps(command, ensure_ascii=False)}",
        f"        args: {json.dumps(args, ensure_ascii=False)}",
        "        env:",
        "          UNIFIED_MEMORY_CLIENT: DeepSeek Harness",
        DSH_END,
    ])
    return "".join(indent + line + newline for line in lines)


def read_dsh_patch(path):
    # Preserve the BOM and newline style when editing just our block.
    raw = path.read_bytes().decode("utf-8") if path.exists() else ""
    bom = "\ufeff" if raw.startswith("\ufeff") else ""
    text = raw[len(bom):]
    block = DSH_BLOCK.search(text)
    if text.count(DSH_START) != int(block is not None) or text.count(DSH_END) != int(block is not None):
        raise ValueError(f"DeepSeek Harness 的 Unified Memory 配置块不完整，请核对 {path}")
    empty_sequence = ""
    if block:
        second_line = block[0].splitlines()[1].lstrip()
        if second_line.startswith(DSH_EMPTY):
            empty_sequence = json.loads(second_line[len(DSH_EMPTY):])
            if not isinstance(empty_sequence, str) or not re.fullmatch(r" *\[ *\](?:[ \t]+#[^\r\n]*|[ \t]*)(?:\r?\n)?", empty_sequence):
                raise ValueError(f"DeepSeek Harness 的空配置记录格式无效，请核对 {path}")
    return bom, text, block, empty_sequence


def dsh_patch_root(text):
    """Locate the top-level row without interpreting Cordis tags or reformatting YAML."""
    offset = 0
    header_end = 0
    for line in text.splitlines(keepends=True):
        clean = line.strip()
        if re.fullmatch(r"---(?:\s+#.*)?", clean):
            header_end = offset + len(line)
        elif clean and not clean.startswith(("#", "%")):
            return offset, line
        offset += len(line)
    return header_end, ""


def edit_dsh_patch(path, connect, force=False, created=False):
    bom, text, block, empty_sequence = read_dsh_patch(path)
    newline = "\r\n" if "\r\n" in text else "\n"
    if block:
        before, after = text[:block.start()], text[block.end():]
        desired = dsh_mcp_block(block["indent"], newline, empty_sequence)
        if not connect and block[0] != desired and not force:
            raise RuntimeError(f"DeepSeek Harness 的托管配置已被编辑，请核对 {path}")
        if connect:
            updated = before + desired + after
        else:
            _, remaining = dsh_patch_root(before + after)
            restored = empty_sequence if not remaining or remaining.strip() == "..." else ""
            if empty_sequence and not restored and "#" in empty_sequence:
                restored = "#" + empty_sequence.split("#", 1)[1].rstrip("\r\n") + newline
            updated = before + restored + after
    elif connect:
        if DSH_OTHER_SERVER.search(text):
            raise RuntimeError(f"DeepSeek Harness 已有同名服务，请先核对 {path}")
        offset, line = dsh_patch_root(text)
        indent = " " * (len(line) - len(line.lstrip(" ")))
        if re.fullmatch(r"\[ *\](?:[ \t]+#.*|[ \t]*)", line.strip()):
            empty_sequence = line
        elif line and not re.match(r"^ *(?:-(?:[ \t]|\r?\n|$)|\.\.\.(?:\s|$))", line):
            raise ValueError(f"DeepSeek Harness 配置需要块状 YAML 列表，请核对 {path}")
        if offset and text[offset - 1] != "\n":
            raise ValueError(f"请在 DeepSeek Harness 配置的文档起始行后换行，再重试接入。{path}")
        updated = text[:offset] + dsh_mcp_block(indent, newline, empty_sequence) + text[offset + len(empty_sequence):]
    else:
        return False

    if updated == text:
        return False
    if not updated and not bom and created:
        path.unlink()
    else:
        atomic_write(path.resolve(), bom + updated)
    return True


def inspect_dsh_patch(path):
    _, text, block, empty_sequence = read_dsh_patch(path)
    if block:
        expected = dsh_mcp_block(block["indent"], "\r\n" if "\r\n" in text else "\n", empty_sequence)
        return "managed" if block[0] == expected else "drifted"
    return "unmanaged"


def is_our_mcp_entry(command, args):
    """True for current launcher entries and legacy direct python+server.py entries."""
    return command == str(shim_path("umem-mcp")) or str(MCP_SERVER) in " ".join(args or [])


def read_json_mcp(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"MCP 配置格式无效，请检查 {path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise ValueError(f"MCP 配置及 mcpServers 必须是 JSON 对象，请检查 {path}")
    return data


def desired_json_mcp(spec):
    """Build expected mcpServers entry for JSON-configured agents."""
    py_cmd, args = desired_mcp_args()
    entry = {"command": py_cmd, "args": args}
    if spec["mcp_path"].name == ".claude.json":
        entry = {"type": "stdio", **entry, "env": {}}
    elif "mcp_env" in spec:
        entry["env"] = spec["mcp_env"]
    return entry


def desired_toml_block(command=None, args=None):
    """Build Codex's [mcp_servers.unified_memory] table.

    No wrapper comments: Codex inserts its own tables before a trailing comment
    and may drop the leading one, so comment markers cannot delimit ownership.
    """
    if command is None:
        command, args = desired_mcp_args()
    args_json = json.dumps(args, ensure_ascii=False)
    return f"{TOML_SECTION_HEADER}\ncommand = {json.dumps(command)}\nargs = {args_json}"


def desired_agy_plugin_files():
    """Return expected core files inside ~/.gemini/config/plugins/unified-memory/."""
    py_cmd, args = desired_mcp_args()
    plugin_json = json.dumps(
        {
            "name": "unified-memory",
            "displayName": "Unified Memory & Skills",
            "version": "1.2.0",
            "description": "Shared L0-L4 local memory library, MCP server, and unified skills hub across Claude Code, Codex, and Antigravity.",
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    mcp_json = json.dumps(
        {"mcpServers": {"unified-memory": {"command": py_cmd, "args": args}}},
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    return {
        "plugin.json": plugin_json,
        "mcp_config.json": mcp_json,
        "rules/unified-memory.md": AGY_RULE_CONTENT,
    }


def is_linked_to_hub(target_path, hub_source):
    """Check whether target_path is a symlink pointing to hub_source."""
    if not target_path.is_symlink():
        return False
    try:
        return os.path.realpath(target_path) == os.path.realpath(hub_source)
    except OSError:
        return False


def inspect_skills_dir(target_skills_dir):
    """Inspect how well target_skills_dir matches enabled/disabled hub skills."""
    enabled_folders, disabled_folders = desired_skill_entries()
    if not target_skills_dir.exists():
        return "unmanaged", 0, len(enabled_folders), []

    linked_ok = 0
    unlinked = []
    stale_disabled = []
    for slug in enabled_folders:
        src = SKILLS_DIR / slug
        dest = target_skills_dir / slug
        if is_linked_to_hub(dest, src):
            linked_ok += 1
        else:
            unlinked.append(slug)

    for slug in disabled_folders:
        src = SKILLS_DIR / slug
        dest = target_skills_dir / slug
        if is_linked_to_hub(dest, src):
            stale_disabled.append(slug)

    issues = []
    if linked_ok == len(enabled_folders) and not stale_disabled:
        return "managed", linked_ok, len(enabled_folders), issues
    if linked_ok == 0 and not stale_disabled:
        return "unmanaged", 0, len(enabled_folders), issues
    if unlinked:
        preview = ", ".join(unlinked[:4]) + ("…" if len(unlinked) > 4 else "")
        issues.append(f"待同步技能 ({len(unlinked)}): {preview}")
    if stale_disabled:
        issues.append(f"已停用技能仍挂载: {', '.join(stale_disabled)}")
    return "drifted", linked_ok, len(enabled_folders), issues


def sync_skills_into_dir(agent_id, target_skills_dir, restore, changes):
    """Symlink all enabled hub skills into target_skills_dir, backing up standalone copies."""
    enabled_folders, disabled_folders = desired_skill_entries()
    target_skills_dir.mkdir(parents=True, exist_ok=True)
    backed_up = set(restore.get("backed_up_skills", []))
    backup_root = SKILL_BACKUP_DIR / agent_id

    for slug in enabled_folders:
        src = SKILLS_DIR / slug
        dest = target_skills_dir / slug
        if is_linked_to_hub(dest, src):
            continue
        if dest.is_symlink():
            # Repoint existing symlink
            prev_links = restore.setdefault("previous_skill_symlinks", {})
            if slug not in prev_links:
                prev_links[slug] = os.readlink(dest)
            dest.unlink()
        elif dest.exists():
            # Move standalone folder/file safely to .state/skill-backups/<agent>/<slug>
            backup_dest = backup_root / slug
            backup_dest.parent.mkdir(parents=True, exist_ok=True)
            if backup_dest.exists() or backup_dest.is_symlink():
                if backup_dest.is_dir() and not backup_dest.is_symlink():
                    shutil.rmtree(backup_dest)
                else:
                    backup_dest.unlink()
            shutil.move(str(dest), str(backup_dest))
            backed_up.add(slug)
            changes.append(f"backed up standalone skill {dest} -> {backup_dest}")
        dest.symlink_to(src)
        changes.append(f"linked skill {dest} -> {src}")

    for slug in disabled_folders:
        src = SKILLS_DIR / slug
        dest = target_skills_dir / slug
        if is_linked_to_hub(dest, src):
            dest.unlink()
            changes.append(f"unlinked disabled skill {dest}")

    restore["backed_up_skills"] = sorted(backed_up)


def restore_skills_dir(agent_id, target_skills_dir, restore, changes):
    """Remove hub symlinks and restore any backed-up standalone skills."""
    enabled, disabled = desired_skill_entries()
    all_hub_names = enabled + disabled
    if target_skills_dir.is_dir():
        for slug in all_hub_names:
            src = SKILLS_DIR / slug
            dest = target_skills_dir / slug
            if is_linked_to_hub(dest, src):
                dest.unlink()
                changes.append(f"unlinked skill {dest}")

    # Restore previous symlinks if any
    for slug, old_target in (restore.get("previous_skill_symlinks") or {}).items():
        dest = target_skills_dir / slug
        if not dest.exists() and not dest.is_symlink():
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(old_target, dest)
            changes.append(f"restored previous symlink {dest} -> {old_target}")

    # Restore backed-up standalone directories
    backup_root = SKILL_BACKUP_DIR / agent_id
    for slug in restore.get("backed_up_skills") or []:
        bpath = backup_root / slug
        dest = target_skills_dir / slug
        if (bpath.exists() or bpath.is_symlink()) and not dest.exists() and not dest.is_symlink():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(bpath), str(dest))
            changes.append(f"restored standalone skill {dest}")
    if backup_root.is_dir() and not any(backup_root.iterdir()):
        backup_root.rmdir()


def inspect_agy_plugin(agent_id, spec):
    """Inspect Antigravity plugin bundle state."""
    detected = any(p.exists() for p in spec["detect_paths"])
    restore = load_restore(agent_id)
    pdir = spec["plugin_dir"]
    expected = desired_agy_plugin_files()
    issues = []

    rule_path = pdir / "rules" / "unified-memory.md"
    mcp_path = pdir / "mcp_config.json"
    manifest_path = pdir / "plugin.json"

    if not pdir.exists():
        _, total = desired_skill_entries()
        return {
            "id": agent_id,
            "name": spec["name"],
            "detected": detected,
            "state": "unmanaged",
            "components": {
                "instructions": {"path": str(rule_path), "state": "unmanaged"},
                "mcp": {"path": str(mcp_path), "state": "unmanaged"},
                "skill": {"path": str(spec["skills_dir"]), "state": "unmanaged", "linked": 0, "total": len(desired_skill_entries()[0])},
            },
            "issues": issues,
        }

    if not rule_path.is_file():
        inst_state = "missing"
        issues.append("plugin rules/unified-memory.md missing")
    else:
        rtext = rule_path.read_text(encoding="utf-8")
        if rtext == expected["rules/unified-memory.md"]:
            inst_state = "managed"
        else:
            saved_hash = restore.get("agy_rule_hash")
            if saved_hash and sha256_text(rtext) != saved_hash:
                inst_state = "modified"
                issues.append("plugin rules/unified-memory.md was manually edited")
            else:
                inst_state = "drifted"
                issues.append("plugin rules/unified-memory.md differs from current template")

    if not mcp_path.is_file() or not manifest_path.is_file():
        mcp_state = "missing"
        issues.append("plugin.json or mcp_config.json missing")
    elif (
        mcp_path.read_text(encoding="utf-8") == expected["mcp_config.json"]
        and manifest_path.read_text(encoding="utf-8") == expected["plugin.json"]
    ):
        mcp_state = "managed"
    else:
        mcp_state = "drifted"
        issues.append("plugin mcp_config.json or plugin.json drifted")

    skill_state, linked_cnt, total_cnt, skill_issues = inspect_skills_dir(spec["skills_dir"])
    issues.extend(skill_issues)

    states = {inst_state, mcp_state, skill_state}
    if states == {"managed"}:
        overall = "managed"
    elif states & {"modified", "drifted"}:
        overall = "drifted"
    else:
        overall = "partial"

    return {
        "id": agent_id,
        "name": spec["name"],
        "detected": detected,
        "state": overall,
        "components": {
            "instructions": {"path": str(rule_path), "state": inst_state},
            "mcp": {"path": str(mcp_path), "state": mcp_state},
            "skill": {
                "path": str(spec["skills_dir"]),
                "state": skill_state,
                "linked": linked_cnt,
                "total": total_cnt,
            },
        },
        "issues": issues,
    }


def desired_cursor_hooks():
    """Return {event: hook-entry} that wire Cursor into the memory read/write contract."""
    entries = {}
    for event in CURSOR_HOOK_EVENTS:
        command = " ".join(
            shlex.quote(part)
            for part in [str(shim_path("umem-hook")), event, "--memory-root", str(MEMORY_ROOT)]
        )
        entry = {"command": command, "timeout": 10}
        if event == "stop":
            entry["loop_limit"] = 1
        entries[event] = entry
    return entries


def desired_nested_hooks(agent_id):
    """Return {event: command-hook-entry} for Claude Code / Codex nested hook configs."""
    entries = {}
    for event in NESTED_HOOK_EVENTS:
        command = " ".join(
            shlex.quote(part)
            for part in [
                str(shim_path("umem-hook")),
                event,
                "--client",
                agent_id,
                "--memory-root",
                str(MEMORY_ROOT),
            ]
        )
        entries[event] = {"type": "command", "command": command, "timeout": 10}
    return entries


def _is_our_hook(entry):
    command = str(entry.get("command", "")) if isinstance(entry, dict) else ""
    return any(marker in command for marker in CURSOR_HOOK_MARKERS)


def _strip_our_nested_hooks(groups):
    """Remove our managed hook entries from nested matcher groups, preserving unrelated hooks."""
    kept_groups = []
    removed = False
    for group in groups or []:
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            orig = group["hooks"]
            kept = [h for h in orig if not _is_our_hook(h)]
            if len(kept) != len(orig):
                removed = True
                if not kept and set(group.keys()) == {"hooks"}:
                    continue
                updated = dict(group)
                updated["hooks"] = kept
                kept_groups.append(updated)
            else:
                kept_groups.append(group)
        else:
            kept_groups.append(group)
    return kept_groups, removed


def inspect_cursor_hooks(hooks_path):
    """Return (state, issues) for Cursor user hooks: managed|drifted|missing."""
    if not hooks_path.is_file():
        return "missing", []
    try:
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
    except ValueError:
        return "drifted", [f"{hooks_path} is not valid JSON"]
    hooks = data.get("hooks") or {}
    desired = desired_cursor_hooks()
    found, exact = 0, 0
    for event, want in desired.items():
        ours = [e for e in (hooks.get(event) or []) if _is_our_hook(e)]
        if ours:
            found += 1
            if len(ours) == 1 and ours[0] == want:
                exact += 1
    if found == 0:
        return "missing", []
    if exact == len(desired):
        return "managed", []
    return "drifted", ["Cursor hooks differ from current script path/args (re-sync to fix)"]


def inspect_nested_hooks(agent_id, spec):
    """Return (state, issues) for Claude Code / Codex hooks: managed|drifted|missing."""
    hooks_path = spec["hooks_path"]
    if not hooks_path.is_file():
        return "missing", []
    try:
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
    except ValueError:
        return "drifted", [f"{hooks_path} is not valid JSON"]
    if not isinstance(data, dict):
        return "drifted", [f"{hooks_path} root must be a JSON object"]
    hooks = data.get("hooks")
    if hooks is None:
        return "missing", []
    if not isinstance(hooks, dict):
        return "drifted", [f"{hooks_path} hooks must be a JSON object"]
    desired = desired_nested_hooks(agent_id)
    found, exact = 0, 0
    for event, want in desired.items():
        ours = []
        for group in hooks.get(event) or []:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                ours.extend(h for h in group["hooks"] if _is_our_hook(h))
        if ours:
            found += 1
            if len(ours) == 1 and ours[0] == want:
                exact += 1
    if found == 0:
        return "missing", []
    if exact == len(desired):
        return "managed", []
    return "drifted", [f"{spec['name']} hooks differ from current script path/args (re-sync to fix)"]


def inspect_agent_hooks(agent_id, spec):
    if spec.get("hooks_style") == "nested":
        return inspect_nested_hooks(agent_id, spec)
    return inspect_cursor_hooks(spec["hooks_path"])


def apply_cursor_hooks(hooks_path, restore, changes):
    """Merge managed hook entries into ~/.cursor/hooks.json, preserving unrelated hooks."""
    existed = hooks_path.is_file()
    data = json.loads(hooks_path.read_text(encoding="utf-8")) if existed else {}
    if "cursor_hooks_file_existed" not in restore:
        restore["cursor_hooks_file_existed"] = existed
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    original = json.dumps(data, sort_keys=True)
    for event, want in desired_cursor_hooks().items():
        kept = [e for e in (hooks.get(event) or []) if not _is_our_hook(e)]
        hooks[event] = kept + [want]
    if json.dumps(data, sort_keys=True) != original or not existed:
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(hooks_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        changes.append(f"wired Cursor hooks in {hooks_path}")


def apply_nested_hooks(agent_id, spec, restore, changes):
    """Merge managed hook entries into Claude Code / Codex JSON hooks, preserving unrelated config."""
    hooks_path = spec["hooks_path"]
    existed = hooks_path.is_file()
    data = json.loads(hooks_path.read_text(encoding="utf-8")) if existed else {}
    if not isinstance(data, dict):
        raise RuntimeError(f"{hooks_path} must be a JSON object")
    if "hooks_file_existed" not in restore:
        restore["hooks_file_existed"] = existed
    if "hooks_key_existed" not in restore:
        restore["hooks_key_existed"] = "hooks" in data
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError(f"{hooks_path} hooks field must be a JSON object")
    original = json.dumps(data, sort_keys=True)
    for event, want in desired_nested_hooks(agent_id).items():
        kept, _ = _strip_our_nested_hooks(hooks.get(event) or [])
        hooks[event] = kept + [{"hooks": [want]}]
    if json.dumps(data, sort_keys=True) != original or not existed:
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(hooks_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        changes.append(f"wired {spec['name']} hooks in {hooks_path}")


def apply_agent_hooks(agent_id, spec, restore, changes):
    if spec.get("hooks_style") == "nested":
        apply_nested_hooks(agent_id, spec, restore, changes)
    else:
        apply_cursor_hooks(spec["hooks_path"], restore, changes)


def remove_cursor_hooks(hooks_path, restore, changes):
    """Remove only managed hook entries; delete the file if we created it and nothing else is left."""
    if not hooks_path.is_file():
        return
    try:
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
    except ValueError:
        return
    hooks = data.get("hooks") or {}
    touched = False
    for event in list(hooks):
        kept = [e for e in (hooks.get(event) or []) if not _is_our_hook(e)]
        if len(kept) != len(hooks.get(event) or []):
            touched = True
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not touched:
        return
    if not hooks and not restore.get("cursor_hooks_file_existed", True):
        hooks_path.unlink()
        changes.append(f"removed {hooks_path}")
    else:
        data["hooks"] = hooks
        atomic_write(hooks_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        changes.append(f"removed managed Cursor hooks from {hooks_path}")


def remove_nested_hooks(agent_id, spec, restore, changes):
    """Remove only managed nested hook entries while preserving unrelated hooks and settings."""
    hooks_path = spec["hooks_path"]
    if not hooks_path.is_file():
        return
    try:
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
    except ValueError:
        return
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        return
    hooks = data["hooks"]
    touched = False
    for event in list(hooks):
        kept, removed = _strip_our_nested_hooks(hooks.get(event) or [])
        if removed:
            touched = True
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    if not touched:
        return
    if not hooks and restore.get("hooks_key_existed") is False:
        data.pop("hooks", None)
    if not data and not restore.get("hooks_file_existed", True):
        hooks_path.unlink()
        changes.append(f"removed {hooks_path}")
    else:
        atomic_write(hooks_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        changes.append(f"removed managed {spec['name']} hooks from {hooks_path}")


def remove_agent_hooks(agent_id, spec, restore, changes):
    if spec.get("hooks_style") == "nested":
        remove_nested_hooks(agent_id, spec, restore, changes)
    else:
        remove_cursor_hooks(spec["hooks_path"], restore, changes)


def inspect_agent(agent_id, spec):
    """Inspect detection and integration state without modifying anything."""
    if spec.get("mode") == "agy_plugin":
        return inspect_agy_plugin(agent_id, spec)

    detected = any(p.exists() for p in spec["detect_paths"])
    restore = load_restore(agent_id)
    issues = []
    inst_path = spec.get("instructions")
    if inst_path is None:
        inst_state = None
    elif inst_path.is_symlink():
        inst_state = "symlink"
        issues.append(f"instructions is a symlink ({inst_path})")
    elif inst_path.is_file():
        text = inst_path.read_text(encoding="utf-8")
        try:
            parsed = extract_managed_md(text, inst_path)
        except RuntimeError as exc:
            parsed = False
            inst_state = "drifted"
            issues.append(str(exc))
        if parsed is False:
            pass
        elif parsed is None:
            inst_state = "unmanaged"
        else:
            block = parsed[1]
            expected_hash = sha256_text(MANAGED_MD_BODY)
            saved_hash = restore.get("instructions_block_hash")
            if sha256_text(block) == expected_hash:
                inst_state = "managed"
            elif saved_hash and sha256_text(block) != saved_hash:
                inst_state = "modified"
                issues.append("managed instruction block was manually edited")
            else:
                inst_state = "drifted"
                issues.append("managed instruction block drifted or conflicts with legacy block")
    else:
        inst_state = "missing"

    mcp_path = _agent_mcp_path(agent_id, spec, restore)
    exp_cmd, exp_args = desired_mcp_args()
    if spec.get("mode") == "dsh_patch":
        try:
            mcp_state = inspect_dsh_patch(mcp_path)
        except (ValueError, OSError) as exc:
            issues.append(str(exc))
            mcp_state = "modified"
    elif spec["mcp_type"] == "json":
        if mcp_path.is_file():
            try:
                current = read_json_mcp(mcp_path).get("mcpServers", {}).get(spec["mcp_key"])
            except (ValueError, OSError) as exc:
                issues.append(str(exc))
                mcp_state = "modified"
            else:
                if current is None:
                    mcp_state = "unmanaged"
                elif current == desired_json_mcp(spec):
                    mcp_state = "managed"
                else:
                    mcp_state = "drifted"
                    issues.append("MCP entry differs from current server path/args")
        else:
            mcp_state = "missing"
    else:
        if mcp_path.is_file():
            text = mcp_path.read_text(encoding="utf-8")
            try:
                loc = locate_toml_mcp(without_toml_markers(text), mcp_path)
            except RuntimeError as exc:
                loc = {"main": None, "error": str(exc)}
            if loc is None or (loc["main"] is None and "error" not in loc):
                mcp_state = "unmanaged"
            elif "error" in loc:
                mcp_state = "drifted"
                issues.append(loc["error"])
            else:
                cmd, args = parse_toml_mcp_section(toml_range_text(loc, loc["main"]))
                if cmd == exp_cmd and args == exp_args:
                    mcp_state = "managed"
                else:
                    mcp_state = "drifted"
                    if cmd != exp_cmd or args != exp_args:
                        issues.append("Codex [mcp_servers.unified_memory] drifted")
                if restore_legacy_codex_memories(text, restore) != text:
                    mcp_state = "drifted"
                    issues.append("此版本可恢复此前接入时被关闭的 Codex 记忆设置，请点「同步」。")
        else:
            mcp_state = "missing"

    components = {"mcp": {"path": str(mcp_path), "state": mcp_state}}
    if inst_path is not None:
        components["instructions"] = {"path": str(inst_path), "state": inst_state}
    if "skills_dir" in spec:
        skill_state, linked, total, skill_issues = inspect_skills_dir(spec["skills_dir"])
        issues.extend(skill_issues)
        components["skill"] = {"path": str(spec["skills_dir"]), "state": skill_state, "linked": linked, "total": total}
    if "hooks_path" in spec:
        hooks_state, hook_issues = inspect_agent_hooks(agent_id, spec)
        issues.extend(hook_issues)
        components["hooks"] = {"path": str(spec["hooks_path"]), "state": hooks_state}

    states = {component["state"] for component in components.values()}
    if states == {"managed"}:
        overall = "managed"
    elif states & {"modified", "conflict", "drifted", "symlink"}:
        overall = "drifted"
    elif "managed" in states:
        overall = "partial"
    else:
        overall = "unmanaged"

    return {
        "id": agent_id,
        "name": spec["name"],
        "detected": detected,
        "state": overall,
        "components": components,
        "issues": issues,
    }


def _cleanup_legacy_agy_loose_files(spec, changes):
    """Remove any loose legacy ~/.gemini/GEMINI.md block or skill symlink if present."""
    gem_md = spec["legacy_gemini_md"]
    if gem_md.is_file() and not gem_md.is_symlink():
        txt = gem_md.read_text(encoding="utf-8")
        parsed = extract_managed_md(txt, gem_md)
        if parsed is not None:
            cleaned = strip_block(parsed[0], parsed[2])
            if cleaned:
                atomic_write(gem_md, cleaned)
            else:
                gem_md.unlink()
            changes.append(f"cleaned legacy block from {gem_md}")

    mcp_json = spec["legacy_mcp_json"]
    if mcp_json.is_file():
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        servers = data.get("mcpServers") or {}
        if "unified-memory" in servers:
            del servers["unified-memory"]
            atomic_write(mcp_json, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            changes.append(f"migrated unified-memory out of loose {mcp_json} into plugin")

    slink = spec["legacy_skill_link"]
    if slink.is_symlink():
        slink.unlink()
        changes.append(f"removed loose skill symlink {slink}")


def connect_agent(agent_id, spec, dry_run=False):
    """Connect one agent safely with marker-block, MCP merge, and unified skills sync."""
    if dry_run:
        info = inspect_agent(agent_id, spec)
        return {"id": agent_id, "dry_run": True, "current_state": info["state"], "issues": info["issues"]}

    changes = []

    with integration_lock():
        restore = load_restore(agent_id)
        for changed in ensure_stable_shims():
            changes.append(f"wrote launcher {changed}")
        if spec.get("mode") == "dsh_patch":
            path = spec["mcp_path"]
            restore.setdefault("dsh_patch_created", not path.exists() and not path.is_symlink())
            save_restore(agent_id, restore)
            if edit_dsh_patch(path, connect=True):
                changes.append(f"configured DeepSeek Harness MCP in {path}")
            return {"id": agent_id, "state": "managed", "changes": changes or ["already up to date"]}
        if spec.get("mode") == "agy_plugin":
            pdir = spec["plugin_dir"]
            expected = desired_agy_plugin_files()
            for rel, content in expected.items():
                dest = pdir / rel
                if not dest.is_file() or dest.read_text(encoding="utf-8") != content:
                    atomic_write(dest, content)
                    changes.append(f"wrote {dest}")
            # Remove old copied unified-memory directory inside plugin/skills if non-symlink
            old_copied_um = spec["skills_dir"] / "unified-memory"
            if old_copied_um.is_dir() and not old_copied_um.is_symlink():
                shutil.rmtree(old_copied_um)
            sync_skills_into_dir(agent_id, spec["skills_dir"], restore, changes)
            _cleanup_legacy_agy_loose_files(spec, changes)
            restore["agy_rule_hash"] = sha256_text(expected["rules/unified-memory.md"])
            restore["connected_at"] = datetime.now().isoformat(timespec="seconds")
            save_restore(agent_id, restore)
            return {"id": agent_id, "state": "managed", "changes": changes or ["already up to date"]}

        inst_path = spec.get("instructions")
        mcp_path = _agent_mcp_path(agent_id, spec, restore)

        if inst_path is not None:
            if inst_path.is_symlink():
                raise RuntimeError(f"{inst_path} is a symlink; remove or convert it to a regular file first.")
            existed_before = inst_path.is_file()
            orig_text = inst_path.read_text(encoding="utf-8") if existed_before else ""
            if "instructions_existed" not in restore:
                restore["instructions_existed"] = existed_before
                if existed_before:
                    restore["instructions_backup"] = orig_text

            working_text = orig_text
            parsed = extract_managed_md(working_text, inst_path)
            if parsed is None:
                prefix = working_text.rstrip("\n")
                new_text = f"{prefix}\n\n{MANAGED_MD_BODY}\n" if prefix else f"{MANAGED_MD_BODY}\n"
                if new_text != orig_text:
                    atomic_write(inst_path, new_text)
                    changes.append(f"injected managed block into {inst_path}")
            else:
                rebuilt = parsed[0] + MANAGED_MD_BODY + parsed[2]
                if rebuilt != orig_text:
                    atomic_write(inst_path, rebuilt)
                    changes.append(f"updated managed block in {inst_path}")
            restore["instructions_block_hash"] = sha256_text(MANAGED_MD_BODY)

        # 2. MCP config
        if spec["mcp_type"] == "json":
            data = read_json_mcp(mcp_path)
            if agent_id in {"workbuddy", "claude-desktop"} and "mcp_had_previous" not in restore:
                restore["mcp_file_existed"] = mcp_path.exists()
                restore["mcp_servers_existed"] = "mcpServers" in data
            if agent_id == "claude-desktop":
                restore["mcp_path"] = str(mcp_path)
            servers = data.setdefault("mcpServers", {})
            desired = desired_json_mcp(spec)
            if "mcp_had_previous" not in restore:
                if spec["mcp_key"] in servers and not (agent_id == "cursor" and servers[spec["mcp_key"]] == desired):
                    restore["mcp_had_previous"] = True
                    restore["previous_mcp"] = servers[spec["mcp_key"]]
                else:
                    restore["mcp_had_previous"] = False
                save_restore(agent_id, restore)  # Preserve the pre-connect entry before replacing it.
            if servers.get(spec["mcp_key"]) != desired:
                servers[spec["mcp_key"]] = desired
                atomic_write(mcp_path.resolve(), json.dumps(data, ensure_ascii=False, indent=2) + "\n")
                changes.append(f"updated mcpServers.{spec['mcp_key']} in {mcp_path}")
        else:
            toml_text = mcp_path.read_text(encoding="utf-8") if mcp_path.is_file() else ""
            orig_toml = toml_text
            toml_text = restore_legacy_codex_memories(toml_text, restore)
            if toml_text != orig_toml:
                changes.append(f"restored the previous Codex memory setting in {mcp_path}")
            restore.pop("previous_codex_features_memories", None)

            block = desired_toml_block()
            exp_cmd, exp_args = desired_mcp_args()
            toml_text = without_toml_markers(toml_text)
            loc = locate_toml_mcp(toml_text, mcp_path)
            if loc is None or loc["main"] is None:
                prefix = toml_text.rstrip("\n")
                toml_text = f"{prefix}\n\n{block}\n" if prefix else f"{block}\n"
                changes.append(f"added {TOML_SECTION_HEADER} to {mcp_path}")
            else:
                main_text = toml_range_text(loc, loc["main"])
                cur_cmd, cur_args = parse_toml_mcp_section(main_text)
                if "toml_had_previous" not in restore and cur_cmd and not is_our_mcp_entry(cur_cmd, cur_args):
                    restore["toml_had_previous"] = True
                    restore["previous_toml_section"] = main_text.rstrip("\n")
                if cur_cmd != exp_cmd or cur_args != exp_args:
                    toml_text = replace_toml_main(loc, updated_toml_main(main_text, exp_cmd, exp_args))
                    changes.append(f"updated {TOML_SECTION_HEADER} in {mcp_path}")
            if toml_text != orig_toml:
                validate_toml_change(orig_toml, toml_text, mcp_path)
                atomic_write(mcp_path, toml_text)
            restore["expected_mcp_cmd"] = exp_cmd
            restore["expected_mcp_args"] = exp_args

        # 2b. Deterministic client hooks (session context + post-turn write check)
        if spec.get("hooks_path") is not None:
            apply_agent_hooks(agent_id, spec, restore, changes)

        if "skills_dir" in spec:
            sync_skills_into_dir(agent_id, spec["skills_dir"], restore, changes)

        restore["connected_at"] = datetime.now().isoformat(timespec="seconds")
        save_restore(agent_id, restore)

    return {"id": agent_id, "state": "managed", "changes": changes or ["already up to date"]}


def disconnect_agent(agent_id, spec, force=False):
    """Revert unified-memory managed blocks/keys/plugins and restore backed-up skills."""
    changes = []

    with integration_lock():
        restore = load_restore(agent_id)
        if spec.get("mode") == "dsh_patch":
            path = spec["mcp_path"]
            if edit_dsh_patch(path, connect=False, force=force, created=restore.get("dsh_patch_created", False)):
                changes.append(f"removed Unified Memory MCP from {path}")
            (RESTORE_DIR / f"{agent_id}.json").unlink(missing_ok=True)
            return {"id": agent_id, "state": "unmanaged", "changes": changes or ["nothing to remove"]}
        if spec.get("mode") == "agy_plugin":
            pdir = spec["plugin_dir"]
            rule_path = pdir / "rules" / "unified-memory.md"
            if rule_path.is_file():
                saved_hash = restore.get("agy_rule_hash")
                cur_hash = sha256_text(rule_path.read_text(encoding="utf-8"))
                if saved_hash and cur_hash != saved_hash and not force:
                    raise RuntimeError(
                        f"Managed rule in {rule_path} was edited manually; reconcile or re-run with --force."
                    )
            if pdir.exists():
                shutil.rmtree(pdir)
                changes.append(f"removed Antigravity plugin {pdir}")
            _cleanup_legacy_agy_loose_files(spec, changes)
            restore_file = RESTORE_DIR / f"{agent_id}.json"
            if restore_file.exists():
                restore_file.unlink()
            return {"id": agent_id, "state": "unmanaged", "changes": changes or ["nothing to remove"]}

        inst_path = spec.get("instructions")
        mcp_path = _agent_mcp_path(agent_id, spec, restore)

        if inst_path is not None and inst_path.is_file() and not inst_path.is_symlink():
            text = inst_path.read_text(encoding="utf-8")
            parsed = extract_managed_md(text, inst_path)
            if parsed is not None:
                saved_hash = restore.get("instructions_block_hash")
                if saved_hash and sha256_text(parsed[1]) != saved_hash and not force:
                    raise RuntimeError(
                        f"Managed block in {inst_path} was edited manually; reconcile or re-run with --force."
                    )
                cleaned = strip_block(parsed[0], parsed[2])
                if not cleaned and not restore.get("instructions_existed", True):
                    inst_path.unlink()
                    changes.append(f"removed empty {inst_path}")
                else:
                    atomic_write(inst_path, cleaned)
                    changes.append(f"removed managed block from {inst_path}")

        if spec["mcp_type"] == "json" and mcp_path.is_file():
            data = read_json_mcp(mcp_path)
            servers = data.get("mcpServers") or {}
            if spec["mcp_key"] in servers:
                current = servers[spec["mcp_key"]]
                if agent_id == "workbuddy":
                    ours = current == desired_json_mcp(spec)
                else:
                    ours = current == desired_json_mcp(spec) or (
                        isinstance(current, dict) and is_our_mcp_entry(current.get("command"), current.get("args")))
                if not ours and not force:
                    if not restore:
                        return {"id": agent_id, "state": "unmanaged", "changes": ["nothing to remove"]}
                    raise RuntimeError(f"{spec['name']} 的同名服务配置已更改，请先核对 {mcp_path}，或加 --force 移除")
                if restore.get("mcp_had_previous"):
                    servers[spec["mcp_key"]] = restore["previous_mcp"]
                    changes.append(f"restored previous mcpServers.{spec['mcp_key']} in {mcp_path}")
                else:
                    del servers[spec["mcp_key"]]
                    changes.append(f"removed mcpServers.{spec['mcp_key']} from {mcp_path}")
                if not servers and restore.get("mcp_servers_existed") is False:
                    data.pop("mcpServers")
                if not data and restore.get("mcp_file_existed") is False:
                    mcp_path.resolve().unlink()
                else:
                    atomic_write(mcp_path.resolve(), json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        elif spec["mcp_type"] == "toml" and mcp_path.is_file():
            orig_toml = mcp_path.read_text(encoding="utf-8")
            text = without_toml_markers(orig_toml)
            loc = locate_toml_mcp(text, mcp_path)
            if loc is not None and loc["main"] is not None:
                cur_cmd, cur_args = parse_toml_mcp_section(toml_range_text(loc, loc["main"]))
                exp_cmd = restore.get("expected_mcp_cmd")
                exp_args = restore.get("expected_mcp_args")
                if exp_cmd and (cur_cmd != exp_cmd or cur_args != exp_args) and not force:
                    raise RuntimeError(
                        f"TOML section {TOML_SECTION_HEADER} in {mcp_path} was edited manually; reconcile or re-run with --force."
                    )
                if not exp_cmd and not is_our_mcp_entry(cur_cmd, cur_args) and not force:
                    # Never connected here (or the restore record is gone): the table is the user's own.
                    changes.append(f"kept {TOML_SECTION_HEADER} in {mcp_path}: not written by Unified Memory")
                elif restore.get("toml_had_previous") and restore.get("previous_toml_section"):
                    prev_sec = without_toml_markers(restore["previous_toml_section"]).strip()
                    text = replace_toml_main(loc, prev_sec)
                    changes.append(f"restored previous {TOML_SECTION_HEADER} in {mcp_path}")
                else:
                    # Our table plus the subtables Codex added for it (tool approvals); nothing else.
                    text = remove_toml_ranges(loc, [loc["main"], *loc["subs"]])
                    changes.append(f"removed {TOML_SECTION_HEADER} from {mcp_path}")
            restored_text = restore_legacy_codex_memories(text, restore)
            if restored_text != text:
                changes.append(f"restored the previous Codex memory setting in {mcp_path}")
            text = restored_text
            if text != orig_toml:
                validate_toml_change(orig_toml, text, mcp_path)
                atomic_write(mcp_path, text)

        if spec.get("hooks_path") is not None:
            remove_agent_hooks(agent_id, spec, restore, changes)

        if "skills_dir" in spec:
            restore_skills_dir(agent_id, spec["skills_dir"], restore, changes)

        restore_file = RESTORE_DIR / f"{agent_id}.json"
        if restore_file.exists():
            restore_file.unlink()

    return {"id": agent_id, "state": "unmanaged", "changes": changes or ["nothing to remove"]}


def set_skill_enabled(target_folder, enabled, home=None):
    """Enable or disable a skill (or 'all') and automatically re-sync connected agents."""
    hub = discover_hub_skills()
    known = {item["folder"] for item in hub}
    if target_folder != "all" and target_folder not in known:
        raise ValueError(f"Unknown skill '{target_folder}'.")

    disabled = load_disabled_skills()
    if target_folder == "all":
        if enabled:
            disabled.clear()
        else:
            disabled = set(known) - {"unified-memory"}
    else:
        if enabled:
            disabled.discard(target_folder)
        else:
            disabled.add(target_folder)
    save_disabled_skills(disabled)

    # Re-sync any agent that is already connected (managed/drifted/partial)
    synced_agents = []
    specs = agent_specs(home=home)
    for aid, spec in specs.items():
        if "skills_dir" not in spec:
            continue
        st = inspect_agent(aid, spec)["state"]
        if st != "unmanaged":
            connect_agent(aid, spec)
            synced_agents.append(aid)
    return {"skill": target_folder, "enabled": enabled, "synced_agents": synced_agents}


def inspect_skills_matrix(home=None):
    """Return full list of hub skills with per-agent attachment states."""
    hub = discover_hub_skills()
    specs = agent_specs(home=home)
    out = []
    for item in hub:
        slug = item["folder"]
        src = SKILLS_DIR / slug
        per_agent = {}
        for aid, spec in specs.items():
            if "skills_dir" not in spec:
                continue
            dest = spec["skills_dir"] / slug
            if is_linked_to_hub(dest, src):
                per_agent[aid] = "linked"
            elif dest.exists() or dest.is_symlink():
                per_agent[aid] = "standalone"
            else:
                per_agent[aid] = "absent"
        out.append({**item, "agents": per_agent})
    return out


def check_library_integrity(memory_root=MEMORY_ROOT, allow_user_managed=True, library_id="personal"):
    """Run bidirectional index <-> disk and syntax checks on a memory library."""
    sys.path.insert(0, str(FRAMEWORK_ROOT / "manager"))
    import manager as web_manager

    root = Path(memory_root).expanduser().resolve()
    findings = []
    area_prefix = "" if library_id == "personal" else f"{library_id}:"
    lib = web_manager.Library(root, library_id, allow_user_managed=allow_user_managed)
    raw_index, topics = lib.read_index()

    missing_topics = [p for p in topics if not (root / p).is_file()]
    if missing_topics:
        findings.append(
            {"area": f"{area_prefix}index", "severity": "error", "detail": f"Missing indexed files: {missing_topics}"}
        )

    disk_files = sorted(
        p.relative_to(root).as_posix()
        for folder in ("constraints", "methods", "preferences", "projects", "archive")
        if (root / folder).is_dir()
        for p in (root / folder).glob("*.md")
        if p.name != "README.md" and not sync_git.is_conflict_filename(p.name)
    )
    orphan_files = [p for p in disk_files if p not in topics]
    if orphan_files:
        findings.append(
            {"area": f"{area_prefix}index", "severity": "error", "detail": f"Unindexed disk files: {orphan_files}"}
        )

    unparsed_bullets = []
    current_layer = ""
    for lineno, line in enumerate(raw_index.splitlines(), start=1):
        if line.startswith("#"):
            m = INDEX_LAYER_RE.match(line)
            current_layer = m.group(1) if m else ""
            continue
        if current_layer and BULLET_LINK_PREFIX_RE.match(line) and not web_manager.INDEX_LINK.match(line):
            unparsed_bullets.append(f"L{lineno}: {line.strip()[:80]}")
    if unparsed_bullets:
        findings.append(
            {"area": f"{area_prefix}index", "severity": "error", "detail": f"Malformed index bullets: {unparsed_bullets}"}
        )

    if allow_user_managed:
        _, unreadable = lib.list()
        if unreadable:
            findings.append(
                {"area": f"{area_prefix}user-managed", "severity": "error", "detail": f"Unreadable entries: {unreadable}"}
            )

    conflicts = sync_git.find_conflict_files(root)
    if conflicts:
        findings.append(
            {
                "area": f"{area_prefix}sync-conflict",
                "severity": "warn",
                "detail": f"Unresolved sync conflict copies: {conflicts}",
            }
        )

    return len(topics), len(disk_files), findings


def mount_list(home=None):
    """List personal library and all configured mounted libraries with Git sync status."""
    issues = []
    libs = resolve_libraries(primary_root=MEMORY_ROOT, home=home, include_disabled=True, issues=issues)
    out = []
    for lib in libs:
        path = lib["path"]
        exists = path.is_dir() and (path / "MEMORY.md").is_file()
        sync_cfg = lib.get("sync") or {}
        st = sync_git.git_status(
            path,
            remote=sync_cfg.get("remote") or "origin",
            branch=sync_cfg.get("branch") or "",
        ) if path.is_dir() else {"is_repo": False, "conflicts": []}
        item = {
            "id": lib["id"],
            "label": lib["label"],
            "path": str(path),
            "role": lib["role"],
            "mode": lib["mode"],
            "layers": list(lib["layers"]),
            "allow_user_managed": bool(lib["allow_user_managed"]),
            "enabled": bool(lib["enabled"]),
            "available": exists,
            "sync": sync_cfg or None,
            "git": st,
        }
        if not exists:
            item["issue"] = {
                "code": "unavailable",
                "message": library_unavailable_reason(path) or "library is unavailable",
            }
        out.append(item)
    # Entries that could not be resolved stay visible instead of silently disappearing.
    for issue in issues:
        out.append(
            {
                "id": issue["id"],
                "path": issue["path"],
                "available": False,
                "issue": {"code": issue["code"], "message": issue["message"]},
            }
        )
    return out


def mount_add(
    mount_id,
    path,
    *,
    label="",
    mode="ro",
    layers=None,
    git_url="",
    remote="origin",
    branch="",
    auto_commit=True,
    auto_push=False,
    role="shared",
    home=None,
):
    """Mount an existing or remote Git shared memory library."""
    mid = validate_mount_id(mount_id)
    mpath = validate_library_path(path)
    mode = str(mode or "ro").strip().lower()
    if mode not in {"ro", "rw"}:
        raise ValueError("mode must be 'ro' or 'rw'.")
    role = str(role or "shared").strip().lower()
    if role not in {"shared", "personal"}:
        raise ValueError("role must be 'shared' or 'personal'.")
    resolved_layers = normalize_layers(
        layers,
        default=ALL_LAYERS if role == "personal" else DEFAULT_SHARED_LAYERS,
    )

    def check_mount_free(cfg):
        if any(item.get("id") == mid for item in cfg["mounts"]):
            raise ValueError(f"Mount '{mid}' already exists. Use 'mount update' or remove it first.")
        if mpath == MEMORY_ROOT:
            raise ValueError("Mount path cannot be the same as the personal memory root.")
        for item in cfg["mounts"]:
            try:
                taken = validate_library_path(item["path"]) if item.get("path") else None
            except (ValueError, TypeError, OSError):
                continue
            if taken == mpath:
                raise ValueError(f"Path already mounted: {mpath}")

    check_mount_free(load_mounts_config(home=home))

    cloned = False
    if git_url and (not mpath.exists() or not any(mpath.iterdir())):
        clone_info = sync_git.clone_repo(git_url, mpath, branch=branch)
        branch = branch or clone_info["branch"]
        cloned = True

    if not (mpath / "MEMORY.md").is_file():
        if mode == "ro" and not git_url and mpath.exists() and any(mpath.iterdir()):
            raise ValueError(f"MEMORY.md not found in read-only directory: {mpath}")
        initialize_library(target_root=mpath, shared=(role == "shared"))

    sync_cfg = None
    if git_url or sync_git.is_git_repo(mpath):
        info = sync_git.ensure_repo(
            mpath,
            remote_url=git_url,
            remote=remote or "origin",
            branch=branch or "main",
            writable=mode == "rw",
        )
        sync_cfg = {
            "driver": "git",
            "remote": info["remote"],
            "branch": branch or info["branch"],
            "remote_url": info["remote_url"],
            "auto_commit": bool(auto_commit and mode == "rw"),
            "auto_push": bool(auto_push and mode == "rw"),
        }

    entry = {
        "id": mid,
        "label": (label or mid).strip(),
        "path": str(mpath),
        "role": role,
        "mode": mode,
        "layers": resolved_layers,
        "enabled": True,
        "sync": sync_cfg,
    }
    def append_entry(cfg):
        check_mount_free(cfg)  # re-check: another process may have mounted while we cloned
        cfg["mounts"].append(entry)

    update_mounts_config(append_entry, home=home)
    return {"mounted": entry, "cloned": cloned}


def mount_update(
    mount_id,
    *,
    label=None,
    mode=None,
    layers=None,
    enabled=None,
    remote_url=None,
    remote=None,
    branch=None,
    auto_commit=None,
    auto_push=None,
    home=None,
):
    """Update settings of an existing mounted shared library."""
    mid = validate_mount_id(mount_id)

    def apply(cfg):
        target = None
        for item in cfg["mounts"]:
            if item.get("id") == mid:
                target = item
                break
        if target is None:
            raise ValueError(f"Mount '{mid}' not found.")

        if label is not None:
            target["label"] = label.strip() or mid
        if mode is not None:
            m = mode.strip().lower()
            if m not in {"ro", "rw"}:
                raise ValueError("mode must be 'ro' or 'rw'.")
            target["mode"] = m
        if layers is not None:
            target["layers"] = normalize_layers(layers)
        if enabled is not None:
            target["enabled"] = bool(enabled)

        if any(v is not None for v in (remote_url, remote, branch, auto_commit, auto_push)):
            cur_sync = dict(target.get("sync") or {"driver": "git", "remote": "origin", "branch": "main", "auto_commit": True, "auto_push": False})
            if remote is not None:
                cur_sync["remote"] = remote.strip() or "origin"
            if branch is not None:
                cur_sync["branch"] = branch.strip()
            if remote_url is not None:
                cur_sync["remote_url"] = remote_url.strip()
            if auto_commit is not None:
                cur_sync["auto_commit"] = bool(auto_commit)
            if auto_push is not None:
                cur_sync["auto_push"] = bool(auto_push)
            info = sync_git.ensure_repo(
                Path(target["path"]),
                remote_url=cur_sync.get("remote_url") or "",
                remote=cur_sync.get("remote") or "origin",
                branch=cur_sync.get("branch") or "main",
                writable=target["mode"] == "rw",
            )
            cur_sync["branch"] = cur_sync.get("branch") or info["branch"]
            cur_sync["remote_url"] = info["remote_url"]
            target["sync"] = cur_sync

        return {"updated": target}

    return update_mounts_config(apply, home=home)


def mount_remove(mount_id, home=None):
    """Unmount a shared library from mounts.json without deleting local files."""
    mid = validate_mount_id(mount_id)

    def apply(cfg):
        before = len(cfg["mounts"])
        cfg["mounts"] = [item for item in cfg["mounts"] if item.get("id") != mid]
        if len(cfg["mounts"]) == before:
            raise ValueError(f"Mount '{mid}' not found.")

    update_mounts_config(apply, home=home)
    return {"removed": mid, "files_preserved": True}


def setup_library_sync(
    library_id="personal",
    *,
    remote_url="",
    remote="origin",
    branch="main",
    auto_commit=True,
    auto_push=False,
    home=None,
):
    """Initialize Git and configure remote sync settings for personal or a mounted library."""
    libs = {lib["id"]: lib for lib in resolve_libraries(primary_root=MEMORY_ROOT, home=home, include_disabled=True)}
    if library_id not in libs:
        raise ValueError(f"Unknown library '{library_id}'. Available: {', '.join(libs)}")
    lib = libs[library_id]
    info = sync_git.ensure_repo(
        lib["path"],
        remote_url=remote_url,
        remote=remote or "origin",
        branch=branch or "main",
        writable=lib["mode"] == "rw",
    )
    sync_cfg = {
        "driver": "git",
        "remote": info["remote"],
        "branch": branch or info["branch"],
        "remote_url": info["remote_url"],
        "auto_commit": bool(auto_commit and lib["mode"] == "rw"),
        "auto_push": bool(auto_push and lib["mode"] == "rw"),
    }
    def apply(cfg):
        if library_id == "personal":
            cfg["personal_sync"] = sync_cfg
        else:
            for item in cfg["mounts"]:
                if item.get("id") == library_id:
                    item["sync"] = sync_cfg
                    break

    update_mounts_config(apply, home=home)
    return {"library": library_id, "repo": info, "sync": sync_cfg}


def rebuild_library_index(target="personal", home=None):
    """Deterministically rebuild MEMORY.md for one library or all enabled libraries."""
    libs = resolve_libraries(primary_root=MEMORY_ROOT, home=home)
    if target == "all":
        selected = libs
    else:
        selected = [lib for lib in libs if lib["id"] == target]
        if not selected:
            raise ValueError(f"Unknown library '{target}'. Available: all, {', '.join(l['id'] for l in libs)}")
    results = []
    for lib in selected:
        res = sync_git.rebuild_index(lib["path"])
        results.append({"library": lib["id"], **res})
    return {"results": results}


def sync_libraries(target="all", *, pull_only=False, push_only=False, status_only=False, home=None):
    """Inspect or execute Git synchronization for personal and/or mounted libraries."""
    libs = resolve_libraries(primary_root=MEMORY_ROOT, home=home)
    if target == "all":
        selected = libs
    else:
        selected = [lib for lib in libs if lib["id"] == target]
        if not selected:
            raise ValueError(f"Unknown library '{target}'. Available: all, {', '.join(l['id'] for l in libs)}")

    results = []
    for lib in selected:
        sync_cfg = lib.get("sync") or {}
        remote = sync_cfg.get("remote") or "origin"
        branch = sync_cfg.get("branch") or ""
        if status_only:
            st = sync_git.git_status(lib["path"], remote=remote, branch=branch)
            results.append({"library": lib["id"], "mode": lib["mode"], "status": st})
            continue
        if not sync_git.is_git_repo(lib["path"]):
            results.append({"library": lib["id"], "skipped": True, "reason": "not_a_git_repo"})
            continue
        st = sync_git.git_status(lib["path"], remote=remote, branch=branch)
        if not st.get("remote_url"):
            results.append({"library": lib["id"], "skipped": True, "reason": "no_remote_configured"})
            continue
        res = sync_git.sync(
            lib["path"],
            remote=remote,
            branch=branch,
            mode=lib["mode"],
            pull_only=pull_only,
            push_only=push_only,
        )
        if lib["mode"] == "rw" and sync_cfg.get("auto_push"):
            async_sync.record_external_result(lib["path"], result=res, reason="cli sync")
        results.append({"library": lib["id"], **res})
    return {"results": results}


def run_doctor(fix=False, home=None):
    """Diagnose library integrity, mounted libraries, MCP health, skills, and agent drift."""
    indexed_count, disk_count, findings = check_library_integrity(MEMORY_ROOT, allow_user_managed=True, library_id="personal")
    repaired = []

    if fix and any(f["area"] == "index" for f in findings):
        rb = sync_git.rebuild_index(MEMORY_ROOT)
        if rb["changed"]:
            repaired.append({"library": "personal", "action": "rebuild_index", "indexed_topics": rb["indexed_topics"]})
            indexed_count, disk_count, findings = check_library_integrity(
                MEMORY_ROOT, allow_user_managed=True, library_id="personal"
            )

    library_reports = []
    for lib in resolve_libraries(primary_root=MEMORY_ROOT, home=home):
        lib_id = lib["id"]
        sync_cfg = lib.get("sync") or {}
        git_st = sync_git.git_status(
            lib["path"],
            remote=sync_cfg.get("remote") or "origin",
            branch=sync_cfg.get("branch") or "",
        ) if lib["path"].is_dir() else {"is_repo": False, "conflicts": []}
        if lib_id != "personal":
            if not (lib["path"] / "MEMORY.md").is_file():
                findings.append(
                    {"area": f"{lib_id}:mount", "severity": "error", "detail": f"Mounted library missing MEMORY.md: {lib['path']}"}
                )
                lib_indexed, lib_disk = 0, 0
            else:
                lib_indexed, lib_disk, lib_findings = check_library_integrity(
                    lib["path"],
                    allow_user_managed=lib["allow_user_managed"],
                    library_id=lib_id,
                )
                if fix and lib["mode"] == "rw" and any(f["area"] == f"{lib_id}:index" for f in lib_findings):
                    rb = sync_git.rebuild_index(lib["path"])
                    if rb["changed"]:
                        repaired.append({"library": lib_id, "action": "rebuild_index", "indexed_topics": rb["indexed_topics"]})
                        lib_indexed, lib_disk, lib_findings = check_library_integrity(
                            lib["path"],
                            allow_user_managed=lib["allow_user_managed"],
                            library_id=lib_id,
                        )
                findings.extend(lib_findings)
        else:
            lib_indexed, lib_disk = indexed_count, disk_count

        library_reports.append(
            {
                "id": lib_id,
                "label": lib["label"],
                "role": lib["role"],
                "mode": lib["mode"],
                "layers": list(lib["layers"]),
                "path": str(lib["path"]),
                "indexed_topics": lib_indexed,
                "disk_topics": lib_disk,
                "git": git_st,
            }
        )

    probe_reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    if fix:
        ensure_stable_shims()
    launcher = shim_path("umem-mcp")
    if not launcher.is_file():
        findings.append({"area": "launcher", "severity": "warn",
                         "detail": f"stable launcher missing: {launcher} (run `memoryctl connect all` or `doctor --fix`)"})
    probe = subprocess.run(
        ([str(launcher)] if launcher.is_file() else [resolve_python_cmd(), str(MCP_SERVER)]) + ["--memory-root", str(MEMORY_ROOT)],
        input="\n".join(json.dumps(r) for r in probe_reqs) + "\n",
        text=True,
        capture_output=True,
    )
    if probe.returncode != 0 or '"memory_search"' not in probe.stdout:
        findings.append({"area": "mcp", "severity": "error", "detail": f"MCP probe failed: {probe.stderr.strip()}"})

    specs = agent_specs(home=home)
    agent_reports = []
    for agent_id, spec in specs.items():
        report = inspect_agent(agent_id, spec)
        if report["state"] in {"drifted", "partial"}:
            if fix and "modified" not in {
                c["state"] for c in report["components"].values()
            }:
                try:
                    res = connect_agent(agent_id, spec)
                    repaired.append({"agent": agent_id, "changes": res["changes"]})
                    report = inspect_agent(agent_id, spec)
                except Exception as exc:
                    findings.append({"area": f"agent:{agent_id}", "severity": "error", "detail": str(exc)})
            if report["state"] in {"drifted", "partial"}:
                findings.append(
                    {
                        "area": f"agent:{agent_id}",
                        "severity": "warn",
                        "detail": "; ".join(report["issues"]) or report["state"],
                    }
                )
        agent_reports.append(report)

    stray = scan_stray_agent_memories(home=home)
    if stray:
        findings.append(
            {
                "area": "stray-memory",
                "severity": "warn",
                "detail": f"Stray native client memory files found outside unified hub: {stray}",
            }
        )

    hub = discover_hub_skills()
    return {
        "ok": not any(f["severity"] == "error" for f in findings),
        "indexed_topics": indexed_count,
        "disk_topics": disk_count,
        "total_skills": len(hub),
        "enabled_skills": sum(s["enabled"] for s in hub),
        "stray_memories": stray,
        "findings": findings,
        "repaired": repaired,
        "libraries": library_reports,
        "agents": agent_reports,
    }


def resolve_targets(target, home=None):
    """Resolve 'all', 'detected', or a specific agent id."""
    specs = agent_specs(home=home)
    if target == "all":
        return list(specs.items())
    if target == "detected":
        return [
            (aid, spec)
            for aid, spec in specs.items()
            if any(p.exists() for p in spec["detect_paths"])
        ]
    if target not in specs:
        raise SystemExit(f"Unknown agent '{target}'. Choose from: all, detected, {', '.join(specs)}")
    return [(target, specs[target])]


def _parse_bool_flag(val):
    s = str(val).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Expected true/false, got: {val}")


CLI_TOOL_NAMES = {
    "list": "memory_list",
    "search": "memory_search",
    "read": "memory_read",
    "write": "memory_upsert",
    "lint": "memory_lint",
}


def _detect_cli_client():
    configured = os.environ.get("UNIFIED_MEMORY_CLIENT", "").strip()
    if configured:
        return audit_log.normalize_client(configured)
    if any(os.environ.get(k) for k in ("CODEX_THREAD_ID", "CODEX_SHELL", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE")):
        return "Codex"
    if any(os.environ.get(k) for k in ("CLAUDECODE", "CLAUDE_CODE", "CLAUDE_CODE_ENTRYPOINT")):
        return "Claude Code"
    if any(os.environ.get(k) for k in ("CURSOR_AGENT", "CURSOR_TRACE_ID")):
        return "Cursor"
    if any(os.environ.get(k) for k in ("DSH_SESSION", "DSH_CLI")):
        return "DeepSeek Harness"
    return "CLI"


def _record_cli_audit(command, args, data, ok, ms, output_text="", error=""):
    tool = CLI_TOOL_NAMES.get(command)
    if not tool:
        return
    try:
        ev = {
            "source": "cli",
            "client": _detect_cli_client(),
            "tool": tool,
            "ok": ok,
            "ms": ms,
            "library": getattr(args, "library", None),
        }
        if command == "search":
            ev["query"] = str(getattr(args, "query", ""))[:120]
        if getattr(args, "layer", ""):
            ev["layer"] = args.layer
        if not ok:
            ev["error"] = str(error)[:160]
            audit_log.record(ev)
            return
        if output_text:
            ev["bytes"] = len(output_text.encode("utf-8"))
            ev["tokens"] = audit_log.estimate_tokens(output_text)
        if command == "search" and isinstance(data, list):
            ev["hits"] = len(data)
            ev["refs"] = [h.get("ref") for h in data[: audit_log.MAX_AUDIT_REFS] if isinstance(h, dict) and h.get("ref")]
        elif command == "list" and isinstance(data, list):
            ev["hits"] = len(data)
        elif isinstance(data, dict):
            if command == "read" and isinstance(data.get("entries"), list):
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
                if command == "write":
                    ev["action"] = data.get("status") or data.get("action")
                    if data.get("mode"):
                        ev["mode"] = data.get("mode")
                    if data.get("section_action"):
                        ev["section_action"] = data.get("section_action")
        audit_log.record(ev)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for command, help_text in (("list", "List memory entries."), ("search", "Search memory entries."),
                               ("read", "Read one memory by path or library:path."),
                               ("write", "Create or update a Markdown memory entry.")):
        item = sub.add_parser(command, help=help_text)
        item.add_argument("--library", default=None, help="Library ID (defaults to personal for writes).")
        if command in {"list", "search"}:
            item.add_argument("--layer", choices=ALL_LAYERS, default="")
            item.add_argument("--archived", action="store_true", help="Include archived entries.")
        if command == "list":
            item.add_argument("--sort", choices=("updated", "created", "layer"), default="updated")
            item.add_argument("--limit", type=int, default=0, help="Optional max entries to return (0 = all).")
            item.add_argument("--compact", action="store_true", help="Omit duplicate routing/bookkeeping fields.")
        if command == "search":
            item.add_argument("query")
            item.add_argument("--limit", type=int, default=10)
            item.add_argument("--compact", action="store_true", help="Omit duplicate routing/bookkeeping fields.")
        if command == "read":
            item.add_argument("paths", nargs="+", help="One or more relative paths (optionally prefixed with '<library>:').")
            item.add_argument("--section", default="", help="Read only this '## ' section.")
            item.add_argument("--outline", action="store_true", help="Return only the intro and section outline.")
        if command == "write":
            item.add_argument("--layer", choices=ALL_LAYERS, required=True)
            item.add_argument("--name", required=True)
            item.add_argument("--title", default="", help="Single-line title (required for new entries; optional when updating/appending).")
            item.add_argument("--description", default="", help="Concise retrieval cue (optional when updating/appending).")
            item.add_argument("--append", action="store_true", help="Append body to an existing entry without replacing it.")
            item.add_argument("--section", default="", help="Target a single '## ' section to replace, append, or create in place.")
            item.add_argument("--verified-at", default=None, help="Optional YYYY-MM-DD verification date (empty string clears).")
            item.add_argument("--valid-until", default=None, help="Optional YYYY-MM-DD expiration date (empty string clears).")
            item.add_argument("--body", default=None, help="Inline UTF-8 Markdown body text (alternative to --body-file).")
            item.add_argument("--body-file", default=None, help="UTF-8 Markdown file, or - for standard input.")

    p_init = sub.add_parser("init", help="Initialize an empty private or shared library.")
    p_init.add_argument("--path", help="Optional target library path (defaults to UNIFIED_MEMORY_ROOT).")
    p_init.add_argument("--shared", action="store_true", help="Initialize as a shared library (no personal skills/user-managed).")

    sub.add_parser("install-shims", help="Create or refresh the stable launchers client configs point at.")
    sub.add_parser("import-codex", help="Copy Codex memories into the local review queue.")
    p_import = sub.add_parser("import-memory", help="Add existing client memories to the local review queue.")
    p_import.add_argument("client", choices=tuple(agent_specs()))
    p_import.add_argument("--file", type=Path, help="A UTF-8 memory file to import.")
    sub.add_parser("interface", help="Emit machine-readable paths and capabilities.")

    p_rb = sub.add_parser("rebuild-index", help="Deterministically rebuild MEMORY.md index from disk entries.")
    p_rb.add_argument("target", nargs="?", default="personal", help="Library id ('personal', mount id, or 'all').")

    p_mount = sub.add_parser("mount", help="Manage mounted shared memory libraries.")
    mount_sub = p_mount.add_subparsers(dest="mount_cmd", required=True)
    mount_sub.add_parser("list", help="List personal and mounted libraries with sync status.")
    p_madd = mount_sub.add_parser("add", help="Mount a local or Git-backed shared library.")
    p_madd.add_argument("id", help="Mount identifier (e.g. team-kb).")
    p_madd.add_argument("--path", required=True, help="Local directory for the mounted library.")
    p_madd.add_argument("--label", default="", help="Human-readable label shown in UI.")
    p_madd.add_argument("--mode", choices=("ro", "rw"), default="ro", help="Access mode: ro (read-only) or rw (read-write).")
    p_madd.add_argument("--layers", default="", help="Comma-separated allowed layers (default for shared: L1,L3,L4).")
    p_madd.add_argument("--git", default="", dest="git_url", help="Optional Git remote URL (GitHub/Gitee/self-hosted Git).")
    p_madd.add_argument("--remote", default="origin", help="Git remote name (default: origin).")
    p_madd.add_argument("--branch", default="", help="Git branch name.")
    p_madd.add_argument("--auto-commit", type=_parse_bool_flag, default=True, help="Auto-commit writes on rw Git mounts.")
    p_madd.add_argument(
        "--auto-push",
        type=_parse_bool_flag,
        default=False,
        help="Debounced background pull/push after successful writes on rw Git mounts.",
    )
    p_madd.add_argument("--role", choices=("shared", "personal"), default="shared", help="Library role.")

    p_mupd = mount_sub.add_parser("update", help="Update an existing mount configuration.")
    p_mupd.add_argument("id")
    p_mupd.add_argument("--label", default=None)
    p_mupd.add_argument("--mode", choices=("ro", "rw"), default=None)
    p_mupd.add_argument("--layers", default=None)
    p_mupd.add_argument("--enabled", type=_parse_bool_flag, default=None)
    p_mupd.add_argument("--git", default=None, dest="remote_url")
    p_mupd.add_argument("--remote", default=None)
    p_mupd.add_argument("--branch", default=None)
    p_mupd.add_argument("--auto-commit", type=_parse_bool_flag, default=None)
    p_mupd.add_argument(
        "--auto-push",
        type=_parse_bool_flag,
        default=None,
        help="Enable or disable debounced background pull/push after writes.",
    )

    p_mrm = mount_sub.add_parser("remove", help="Unmount a shared library (keeps local files intact).")
    p_mrm.add_argument("id")

    p_sync = sub.add_parser("sync", help="Configure or execute Git sync across personal and mounted libraries.")
    sync_sub = p_sync.add_subparsers(dest="sync_cmd", required=True)
    p_srun = sync_sub.add_parser("run", help="Pull and push Git-backed libraries.")
    p_srun.add_argument("target", nargs="?", default="all", help="Library id ('personal', mount id, or 'all').")
    p_srun.add_argument("--pull-only", action="store_true")
    p_srun.add_argument("--push-only", action="store_true")

    p_sstat = sync_sub.add_parser("status", help="Inspect local Git sync status without hitting network.")
    p_sstat.add_argument("target", nargs="?", default="all")

    p_ssetup = sync_sub.add_parser("setup", help="Initialize Git and attach a remote URL to a library.")
    p_ssetup.add_argument("library", nargs="?", default="personal", help="Library id (default: personal).")
    p_ssetup.add_argument("--remote-url", required=True, help="Git remote URL (GitHub/Gitee/self-hosted Git).")
    p_ssetup.add_argument("--remote", default="origin")
    p_ssetup.add_argument("--branch", default="main")
    p_ssetup.add_argument("--auto-commit", type=_parse_bool_flag, default=True)
    p_ssetup.add_argument("--auto-push", type=_parse_bool_flag, default=False)

    p_agent = sub.add_parser("agent", help="Inspect or manage agent integrations.")
    agent_sub = p_agent.add_subparsers(dest="agent_cmd", required=True)
    agent_sub.add_parser("list", help="Inspect detected agents and integration states.")

    p_skill = sub.add_parser("skill", help="Inspect, enable, or disable unified skills.")
    skill_sub = p_skill.add_subparsers(dest="skill_cmd", required=True)
    skill_sub.add_parser("list", help="List all skills in the unified hub and per-agent link status.")
    p_sen = skill_sub.add_parser("enable", help="Enable a skill (folder name or 'all') and sync connected agents.")
    p_sen.add_argument("folder")
    p_sdis = skill_sub.add_parser("disable", help="Disable a skill (folder name) and unlink from connected agents.")
    p_sdis.add_argument("folder")

    p_conn = sub.add_parser("connect", help=f"Connect an agent ({', '.join(agent_specs())}, detected, or all).")
    p_conn.add_argument("target", nargs="?", default="all")
    p_conn.add_argument("--dry-run", action="store_true")

    p_disc = sub.add_parser("disconnect", help="Cleanly remove unified-memory integration from an agent.")
    p_disc.add_argument("target", nargs="?", default="all")
    p_disc.add_argument("--force", action="store_true", help="Remove even if managed blocks were manually edited.")

    p_doc = sub.add_parser("doctor", help="Diagnose index, MCP server, skills, and agent integration drift.")
    p_doc.add_argument("--fix", action="store_true", help="Automatically reconcile drifted/partial integrations.")
    p_doc.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_usage = sub.add_parser("usage", help="Report how much memory context clients received and used.")
    p_usage.add_argument("--days", type=float, default=7, help="Look back this many days (default 7).")

    p_lint = sub.add_parser("lint", help="Report consolidation problems: index drift, stale snapshots, duplicate topics.")
    p_lint.add_argument("--library", default="personal", help="Library id, or '*' for every mounted library.")
    p_lint.add_argument("--stale-days", type=int, default=90, help="Flag L3 snapshots older than this (default 90).")

    sub.add_parser("version", help="Print the installed Unified Memory version and runtime location.")
    p_upg = sub.add_parser("upgrade", help="Check for or apply verified standalone CLI upgrades from GitHub Releases.")
    p_upg.add_argument("--check", action="store_true", help="Only check for a newer release without modifying files.")
    p_upg.add_argument("--force", action="store_true", help="Reinstall even if the current version matches latest.")

    args = parser.parse_args()

    if args.command in {"list", "search", "read", "write", "lint"}:
        sys.path.insert(0, str(FRAMEWORK_ROOT / "mcp"))
        from server import WorkspaceStore, compact_list_entry, compact_search_hit
        started = time.monotonic()
        scheduler = async_sync.DetachedSyncScheduler() if args.command == "write" else None
        store = None
        try:
            store = WorkspaceStore(MEMORY_ROOT, async_scheduler=scheduler)
            if args.command == "list":
                result = store.list_entries(
                    library=args.library or "*",
                    layer=args.layer,
                    include_archived=args.archived,
                    sort=args.sort,
                    limit=args.limit,
                )
                if args.compact:
                    result = [compact_list_entry(item) for item in result]
            elif args.command == "search":
                result = store.search(args.query, library=args.library or "*", layer=args.layer,
                                      include_archived=args.archived, limit=args.limit)
                if args.compact:
                    result = [compact_search_hit(item) for item in result]
            elif args.command == "lint":
                result = store.lint(stale_days=args.stale_days, library=args.library or "personal")
            elif args.command == "read":
                if len(args.paths) == 1:
                    result = store.read_entry(
                        args.paths[0],
                        library=args.library or "",
                        section=args.section,
                        outline=args.outline,
                    )
                else:
                    result = store.read_entries(
                        args.paths,
                        library=args.library or "",
                        section=args.section,
                        outline=args.outline,
                    )
            else:
                if args.body is not None and args.body_file is not None:
                    raise ValueError("Pass either --body or --body-file, not both.")
                if args.body is None and args.body_file is None:
                    raise ValueError("Provide either --body or --body-file.")
                if args.body is not None:
                    body = args.body
                else:
                    body = sys.stdin.read() if args.body_file == "-" else Path(args.body_file).read_text(encoding="utf-8")
                result = store.upsert_entry(
                    args.layer,
                    args.name,
                    args.title,
                    args.description,
                    body,
                    library=args.library or "personal",
                    mode="append" if args.append else "replace",
                    section=args.section,
                    verified_at=args.verified_at,
                    valid_until=args.valid_until,
                )
        except (ValueError, OSError) as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            _record_cli_audit(args.command, args, None, False, elapsed_ms, error=str(exc))
            parser.exit(1, f"{exc}\n")
        finally:
            if store is not None:
                store.shutdown()
        output_text = json.dumps(result, ensure_ascii=False, indent=2)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        _record_cli_audit(args.command, args, result, True, elapsed_ms, output_text=output_text)
        print(output_text)
    elif args.command == "init":
        print(json.dumps(initialize_library(target_root=args.path, shared=args.shared), ensure_ascii=False, indent=2))
    elif args.command == "rebuild-index":
        print(json.dumps(rebuild_library_index(args.target), ensure_ascii=False, indent=2))
    elif args.command == "mount":
        if args.mount_cmd == "list":
            print(json.dumps({"libraries": mount_list()}, ensure_ascii=False, indent=2))
        elif args.mount_cmd == "add":
            res = mount_add(
                args.id,
                args.path,
                label=args.label,
                mode=args.mode,
                layers=args.layers or None,
                git_url=args.git_url,
                remote=args.remote,
                branch=args.branch,
                auto_commit=args.auto_commit,
                auto_push=args.auto_push,
                role=args.role,
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif args.mount_cmd == "update":
            res = mount_update(
                args.id,
                label=args.label,
                mode=args.mode,
                layers=args.layers,
                enabled=args.enabled,
                remote_url=args.remote_url,
                remote=args.remote,
                branch=args.branch,
                auto_commit=args.auto_commit,
                auto_push=args.auto_push,
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif args.mount_cmd == "remove":
            print(json.dumps(mount_remove(args.id), ensure_ascii=False, indent=2))
    elif args.command == "sync":
        if args.sync_cmd == "run":
            res = sync_libraries(args.target, pull_only=args.pull_only, push_only=args.push_only)
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif args.sync_cmd == "status":
            res = sync_libraries(args.target, status_only=True)
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif args.sync_cmd == "setup":
            res = setup_library_sync(
                args.library,
                remote_url=args.remote_url,
                remote=args.remote,
                branch=args.branch,
                auto_commit=args.auto_commit,
                auto_push=args.auto_push,
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
    elif args.command == "install-shims":
        print(json.dumps({"directory": str(stable_bin_dir()), "changed": ensure_stable_shims()}, ensure_ascii=False, indent=2))
    elif args.command == "interface":
        import cli_updater
        hub = discover_hub_skills()
        print(
            json.dumps(
                {
                    "protocol_version": 4,
                    "version": cli_updater.current_version(),
                    "python_command": resolve_python_cmd(),
                    "capabilities": {
                        "marker_block_injection": True,
                        "antigravity_plugin_bundle": True,
                        "unified_skills_hub": True,
                        "section_aware_toml": True,
                        "bidirectional_index_doctor": True,
                        "reversible_disconnect": True,
                        "conflict_detection": True,
                        "externalized_local_state": True,
                        "multi_library_mounts": True,
                        "git_sync_driver": True,
                        "deterministic_index_rebuild": True,
                    },
                    "paths": {
                        "memory_root": str(MEMORY_ROOT),
                        "index": str(MEMORY_ROOT / "MEMORY.md"),
                        "mcp_server": str(MCP_SERVER),
                        "launcher": str(shim_path("umem-mcp")),
                        "skills_dir": str(SKILLS_DIR),
                        "state": str(STATE_DIR),
                    },
                    "skills_count": len(hub),
                    "libraries": [l["id"] for l in resolve_libraries(primary_root=MEMORY_ROOT)],
                    "agents": list(agent_specs().keys()),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command in {"import-codex", "import-memory"}:
        from memory_import import import_memories, import_text

        if not (MEMORY_ROOT / "MEMORY.md").is_file():
            parser.error("Initialize the personal library first with umem init.")
        client = getattr(args, "client", "codex")
        source = getattr(args, "file", None)
        try:
            report = (import_text(MEMORY_ROOT, client, source.stem, source.read_text(encoding="utf-8"))
                      if source else import_memories(MEMORY_ROOT, client))
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "agent" and args.agent_cmd == "list":
        reports = [inspect_agent(aid, spec) for aid, spec in agent_specs().items()]
        print(json.dumps({"agents": reports}, ensure_ascii=False, indent=2))
    elif args.command == "skill":
        if args.skill_cmd == "list":
            print(json.dumps({"skills": inspect_skills_matrix()}, ensure_ascii=False, indent=2))
        elif args.skill_cmd == "enable":
            print(json.dumps(set_skill_enabled(args.folder, True), ensure_ascii=False, indent=2))
        elif args.skill_cmd == "disable":
            print(json.dumps(set_skill_enabled(args.folder, False), ensure_ascii=False, indent=2))
    elif args.command == "connect":
        results = [connect_agent(aid, spec, dry_run=args.dry_run) for aid, spec in resolve_targets(args.target)]
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    elif args.command == "disconnect":
        results = [disconnect_agent(aid, spec, force=args.force) for aid, spec in resolve_targets(args.target)]
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    elif args.command == "doctor":
        report = run_doctor(fix=args.fix)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "usage":
        since = time.time() - args.days * 86400
        report = audit_log.usage_stats(audit_log.read_events(since=since))
        report["days"] = args.days
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "version":
        import cli_updater
        info = cli_updater.inspect_install_target()
        print(
            json.dumps(
                {
                    "version": cli_updater.current_version(),
                    "install_kind": info["install_kind"],
                    "can_replace": info["can_replace"],
                    "framework_root": info["target"],
                    "python_command": resolve_python_cmd(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "upgrade":
        import cli_updater
        try:
            report = cli_updater.check_or_upgrade(apply=not args.check, force=args.force)
        except (ValueError, RuntimeError, OSError) as exc:
            parser.exit(1, f"{exc}\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
