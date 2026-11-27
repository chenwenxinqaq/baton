"""Tests for cross-client Session Handoff packs across MCP, CLI, hooks, and UI."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "scripts"))
sys.path.insert(0, str(CODE / "mcp"))
sys.path.insert(0, str(CODE / "manager"))

import cursor_hook  # noqa: E402
import handoff  # noqa: E402
from manager import Library, render_handoff_panel  # noqa: E402


class SessionHandoffTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.root = self.base / "library"
        self.root.mkdir()
        for folder in ("constraints", "methods", "preferences", "projects", "archive", "user-managed"):
            (self.root / folder).mkdir()
        (self.root / "MEMORY.md").write_text(
            "# M\n\n## L0\n\n## L1\n\n## L2\n\n## L3\n\n## L4\n",
            encoding="utf-8",
        )
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.addCleanup(self._restore_home)
        self.library = Library(self.root, "个人库")
        self.store = self.library.store

    def _restore_home(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home

    def _env(self, **extra):
        return {
            **os.environ,
            "HOME": str(self.home),
            "UNIFIED_MEMORY_ROOT": str(self.root),
            "PYTHONDONTWRITEBYTECODE": "1",
            **extra,
        }

    def _mcp_call(self, tool_name, arguments, **extra_env):
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments}}
        out = subprocess.check_output(
            [sys.executable, str(CODE / "mcp/server.py")],
            input=json.dumps(req) + "\n",
            env=self._env(**extra_env),
            text=True,
        )
        return json.loads(out)["result"]

    def test_mcp_create_session_start_hint_claim_ui_and_complete(self):
        # Seed an L0 rule and an L3 project so the briefing enriches them.
        self.store.upsert_entry(
            "L0", "no-force-push", "禁止强推 main", "严禁对 main 执行 force push", "不要强推 main 分支。"
        )
        self.store.upsert_entry(
            "L3", "updater-bug", "自动更新排查", "定位到代理重定向超时", "2026-11-27 已复现。"
        )

        # 1. Codex creates a handoff pack via MCP.
        res = self._mcp_call(
            "memory_handoff",
            {
                "action": "create",
                "title": "修复自动更新代理重试",
                "summary": "根因已定位在 updater.cjs 未对 502 做退避重试。",
                "next_steps": ["给 downloadAsset 加指数退避", "补齐 updater.test.cjs 用例"],
                "files": ["desktop/updater.cjs", "desktop/updater.test.cjs"],
                "memory_refs": ["projects/updater-bug.md"],
                "target_client": "Claude Code",
                "workspace": "/repo/demo",
            },
            UNIFIED_MEMORY_CLIENT="Codex",
        )
        self.assertFalse(res.get("isError"), res)
        created = json.loads(res["content"][0]["text"])
        hid = created["id"]
        self.assertTrue(hid.startswith("handoff-"))
        self.assertEqual(created["status"], "open")
        self.assertEqual(created["source_client"], "Codex")
        self.assertIn("projects/updater-bug.md", created["briefing"])
        self.assertIn("禁止强推 main", created["briefing"])
        self.assertIn("memory_read(paths=", created["briefing"])

        # 2. SessionStart hook in Claude Code surfaces the open handoff pack.
        hook_out = cursor_hook.handle("SessionStart", {"session_id": "s-claude"}, self.root, client="claude")
        ctx = hook_out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("待接力会话交接包", ctx)
        self.assertIn(hid, ctx)

        # 3. Desktop UI renders the handoff card with copyable briefing and controls.
        html = render_handoff_panel("tok", "primary", self.library)
        self.assertIn("修复自动更新代理重试", html)
        self.assertIn("复制交接包", html)
        self.assertIn("标记结单", html)

        # 4. Claude Code claims the newest active handoff via MCP without even passing id.
        claim_res = self._mcp_call(
            "memory_handoff",
            {"action": "claim"},
            UNIFIED_MEMORY_CLIENT="Claude Code",
        )
        self.assertFalse(claim_res.get("isError"), claim_res)
        claimed = json.loads(claim_res["content"][0]["text"])
        self.assertEqual(claimed["id"], hid)
        self.assertEqual(claimed["status"], "claimed")
        self.assertEqual(claimed["claimed_by"], "Claude Code")
        self.assertIn("给 downloadAsset 加指数退避", claimed["briefing"])

        # 5. Complete via MCP clears it from active list and SessionStart hint.
        done_res = self._mcp_call("memory_handoff", {"action": "complete", "handoff_id": hid})
        self.assertFalse(done_res.get("isError"), done_res)
        self.assertEqual(json.loads(done_res["content"][0]["text"])["status"], "done")
        self.assertEqual(handoff.count_active(self.root), 0)
        self.assertEqual(handoff.active_handoff_hint(self.root), "")

    def test_cli_handoff_roundtrip_and_secret_rejection(self):
        # Secret guard blocks plaintext tokens from entering handoff packs.
        with self.assertRaises(ValueError):
            handoff.create_handoff(
                self.root,
                title="泄漏测试",
                summary="token=ghp_1234567890abcdefghijklmnopqrstuvwxyz1234",
            )

        created = json.loads(
            subprocess.check_output(
                [
                    sys.executable,
                    str(CODE / "scripts/memoryctl.py"),
                    "handoff",
                    "create",
                    "--title",
                    "排查构建缓存",
                    "--summary",
                    "已在本地复现缓存失效。",
                    "--next-step",
                    "验证锁定哈希",
                    "--file",
                    "Dockerfile",
                    "--from-client",
                    "Codex",
                    "--to-client",
                    "Claude Code",
                ],
                env=self._env(),
                text=True,
            )
        )
        hid = created["id"]
        md = subprocess.check_output(
            [sys.executable, str(CODE / "scripts/memoryctl.py"), "handoff", "read", "--markdown"],
            env=self._env(),
            text=True,
        )
        self.assertIn("# 跨客户端会话交接：排查构建缓存", md)
        self.assertIn("- [ ] 验证锁定哈希", md)

        subprocess.check_output(
            [sys.executable, str(CODE / "scripts/memoryctl.py"), "handoff", "delete", hid],
            env=self._env(),
            text=True,
        )
        listed = json.loads(
            subprocess.check_output(
                [sys.executable, str(CODE / "scripts/memoryctl.py"), "handoff", "list", "--all"],
                env=self._env(),
                text=True,
            )
        )
        self.assertEqual(listed["handoffs"], [])


if __name__ == "__main__":
    unittest.main()
