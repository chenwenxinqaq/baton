"""The desktop UI must expose the same version history the agents can restore."""

from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "manager"))
sys.path.insert(0, str(CODE / "scripts"))
sys.path.insert(0, str(CODE / "mcp"))

import server as mcp_server  # noqa: E402
from manager import Entry, InvalidEntry, Library, render_topic_versions_html  # noqa: E402
from test_write_safety import FrozenClock  # noqa: E402


class VersionHistoryUITests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        # The manager and the MCP server both derive the trash directory from the
        # library path, so pinning the state root keeps this test off the real one.
        previous = os.environ.get("UNIFIED_MEMORY_STATE_DIR")
        os.environ["UNIFIED_MEMORY_STATE_DIR"] = str(self.base / "state")
        self.addCleanup(self._restore_env, previous)

        self.root = self.base / "lib"
        self.root.mkdir()
        (self.root / "MEMORY.md").write_text(
            "# M\n\n## L0\n\n## L1\n\n## L2\n\n## L3\n\n## L4\n", encoding="utf-8"
        )
        for folder in ("constraints", "methods", "preferences", "projects", "archive", "user-managed"):
            (self.root / folder).mkdir()
        self.store = mcp_server.MemoryStore(self.root, library_id="personal")
        self.library = Library(self.root, "demo")

    @staticmethod
    def _restore_env(previous):
        if previous is None:
            os.environ.pop("UNIFIED_MEMORY_STATE_DIR", None)
        else:
            os.environ["UNIFIED_MEMORY_STATE_DIR"] = previous

    def _write(self, body):
        return self.store.upsert_entry(
            layer="L1",
            name="docker-cache",
            title="Docker 构建缓存排查",
            description="缓存失效的根因",
            body=body,
        )

    def test_ui_sees_the_backups_the_agent_side_wrote(self):
        self._write("第一版结论。")
        self.assertEqual(self.library.topic_versions("docker-cache"), [])
        self._write("第二版结论。")
        self._write("第三版结论。")

        versions = self.library.topic_versions("docker-cache")
        self.assertEqual([v["reason"] for v in versions], ["update", "update"])
        self.assertGreaterEqual(versions[0]["saved_at"], versions[-1]["saved_at"])

    def test_restoring_from_the_ui_keeps_the_replaced_content_too(self):
        self._write("第一版结论。")
        self._write("第二版结论。")
        oldest = self.library.topic_versions("docker-cache")[-1]["version"]

        path = self.library.restore_version(oldest)
        self.assertEqual(path, "methods/docker-cache.md")
        current = (self.root / "methods" / "docker-cache.md").read_text(encoding="utf-8")
        self.assertIn("第一版结论", current)
        # Restoring backs up what it replaced, so the list grows instead of losing it.
        self.assertEqual(len(self.library.topic_versions("docker-cache")), 2)

    def test_desktop_edits_invalidate_stale_agent_versions(self):
        with patch.object(mcp_server, "datetime", FrozenClock):
            first = self._write("第一版结论。")
            current = self.library.read_topic(first["path"])
            self.library.save_topic(current.path, current.revision, "L1", current.title,
                                    current.description, "# Docker 构建缓存排查\n\n桌面改写。")
            # A different client can have a good hash guard and still must advance
            # the timestamp the agent used as its version token.
            with self.assertRaisesRegex(ValueError, "Entry changed since you read it"):
                self.store.upsert_entry("L1", current.slug, current.title, current.description,
                                        "旧会话改写。", expected_updated_at=first["updated_at"])

            name = "handwritten"
            self.library.save(Entry(name=name, title="手记", description="手记", body="原文"))
            current = self.library.read(name)
            self.library.save(replace(current, body="桌面改写。"))
            with self.assertRaisesRegex(ValueError, "Entry changed since you read it"):
                self.store.upsert_entry("L2", name, current.title, current.description, "旧会话改写。",
                                        user_managed=True, expected_updated_at=current.updated_at)

    def test_unknown_or_malformed_slugs_list_nothing(self):
        self._write("只写了一次。")
        self.assertEqual(self.library.topic_versions("../escape"), [])
        self.assertEqual(self.library.topic_versions(""), [])
        self.assertEqual(self.library.topic_versions("never-written"), [])

    def test_read_only_mounts_refuse_to_restore(self):
        self._write("第一版结论。")
        self._write("第二版结论。")
        version = self.library.topic_versions("docker-cache")[0]["version"]
        readonly = Library(self.root, "shared", library_id="team", role="shared", mode="ro")
        with self.assertRaises(InvalidEntry):
            readonly.restore_version(version)

    def test_only_writable_libraries_render_a_restore_button(self):
        versions = [
            {
                "version": "20261125-113246-docker-cache-59bc.md",
                "saved_at": "2026-11-25T11:32:46",
                "reason": "update",
                "chars": 157,
            }
        ]
        writable = render_topic_versions_html("tok", "primary", "docker-cache", versions, False)
        self.assertIn("历史版本", writable)
        self.assertIn("2026-11-25 11:32:46", writable)
        self.assertIn("编辑前备份", writable)
        self.assertIn("157 字", writable)
        self.assertIn('action="/restore_version#feedback"', writable)

        readonly = render_topic_versions_html("tok", "team", "docker-cache", versions, True)
        self.assertIn("历史版本", readonly)
        self.assertNotIn("/restore_version", readonly)

    def test_nothing_renders_when_there_is_no_history(self):
        self.assertEqual(render_topic_versions_html("tok", "primary", "docker-cache", [], False), "")

    def test_verify_topic_sets_today_and_clears_expired_valid_until(self):
        self.store.upsert_entry(
            "L3",
            "release-window",
            "发布窗口",
            "当前发布窗口状态",
            "待核实。",
            verified_at="2020-01-01",
            valid_until="2020-02-01",
        )
        rel = self.library.verify_topic("projects/release-window.md")
        self.assertEqual(rel, "projects/release-window.md")
        updated = self.library.read_topic(rel)
        self.assertRegex(updated.verified_at, r"^\d{4}-\d{2}-\d{2}$")
        self.assertNotEqual(updated.verified_at, "2020-01-01")
        self.assertEqual(updated.valid_until, "")

        # A future valid_until must stay intact when verifying today.
        self.store.upsert_entry(
            "L3",
            "future-window",
            "未来窗口",
            "长期有效状态",
            "正文。",
            verified_at="2020-01-01",
            valid_until="2099-12-31",
        )
        self.library.verify_topic("projects/future-window.md")
        kept = self.library.read_topic("projects/future-window.md")
        self.assertEqual(kept.valid_until, "2099-12-31")

    def test_topic_links_and_actionable_lint_section_render_safely(self):
        import manager as manager_mod

        self.store.upsert_entry("L1", "base-guide", "基础指南", "基础方法摘要", "核心步骤。")
        self.store.upsert_entry(
            "L3",
            "rollout-plan",
            "上线计划",
            "参考基础指南执行",
            "遵循 methods/base-guide.md 完成上线。",
            verified_at="2020-01-01",
            valid_until="2020-01-02",
        )
        links, backlinks = self.library.topic_links("projects/rollout-plan.md", "遵循 methods/base-guide.md 完成上线。")
        self.assertEqual([x["ref"] for x in links], ["methods/base-guide.md"])
        self.assertEqual(backlinks, [])
        base_links, base_backlinks = self.library.topic_links("methods/base-guide.md", "核心步骤。")
        self.assertEqual(base_links, [])
        self.assertEqual([x["ref"] for x in base_backlinks], ["projects/rollout-plan.md"])

        links_html = manager_mod.render_topic_links_html("primary", links, base_backlinks)
        self.assertIn("关联记忆", links_html)
        self.assertIn("引用了 (1)", links_html)
        self.assertIn("被引用 (1)", links_html)
        self.assertIn("methods/base-guide.md", links_html)
        self.assertIn("projects/rollout-plan.md", links_html)

        writable_lint = manager_mod._lint_section("primary", self.root, token="tok", readonly=False, return_view="activity")
        self.assertIn('action="/verify_topic#feedback"', writable_lint)
        self.assertIn("今日核实", writable_lint)
        self.assertIn("去编辑", writable_lint)

        ro_lint = manager_mod._lint_section("team", self.root, token="tok", readonly=True, return_view="activity")
        self.assertNotIn("/verify_topic", ro_lint)
        self.assertNotIn("/rebuild_index", ro_lint)

    def test_quick_search_returns_recent_items_matches_and_actions(self):
        from types import SimpleNamespace
        import manager as manager_mod

        self.store.upsert_entry(
            "L1",
            "docker-cache",
            "Docker 缓存清理方法",
            "定期清理 buildx 缓存避免磁盘占满",
            "执行 docker builder prune 清理缓存。",
        )
        self.store.upsert_entry(
            "L3",
            "expired-note",
            "临时环境配置",
            "过期快照测试",
            "仅用于测试。",
            verified_at="2020-01-01",
            valid_until="2020-01-02",
        )
        captured = {}
        handler = object.__new__(manager_mod.Handler)
        handler.server = SimpleNamespace(libraries={"primary": self.library})
        handler.respond_json = lambda status, payload: captured.update(status=status, payload=payload)

        handler.handle_quick_search({"library": ["primary"], "q": [""]})
        self.assertEqual(captured["status"], 200)
        refs = [item["ref"] for item in captured["payload"]["items"]]
        self.assertIn("methods/docker-cache.md", refs)
        self.assertIn("projects/expired-note.md", refs)
        expired_item = next(item for item in captured["payload"]["items"] if item["ref"] == "projects/expired-note.md")
        self.assertTrue(expired_item["expired"])
        self.assertTrue(any(a["title"] == "新建记忆" for a in captured["payload"]["actions"]))

        captured.clear()
        handler.handle_quick_search({"library": ["primary"], "q": ["Docker 缓存"]})
        self.assertEqual(captured["status"], 200)
        self.assertEqual(captured["payload"]["items"][0]["ref"], "methods/docker-cache.md")
        self.assertIn("topic=methods%2Fdocker-cache.md", captured["payload"]["items"][0]["href"])


if __name__ == "__main__":
    unittest.main()
