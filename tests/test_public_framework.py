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


if __name__ == "__main__":
    unittest.main()
