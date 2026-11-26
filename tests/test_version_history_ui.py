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

    def test_web_edits_layer_moves_and_archives_record_shared_history(self):
        first = self._write("初版内容。")
        cur = self.library.read_topic(first["path"])
        self.library.save_topic(
            cur.path, cur.revision, "L1", cur.title, cur.description, "# Docker 构建缓存排查\n\nWeb 修订版。"
        )
        cur2 = self.library.read_topic(first["path"])
        moved = self.library.save_topic(
            cur2.path, cur2.revision, "L3", cur2.title, cur2.description, "# Docker 构建缓存排查\n\n移至 L3。"
        )
        self.assertEqual(moved, "projects/docker-cache.md")
        cur3 = self.library.read_topic(moved)
        archived = self.library.archive_topic(cur3.path, cur3.revision)
        self.assertEqual(archived, "archive/docker-cache.md")

        reasons = {v["reason"] for v in self.store.entry_history("docker-cache")["versions"]}
        self.assertEqual(reasons, {"update", "layer-change", "archive"})

        self.library.save(Entry(name="ui-note", title="手记", description="初版", body="手记一"))
        note = self.library.read("ui-note")
        self.library.save(replace(note, body="手记二"))
        user_versions = self.store.entry_history("ui-note")["versions"]
        self.assertEqual(len(user_versions), 1)
        self.assertEqual(user_versions[0]["reason"], "update")

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
        self.assertIn("library=team&amp;topic=projects%2Frollout-plan.md", ro_lint)
        self.assertNotIn("library=primary&amp;topic=projects%2Frollout-plan.md", ro_lint)

        user_link = manager_mod._ref_link("user-managed/personal-note.md", "team")
        self.assertIn("library=team&amp;entry=personal-note", user_link)
        self.assertNotIn("topic=user-managed", user_link)

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

    def test_activity_panel_promotes_urgent_lint_and_shows_pipeline_strip(self):
        import audit_log
        import manager as manager_mod

        self.store.upsert_entry(
            "L3",
            "expired-alert",
            "过期配置提醒",
            "需要尽快复核的临时配置",
            "仅测试用。",
            verified_at="2020-01-01",
            valid_until="2020-01-02",
        )
        audit_log.record(
            {
                "source": "mcp",
                "tool": "memory_upsert",
                "ok": True,
                "mode": "append",
                "ms": 38,
                "ref": "projects/expired-alert.md",
                "client": "Codex",
            },
        )
        audit_log.record({"source": "hook", "event": "checkpoint", "client": "Codex"})
        panel = manager_mod.render_activity_panel(
            "primary",
            agent_home=self.root,
            root=self.root,
            token="tok",
            readonly=False,
            return_view="activity",
        )
        self.assertIn("lint-urgent", panel)
        self.assertIn("pipeline-strip", panel)
        self.assertIn("增量追加", panel)
        self.assertLess(panel.index("记忆库体检"), panel.index("近 7 天读写趋势"))

    def test_desktop_results_surfaces_layer_pills_directly(self):
        import manager as manager_mod

        html_out = manager_mod.desktop_results(
            "<span>L0</span>",
            "<span>全部</span>",
            "<ul><li>item</li></ul>",
            2,
            "知识库记忆",
        )
        self.assertIn('class="desk-pills"', html_out)
        self.assertIn('class="desk-sources"', html_out)
        self.assertNotIn("<details", html_out)

    def test_markdown_toc_code_copy_expired_badge_and_live_preview(self):
        import types
        import manager as manager_mod

        md = (
            "## 背景\n\n说明。\n\n"
            "## 构建命令\n\n```bash\necho hello\n```\n\n"
            "## 验证清单\n\n- 全部通过\n"
        )
        rendered = manager_mod.render_markdown_html(
            md,
            "methods/demo.md",
            self.root,
            {},
            {"library": "primary"},
        )
        self.assertIn('class="doc-toc"', rendered)
        self.assertIn('href="#sec-1"', rendered)
        self.assertIn('id="sec-2"', rendered)
        self.assertIn('class="code-wrap"', rendered)
        self.assertIn('class="code-lang">bash</span>', rendered)
        self.assertIn("data-copy-code", rendered)

        self.store.upsert_entry(
            "L3",
            "expiring-card",
            "即将过期的卡片",
            "测试列表过期标记",
            "正文内容。",
            verified_at="2020-01-01",
            valid_until="2020-01-02",
        )
        lib = manager_mod.Library(self.root, "个人库")
        topic = lib.read_topic("projects/expiring-card.md")
        card_html = manager_mod.entry_link(topic, "/?topic=projects/expiring-card.md", False, False)
        self.assertIn("entry-tag-expired", card_html)
        self.assertIn("已过期", card_html)

        captured = {}
        handler = manager_mod.Handler.__new__(manager_mod.Handler)
        handler.server = types.SimpleNamespace(libraries={"primary": lib})
        handler.respond_json = lambda status, payload: captured.update(status=status, payload=payload)

        fake_tok = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        handler.handle_preview_markdown(
            {
                "library": "primary",
                "title": "预览草稿",
                "description": "说明",
                "body": f"## 小节\n\ntoken={fake_tok}\n",
            }
        )
        self.assertEqual(captured["status"], 200)
        self.assertIn("<h2", captured["payload"]["html"])
        self.assertGreater(captured["payload"]["chars"], 0)
        self.assertGreater(captured["payload"]["tokens"], 0)
        self.assertIn("明文凭据", captured["payload"]["secret_warning"])

    def test_version_diff_endpoint_and_button(self):
        import types
        import manager as manager_mod

        self._write("第一版步骤：\n- 运行构建\n")
        self._write("第二版步骤：\n- 运行构建\n- 校验签名\n")
        versions = self.library.topic_versions("docker-cache")
        self.assertEqual(len(versions), 1)
        ver = versions[0]["version"]

        html_out = render_topic_versions_html("tok", "primary", "docker-cache", versions, False)
        self.assertIn("data-version-diff", html_out)
        self.assertIn("对比差异", html_out)
        self.assertIn('class="version-diff-slot"', html_out)

        captured = {}
        handler = manager_mod.Handler.__new__(manager_mod.Handler)
        handler.server = types.SimpleNamespace(libraries={"primary": self.library})
        handler.respond_json = lambda status, payload: captured.update(status=status, payload=payload)

        handler.handle_version_diff({"library": ["primary"], "version": [ver]})
        self.assertEqual(captured["status"], 200)
        payload = captured["payload"]
        self.assertFalse(payload["identical"])
        self.assertGreaterEqual(payload["added"], 1)
        self.assertTrue(any("+第二版步骤：" in ln or "+- 校验签名" in ln for ln in payload["diff_lines"]))

        captured.clear()
        handler.handle_version_diff({"library": ["primary"], "version": ["../evil.md"]})
        self.assertEqual(captured["status"], 400)
        self.assertIn("error", captured["payload"])

    def test_inline_code_memory_refs_and_root_relative_links_are_clickable(self):
        import manager as manager_mod

        self._write("Docker 缓存清理步骤。")
        self.library.save(
            manager_mod.Entry(
                name="my-note",
                title="个人手记",
                description="说明",
                entry_type="user",
                layer="L2",
                status="active",
                body="个人手记正文",
            )
        )
        _, topics = self.library.read_index()
        md = (
            "参见 `methods/docker-cache.md`、`user-managed/my-note.md`、`MEMORY.md`，"
            "以及不存在的 `methods/missing.md` 和 [直接引用](methods/docker-cache.md)。"
        )
        rendered = manager_mod.render_markdown_html(
            md,
            "projects/caller.md",
            self.library.root,
            topics,
            {"library": "primary"},
        )
        self.assertIn('class="inline-code-link"', rendered)
        self.assertIn("topic=methods%2Fdocker-cache.md#detail-title", rendered)
        self.assertIn("entry=my-note#detail-title", rendered)
        self.assertIn("view=index#detail-title", rendered)
        # Non-existent path must stay plain code, not a broken link.
        self.assertIn('<code class="inline-code">methods/missing.md</code>', rendered)
        self.assertNotIn("topic=methods%2Fmissing.md", rendered)

    def test_clients_panel_guides_stray_memories_and_detected_unmanaged_agents(self):
        import manager as manager_mod
        import memoryctl

        fake_home = self.base / "agent-home"
        stray_dir = fake_home / ".claude" / "projects" / "-demo" / "memory"
        stray_dir.mkdir(parents=True)
        (stray_dir / "old-note.md").write_text("# Old Claude Note\nRemember this.\n", encoding="utf-8")
        (fake_home / ".cursor").mkdir(parents=True)

        # Avoid picking up host-level /Applications/Claude.app or WorkBuddy.app during test
        orig_specs = memoryctl.agent_specs

        def isolated_specs(home=None):
            specs = orig_specs(home=home)
            for spec in specs.values():
                spec["detect_paths"] = [p for p in spec.get("detect_paths", []) if not str(p).startswith("/Applications/")]
            return specs

        with patch.object(memoryctl, "agent_specs", side_effect=isolated_specs):
            detected_ids = [aid for aid, _ in memoryctl.resolve_targets("detected", home=fake_home)]
            html = manager_mod.render_agent_panel(
                "tok-123",
                "primary",
                agent_home=fake_home,
                library=self.library,
                flat=True,
            )
        self.assertIn("claude", detected_ids)
        self.assertIn("cursor", detected_ids)
        self.assertNotIn("workbuddy", detected_ids)
        self.assertIn("发现 1 份未归集的 Claude Code 散落记忆", html)
        self.assertIn("一键导入散落记忆", html)
        self.assertIn("检测到本机已安装但尚未接入的客户端", html)
        self.assertIn('name="target" value="detected"', html)
        self.assertIn("一键接管已安装客户端", html)
        self.assertIn("本机已安装", html)


if __name__ == "__main__":
    unittest.main()
