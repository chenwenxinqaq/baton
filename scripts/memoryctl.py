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


from library_paths import (
    ALL_LAYERS,
    DEFAULT_SHARED_LAYERS,
    FRAMEWORK_ROOT,
    data_root,
    global_state_dir,
    load_mounts_config,
    library_seed_files,
    normalize_layers,
    resolve_libraries,
    save_mounts_config,
    validate_library_path,
    validate_mount_id,
)
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
  for APP in "$HOME/Applications/Unified Memory.app" "/Applications/Unified Memory.app"; do
    R="$APP/Contents/Resources"
    if [ -x "$R/umem-python" ] && [ -f "$R/framework/{rel_script}" ]; then
      PY="$R/umem-python"; FW="$R/framework"; break
    fi
  done
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


def extract_toml_section(text, section_header=TOML_SECTION_HEADER):
    """Locate [mcp_servers.unified_memory] with or without wrapper comments."""
    marked = extract_block(text, TOML_START, TOML_END)
    if marked is not None:
        return marked

    lines = text.splitlines(keepends=True)
    start_line = None
    end_line = len(lines)
    header_re = re.compile(r"^\s*\[[^\]]+\]\s*(?:#.*)?$")

    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if start_line is None:
            if stripped == section_header:
                start_line = idx
        else:
            if header_re.match(stripped):
                end_line = idx
                break

    if start_line is None:
        return None

    before = "".join(lines[:start_line])
    section = "".join(lines[start_line:end_line]).rstrip("\n")
    after = "".join(lines[end_line:])
    return before, section, after


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


def get_codex_features_memories(toml_text):
    """Return 'true', 'false', or 'absent' for [features] memories in Codex config.toml."""
    header_re = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
    in_features = False
    for raw in toml_text.splitlines():
        s = raw.strip()
        m = header_re.match(s)
        if m:
            in_features = m.group(1).strip() == "features"
            continue
        if not in_features or not s or s.startswith("#"):
            continue
        key, sep, val = s.partition("=")
        if sep and key.strip() == "memories":
            v = val.split("#", 1)[0].strip().lower()
            if v in {"true", "false"}:
                return v
            return v
    return "absent"


def set_codex_features_memories(toml_text, desired_state):
    """Set or remove memories key inside [features] in Codex config.toml.

    desired_state: 'false', 'true', or 'absent' (removes key, and removes empty [features]).
    """
    header_re = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
    lines = toml_text.splitlines()
    feat_idx = None
    feat_end = len(lines)
    mem_line_idx = None

    for idx, raw in enumerate(lines):
        m = header_re.match(raw.strip())
        if m:
            if feat_idx is not None:
                feat_end = idx
                break
            if m.group(1).strip() == "features":
                feat_idx = idx
            continue
        if feat_idx is not None:
            s = raw.strip()
            if s and not s.startswith("#"):
                k, sep, _ = s.partition("=")
                if sep and k.strip() == "memories":
                    mem_line_idx = idx

    if desired_state == "absent":
        if mem_line_idx is None:
            return toml_text
        del lines[mem_line_idx]
        # Check if [features] now has zero keys
        has_other_keys = False
        for idx in range(feat_idx + 1, feat_end - 1):
            s = lines[idx].strip()
            if s and not s.startswith("#"):
                has_other_keys = True
                break
        if not has_other_keys:
            del lines[feat_idx:feat_end - 1]
        out = "\n".join(lines).strip()
        return out + "\n" if out else ""

    new_kv = f"memories = {desired_state}"
    if feat_idx is not None:
        if mem_line_idx is not None:
            lines[mem_line_idx] = new_kv
        else:
            lines.insert(feat_idx + 1, new_kv)
        return "\n".join(lines).rstrip() + "\n"

    # Insert [features] cleanly before managed MCP block if present, else append
    block_pos = toml_text.find(TOML_START)
    if block_pos == -1:
        block_pos = toml_text.find(TOML_SECTION_HEADER)
    feat_section = f"[features]\n{new_kv}\n"
    if block_pos != -1:
        head = toml_text[:block_pos].rstrip()
        tail = toml_text[block_pos:].lstrip("\n")
        return (f"{head}\n\n{feat_section}\n{tail}" if head else f"{feat_section}\n{tail}")
    prefix = toml_text.rstrip()
    return f"{prefix}\n\n{feat_section}" if prefix else feat_section


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


