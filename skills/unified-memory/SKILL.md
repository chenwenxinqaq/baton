---
name: unified-memory
description: 开始任务前查阅当前用户的 L0–L4 记忆库，按 §0 判断哪些结论值得记忆，回复前保存本轮已验证的长期结论。
---

使用客户端接入指令指定的私有库。通过 `memory_read` 读取 `MEMORY.md` 的 §0，按其中的触发条件、分层和写作要求维护条目。MCP 不可用时从同一目录读取 Markdown。

任务开始先用 `memory_search` 查相关主题，再读命中原文。写入前核对已有条目和证据，沿用同主题的名称。用 `memory_upsert` 保存完整的更新后正文。可用 `memory_read` 查阅 `templates/entry.md`、`templates/project.md` 或 `templates/incident.md`。

事实一经核实就写，事故等根因定位后归档。最终回复前按 §0 再检查一次，命中写入条件就先保存。仅在用户明确要求时修改 `user-managed/`。
