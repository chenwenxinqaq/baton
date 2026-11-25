"""Writes must not silently lose another session's edit or the previous version."""

from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "scripts"))
sys.path.insert(0, str(CODE / "mcp"))
sys.path.insert(0, str(CODE / "tests"))

import server as mcp_server  # noqa: E402
from test_search_resilience import _store  # noqa: E402

LONG = "排查步骤与证据。" * 80


class FrozenClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 25, 12, 0, 0, 123456).astimezone(tz)


class WriteSafetyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.store = _store(self.base, "personal")

    def _write(self, name, body, **kwargs):
        return self.store.upsert_entry(
            layer=kwargs.pop("layer", "L1"),
            name=name,
            title=kwargs.pop("title", "Docker 构建缓存排查"),
            description=kwargs.pop("description", "缓存失效的根因"),
            body=body,
            **kwargs,
        )

    def test_read_reports_updated_at_for_compare_and_set(self):
        self._write("docker-cache", LONG)
        read = self.store.read_entry("methods/docker-cache.md")
        self.assertTrue(read["updated_at"])

    def test_stale_write_is_rejected_and_fresh_one_succeeds(self):
        first = self._write("docker-cache", LONG)
        with self.assertRaises(ValueError) as caught:
            self._write("docker-cache", LONG + "新结论。", expected_updated_at="2020-01-01T00:00:00+08:00")
        self.assertIn("changed since you read it", str(caught.exception))
        self.assertIn(first["updated_at"], str(caught.exception))
        again = self._write("docker-cache", LONG + "新结论。", expected_updated_at=first["updated_at"])
        self.assertEqual(again["path"], "methods/docker-cache.md")
        self.assertIn("新结论", self.store.read_entry("methods/docker-cache.md")["content"])

    def test_writes_change_the_version_even_with_a_frozen_clock(self):
        for user_managed, path in ((False, "methods"), (True, "user-managed")):
            with self.subTest(path=path), patch.object(mcp_server, "datetime", FrozenClock):
                self._write("docker-cache", LONG, user_managed=user_managed)
                read = self.store.read_entry(f"{path}/docker-cache.md")
                updated = self._write(
                    "docker-cache", "另一会话的新结论。", user_managed=user_managed,
                    expected_updated_at=read["updated_at"],
                )
                self.assertGreater(
                    datetime.fromisoformat(updated["updated_at"]), datetime.fromisoformat(read["updated_at"])
                )
                with self.assertRaisesRegex(ValueError, "Entry changed since you read it"):
                    self._write("docker-cache", LONG, user_managed=user_managed, expected_updated_at=read["updated_at"])
                self.assertIn("另一会话的新结论", self.store.read_entry(f"{path}/docker-cache.md")["content"])

    def test_layer_move_compares_the_source_and_keeps_the_replaced_version(self):
        with patch.object(mcp_server, "datetime", FrozenClock):
            first = self._write("docker-cache", LONG)
            fresh = self._write("docker-cache", "# Docker 构建缓存排查\n\n" + LONG + "最新结论。")
            with self.assertRaisesRegex(ValueError, "Entry changed since you read it"):
                self._write("docker-cache", "旧会话试图换层。", layer="L3", expected_updated_at=first["updated_at"])
            self.assertIn("最新结论", self.store.read_entry(first["path"])["content"])
            self.assertFalse((self.store.root / "projects/docker-cache.md").exists())
            self.assertEqual(self.store.entry_history("docker-cache")["total_versions"], 1)

            moved = self._write("docker-cache", "移动后只留一句。", layer="L3", expected_updated_at=fresh["updated_at"])
            self.assertEqual(moved["migrated_from"], [first["path"]])
            self.assertEqual(moved["created_at"], first["created_at"])
            self.assertGreater(datetime.fromisoformat(moved["updated_at"]), datetime.fromisoformat(fresh["updated_at"]))
            self.assertEqual(moved["previous"]["updated_at"], fresh["updated_at"])
            self.assertIn("最新结论", Path(moved["previous"]["backup"]).read_text(encoding="utf-8"))
            self.assertIn("content_warning", moved)
            self.assertFalse((self.store.root / first["path"]).exists())
            self.assertIn(moved["path"], self.store.parse_index())
            self.assertNotIn(first["path"], self.store.parse_index())
            with self.assertRaisesRegex(ValueError, "Entry changed since you read it"):
                self._write("docker-cache", "旧会话试图写回原层。", expected_updated_at=fresh["updated_at"])

    def test_conditional_write_cannot_recreate_a_deleted_entry(self):
        for user_managed in (False, True):
            with self.subTest(user_managed=user_managed):
                old = self._write("docker-cache", LONG, user_managed=user_managed)
                path = self.store.root / old["path"]
                path.unlink()
                with self.assertRaisesRegex(ValueError, "no current entry exists"):
                    self._write("docker-cache", "陈旧编辑。", user_managed=user_managed, expected_updated_at=old["updated_at"])
                self.assertFalse(path.exists())

    def test_conditional_move_refuses_duplicate_live_sources(self):
        old = self._write("docker-cache", LONG)
        (self.store.root / "projects/docker-cache.md").write_text("# 另一个版本\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "multiple layers contain this name"):
            self._write("docker-cache", "不能猜要保留哪篇。", expected_updated_at=old["updated_at"])
        self.assertTrue((self.store.root / "projects/docker-cache.md").is_file())
        self.assertEqual(self.store.entry_history("docker-cache")["total_versions"], 0)

    def test_update_reports_the_version_it_replaced(self):
        self._write("docker-cache", LONG)
        res = self._write("docker-cache", LONG + "补充一句。")
        self.assertTrue(res["previous"]["updated_at"])
        self.assertGreater(res["previous"]["chars"], 400)
        self.assertTrue(Path(res["previous"]["backup"]).is_file())
        self.assertNotIn("content_warning", res)

    def test_shrinking_write_warns_and_keeps_the_backup(self):
        self._write("docker-cache", LONG)
        res = self._write("docker-cache", "一句话。")
        self.assertIn("content_warning", res)
        backup = Path(res["previous"]["backup"]).read_text(encoding="utf-8")
        self.assertIn("排查步骤与证据", backup)

    def test_new_entry_points_at_existing_entries_on_the_same_topic(self):
        self._write("docker-cache", LONG)
        res = self._write("docker-cache-notes", LONG, layer="L3", title="Docker 构建缓存排查记录")
        refs = [item["ref"] for item in res["related"]]
        self.assertIn("methods/docker-cache.md", refs)
        self.assertIn("related", res)
        self.assertIn("one current conclusion", res["notice"])

    def test_updates_do_not_get_duplicate_hints(self):
        self._write("docker-cache", LONG)
        res = self._write("docker-cache", LONG + "再补充。")
        self.assertNotIn("related", res)

    def test_check_expected_updated_handles_missing_old_meta(self):
        first = self._write("docker-cache", LONG)
        target = self.store.root / first["path"]
        _, fallback_ts, _, _ = mcp_server.file_timestamps(target, {})
        mcp_server.MemoryStore._check_expected_updated(fallback_ts, old_meta=None, target=target)
        with self.assertRaisesRegex(ValueError, "Entry changed since you read it"):
            mcp_server.MemoryStore._check_expected_updated("2020-01-01T00:00:00+08:00", old_meta=None, target=target)

    def test_tool_schemas_keep_defaults_valid_against_enums(self):
        for tool in mcp_server.TOOLS:
            props = tool["inputSchema"].get("properties", {})
            for prop_name, schema in props.items():
                if "enum" not in schema:
                    continue
                with self.subTest(tool=tool["name"], prop=prop_name):
                    self.assertTrue(all(val != "" for val in schema["enum"]))
                    if "default" in schema:
                        self.assertIn(schema["default"], schema["enum"])

    def test_append_mode_adds_incremental_note_without_rewriting_whole_entry(self):
        first = self._write("docker-cache", "2026-11-25 首次定位到缓存键漂移。")
        second = self.store.upsert_entry(
            layer="L1",
            name="docker-cache",
            body="## 2026-11-26 补充\n\n已改为显式传入锁定哈希，构建耗时下降 40%。",
            mode="append",
        )
        self.assertEqual(second["mode"], "append")
        self.assertNotIn("content_warning", second)
        self.assertEqual(second["created_at"], first["created_at"])
        self.assertNotEqual(second["updated_at"], first["updated_at"])
        saved = self.store.read_entry("methods/docker-cache.md")
        self.assertIn("# Docker 构建缓存排查", saved["content"])
        self.assertIn("2026-11-25 首次定位到缓存键漂移。", saved["content"])
        self.assertIn("## 2026-11-26 补充", saved["content"])
        self.assertIn("构建耗时下降 40%", saved["content"])
        index_text = (self.store.root / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("[Docker 构建缓存排查](methods/docker-cache.md) — 缓存失效的根因", index_text)

    def test_append_mode_respects_expected_updated_at_and_requires_title_for_new_entry(self):
        first = self._write("docker-cache", "第一段。")
        second = self.store.upsert_entry(
            layer="L1",
            name="docker-cache",
            body="第二段。",
            mode="append",
            expected_updated_at=first["updated_at"],
        )
        with self.assertRaisesRegex(ValueError, "Entry changed since you read it"):
            self.store.upsert_entry(
                layer="L1",
                name="docker-cache",
                body="过期的追加。",
                mode="append",
                expected_updated_at=first["updated_at"],
            )
        self.assertNotEqual(second["updated_at"], first["updated_at"])
        with self.assertRaisesRegex(ValueError, "title must be a single non-empty line"):
            self.store.upsert_entry(layer="L1", name="brand-new", body="没有标题的新条目。", mode="append")

    def test_mcp_initialize_returns_write_guidance_instructions_for_all_clients(self):
        resp = mcp_server.process_one_message(
            self.store,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "claude-ai"}},
            },
        )
        instructions = resp["result"].get("instructions", "")
        self.assertIn("memory_search", instructions)
        self.assertIn("mode='append'", instructions)
        self.assertIn("MEMORY.md §0", instructions)

    def test_mcp_tool_call_supports_append_without_title_or_description(self):
        self._write("docker-cache", "初始结论。")
        res, data = mcp_server._handle_tool_call(
            self.store,
            "memory_upsert",
            {"layer": "L1", "name": "docker-cache", "body": "追加结论。", "mode": "append"},
        )
        self.assertFalse(res.get("isError", False))
        self.assertEqual(data["mode"], "append")
        text = self.store.read_entry("methods/docker-cache.md")["content"]
        self.assertIn("初始结论。", text)
        self.assertIn("追加结论。", text)

    def test_section_upsert_replaces_appends_creates_and_ignores_code_fences(self):
        body = (
            "背景摘要。\n\n"
            "## 当前进展\n\n"
            "已完成初版验证。\n\n"
            "```markdown\n"
            "## 示例代码块里的标题\n"
            "不要当成真小节。\n"
            "```\n\n"
            "## 下一步\n\n"
            "整理文档。"
        )
        self._write("docker-cache", body)

        # 1. Replace a targeted section in place while preserving sibling sections & code blocks.
        replaced = self.store.upsert_entry(
            layer="L1",
            name="docker-cache",
            body="已完成 v2 回归测试。",
            section="当前进展",
        )
        self.assertEqual(replaced["section"], "当前进展")
        self.assertEqual(replaced["section_action"], "replaced")
        saved = self.store.read_entry("methods/docker-cache.md")["content"]
        self.assertIn("已完成 v2 回归测试。", saved)
        self.assertNotIn("已完成初版验证。", saved)
        self.assertIn("## 下一步\n\n整理文档。", saved)
        self.assertIn("背景摘要。", saved)

        # 2. Append inside an existing section without touching other sections.
        appended = self.store.upsert_entry(
            layer="L1",
            name="docker-cache",
            body="2026-11-26 补充：压测通过。",
            mode="append",
            section="下一步",
        )
        self.assertEqual(appended["section"], "下一步")
        self.assertEqual(appended["section_action"], "appended")
        saved = self.store.read_entry("methods/docker-cache.md")["content"]
        self.assertIn("整理文档。\n\n2026-11-26 补充：压测通过。", saved)

        # 3. Create a new section when absent via MCP tool call.
        res, created = mcp_server._handle_tool_call(
            self.store,
            "memory_upsert",
            {"layer": "L1", "name": "docker-cache", "section": "风险与边界", "body": "仅适用 Linux 构建节点。"},
        )
        self.assertFalse(res.get("isError", False))
        self.assertEqual(created["section"], "风险与边界")
        self.assertEqual(created["section_action"], "created")
        saved = self.store.read_entry("methods/docker-cache.md")["content"]
        self.assertIn("## 风险与边界\n\n仅适用 Linux 构建节点。", saved)

        # 4. Ambiguous substring match raises ValueError rather than silently picking one.
        self.store.upsert_entry(layer="L1", name="docker-cache", section="测试方案 A", body="A")
        self.store.upsert_entry(layer="L1", name="docker-cache", section="测试方案 B", body="B")
        with self.assertRaisesRegex(ValueError, "Ambiguous section"):
            self.store.upsert_entry(layer="L1", name="docker-cache", section="测试方案", body="C")

        # 5. Fenced code block heading is not treated as an outline section.
        self.store.upsert_entry(
            layer="L1",
            name="fenced-doc",
            title="带代码块的条目",
            description="验证代码块标题不干扰分节",
            body="## 正文小节\n\n```md\n## 假标题\n```\n",
        )
        outline = self.store.read_entry("methods/fenced-doc.md", outline=True)
        self.assertEqual([s["section"] for s in outline["sections"]], ["正文小节"])

    def test_section_upsert_works_on_user_managed_entries(self):
        self.store.upsert_entry(
            layer="L2",
            name="my-pref",
            title="我的偏好",
            description="长期工作习惯",
            body="## 沟通\n\n先给结论。\n\n## 输出\n\n保持简洁。",
            user_managed=True,
        )
        updated = self.store.upsert_entry(
            layer="L2",
            name="my-pref",
            body="多用表格对比。",
            mode="append",
            section="输出",
            user_managed=True,
        )
        self.assertEqual(updated["section_action"], "appended")
        content = self.store.read_entry("user-managed/my-pref.md")["content"]
        self.assertIn("保持简洁。\n\n多用表格对比。", content)
        self.assertIn("## 沟通\n\n先给结论。", content)


if __name__ == "__main__":
    unittest.main()
