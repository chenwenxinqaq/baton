"""Tests for L0 write-approval staging and import-page similar-topic merge suggestions."""

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

import l0_guard  # noqa: E402
import memory_import  # noqa: E402
import server as mcp_server  # noqa: E402
from manager import Library, approve_import, render_l0_review, render_memory_import  # noqa: E402


class WriteGovernanceTests(unittest.TestCase):
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
        result = json.loads(out)["result"]
        self.assertFalse(result.get("isError"), result)
        return json.loads(result["content"][0]["text"])

    def test_mcp_l0_write_stages_proposal_until_human_approves_or_rejects(self):
        res = self._mcp_call(
            "memory_upsert",
            {
                "layer": "L0",
                "name": "prod-db-guard",
                "title": "禁止直接修改生产库",
                "description": "生产数据库写操作必须走工单",
                "body": "禁止在生产库执行 DROP/DELETE。",
            },
        )
        self.assertEqual(res["status"], "pending_approval")
        self.assertTrue(res["pending_approval"])
        self.assertTrue(res["proposal_id"].startswith("l0-"))
        # Nothing is written to constraints/ or MEMORY.md yet.
        self.assertFalse((self.root / "constraints/prod-db-guard.md").exists())
        self.assertNotIn("prod-db-guard", (self.root / "MEMORY.md").read_text(encoding="utf-8"))

        # Desktop review page shows the staged diff and approval controls.
        html = render_l0_review("tok", "primary", self.library)
        self.assertIn("禁止直接修改生产库", html)
        self.assertIn("确认生效", html)
        self.assertIn("拒绝丢弃", html)

        # CLI lists and approves the proposal.
        listed = json.loads(
            subprocess.check_output(
                [sys.executable, str(CODE / "scripts/memoryctl.py"), "l0", "list"],
                env=self._env(),
                text=True,
            )
        )
        self.assertEqual(len(listed["proposals"]), 1)
        self.assertEqual(listed["proposals"][0]["id"], res["proposal_id"])

        approved = json.loads(
            subprocess.check_output(
                [sys.executable, str(CODE / "scripts/memoryctl.py"), "l0", "approve", res["proposal_id"]],
                env=self._env(),
                text=True,
            )
        )
        self.assertEqual(approved["path"], "constraints/prod-db-guard.md")
        self.assertTrue((self.root / "constraints/prod-db-guard.md").is_file())
        self.assertIn("constraints/prod-db-guard.md", (self.root / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertEqual(l0_guard.count_proposals(self.root), 0)

        # Updating an existing L0 rule via MCP also stages a diff without overwriting the live file.
        upd = self._mcp_call(
            "memory_upsert",
            {
                "layer": "L0",
                "name": "prod-db-guard",
                "body": "新增例外：只读 SELECT 允许。",
                "mode": "append",
            },
        )
        self.assertTrue(upd["pending_approval"])
        live_text = (self.root / "constraints/prod-db-guard.md").read_text(encoding="utf-8")
        self.assertNotIn("只读 SELECT 允许", live_text)
        diff = l0_guard.proposal_diff(self.root, l0_guard.read_proposal(self.root, upd["proposal_id"]))
        self.assertIn("+新增例外：只读 SELECT 允许。", diff)

        # Rejecting leaves the live constraint intact and clears the queue.
        subprocess.check_output(
            [sys.executable, str(CODE / "scripts/memoryctl.py"), "l0", "reject", upd["proposal_id"]],
            env=self._env(),
            text=True,
        )
        self.assertEqual(l0_guard.count_proposals(self.root), 0)
        self.assertNotIn("只读 SELECT 允许", (self.root / "constraints/prod-db-guard.md").read_text(encoding="utf-8"))

    def test_l0_guard_can_be_disabled_via_env_and_does_not_block_human_cli(self):
        # Human CLI write applies directly.
        out = subprocess.check_output(
            [
                sys.executable,
                str(CODE / "scripts/memoryctl.py"),
                "write",
                "--layer",
                "L0",
                "--name",
                "human-rule",
                "--title",
                "人工红线",
                "--description",
                "直接写入",
                "--body",
                "不要推送未评审分支。",
            ],
            env=self._env(),
            text=True,
        )
        self.assertEqual(json.loads(out)["path"], "constraints/human-rule.md")
        self.assertTrue((self.root / "constraints/human-rule.md").is_file())

        # MCP with UNIFIED_MEMORY_L0_GUARD=0 writes directly.
        direct = self._mcp_call(
            "memory_upsert",
            {
                "layer": "L0",
                "name": "direct-rule",
                "title": "显式放行规则",
                "description": "关闭守卫",
                "body": "显式关闭守卫后直接写入。",
            },
            UNIFIED_MEMORY_L0_GUARD="0",
        )
        self.assertTrue(direct.get("indexed"))
        self.assertFalse(direct.get("pending_approval", False))
        self.assertTrue((self.root / "constraints/direct-rule.md").is_file())

    def test_import_page_suggests_similar_topics_and_can_merge_into_existing_entry(self):
        self.library.store.upsert_entry(
            layer="L1",
            name="docker-build-cache",
            title="Docker 构建缓存排查",
            description="Docker 镜像构建缓存失效定位方法",
            body="检查 buildx cache-from 配置与锁文件变更。",
        )
        memory_import.import_text(
            self.root,
            "claude",
            "Docker 构建缓存排查补充",
            "当 COPY . . 放在依赖安装前时会导致缓存穿透。",
        )
        pending = memory_import.pending(self.root, "claude")
        self.assertEqual(len(pending), 1)
        item = pending[0]

        html = render_memory_import("tok", self.library, "claude", item["id"], "", {})
        self.assertIn("发现近似已有条目", html)
        self.assertIn("methods/docker-build-cache.md", html)

        merged_path = approve_import(
            self.library,
            "claude",
            item["id"],
            memory_import.revision(item),
            {"layer": "L1", "title": item["title"], "description": "缓存穿透补充", "body": item["body"]},
            merge_target="methods/docker-build-cache.md",
        )
        self.assertEqual(merged_path, "methods/docker-build-cache.md")
        self.assertEqual(memory_import.pending(self.root, "claude"), [])
        content = (self.root / "methods/docker-build-cache.md").read_text(encoding="utf-8")
        self.assertIn("检查 buildx cache-from 配置与锁文件变更。", content)
        self.assertIn("当 COPY . . 放在依赖安装前时会导致缓存穿透。", content)
        self.assertIn("> 来源：", content)


if __name__ == "__main__":
    unittest.main()
