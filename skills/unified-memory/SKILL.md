---
name: unified-memory
description: 开始任务前查阅当前用户的 L0–L4 记忆库，按 §0 判断哪些结论值得记忆，回复前保存本轮已验证的长期结论。
---

使用客户端接入指令指定的私有库。通过 `memory_read` 读取 `MEMORY.md` 的 §0，按其中的触发条件、分层和写作要求维护条目。MCP 不可用时从同一目录读取 Markdown。

先用 `memory_search` 查相关主题；工具支持时，初查用 `compact=true`，按返回的 `ref` 读原文。问题涉及多个事实时，`see_also` 只是待读线索；若只找到一部分，用缺失主题的关键词另搜（可先取 3 条），不要拿一条命中回答整题。长条目先 `outline=true` 看小节、再 `section=` 读需要的那节。写入前核对已有条目和证据，沿用同主题的名称。用 `memory_upsert` 保存完整的更新后正文；正文不含 frontmatter 和一级标题，二者由服务端生成。更新时回传读到的 `updated_at` 作为 `expected_updated_at`；指向另一篇时写 `methods/xxx.md` 这样的库内路径，读的时候会返回 `links` 和 `backlinks`；返回 `related` 说明可能撞上同主题条目，返回 `content_warning` 说明这次删掉了大段内容，可用 `memory_history` 和 `memory_restore` 找回上一版。可用 `memory_read` 查阅 `templates/entry.md`、`templates/project.md` 或 `templates/incident.md`。

事实一经核实就写，事故等根因定位后归档。最终回复前按 §0 再检查一次，命中写入条件就先保存。仅在用户明确要求时修改 `user-managed/`。
