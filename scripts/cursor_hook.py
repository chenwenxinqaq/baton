#!/usr/bin/env python3
"""Cursor hook entrypoint that makes unified-memory read/write habits deterministic.

Events (installed by `memoryctl connect cursor`):
  sessionStart       -> inject a compact memory digest + usage contract as additional_context
  beforeSubmitPrompt -> remember when the current user turn started (no output)
  stop               -> after a substantial turn that wrote nothing, ask the agent once to run
                        the MEMORY.md section 0 write check (never loops, cooldown applies)

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
MIN_TURN_SECONDS = int(os.environ.get("UNIFIED_MEMORY_NUDGE_MIN_SECONDS", "120"))
COOLDOWN_SECONDS = int(os.environ.get("UNIFIED_MEMORY_NUDGE_COOLDOWN", "600"))
NUDGE_ENABLED = os.environ.get("UNIFIED_MEMORY_NUDGE", "1") not in {"0", "false", "off"}

NUDGE_MESSAGE = (
    f"{NUDGE_MARKER}请按 unified_memory 的 MEMORY.md §0 复查本轮对话："
    "是否产生了可跨会话复用的根因/方法、用户长期偏好或约束、项目关键决策？"
    "若有：先 memory_search 同主题，再用 memory_upsert 保存（已有条目沿用原 name 更新）；"
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
    """Return compact lines from MEMORY.md for L0/L2/L3 (titles + one-line descriptions)."""
    index = Path(memory_root) / "MEMORY.md"
    try:
        text = index.read_text(encoding="utf-8")
    except OSError:
        return []
    wanted = {"L0": "约束", "L2": "偏好", "L3": "项目"}
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
            counts[current] = counts.get(current, 0) + 1
            if counts[current] <= max_lines_per_layer:
                out.append(f"[{current}{wanted[current]}] {line.lstrip()[2:].strip()[:140]}")
    return out


def build_context(memory_root):
    lines = [
        "【Unified Memory】本机跨 Agent 统一记忆库已通过 MCP 服务 `unified_memory` 接入"
        "（memory_search / memory_read / memory_upsert / memory_archive / memory_libraries）。",
        "- 任务开始、或涉及用户偏好/历史决策/项目背景时：先 memory_search 再动手。",
        "- 最终回复前：按 MEMORY.md §0 检查本轮是否产生值得长期保存的结论，命中则 memory_upsert；没有就不要提。",
        "- 不要把一次性过程、密钥、凭据写入记忆。",
    ]
    digest = index_digest(memory_root)
    if digest:
        lines.append("当前库索引摘录：")
        lines.extend(f"  {d}" for d in digest)
    return "\n".join(lines)


def handle(event, payload, memory_root):
    conv_id = payload.get("conversation_id") or payload.get("session_id")

    if event == "sessionStart":
        prune_state()
        audit_log.record({"source": "hook", "client": "Cursor", "event": "sessionStart", "conversation_id": safe_id(conv_id)})
        return {"additional_context": build_context(memory_root)}

    if event == "beforeSubmitPrompt":
        prompt = str(payload.get("prompt") or "")
        if prompt.startswith(NUDGE_MARKER):
            return {}
        state = load_state(conv_id)
        state["prompt_t"] = time.time()
        save_state(conv_id, state)
        return {}

    if event == "stop":
        if not NUDGE_ENABLED:
            return {}
        if payload.get("status") != "completed" or int(payload.get("loop_count") or 0) > 0:
            return {}
        state = load_state(conv_id)
        prompt_t = state.get("prompt_t")
        now = time.time()
        if not prompt_t or now - prompt_t < MIN_TURN_SECONDS:
            return {}
        if now - state.get("nudge_t", 0) < COOLDOWN_SECONDS:
            return {}
        if audit_log.has_write_since(prompt_t):
            return {}
        state["nudge_t"] = now
        save_state(conv_id, state)
        audit_log.record(
            {"source": "hook", "client": "Cursor", "event": "nudge", "conversation_id": safe_id(conv_id), "turn_seconds": int(now - prompt_t)}
        )
        return {"followup_message": NUDGE_MESSAGE}

    return {}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("event")
    parser.add_argument("--memory-root", default=os.environ.get("UNIFIED_MEMORY_ROOT", ""))
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        result = handle(args.event, payload if isinstance(payload, dict) else {}, args.memory_root)
    except Exception:
        result = {}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
