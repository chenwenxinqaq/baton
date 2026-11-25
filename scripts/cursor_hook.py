#!/usr/bin/env python3
"""Client hook entrypoint that makes unified-memory read/write habits deterministic.

Supports both Cursor's flat hook protocol (`sessionStart` / `beforeSubmitPrompt` / `stop`)
and the Claude Code / Codex nested hook protocol (`SessionStart` / `UserPromptSubmit` /
`PostToolUse` / `Stop`).

Events:
  sessionStart / SessionStart          -> inject a compact memory digest + usage contract
  beforeSubmitPrompt / UserPromptSubmit -> remember when the current user turn started
  PostToolUse                          -> count tool activity so short high-intensity turns
                                          still trigger a memory check
  stop / Stop                          -> after a substantial turn that wrote nothing, ask the
                                          agent once to run the MEMORY.md section 0 write check
                                          (never loops, cooldown applies)

Design rules: fail open (always exit 0, valid JSON), no network, no entry bodies in logs,
finish well under a second.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_log  # noqa: E402
from library_paths import global_state_dir  # noqa: E402

NUDGE_MARKER = "【记忆沉淀检查】"
MIN_TURN_SECONDS = int(os.environ.get("UNIFIED_MEMORY_NUDGE_MIN_SECONDS", "90"))
MIN_TOOL_CALLS = int(os.environ.get("UNIFIED_MEMORY_NUDGE_MIN_TOOLS", "4"))
COOLDOWN_SECONDS = int(os.environ.get("UNIFIED_MEMORY_NUDGE_COOLDOWN", "300"))
NUDGE_ENABLED = os.environ.get("UNIFIED_MEMORY_NUDGE", "1") not in {"0", "false", "off"}

CLIENT_PROFILES = {
    "cursor": ("Cursor", "cursor"),
    "claude": ("Claude Code", "nested"),
    "claude-code": ("Claude Code", "nested"),
    "codex": ("Codex", "nested"),
}

NUDGE_MESSAGE = (
    f"{NUDGE_MARKER}请按 unified_memory 的 MEMORY.md §0 复查本轮对话："
    "是否产生了可跨会话复用的根因/踩坑/方法、用户长期偏好或约束、项目关键决策或进展？"
    "若有：先 memory_search 同主题，再用 memory_upsert 保存"
    "（已有条目沿用原 name，局部补充可直接用 mode='append'）；"
    "若没有：只回复一句“本轮无需沉淀”，不要做其他操作。"
)


def state_dir():
    path = global_state_dir() / "hook-state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_id(value):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(value or "unknown"))[:80]


def load_state(conv_id):
    path = state_dir() / f"{safe_id(conv_id)}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(conv_id, data):
    path = state_dir() / f"{safe_id(conv_id)}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def prune_state(max_age=14 * 86400):
    cutoff = time.time() - max_age
    try:
        for item in state_dir().glob("*.json"):
            if item.stat().st_mtime < cutoff:
                item.unlink(missing_ok=True)
    except OSError:
        pass


def index_digest(memory_root, max_lines_per_layer=6):
    """Return compact lines from MEMORY.md for L0/L1/L2/L3 (titles + one-line descriptions)."""
    index = Path(memory_root) / "MEMORY.md"
    try:
        text = index.read_text(encoding="utf-8")
    except OSError:
        return []
    wanted = {
        "L0": ("约束", max_lines_per_layer),
        "L1": ("方法", min(4, max_lines_per_layer)),
        "L2": ("偏好", max_lines_per_layer),
        "L3": ("项目", max_lines_per_layer),
    }
    out, current, counts = [], None, {}
    for line in text.splitlines():
        header = re.match(r"^##\s+.*?\b(L[0-4])\b", line)
        if header:
            current = header.group(1)
            continue
        if line.startswith("## "):
            current = None
            continue
        if current in wanted and line.lstrip().startswith("- "):
            label, limit = wanted[current]
            counts[current] = counts.get(current, 0) + 1
            if counts[current] <= limit:
                out.append(f"[{current}{label}] {line.lstrip()[2:].strip()[:140]}")
    return out


def build_context(memory_root):
    lines = [
        "【Unified Memory】本机跨 Agent 统一记忆库已通过 MCP 服务 `unified_memory` 接入"
        "（memory_search / memory_read / memory_upsert / memory_archive / memory_libraries）。",
        "- 任务开始、或涉及用户偏好/历史决策/项目背景时：先 memory_search(compact=true) 再用 memory_read(paths=...) 批量核对。",
        "- 核实根因、踩坑、方法、偏好或项目进展后及时 memory_upsert（已有条目局部补充可直接用 mode='append'）；最终回复前再查一次是否遗漏。",
        "- 不要把一次性过程、密钥、凭据写入记忆。",
    ]
    digest = index_digest(memory_root)
    if digest:
        lines.append("当前库索引摘录：")
        lines.extend(f"  {d}" for d in digest)
    return "\n".join(lines)


def _resolve_client(client, event):
    key = (client or "").strip().lower()
    if key in CLIENT_PROFILES:
        return key, *CLIENT_PROFILES[key]
    if event in {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"}:
        return "codex", "Codex", "nested"
    return "cursor", "Cursor", "cursor"


def _normalize_event(event):
    mapping = {
        "sessionstart": "session_start",
        "beforesubmitprompt": "prompt_submit",
        "userpromptsubmit": "prompt_submit",
        "posttooluse": "post_tool_use",
        "stop": "stop",
    }
    return mapping.get(str(event or "").strip().lower(), "")


def handle(event, payload, memory_root, client="cursor"):
    client_key, client_name, protocol = _resolve_client(client, event)
    kind = _normalize_event(event)
    conv_id = payload.get("conversation_id") or payload.get("session_id")
    state_key = f"{client_key}_{conv_id or 'unknown'}"

    if kind == "session_start":
        if protocol == "nested" and payload.get("agent_id"):
            return {}
        prune_state()
        context = build_context(memory_root)
        audit_log.record(
            {
                "source": "hook",
                "client": client_name,
                "event": "sessionStart",
                "conversation_id": safe_id(conv_id),
                "bytes": len(context.encode("utf-8")),
                "tokens": audit_log.estimate_tokens(context),
            }
        )
        if protocol == "nested":
            return {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            }
        return {"additional_context": context}

    if kind == "prompt_submit":
        if protocol == "nested" and payload.get("agent_id"):
            return {}
        prompt = str(payload.get("prompt") or "")
        if prompt.startswith(NUDGE_MARKER):
            return {}
        state = load_state(state_key)
        state["prompt_t"] = time.time()
        state["tool_calls"] = 0
        save_state(state_key, state)
        return {}

    if kind == "post_tool_use":
        if payload.get("agent_id"):
            return {}
        state = load_state(state_key)
        if not state.get("prompt_t"):
            state["prompt_t"] = time.time()
        state["tool_calls"] = int(state.get("tool_calls") or 0) + 1
        save_state(state_key, state)
        return {}

    if kind == "stop":
        if not NUDGE_ENABLED:
            return {}
        if protocol == "cursor":
            if payload.get("status") != "completed" or int(payload.get("loop_count") or 0) > 0:
                return {}
        else:
            if payload.get("stop_hook_active") or payload.get("agent_id"):
                return {}
            last_msg = str(payload.get("last_assistant_message") or "")
            if NUDGE_MARKER in last_msg or "本轮无需沉淀" in last_msg:
                return {}
        state = load_state(state_key)
        prompt_t = state.get("prompt_t")
        now = time.time()
        if not prompt_t:
            return {}
        elapsed = now - prompt_t
        tool_calls = int(state.get("tool_calls") or 0)
        if elapsed < MIN_TURN_SECONDS and tool_calls < MIN_TOOL_CALLS:
            return {}
        if now - state.get("nudge_t", 0) < COOLDOWN_SECONDS:
            return {}
        if audit_log.has_write_since(prompt_t, client=client_name):
            return {}
        state["nudge_t"] = now
        state["tool_calls"] = 0
        save_state(state_key, state)
        audit_log.record(
            {
                "source": "hook",
                "client": client_name,
                "event": "nudge",
                "conversation_id": safe_id(conv_id),
                "turn_seconds": int(elapsed),
                "tool_calls": tool_calls,
            }
        )
        if protocol == "nested":
            return {"decision": "block", "reason": NUDGE_MESSAGE}
        return {"followup_message": NUDGE_MESSAGE}

    return {}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("event")
    parser.add_argument("--memory-root", default=os.environ.get("UNIFIED_MEMORY_ROOT", ""))
    parser.add_argument("--client", default="")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        result = handle(
            args.event,
            payload if isinstance(payload, dict) else {},
            args.memory_root,
            client=args.client or ("cursor" if args.event in {"sessionStart", "beforeSubmitPrompt", "stop"} else "codex"),
        )
    except Exception:
        result = {}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
