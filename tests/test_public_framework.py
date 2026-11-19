"""Integration checks with synthetic libraries and isolated client configurations."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.request import urlopen


CODE = Path(__file__).resolve().parent.parent


class PublicFrameworkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        self.library = Path(tmp.name) / "private library"
        self.other = Path(tmp.name) / "other library"
        self.cli("init")

    def environment(self, root=None):
        return {**os.environ, "HOME": str(self.home), "UNIFIED_MEMORY_ROOT": str(root or self.library),
                "PYTHONDONTWRITEBYTECODE": "1"}

    def cli(self, *args, root=None):
        output = subprocess.check_output([sys.executable, str(CODE / "scripts/memoryctl.py"), *args],
                                         env=self.environment(root), cwd=self.home, text=True)
        return json.loads(output)

    def tool(self, name, args, root=None):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": args}}
        output = subprocess.check_output([sys.executable, str(CODE / "mcp/server.py")],
                                         input=json.dumps(request) + "\n", env=self.environment(root),
                                         cwd=self.home, text=True)
        result = json.loads(output)["result"]
        self.assertFalse(result.get("isError"), result)
        return json.loads(result["content"][0]["text"])

    def test_two_libraries_and_existing_data(self):
        self.cli("init", root=self.other)
        item = dict(layer="L3", name="sample", title="归档练习", description="合成样本", body="校验每周导出")
        self.tool("memory_upsert", item)
        self.assertEqual(self.tool("memory_search", {"query": "导出"})[0]["path"], "projects/sample.md")
        self.assertEqual(self.tool("memory_search", {"query": "最近工作"}, self.other), [])
        self.tool("memory_upsert", {**item, "layer": "L1"})
        self.assertFalse((self.library / "projects/sample.md").exists())
        self.assertEqual(self.tool("memory_archive", {"name": "sample"})["to"], "archive/sample.md")
        self.cli("init")
        self.assertIn("每周导出", self.tool("memory_read", {"path": "archive/sample.md"})["content"])
        for root, count in ((self.library, 1), (self.other, 0)):
            report = self.cli("doctor", root=root)
            self.assertTrue(report["ok"], report["findings"])
            self.assertEqual((report["indexed_topics"], report["disk_topics"]), (count, count))

    def test_client_configuration_and_private_skills(self):
        skill = self.library / "skills" / "sample"
        skill.mkdir()
        (skill / "SKILL.md").write_text("---\nname: sample\ndescription: synthetic\n---\n", encoding="utf-8")
        resource = self.library / "skills" / "shared-guides"
        resource.mkdir()
        (self.library / "skills/resources.json").write_text(json.dumps({"sample": ["shared-guides"]}))
        (self.home / ".codex").mkdir()
        config = self.home / ".codex/config.toml"
        config.write_text('[sample]\nkeep = true\n', encoding="utf-8")
        self.cli("connect", "all")
        report = self.cli("doctor")
        self.assertEqual([a["state"] for a in report["agents"]], ["managed"] * 4)
        self.assertIn(str(CODE / "mcp/server.py"), config.read_text())
        self.assertIn(str(self.library), config.read_text())
        self.assertTrue((self.home / ".codex/skills/sample").samefile(skill))
        self.assertTrue((self.home / ".codex/skills/shared-guides").samefile(resource))
        self.cli("skill", "disable", "sample")
        self.assertFalse((self.home / ".codex/skills/shared-guides").exists())
        self.assertFalse((self.home / ".codex/skills/sample").exists())
        self.cli("skill", "enable", "sample")
        self.assertTrue((self.home / ".codex/skills/sample").samefile(skill))
        self.cli("disconnect", "all")
        self.assertEqual(config.read_text().strip(), '[sample]\nkeep = true')
        self.assertTrue((skill / "SKILL.md").is_file())

    def test_writing_policy_reaches_clients_without_private_index(self):
        index = self.library / "MEMORY.md"
        public_policy = (CODE / "templates/library.md").read_text().split("\n## L0 ", 1)[0].rstrip()
        self.assertTrue(index.read_text().startswith(public_policy))
        for decision in ("记一下", "事实一经核实就写", "事故等根因定位后", "临时", "未验证",
                         "最终回复前", "先查后写", "验证日期", "同一主题只保留一个当前状态"):
            self.assertIn(decision, public_policy)
        for name in ("entry", "project", "incident"):
            path = f"templates/{name}.md"
            self.assertEqual(self.tool("memory_read", {"path": path})["content"], (CODE / path).read_text())
        self.assertEqual(self.tool("memory_list", {}), [])

        # Simulate an existing private library with a customized index and template.
        private_index = "# Private index fixture\n\n## L0 " + index.read_text().split("\n## L0 ", 1)[1]
        index.write_text(private_index)
        custom_template = self.library / "templates/project.md"
        custom_template.write_text("Custom project template")
        (self.library / "templates/entry.md").unlink()
        self.cli("init")
        self.assertEqual(index.read_text(), private_index)
        self.assertEqual(custom_template.read_text(), "Custom project template")
        self.assertTrue((self.library / "templates/entry.md").is_file())
        self.cli("connect", "all")
        for relative in (".claude/CLAUDE.md", ".codex/AGENTS.md",
                         ".gemini/config/plugins/unified-memory/rules/unified-memory.md"):
            with self.subTest(client=relative):
                instructions = (self.home / relative).read_text()
                self.assertIn(public_policy, instructions)
                self.assertIn(str(self.library), instructions)
                self.assertNotIn("Private index fixture", instructions)

    def test_web_console_uses_requested_library(self):
        self.cli("init", root=self.other)
        self.tool("memory_upsert", dict(layer="L1", name="web-demo", title="界面专用样本",
                                       description="测试", body="示例正文"), self.other)
        command = [sys.executable, str(CODE / "manager/manager.py"), "--port", "0",
                   "--memory-root", str(self.other)]
        with subprocess.Popen(command, env=self.environment(), stdout=subprocess.PIPE, text=True) as process:
            try:
                address = process.stdout.readline().strip()
                for query, expected in (("", "界面专用样本"), ("?tab=skills&skill=unified-memory", "memory_upsert")):
                    with urlopen(address + query, timeout=5) as response:
                        self.assertIn(expected, response.read().decode())
            finally:
                process.terminate()

    def test_external_state_trash_backup_and_rebuild_index(self):
        # Writes must keep the memory folder free of runtime .state/ files and preserve overwritten content in local trash.
        self.tool("memory_upsert", dict(layer="L1", name="build-guide", title="构建指南",
                                        description="初版", body="第一步：make build"))
        self.assertFalse((self.library / ".state").exists())
        self.tool("memory_upsert", dict(layer="L1", name="build-guide", title="构建指南",
                                        description="二版", body="第一步：make all"))
        trash_files = list((self.home / ".local/state/unified-memory").rglob("*.md"))
        self.assertTrue(any("make build" in f.read_text(encoding="utf-8") for f in trash_files))

        # Corrupt MEMORY.md index entries and verify deterministic rebuild recovers them.
        (self.library / "MEMORY.md").write_text("# §0\n\n## L0 操作约束\n\n## L1 可复用方法\n", encoding="utf-8")
        res = self.cli("rebuild-index", "personal")
        self.assertEqual(res["results"][0]["indexed_topics"], 1)
        report = self.cli("doctor")
        self.assertTrue(report["ok"], report["findings"])

    def test_multi_library_mounts_permissions_and_web_ro_guard(self):
        team_rw = self.home / "team-rw"
        team_ro = self.home / "team-ro"
        self.cli("mount", "add", "team-rw", "--path", str(team_rw), "--label", "研发共享", "--mode", "rw")
        self.cli("mount", "add", "team-ro", "--path", str(team_ro), "--label", "只读规范", "--mode", "ro")

        # Seed a doc directly in team-ro before testing read-only enforcement via MCP.
        (team_ro / "methods/release-check.md").write_text(
            "---\nname: release-check\ndescription: \"发布核对清单\"\ntype: reference\n---\n\n# 发布核对清单\n\n核对版本号\n",
            encoding="utf-8",
        )
        self.cli("rebuild-index", "team-ro")

        # Default upsert goes to personal; explicit library writes to rw shared mount.
        self.tool("memory_upsert", dict(layer="L0", name="personal-rule", title="个人红线",
                                        description="仅限个人", body="不要自动删表"))
        self.tool("memory_upsert", dict(layer="L1", name="k8s-debug", title="K8s排障",
                                        description="Pod排查步骤", body="先看 events", library="team-rw"))

        # Shared rw mount must reject L0 by default and reject user_managed.
        for bad_args in (
            dict(layer="L0", name="evil", title="越权", description="d", body="b", library="team-rw"),
            dict(layer="L1", name="u1", title="手记", description="d", body="b", user_managed=True, library="team-rw"),
            dict(layer="L1", name="blocked", title="只读", description="d", body="b", library="team-ro"),
        ):
            req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "memory_upsert", "arguments": bad_args}}
            out = subprocess.check_output([sys.executable, str(CODE / "mcp/server.py")],
                                          input=json.dumps(req) + "\n", env=self.environment(), text=True)
            self.assertTrue(json.loads(out)["result"].get("isError"), out)

        # Unified search returns hits from personal + rw + ro with library attribution and namespace read.
        libs = self.tool("memory_libraries", {})
        self.assertEqual([l["id"] for l in libs], ["personal", "team-rw", "team-ro"])
        hits = self.tool("memory_search", {"query": "排查"})
        self.assertEqual(hits[0]["library"], "team-rw")
        self.assertEqual(hits[0]["ref"], "team-rw:methods/k8s-debug.md")
        content = self.tool("memory_read", {"path": "team-ro:methods/release-check.md"})["content"]
        self.assertIn("核对版本号", content)

        # Web UI exposes mounted library tabs and blocks editing on ro mount.
        command = [sys.executable, str(CODE / "manager/manager.py"), "--port", "0",
                   "--memory-root", str(self.library)]
        with subprocess.Popen(command, env=self.environment(), stdout=subprocess.PIPE, text=True) as process:
            try:
                address = process.stdout.readline().strip()
                home_html = urlopen(address, timeout=5).read().decode()
                self.assertIn("研发共享", home_html)
                self.assertIn("只读规范", home_html)
                ro_new_html = urlopen(address + "?library=team-ro&view=new", timeout=5).read().decode()
                self.assertIn("只读挂载", ro_new_html)
            finally:
                process.terminate()

    def test_git_sync_two_devices_index_autoheal_and_entry_conflict_preservation(self):
        bare_remote = self.home / "remote.git"
        subprocess.check_call(["git", "init", "--bare", "-b", "main", str(bare_remote)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Device A sets up Git sync on personal library and pushes initial content.
        self.cli("sync", "setup", "personal", "--remote-url", str(bare_remote), "--branch", "main")
        self.tool("memory_upsert", dict(layer="L1", name="shared-tip", title="通用技巧",
                                        description="初始技巧", body="版本1"))
        sync_a1 = self.cli("sync", "run", "personal")
        self.assertEqual(sync_a1["results"][0]["status"], "ok")

        # Device B clones into a second HOME + library and syncs.
        home_b = self.home / "device-b-home"
        home_b.mkdir()
        lib_b = self.home / "device-b-lib"
        env_b = {**os.environ, "HOME": str(home_b), "UNIFIED_MEMORY_ROOT": str(lib_b), "PYTHONDONTWRITEBYTECODE": "1"}
        subprocess.check_call(["git", "clone", str(bare_remote), str(lib_b)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def cli_b(*args):
            out = subprocess.check_output([sys.executable, str(CODE / "scripts/memoryctl.py"), *args],
                                          env=env_b, cwd=home_b, text=True)
            return json.loads(out)

        def tool_b(name, args):
            req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": args}}
            out = subprocess.check_output([sys.executable, str(CODE / "mcp/server.py")],
                                          input=json.dumps(req) + "\n", env=env_b, cwd=home_b, text=True)
            res = json.loads(out)["result"]
            self.assertFalse(res.get("isError"), res)
            return json.loads(res["content"][0]["text"])

        cli_b("sync", "setup", "personal", "--remote-url", str(bare_remote), "--branch", "main")

        # Concurrent non-overlapping entries on Device A and Device B -> MEMORY.md conflicts and auto-heals!
        self.tool("memory_upsert", dict(layer="L1", name="from-a", title="来自A机", description="A", body="A内容"))
        self.cli("sync", "run", "personal")
        tool_b("memory_upsert", dict(layer="L1", name="from-b", title="来自B机", description="B", body="B内容"))
        sync_b = cli_b("sync", "run", "personal")
        self.assertEqual(sync_b["results"][0]["status"], "ok", sync_b)
        self.assertTrue((lib_b / "methods/from-a.md").is_file())
        self.assertTrue((lib_b / "methods/from-b.md").is_file())
        self.assertTrue(cli_b("doctor")["ok"])

        # Pull back to Device A and verify both entries exist cleanly.
        self.cli("sync", "run", "personal")
        self.assertTrue((self.library / "methods/from-b.md").is_file())

        # Concurrent edit to the SAME entry -> preserves remote conflict copy without data loss and blocks push.
        self.tool("memory_upsert", dict(layer="L1", name="shared-tip", title="通用技巧",
                                        description="A改", body="A机的修订"))
        self.cli("sync", "run", "personal")
        tool_b("memory_upsert", dict(layer="L1", name="shared-tip", title="通用技巧",
                                     description="B改", body="B机的修订"))
        conflict_res = cli_b("sync", "run", "personal")
        self.assertEqual(conflict_res["results"][0]["status"], "needs_manual_merge", conflict_res)
        conflicts = conflict_res["results"][0]["pull"]["conflicts"]
        self.assertEqual(len(conflicts), 1)
        copy_path = lib_b / conflicts[0]["remote_copy"]
        self.assertTrue(copy_path.is_file())
        self.assertIn("A机的修订", copy_path.read_text(encoding="utf-8"))
        self.assertIn("B机的修订", (lib_b / "methods/shared-tip.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