def agent_specs(home=None):
    """Return per-agent paths and detection rules."""
    home = home or Path.home()
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


def desired_toml_block():
    """Build expected Codex TOML managed block."""
    py_cmd, args = desired_mcp_args()
    args_json = json.dumps(args, ensure_ascii=False)
    return (
        f"{TOML_START}\n"
        f"{TOML_SECTION_HEADER}\n"
        f"command = {json.dumps(py_cmd)}\n"
        f"args = {args_json}\n"
        f"{TOML_END}"
    )


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


def _is_our_hook(entry):
    command = str(entry.get("command", "")) if isinstance(entry, dict) else ""
    return any(marker in command for marker in CURSOR_HOOK_MARKERS)


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
        parsed = extract_block(text, MD_START, MD_END)
        if parsed is None:
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

    mcp_path = spec["mcp_path"]
    exp_cmd, exp_args = desired_mcp_args()
    if spec["mcp_type"] == "json":
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
            sec = extract_toml_section(text)
            feat_mem = get_codex_features_memories(text)
            if sec is None:
                mcp_state = "unmanaged"
            else:
                cmd, args = parse_toml_mcp_section(sec[1])
                if cmd == exp_cmd and args == exp_args and feat_mem == "false":
                    mcp_state = "managed"
                else:
                    mcp_state = "drifted"
                    if cmd != exp_cmd or args != exp_args:
                        issues.append("Codex [mcp_servers.unified_memory] drifted")
                    if feat_mem != "false":
                        issues.append(f"Codex native [features] memories={feat_mem} (should be false to prevent split-brain)")
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
        hooks_state, hook_issues = inspect_cursor_hooks(spec["hooks_path"])
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
        parsed = extract_block(txt, MD_START, MD_END)
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
        mcp_path = spec["mcp_path"]

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
            parsed = extract_block(working_text, MD_START, MD_END)
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
            if agent_id == "workbuddy" and "mcp_had_previous" not in restore:
                restore["mcp_file_existed"] = mcp_path.exists()
                restore["mcp_servers_existed"] = "mcpServers" in data
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
            if "previous_codex_features_memories" not in restore:
                restore["previous_codex_features_memories"] = get_codex_features_memories(toml_text)
            if get_codex_features_memories(toml_text) != "false":
                toml_text = set_codex_features_memories(toml_text, "false")
                changes.append(f"set [features] memories = false in {mcp_path}")

            block = desired_toml_block()
            exp_cmd, exp_args = desired_mcp_args()
            sec = extract_toml_section(toml_text)
            if sec is None:
                prefix = toml_text.rstrip("\n")
                toml_text = f"{prefix}\n\n{block}\n" if prefix else f"{block}\n"
                changes.append(f"added {TOML_SECTION_HEADER} to {mcp_path}")
            else:
                cur_cmd, cur_args = parse_toml_mcp_section(sec[1])
                if "toml_had_previous" not in restore and cur_cmd and not is_our_mcp_entry(cur_cmd, cur_args):
                    restore["toml_had_previous"] = True
                    restore["previous_toml_section"] = sec[1]
                if cur_cmd != exp_cmd or cur_args != exp_args:
                    rebuilt_toml = strip_block(sec[0], sec[2]).rstrip("\n")
                    toml_text = f"{rebuilt_toml}\n\n{block}\n" if rebuilt_toml else f"{block}\n"
                    changes.append(f"updated {TOML_SECTION_HEADER} in {mcp_path}")
            if toml_text != orig_toml:
                atomic_write(mcp_path, toml_text)
            restore["expected_mcp_cmd"] = exp_cmd
            restore["expected_mcp_args"] = exp_args

        # 2b. Cursor hooks (deterministic read/write triggers)
        if spec.get("hooks_path") is not None:
            apply_cursor_hooks(spec["hooks_path"], restore, changes)

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
        mcp_path = spec["mcp_path"]

        if inst_path is not None and inst_path.is_file() and not inst_path.is_symlink():
            text = inst_path.read_text(encoding="utf-8")
            parsed = extract_block(text, MD_START, MD_END)
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
                if agent_id == "workbuddy" and servers[spec["mcp_key"]] != desired_json_mcp(spec) and not force:
                    if not restore:
                        return {"id": agent_id, "state": "unmanaged", "changes": ["nothing to remove"]}
                    raise RuntimeError(f"WorkBuddy 的同名服务配置已更改，请先核对 {mcp_path}")
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
            text = mcp_path.read_text(encoding="utf-8")
            orig_toml = text
            sec = extract_toml_section(text)
            if sec is not None:
                cur_cmd, cur_args = parse_toml_mcp_section(sec[1])
                exp_cmd = restore.get("expected_mcp_cmd")
                exp_args = restore.get("expected_mcp_args")
                if exp_cmd and (cur_cmd != exp_cmd or cur_args != exp_args) and not force:
                    raise RuntimeError(
                        f"TOML section {TOML_SECTION_HEADER} in {mcp_path} was edited manually; reconcile or re-run with --force."
                    )
                base = strip_block(sec[0], sec[2])
                if restore.get("toml_had_previous") and restore.get("previous_toml_section"):
                    prev_sec = restore["previous_toml_section"].strip()
                    text = f"{base.rstrip()}\n\n{prev_sec}\n" if base.strip() else f"{prev_sec}\n"
                    changes.append(f"restored previous {TOML_SECTION_HEADER} in {mcp_path}")
                else:
                    text = base
                    changes.append(f"removed {TOML_SECTION_HEADER} from {mcp_path}")
            prev_feat_mem = restore.get("previous_codex_features_memories")
            if prev_feat_mem in {"true", "false", "absent"}:
                text = set_codex_features_memories(text, prev_feat_mem)
                changes.append(f"restored [features] memories -> {prev_feat_mem} in {mcp_path}")
            if text != orig_toml:
                atomic_write(mcp_path, text)

        if spec.get("hooks_path") is not None:
            remove_cursor_hooks(spec["hooks_path"], restore, changes)

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
    libs = resolve_libraries(primary_root=MEMORY_ROOT, home=home, include_disabled=True)
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
        out.append(
            {
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

    cfg = load_mounts_config(home=home)
    if any(item.get("id") == mid for item in cfg["mounts"]):
        raise ValueError(f"Mount '{mid}' already exists. Use 'mount update' or remove it first.")
    if mpath == MEMORY_ROOT:
        raise ValueError("Mount path cannot be the same as the personal memory root.")
    if any(validate_library_path(item["path"]) == mpath for item in cfg["mounts"] if item.get("path")):
        raise ValueError(f"Path already mounted: {mpath}")

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
            "auto_push": False,
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
    cfg["mounts"].append(entry)
    save_mounts_config(cfg, home=home)
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
    home=None,
):
    """Update settings of an existing mounted shared library."""
    mid = validate_mount_id(mount_id)
    cfg = load_mounts_config(home=home)
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

    if any(v is not None for v in (remote_url, remote, branch, auto_commit)):
        cur_sync = dict(target.get("sync") or {"driver": "git", "remote": "origin", "branch": "main", "auto_commit": True, "auto_push": False})
        if remote is not None:
            cur_sync["remote"] = remote.strip() or "origin"
        if branch is not None:
            cur_sync["branch"] = branch.strip()
        if remote_url is not None:
            cur_sync["remote_url"] = remote_url.strip()
        if auto_commit is not None:
            cur_sync["auto_commit"] = bool(auto_commit)
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

    save_mounts_config(cfg, home=home)
    return {"updated": target}


def mount_remove(mount_id, home=None):
    """Unmount a shared library from mounts.json without deleting local files."""
    mid = validate_mount_id(mount_id)
    cfg = load_mounts_config(home=home)
    before = len(cfg["mounts"])
    cfg["mounts"] = [item for item in cfg["mounts"] if item.get("id") != mid]
    if len(cfg["mounts"]) == before:
        raise ValueError(f"Mount '{mid}' not found.")
    save_mounts_config(cfg, home=home)
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
    cfg = load_mounts_config(home=home)
    if library_id == "personal":
        cfg["personal_sync"] = sync_cfg
    else:
        for item in cfg["mounts"]:
            if item.get("id") == library_id:
                item["sync"] = sync_cfg
                break
    save_mounts_config(cfg, home=home)
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
    """Resolve 'all' or a specific agent id."""
    specs = agent_specs(home=home)
    if target == "all":
        return list(specs.items())
    if target not in specs:
        raise SystemExit(f"Unknown agent '{target}'. Choose from: all, {', '.join(specs)}")
    return [(target, specs[target])]


def _parse_bool_flag(val):
    s = str(val).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Expected true/false, got: {val}")


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
        if command == "search":
            item.add_argument("query")
            item.add_argument("--limit", type=int, default=10)
        if command == "read":
            item.add_argument("path")
        if command == "write":
            item.add_argument("--layer", choices=ALL_LAYERS, required=True)
            item.add_argument("--name", required=True)
            item.add_argument("--title", required=True)
            item.add_argument("--description", required=True)
            item.add_argument("--body-file", required=True, help="UTF-8 Markdown file, or - for standard input.")

    p_init = sub.add_parser("init", help="Initialize an empty private or shared library.")
    p_init.add_argument("--path", help="Optional target library path (defaults to UNIFIED_MEMORY_ROOT).")
    p_init.add_argument("--shared", action="store_true", help="Initialize as a shared library (no personal skills/user-managed).")

    sub.add_parser("install-shims", help="Create or refresh the stable launchers client configs point at.")
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

    p_conn = sub.add_parser("connect", help="Connect an agent (claude, codex, cursor, workbuddy, antigravity, or all).")
    p_conn.add_argument("target", nargs="?", default="all")
    p_conn.add_argument("--dry-run", action="store_true")

    p_disc = sub.add_parser("disconnect", help="Cleanly remove unified-memory integration from an agent.")
    p_disc.add_argument("target", nargs="?", default="all")
    p_disc.add_argument("--force", action="store_true", help="Remove even if managed blocks were manually edited.")

    p_doc = sub.add_parser("doctor", help="Diagnose index, MCP server, skills, and agent integration drift.")
    p_doc.add_argument("--fix", action="store_true", help="Automatically reconcile drifted/partial integrations.")
    p_doc.add_argument("--json", action="store_true", help="Emit JSON output.")

    args = parser.parse_args()

    if args.command in {"list", "search", "read", "write"}:
        sys.path.insert(0, str(FRAMEWORK_ROOT / "mcp"))
        from server import WorkspaceStore
        try:
            store = WorkspaceStore(MEMORY_ROOT)
            if args.command == "list":
                result = store.list_entries(library=args.library or "*", layer=args.layer, include_archived=args.archived)
            elif args.command == "search":
                result = store.search(args.query, library=args.library or "*", layer=args.layer,
                                      include_archived=args.archived, limit=args.limit)
            elif args.command == "read":
                result = store.read_entry(args.path, library=args.library or "")
            else:
                body = sys.stdin.read() if args.body_file == "-" else Path(args.body_file).read_text(encoding="utf-8")
                result = store.upsert_entry(args.layer, args.name, args.title, args.description, body,
                                            library=args.library or "personal")
        except (ValueError, OSError) as exc:
            parser.exit(1, f"{exc}\n")
        print(json.dumps(result, ensure_ascii=False, indent=2))
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
        hub = discover_hub_skills()
        print(
            json.dumps(
                {
                    "protocol_version": 4,
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


if __name__ == "__main__":
    main()
