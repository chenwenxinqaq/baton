"""Integration checks with synthetic libraries and isolated client configurations."""

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import urldefrag, urlencode, urljoin
from urllib.request import Request, urlopen


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
        env = dict(os.environ)
        env.pop("CLAUDE_DESKTOP_CONFIG", None)
        env.pop("XDG_CONFIG_HOME", None)
        env.update({
            "HOME": str(self.home),
            "DSH_HOME": str(self.home / ".dsh"),
            "UNIFIED_MEMORY_ROOT": str(root or self.library),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        return env

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
        original = '[features]\nmemories = true\n\n[sample]\nkeep = true\n'
        config.write_text(original, encoding="utf-8")
        self.cli("connect", "all")
        report = self.cli("doctor")
        self.assertEqual(
            {a["id"]: a["state"] for a in report["agents"]},
            {
                "claude": "managed",
                "claude-desktop": "managed",
                "codex": "managed",
                "cursor": "managed",
                "workbuddy": "managed",
                "antigravity": "managed",
                "deepseek": "managed",
            },
        )
        launcher = self.home / "Library/Application Support/Unified Memory/bin/umem-mcp"
        self.assertIn(str(launcher), config.read_text())  # clients never point at the source tree / app bundle
        self.assertNotIn(str(CODE / "mcp/server.py"), config.read_text())
        self.assertIn(f"FW={CODE}", launcher.read_text())
        self.assertTrue(report["ok"], report["findings"])  # doctor probes MCP through the launcher
        self.assertIn(str(self.library), config.read_text())
        self.assertIn("memories = true", config.read_text())
        self.assertTrue((self.home / ".codex/skills/sample").samefile(skill))
        self.assertTrue((self.home / ".codex/skills/shared-guides").samefile(resource))
        self.cli("skill", "disable", "sample")
        self.assertFalse((self.home / ".codex/skills/shared-guides").exists())
        self.assertFalse((self.home / ".codex/skills/sample").exists())
        self.cli("skill", "enable", "sample")
        self.assertTrue((self.home / ".codex/skills/sample").samefile(skill))
        self.cli("disconnect", "all")
        self.assertEqual(config.read_text().strip(), original.strip())
        self.assertTrue((skill / "SKILL.md").is_file())

    def test_codex_reconnect_restores_only_legacy_memory_override(self):
        self.cli("connect", "codex")
        config = self.home / ".codex/config.toml"
        mcp_config = config.read_text()
        self.assertNotIn("memories =", mcp_config)
        restore_file = next(self.home.rglob("restore/codex.json"))
        for previous, setting, expected in (
            ("true", "memories = false", "memories = true"),
            ("absent", "memories = false", None),
            ("true", "memories = false # my setting", "memories = false # my setting"),
            ("false", "memories = false", "memories = false"),
        ):
            with self.subTest(previous=previous, setting=setting):
                restore = json.loads(restore_file.read_text())
                restore["previous_codex_features_memories"] = previous
                restore_file.write_text(json.dumps(restore))
                config.write_text(f"[features]\n{setting}\nother = true\n\n{mcp_config}")
                self.cli("connect", "codex")
                settings = config.read_text()
                self.assertIn("other = true", settings)
                if expected:
                    self.assertIn(expected, settings.splitlines())
                else:
                    self.assertNotIn("memories =", settings)
                self.assertNotIn("previous_codex_features_memories", json.loads(restore_file.read_text()))
        config.write_text(config.read_text().replace("memories = false", "memories = true"))
        self.cli("disconnect", "codex")
        self.assertIn("memories = true", config.read_text())

    def test_codex_disconnect_keeps_tables_codex_added(self):
        config = self.home / ".codex/config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('model = "gpt-5"\n\n[projects."/tmp/a"]\ntrust_level = "trusted"\n', encoding="utf-8")
        self.cli("connect", "codex")
        # Codex inserts new tables before the trailing comment of the file, so
        # anything it adds can land between the legacy BEGIN/END markers.
        text = config.read_text(encoding="utf-8").replace(
            "[mcp_servers.unified_memory]", "# BEGIN unified-memory managed\n[mcp_servers.unified_memory]")
        text += ('\n[mcp_servers.unified_memory.tools.memory_search]\napproval_mode = "approve"\n'
                 '\n[features]\nasync_compaction = true\n# END unified-memory managed\n')
        config.write_text(text, encoding="utf-8")
        self.cli("connect", "codex")
        self.assertIn("async_compaction = true", config.read_text(encoding="utf-8"))
        self.cli("disconnect", "codex")
        result = config.read_text(encoding="utf-8")
        self.assertIn('[projects."/tmp/a"]', result)
        self.assertIn("[features]\nasync_compaction = true", result)
        self.assertNotIn("unified_memory", result)
        self.assertNotIn("unified-memory managed", result)

    def test_codex_hand_written_header_is_reused_and_restored(self):
        config = self.home / ".codex/config.toml"
        config.parent.mkdir(parents=True)
        original = ('model = "x"\n\n[mcp_servers."unified_memory"] # hand written\n'
                    'command = "/bin/old"\nargs = []\n\n[other]\nk = 1\n')
        config.write_text(original, encoding="utf-8")
        self.cli("connect", "codex")
        connected = config.read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"^\[mcp_servers\.", connected, re.M)), 1, connected)
        self.assertNotIn("/bin/old", connected)
        self.assertIn("[other]\nk = 1", connected)
        self.cli("disconnect", "codex")
        self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_connect_refuses_unbalanced_instruction_markers(self):
        agents = self.home / ".codex/AGENTS.md"
        agents.parent.mkdir(parents=True)
        original = "USER_A\n<!-- unified-memory:managed:start -->\nUSER_B\n"
        agents.write_text(original, encoding="utf-8")
        with self.assertRaises(subprocess.CalledProcessError):
            subprocess.run([sys.executable, str(CODE / "scripts/memoryctl.py"), "connect", "codex"],
                           env=self.environment(), cwd=self.home, text=True, capture_output=True, check=True)
        self.assertEqual(agents.read_text(encoding="utf-8"), original)

    def test_disconnect_keeps_users_own_same_name_servers(self):
        claude = self.home / ".claude.json"
        cursor = self.home / ".cursor/mcp.json"
        codex = self.home / ".codex/config.toml"
        cursor.parent.mkdir(parents=True)
        codex.parent.mkdir(parents=True)
        own = {"command": "my-own-server", "args": []}
        claude.write_text(json.dumps({"mcpServers": {"unified-memory": own}}), encoding="utf-8")
        cursor.write_text(json.dumps({"mcpServers": {"unified_memory": own}}), encoding="utf-8")
        toml = '[mcp_servers.unified_memory]\ncommand = "my-own-server"\nargs = []\n'
        codex.write_text(toml, encoding="utf-8")
        for target in ("claude", "cursor", "codex"):
            self.cli("disconnect", target)
        self.assertEqual(json.loads(claude.read_text(encoding="utf-8"))["mcpServers"]["unified-memory"], own)
        self.assertEqual(json.loads(cursor.read_text(encoding="utf-8"))["mcpServers"]["unified_memory"], own)
        self.assertEqual(codex.read_text(encoding="utf-8"), toml)

    def test_workbuddy_one_click_connect_and_disconnect(self):
        from urllib.error import HTTPError

        config = self.home / ".workbuddy/mcp.json"
        command = [sys.executable, str(CODE / "manager/manager.py"), "--port", "0"]
        with subprocess.Popen(command, stdout=subprocess.PIPE, text=True, env=self.environment()) as manager:
            try:
                base = manager.stdout.readline().strip().rstrip("/")
                with urlopen(base + "/?view=clients", timeout=5) as response:
                    page = response.read().decode()
                self.assertIn("<strong>WorkBuddy</strong>", page)
                csrf = re.search(r'name="csrf" value="([^"]+)"', page)[1]

                def click(op):
                    data = urlencode(dict(csrf=csrf, target="workbuddy", op=op)).encode()
                    with urlopen(Request(base + "/agent", data=data, headers={"Origin": base}), timeout=8) as response:
                        self.assertIn("view=clients", response.geturl())
                        return response.read().decode()

                self.assertIn("WorkBuddy 配置已保存", click("connect"))
                config_data = json.loads(config.read_text())
                entry = config_data["mcpServers"]["unified_memory"]
                self.assertEqual(entry["args"], ["--memory-root", str(self.library.resolve())])
                requests = [
                    {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "generic-mcp-client"},
                                                                 "protocolVersion": "2025-06-18", "capabilities": {}}},
                    {"method": "notifications/initialized"},
                    {"id": 2, "method": "tools/list"},
                    {"id": 3, "method": "tools/call", "params": {"name": "memory_search", "arguments": {"query": "测试"}}},
                ]
                env = {**self.environment(), **entry["env"]}
                result = subprocess.check_output([entry["command"], *entry["args"]], env=env, text=True,
                    input="".join(json.dumps({"jsonrpc": "2.0", **req}) + "\n" for req in requests))
                results = {r["id"]: r["result"] for r in map(json.loads, result.splitlines())}
                self.assertIn("memory_search", {t["name"] for t in results[2]["tools"]})
                self.assertFalse(results[3].get("isError", False))
                events = next(self.home.rglob("audit.jsonl")).read_text().splitlines()
                self.assertEqual(json.loads(events[-1])["client"], "WorkBuddy")
                workbuddy = next(a for a in self.cli("agent", "list")["agents"] if a["id"] == "workbuddy")
                self.assertEqual((workbuddy["state"], set(workbuddy["components"])), ("managed", {"mcp"}))
                click("disconnect")
                self.assertFalse(config.exists())

                original = {"settings": {"keep": True}, "mcpServers": {
                    "other": {"command": "other"}, "unified_memory": {"command": "previous"}}}
                shared_config = self.home / "shared-config.json"
                shared_config.write_text(json.dumps(original))
                config.symlink_to(shared_config)
                click("connect")
                click("connect")
                updated = json.loads(config.read_text())
                self.assertEqual(updated["mcpServers"]["other"], original["mcpServers"]["other"])
                self.assertEqual(updated["mcpServers"]["unified_memory"], entry)
                updated["mcpServers"]["added_later"] = {"command": "third"}
                config.write_text(json.dumps(updated))
                click("disconnect")
                original["mcpServers"]["added_later"] = {"command": "third"}
                self.assertTrue(config.is_symlink())
                self.assertEqual(json.loads(shared_config.read_text()), original)

                config.write_text("{invalid")
                with self.assertRaises(HTTPError) as error:
                    click("connect")
                self.assertEqual(error.exception.code, 409)
                self.assertEqual(config.read_text(), "{invalid")
                self.assertIn("MCP 配置格式无效", error.exception.read().decode())
            finally:
                manager.terminate()

    def test_claude_desktop_one_click_connect_and_disconnect(self):
        config = self.home / "Library/Application Support/Claude-3p/claude_desktop_config.json"
        env = {**self.environment(), "CLAUDE_DESKTOP_CONFIG": str(config)}
        command = [sys.executable, str(CODE / "manager/manager.py"), "--port", "0"]
        with subprocess.Popen(command, stdout=subprocess.PIPE, text=True, env=env) as manager:
            try:
                base = manager.stdout.readline().strip().rstrip("/")
                with urlopen(base + "/?view=clients", timeout=5) as response:
                    page = response.read().decode()
                self.assertIn("<strong>Claude Desktop</strong>", page)
                csrf = re.search(r'name="csrf" value="([^"]+)"', page)[1]

                def click(op):
                    data = urlencode(dict(csrf=csrf, target="claude-desktop", op=op)).encode()
                    with urlopen(Request(base + "/agent", data=data, headers={"Origin": base}), timeout=8) as response:
                        self.assertIn("view=clients", response.geturl())
                        return response.read().decode()

                self.assertIn("Claude Desktop 配置已保存", click("connect"))
                config_data = json.loads(config.read_text(encoding="utf-8"))
                entry = config_data["mcpServers"]["unified-memory"]
                self.assertEqual(entry["args"], ["--memory-root", str(self.library.resolve())])
                self.assertEqual(entry["env"], {"UNIFIED_MEMORY_CLIENT": "Claude Desktop"})

                requests = [
                    {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "claude-ai"},
                                                                 "protocolVersion": "2025-06-18", "capabilities": {}}},
                    {"method": "notifications/initialized"},
                    {"id": 2, "method": "tools/list"},
                    {"id": 3, "method": "tools/call", "params": {"name": "memory_search", "arguments": {"query": "测试"}}},
                ]
                run_env = {**env, **entry["env"]}
                result = subprocess.check_output([entry["command"], *entry["args"]], env=run_env, text=True,
                    input="".join(json.dumps({"jsonrpc": "2.0", **req}) + "\n" for req in requests))
                results = {r["id"]: r["result"] for r in map(json.loads, result.splitlines())}
                self.assertIn("memory_search", {t["name"] for t in results[2]["tools"]})
                self.assertFalse(results[3].get("isError", False))
                events = next(self.home.rglob("audit.jsonl")).read_text(encoding="utf-8").splitlines()
                self.assertEqual(json.loads(events[-1])["client"], "Claude Desktop")

                # Also verify clientInfo.name="claude-ai" maps to Claude Desktop without UNIFIED_MEMORY_CLIENT.
                fallback_env = {k: v for k, v in env.items() if k != "UNIFIED_MEMORY_CLIENT"}
                subprocess.check_output(
                    [entry["command"], *entry["args"]],
                    env=fallback_env,
                    text=True,
                    input="".join(json.dumps({"jsonrpc": "2.0", **req}) + "\n" for req in requests),
                )
                events = next(self.home.rglob("audit.jsonl")).read_text(encoding="utf-8").splitlines()
                self.assertEqual(json.loads(events[-1])["client"], "Claude Desktop")

                listed = json.loads(subprocess.check_output(
                    [sys.executable, str(CODE / "scripts/memoryctl.py"), "agent", "list"],
                    env=env, cwd=self.home, text=True))
                desktop = next(a for a in listed["agents"] if a["id"] == "claude-desktop")
                self.assertEqual((desktop["state"], set(desktop["components"])), ("managed", {"mcp"}))

                click("disconnect")
                self.assertFalse(config.exists())

                # Reconnect and change --memory-root; disconnect should still recognize our launcher entry.
                click("connect")
                drifted = json.loads(config.read_text(encoding="utf-8"))
                drifted["mcpServers"]["unified-memory"]["args"] = ["--memory-root", str(self.other.resolve())]
                config.write_text(json.dumps(drifted), encoding="utf-8")
                click("disconnect")
                self.assertFalse(config.exists())

                own = {"command": "my-own-server", "args": []}
                config.parent.mkdir(parents=True, exist_ok=True)
                config.write_text(json.dumps({"mcpServers": {"unified-memory": own}}), encoding="utf-8")
                click("disconnect")
                self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["unified-memory"], own)
            finally:
                manager.terminate()

        sample = self.home / "claude-desktop-note.md"
        sample.write_text("Claude Desktop 文件导入样本", encoding="utf-8")
        self.assertEqual(self.cli("import-memory", "claude-desktop", "--file", str(sample))["added"], 1)

        from unittest.mock import patch
        sys.path.insert(0, str(CODE / "scripts"))
        import memoryctl
        support = self.home / "darwin-home" / "Library" / "Application Support"
        (support / "Claude-3p").mkdir(parents=True)
        with patch.object(memoryctl.sys, "platform", "darwin"), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_DESKTOP_CONFIG", None)
            self.assertEqual(
                memoryctl._claude_desktop_config_path(self.home / "darwin-home"),
                support / "Claude-3p" / "claude_desktop_config.json",
            )
            (support / "Claude").mkdir(parents=True)
            std_cfg = support / "Claude" / "claude_desktop_config.json"
            tp_cfg = support / "Claude-3p" / "claude_desktop_config.json"
            std_cfg.write_text("{}", encoding="utf-8")
            self.assertEqual(
                memoryctl._claude_desktop_config_path(self.home / "darwin-home"),
                std_cfg,
            )
            # When both config files exist, prefer the more recently modified config file rather than dir mtime.
            tp_cfg.write_text("{}", encoding="utf-8")
            os.utime(std_cfg, (1000, 1000))
            os.utime(tp_cfg, (2000, 2000))
            os.utime(support / "Claude", (3000, 3000))
            self.assertEqual(
                memoryctl._claude_desktop_config_path(self.home / "darwin-home"),
                tp_cfg,
            )
            # Passing an explicit home ignores CLAUDE_DESKTOP_CONFIG env override.
            os.environ["CLAUDE_DESKTOP_CONFIG"] = str(self.home / "elsewhere.json")
            self.assertEqual(
                memoryctl.agent_specs(home=self.home / "darwin-home")["claude-desktop"]["mcp_path"],
                tp_cfg,
            )
            # Restore pinning keeps inspect/disconnect bound to the originally connected config path.
            spec = memoryctl.agent_specs(home=self.home / "darwin-home")["claude-desktop"]
            self.assertEqual(
                memoryctl._agent_mcp_path("claude-desktop", spec, {"mcp_path": str(std_cfg)}),
                std_cfg,
            )

    def test_codex_import_review_and_classification(self):
        from urllib.error import HTTPError

        native = self.home / ".codex"
        native.mkdir()
        db_v1 = native / "memories_1.sqlite"
        db_v2 = native / "memories_v2_1.sqlite"
        for path, body in ((db_v1, "旧版重复原文"), (db_v2, "# 导入样本\n杜鹃候选记忆")):
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE stage1_outputs(thread_id TEXT PRIMARY KEY, source_updated_at INTEGER, "
                           "raw_memory TEXT, rollout_summary TEXT)")
                db.execute("INSERT INTO stage1_outputs VALUES (?, ?, ?, ?)", ("same-thread", 1735689600, body, ""))
        summaries = native / "memories_v2/rollout_summaries"
        summaries.mkdir(parents=True)
        (summaries / "kept.md").write_text(
            "thread_id: another-thread\nupdated_at: 2025-01-02T00:00:00Z\ncwd: /not-imported\n\n# 摘要回退\n白鹭候选记忆",
            encoding="utf-8",
        )
        (native / "memories_v2/MEMORY.md").write_text("重复的合并汇总", encoding="utf-8")
        memory_index = self.library / "MEMORY.md"
        initial_index = memory_index.read_bytes()
        initial_db = {path: path.read_bytes() for path in (db_v1, db_v2)}
        command = [sys.executable, str(CODE / "manager/manager.py"), "--desktop", "--port", "0"]
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=self.environment()) as manager:
            try:
                base = manager.stdout.readline().strip().rstrip("/")

                def get(route):
                    with urlopen(base + route, timeout=5) as response:
                        return response.read().decode()

                clients = get("/?view=clients")
                self.assertIn("一键导入记忆", clients)
                csrf = re.search(r'name="csrf" value="([^"]+)"', clients)[1]

                def post(**fields):
                    request = Request(base + "/memory_import", data=urlencode(dict(csrf=csrf, **fields)).encode(),
                                      headers={"Origin": base})
                    with urlopen(request, timeout=8) as response:
                        return response.read().decode()

                imported = post(op="import")
                self.assertIn("新增 2 条", imported)
                self.assertIn("待确认 · 2 条", imported)
                self.assertIn("摘要回退", imported)
                self.assertEqual(self.cli("import-codex")["added"], 0)
                self.assertEqual(memory_index.read_bytes(), initial_index)
                self.assertEqual({p: p.read_bytes() for p in initial_db}, initial_db)
                self.assertEqual(self.tool("memory_search", {"query": "杜鹃候选记忆"}), [])
                self.assertEqual(list(self.library.glob("imported")), [])
                filtered = get("/?view=memory_import&" + urlencode({"q": "杜鹃候选记忆"}))
                self.assertNotIn("摘要回退", filtered)
                path = re.search(r"topic=(codex-import-[0-9a-f]{32})", filtered)[1]
                review = get("/?view=memory_import&topic=" + path)
                self.assertIn("杜鹃候选记忆", review)
                self.assertNotIn("旧版重复原文", review)
                revision = re.search(r'name="revision" value="([^"]+)"', review)[1]
                request = dict(op="approve", import_id=path, title="已确认标题", description="审阅摘要",
                               body="# 已确认标题\n杜鹃候选记忆")
                with self.assertRaises(HTTPError) as stale:
                    post(**request, layer="L3", revision="stale")
                self.assertEqual(stale.exception.code, 409)
                with self.assertRaises(HTTPError) as missing_layer:
                    post(**request, revision=revision)
                self.assertEqual(missing_layer.exception.code, 409)
                self.assertEqual(memory_index.read_bytes(), initial_index)
                self.assertIn("已将确认后的记忆归入个人记忆库", post(**request, layer="L3", revision=revision))
                hits = self.tool("memory_search", {"query": "杜鹃候选记忆"})
                self.assertEqual([hit["path"] for hit in hits], [f"projects/{path}.md"])
                topic = (self.library / hits[0]["path"]).read_text()
                self.assertIn("Codex v2", topic)
                self.assertIn("2025-01-01", topic)
                self.assertIn("same-thread", topic)
                self.assertIn("新增 0 条", post(op="import"))
                self.assertIn("待确认 · 1 条", get("/?view=memory_import"))
            finally:
                manager.stdin.close()
                manager.wait(timeout=8)

    def test_deepseek_connect_preserves_patch_and_exposes_import(self):
        dsh_home = self.home / "custom dsh"
        dsh_home.mkdir()
        patch = dsh_home / "cordis.patch.yml"
        original = b"\xef\xbb\xbf---\r\n# keep this comment\r\n  - id: other-plugin\r\n    disabled: !!js false\r\n...\r\n"
        patch.write_bytes(original)
        command = [sys.executable, str(CODE / "manager/manager.py"), "--port", "0"]
        env = {**self.environment(), "DSH_HOME": str(dsh_home)}
        with subprocess.Popen(command, stdout=subprocess.PIPE, text=True, env=env) as manager:
            try:
                base = manager.stdout.readline().strip().rstrip("/")
                with urlopen(base + "/?view=clients", timeout=5) as response:
                    page = response.read().decode()
                self.assertIn("<strong>DeepSeek Harness</strong>", page)
                self.assertIn("client=deepseek", page)
                csrf = re.search(r'name="csrf" value="([^"]+)"', page)[1]

                def click(op):
                    data = urlencode(dict(csrf=csrf, target="deepseek", op=op)).encode()
                    with urlopen(Request(base + "/agent", data=data, headers={"Origin": base}), timeout=8) as response:
                        return response.read().decode()

                self.assertIn("DeepSeek Harness 配置已保存", click("connect"))
                configured = patch.read_bytes()
                self.assertIn(b"\r\n  - insert:\r\n", configured)
                self.assertIn(b"serverName: unified_memory", configured)
                self.assertIn(b"    disabled: !!js false\r\n", configured)
                click("connect")
                self.assertEqual(patch.read_bytes(), configured)
                click("disconnect")
                self.assertEqual(patch.read_bytes(), original)
                patch.write_text("[] # empty\n")
                click("connect")
                self.assertNotIn("\n[]", patch.read_text())
                click("disconnect")
                self.assertEqual(patch.read_text(), "[] # empty\n")
            finally:
                manager.terminate()

    def test_memory_import_for_other_clients(self):
        claude = self.home / ".claude/projects/sample/memory"
        claude.mkdir(parents=True)
        (claude / "MEMORY.md").write_text("- [fixture](fixture.md)")
        (claude / "fixture.md").write_text("Claude 索引之外的正文")
        (claude.parent / "session.jsonl").write_text("原始会话不应被导入")
        configured = self.home / "custom-claude-memory"
        configured.mkdir()
        (configured / "MEMORY.md").write_text("自定义 Claude 记忆目录")
        (self.home / ".claude/settings.json").write_text(json.dumps({"autoMemoryDirectory": str(configured)}))
        for app in ("antigravity", "antigravity-ide"):
            knowledge = self.home / f".gemini/{app}/knowledge/sample/artifacts"
            knowledge.mkdir(parents=True)
            (knowledge / "knowledge.md").write_text(f"{app} 客户端知识标记")
        initial = (self.library / "MEMORY.md").read_bytes()
        self.assertEqual(self.cli("import-memory", "claude")["added"], 2)
        self.assertEqual(self.cli("import-memory", "antigravity")["added"], 2)
        self.assertEqual(self.cli("import-memory", "claude")["added"], 0)
        codebuddy_file = self.home / "MEMORY.md"
        codebuddy_file.write_text("WorkBuddy 文件导入样本")
        self.assertEqual(self.cli("import-memory", "workbuddy", "--file", str(codebuddy_file))["added"], 1)
        self.assertEqual(self.cli("import-memory", "deepseek", "--file", str(codebuddy_file))["added"], 1)
        command = [sys.executable, str(CODE / "manager/manager.py"), "--port", "0"]
        with subprocess.Popen(command, stdout=subprocess.PIPE, text=True, env=self.environment()) as manager:
            try:
                base = manager.stdout.readline().strip().rstrip("/")

                def queue(client):
                    with urlopen(base + f"/?view=memory_import&client={client}", timeout=5) as response:
                        return response.read().decode()

                cursor = queue("cursor")
                self.assertIn("选择记忆文件", cursor)
                csrf = re.search(r'name="csrf" value="([^"]+)"', cursor)[1]
                fields = dict(csrf=csrf, client="cursor", op="text", title="Cursor 导入标题", body="Cursor 原文标记")
                request = Request(base + "/memory_import", data=urlencode(fields).encode(), headers={"Origin": base})
                with urlopen(request, timeout=8) as response:
                    self.assertIn("新增 1 条", response.read().decode())
                self.assertIn("Cursor 导入标题", queue("cursor"))
                self.assertIn("待确认 · 1 条", queue("workbuddy"))
                self.assertIn("待确认 · 2 条", queue("claude"))
                self.assertIn("待确认 · 2 条", queue("antigravity"))
                self.assertIn("待确认 · 1 条", queue("deepseek"))
                self.assertNotIn("Cursor 导入标题", queue("workbuddy"))
                self.assertEqual((self.library / "MEMORY.md").read_bytes(), initial)
                self.assertEqual(self.tool("memory_search", {"query": "Cursor 原文标记"}), [])
                self.assertEqual(codebuddy_file.read_text(), "WorkBuddy 文件导入样本")
            finally:
                manager.terminate()

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

    def test_cursor_hooks_are_merged_reversible_and_honestly_reported(self):
        (self.home / ".cursor").mkdir()
        hooks_file = self.home / ".cursor/hooks.json"
        mine = {"command": "./hooks/mine.sh"}
        hooks_file.write_text(json.dumps({"version": 1, "hooks": {"stop": [mine]}}), encoding="utf-8")
        self.cli("connect", "cursor")
        data = json.loads(hooks_file.read_text())
        self.assertEqual(set(data["hooks"]), {"stop", "sessionStart", "beforeSubmitPrompt"})
        self.assertEqual(data["hooks"]["stop"][0], mine)
        stop_ours = data["hooks"]["stop"][1]
        self.assertEqual(stop_ours["loop_limit"], 1)
        self.assertIn("umem-hook", stop_ours["command"])
        cursor = next(a for a in self.cli("doctor")["agents"] if a["id"] == "cursor")
        self.assertEqual(cursor["components"]["hooks"]["state"], "managed")
        self.cli("connect", "cursor")  # idempotent
        self.assertEqual(json.loads(hooks_file.read_text()), data)
        # Tampering shows up as drift instead of a false green.
        data["hooks"]["sessionStart"][0]["command"] += " --stale"
        hooks_file.write_text(json.dumps(data), encoding="utf-8")
        cursor = next(a for a in self.cli("doctor")["agents"] if a["id"] == "cursor")
        self.assertEqual((cursor["components"]["hooks"]["state"], cursor["state"]), ("drifted", "drifted"))
        self.cli("disconnect", "cursor")
        self.assertEqual(json.loads(hooks_file.read_text()), {"version": 1, "hooks": {"stop": [mine]}})

        # When we created the file ourselves, disconnect removes it entirely.
        hooks_file.unlink()
        self.cli("connect", "cursor")
        self.assertTrue(hooks_file.is_file())
        self.cli("disconnect", "cursor")
        self.assertFalse(hooks_file.exists())

    def test_claude_and_codex_nested_hooks_merge_trigger_and_disconnect_cleanly(self):
        (self.home / ".claude").mkdir(exist_ok=True)
        (self.home / ".codex").mkdir(exist_ok=True)
        claude_settings = self.home / ".claude/settings.json"
        codex_hooks = self.home / ".codex/hooks.json"
        foreign_hook = {"hooks": [{"type": "command", "command": "/usr/local/bin/other-hook", "timeout": 30}]}
        claude_settings.write_text(
            json.dumps({"theme": "dark", "hooks": {"Stop": [foreign_hook], "PreToolUse": [{"hooks": []}]}}),
            encoding="utf-8",
        )
        codex_hooks.write_text(json.dumps({"hooks": {"SessionStart": [foreign_hook]}}), encoding="utf-8")

        self.cli("connect", "claude")
        self.cli("connect", "codex")

        claude_data = json.loads(claude_settings.read_text(encoding="utf-8"))
        self.assertEqual(claude_data["theme"], "dark")
        self.assertEqual(claude_data["hooks"]["PreToolUse"], [{"hooks": []}])
        self.assertEqual(claude_data["hooks"]["Stop"][0], foreign_hook)
        self.assertEqual(
            set(claude_data["hooks"]),
            {"PreToolUse", "SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"},
        )
        codex_data = json.loads(codex_hooks.read_text(encoding="utf-8"))
        self.assertEqual(codex_data["hooks"]["SessionStart"][0], foreign_hook)
        self.assertEqual(
            set(codex_data["hooks"]),
            {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"},
        )

        # Idempotent and reported managed.
        self.cli("connect", "claude")
        self.cli("connect", "codex")
        self.assertEqual(json.loads(claude_settings.read_text(encoding="utf-8")), claude_data)
        self.assertEqual(json.loads(codex_hooks.read_text(encoding="utf-8")), codex_data)
        agents = {a["id"]: a for a in self.cli("doctor")["agents"]}
        self.assertEqual(agents["claude"]["components"]["hooks"]["state"], "managed")
        self.assertEqual(agents["codex"]["components"]["hooks"]["state"], "managed")

        # Tampering is reported as drift.
        codex_data["hooks"]["Stop"][0]["hooks"][0]["command"] += " --stale"
        codex_hooks.write_text(json.dumps(codex_data), encoding="utf-8")
        codex = next(a for a in self.cli("doctor")["agents"] if a["id"] == "codex")
        self.assertEqual((codex["components"]["hooks"]["state"], codex["state"]), ("drifted", "drifted"))

        # Runtime protocol test for nested hooks: SessionStart + tool-count Stop trigger + loop/subagent guards.
        def run_nested(event, client, payload, **extra):
            out = subprocess.check_output(
                [
                    sys.executable,
                    str(CODE / "scripts/cursor_hook.py"),
                    event,
                    "--client",
                    client,
                    "--memory-root",
                    str(self.library),
                ],
                input=json.dumps(payload),
                env={**self.environment(), **extra},
                cwd=self.home,
                text=True,
            )
            return json.loads(out)

        start = run_nested("SessionStart", "codex", {"session_id": "s1"})
        self.assertEqual(start["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("mode='append'", start["hookSpecificOutput"]["additionalContext"])

        # Subagents stay quiet on SessionStart and Stop.
        self.assertEqual(run_nested("SessionStart", "codex", {"session_id": "s1", "agent_id": "sub1"}), {})

        # Short turn with >= 4 tool calls triggers a nested block-reason nudge even before MIN_TURN_SECONDS.
        run_nested("UserPromptSubmit", "codex", {"session_id": "s1", "prompt": "排查部署问题"})
        for _ in range(4):
            run_nested("PostToolUse", "codex", {"session_id": "s1", "tool_name": "Bash"})
        self.assertEqual(
            run_nested("Stop", "codex", {"session_id": "s1", "stop_hook_active": True}, UNIFIED_MEMORY_NUDGE_MIN_SECONDS="9999"),
            {},
        )
        self.assertEqual(
            run_nested("Stop", "codex", {"session_id": "s1", "agent_id": "sub1"}, UNIFIED_MEMORY_NUDGE_MIN_SECONDS="9999"),
            {},
        )
        nudge = run_nested("Stop", "codex", {"session_id": "s1"}, UNIFIED_MEMORY_NUDGE_MIN_SECONDS="9999")
        self.assertEqual(nudge.get("decision"), "block")
        self.assertIn("【记忆沉淀检查】", nudge.get("reason", ""))
        # Cooldown suppresses an immediate repeat.
        self.assertEqual(run_nested("Stop", "codex", {"session_id": "s1"}, UNIFIED_MEMORY_NUDGE_MIN_SECONDS="0"), {})

        # Mid-turn PostToolUse emits a non-blocking checkpoint after sustained work without a write.
        run_nested("UserPromptSubmit", "codex", {"session_id": "s2", "prompt": "长任务排查"})
        checkpoint = {}
        for _ in range(3):
            checkpoint = run_nested(
                "PostToolUse",
                "codex",
                {"session_id": "s2", "tool_name": "Bash"},
                UNIFIED_MEMORY_CHECKPOINT_TOOLS="3",
            )
        self.assertEqual(checkpoint.get("hookSpecificOutput", {}).get("hookEventName"), "PostToolUse")
        self.assertIn("【阶段记忆提醒】", checkpoint.get("hookSpecificOutput", {}).get("additionalContext", ""))
        # A memory write resets tools_since_write so immediate work doesn't re-trigger checkpoint or Stop.
        run_nested("PostToolUse", "codex", {"session_id": "s2", "tool_name": "mcp__unified_memory__memory_upsert"})
        write_event = {"source": "mcp", "client": "Codex", "tool": "memory_upsert", "ok": True}
        subprocess.check_call(
            [
                sys.executable,
                "-c",
                f"import sys; sys.path.insert(0, {str(CODE / 'scripts')!r}); import audit_log; audit_log.record({write_event!r})",
            ],
            env=self.environment(),
            cwd=self.home,
        )
        self.assertEqual(
            run_nested("Stop", "codex", {"session_id": "s2"}, UNIFIED_MEMORY_NUDGE_MIN_SECONDS="0", UNIFIED_MEMORY_NUDGE_COOLDOWN="0"),
            {},
        )
        # Substantial post-write work in the same long turn re-enables Stop reminder instead of staying silent forever.
        for _ in range(2):
            run_nested("PostToolUse", "codex", {"session_id": "s2", "tool_name": "Bash"}, UNIFIED_MEMORY_CHECKPOINT_TOOLS="99")
        post_write_nudge = run_nested(
            "Stop",
            "codex",
            {"session_id": "s2"},
            UNIFIED_MEMORY_NUDGE_MIN_SECONDS="0",
            UNIFIED_MEMORY_NUDGE_COOLDOWN="0",
            UNIFIED_MEMORY_POST_WRITE_MIN_TOOLS="2",
        )
        self.assertEqual(post_write_nudge.get("decision"), "block")
        self.assertIn("【记忆沉淀检查】", post_write_nudge.get("reason", ""))

        # Disconnect restores both files without touching unrelated settings or foreign hooks.
        self.cli("disconnect", "claude")
        self.cli("disconnect", "codex")
        self.assertEqual(
            json.loads(claude_settings.read_text(encoding="utf-8")),
            {"theme": "dark", "hooks": {"Stop": [foreign_hook], "PreToolUse": [{"hooks": []}]}},
        )
        self.assertEqual(
            json.loads(codex_hooks.read_text(encoding="utf-8")),
            {"hooks": {"SessionStart": [foreign_hook]}},
        )

        # If we created ~/.codex/hooks.json from scratch, disconnect removes it cleanly.
        codex_hooks.unlink()
        self.cli("connect", "codex")
        self.assertTrue(codex_hooks.is_file())
        self.cli("disconnect", "codex")
        self.assertFalse(codex_hooks.exists())

    def test_audit_log_hooks_and_dashboard(self):
        def mcp_session(*calls):
            lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "cursor-vscode"}}}]
            for i, (name, args) in enumerate(calls, start=2):
                lines.append({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                              "params": {"name": name, "arguments": args}})
            subprocess.check_output([sys.executable, str(CODE / "mcp/server.py")],
                                    input="".join(json.dumps(m) + "\n" for m in lines),
                                    env=self.environment(), cwd=self.home, text=True)

        mcp_session(("memory_upsert", dict(layer="L2", name="csv-pref", title="周报用 CSV",
                                           description="周报一律 CSV", body="SECRET-BODY-TEXT")),
                    ("memory_search", {"query": "CSV"}), ("memory_search", {"query": "不存在的主题"}))
        audit = (self.home / ".local/state/unified-memory/global/audit.jsonl").read_text()
        events = [json.loads(line) for line in audit.splitlines()]
        self.assertEqual([e["tool"] for e in events], ["memory_upsert", "memory_search", "memory_search"])
        self.assertTrue(all(e["client"] == "Cursor" for e in events))
        self.assertEqual((events[1]["hits"], events[2]["hits"]), (1, 0))
        self.assertNotIn("SECRET-BODY-TEXT", audit)  # bodies are never logged

        def hook(event, payload, **extra):
            out = subprocess.check_output(
                [sys.executable, str(CODE / "scripts/cursor_hook.py"), event, "--memory-root", str(self.library)],
                input=json.dumps(payload), env={**self.environment(), **extra}, cwd=self.home, text=True)
            return json.loads(out)

        context = hook("sessionStart", {"conversation_id": "c1"})["additional_context"]
        self.assertIn("memory_search", context)
        self.assertIn("周报用 CSV", context)
        injected = [json.loads(line) for line in
                    (self.home / ".local/state/unified-memory/global/audit.jsonl").read_text().splitlines()]
        session = [e for e in injected if e.get("event") == "sessionStart"][-1]
        self.assertGreater(session["tokens"], 0)  # injected context size is measured
        fast = {"UNIFIED_MEMORY_NUDGE_MIN_SECONDS": "0"}
        hook("beforeSubmitPrompt", {"conversation_id": "c1", "prompt": "做点事"})
        stop = {"conversation_id": "c1", "status": "completed", "loop_count": 0}
        self.assertEqual(hook("stop", stop), {})  # short turn: stay silent
        self.assertEqual(hook("stop", {**stop, "loop_count": 1}, **fast), {})  # never loops
        self.assertIn("followup_message", hook("stop", stop, **fast))
        self.assertEqual(hook("stop", stop, **fast), {})  # cooldown
        # A write during the turn suppresses the nudge.
        hook("beforeSubmitPrompt", {"conversation_id": "c2", "prompt": "再做点事"})
        mcp_session(("memory_upsert", dict(layer="L2", name="csv-pref", title="周报用 CSV",
                                           description="周报一律 CSV(更新)", body="x")))
        self.assertEqual(hook("stop", {**stop, "conversation_id": "c2"}, **fast), {})
        # A write by a different client must not silence Cursor's own reminder.
        hook("beforeSubmitPrompt", {"conversation_id": "c3", "prompt": "第三件事"})
        other = {"source": "mcp", "client": "Codex", "tool": "memory_upsert", "ok": True}
        subprocess.check_call([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(CODE / 'scripts')!r}); "
                               f"import audit_log; audit_log.record({other!r})"], env=self.environment(), cwd=self.home)
        self.assertIn("followup_message", hook("stop", {**stop, "conversation_id": "c3"},
                                               UNIFIED_MEMORY_NUDGE_COOLDOWN="0", **fast))

        command = [sys.executable, str(CODE / "manager/manager.py"), "--port", "0", "--memory-root", str(self.library)]
        with subprocess.Popen(command, env=self.environment(), stdout=subprocess.PIPE, text=True) as process:
            try:
                address = process.stdout.readline().strip()
                with urlopen(address, timeout=5) as response:
                    page = response.read().decode()
                for expected in ("AI 读写动态", "今日写入 3 次", "不存在的主题", "沉淀提醒 2 次", "上下文用量", "搜了没读", "结果被读取"):
                    self.assertIn(expected, page)
            finally:
                process.terminate()

    def test_stable_launchers_survive_runtime_moves_and_migrate_legacy_configs(self):
        (self.home / ".cursor").mkdir()
        legacy_mcp = {"command": "/old/python3", "args": [str(CODE / "mcp/server.py"), "--memory-root", str(self.library)]}
        (self.home / ".cursor/mcp.json").write_text(json.dumps({"mcpServers": {"unified_memory": legacy_mcp}}))
        legacy_hook = {"command": f"/old/python3 {CODE}/scripts/cursor_hook.py stop --memory-root {self.library}"}
        (self.home / ".cursor/hooks.json").write_text(json.dumps({"version": 1, "hooks": {"stop": [legacy_hook]}}))
        cursor = lambda: next(a for a in self.cli("doctor")["agents"] if a["id"] == "cursor")
        self.assertEqual(cursor()["state"], "drifted")  # legacy direct paths are flagged, not accepted
        self.cli("connect", "cursor")
        self.assertEqual(cursor()["state"], "managed")
        launcher = self.home / "Library/Application Support/Unified Memory/bin/umem-mcp"
        entry = json.loads((self.home / ".cursor/mcp.json").read_text())["mcpServers"]["unified_memory"]
        self.assertEqual(entry["command"], str(launcher))
        stop_hooks = json.loads((self.home / ".cursor/hooks.json").read_text())["hooks"]["stop"]
        self.assertEqual(len(stop_hooks), 1)  # legacy entry replaced, not duplicated

        def initialize(command, **extra_env):
            result = subprocess.run([command, "--memory-root", str(self.library)],
                                    env={**self.environment(), **extra_env}, cwd=self.home,
                                    input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
                                    capture_output=True, text=True)
            return result

        self.assertIn("unified-memory", initialize(str(launcher)).stdout)
        # Simulate the app being moved/replaced: the recorded runtime path disappears.
        broken = launcher.read_text().replace(str(CODE), str(self.home / "gone"))
        launcher.write_text(broken)
        result = initialize(str(launcher), UNIFIED_MEMORY_DISABLE_APP_FALLBACK="1")
        self.assertEqual(result.returncode, 127)
        self.assertIn("runtime not found", result.stderr)
        # Any app/CLI run regenerates the launcher; configs never needed to change.
        changed = self.cli("install-shims")["changed"]
        self.assertIn(str(launcher), changed)
        self.assertIn("unified-memory", initialize(str(launcher)).stdout)
        self.assertEqual(self.cli("install-shims")["changed"], [])  # idempotent

    def test_desktop_mode_pages_lifecycle_and_launchers(self):
        self.tool("memory_upsert", dict(layer="L3", name="desk", title="桌面样本", description="桌面描述", body="正文"))
        command = [sys.executable, str(CODE / "manager/manager.py"), "--desktop", "--port", "0",
                   "--memory-root", str(self.library)]
        process = subprocess.Popen(command, env=self.environment(), cwd=self.home, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            address = process.stdout.readline().strip()
            self.assertRegex(address, r"^http://127\.0\.0\.1:\d+/$")
            for query, expected in (("", "桌面样本"), ("?view=activity", "AI 读写动态"), ("?view=clients", "跨 Agent 统一接入控制"),
                                    ("?tab=skills", "技能"), ("?view=sync_hub", "同步")):
                with self.subTest(query=query):
                    with urlopen(address + query, timeout=5) as response:
                        page = response.read().decode()
                    self.assertRegex(page, r'<body class="desk(?: [^"]*)?">')
                    self.assertIn("读写动态", page)  # desktop navigation present on every page
                    self.assertIn(expected, page)
            self.assertTrue((self.home / "Library/Application Support/Unified Memory/bin/umem-mcp").is_file())
            with urlopen(address + "style.css", timeout=5) as response:
                self.assertIn(".rail", response.read().decode())  # desktop stylesheet appended
            process.stdin.close()  # the window owns stdin: EOF must stop the backend
            self.assertEqual(process.wait(timeout=10), 0)
        finally:
            if process.poll() is None:
                process.kill()
            process.stdout.close()
            process.stderr.close()

    def test_sync_remote_delete_vs_local_edit_and_nested_repo_detection(self):
        sys.path.insert(0, str(CODE / "scripts"))
        import sync_git
        from unittest import mock

        bare_remote = self.home / "remote.git"
        subprocess.check_call(["git", "init", "--bare", "-b", "main", str(bare_remote)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.cli("sync", "setup", "personal", "--remote-url", str(bare_remote), "--branch", "main")
        self.tool("memory_upsert", dict(layer="L1", name="doomed", title="将被删", description="d", body="原文"))
        self.assertEqual(self.cli("sync", "run", "personal")["results"][0]["status"], "ok")

        home_b, lib_b = self.home / "b-home", self.home / "b-lib"
        home_b.mkdir()
        subprocess.check_call(["git", "clone", str(bare_remote), str(lib_b)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        env_b = {**os.environ, "HOME": str(home_b), "UNIFIED_MEMORY_ROOT": str(lib_b), "PYTHONDONTWRITEBYTECODE": "1"}

        def cli_b(*args):
            return json.loads(subprocess.check_output([sys.executable, str(CODE / "scripts/memoryctl.py"), *args],
                                                      env=env_b, cwd=home_b, text=True))

        cli_b("sync", "setup", "personal", "--remote-url", str(bare_remote), "--branch", "main")
        # Device A deletes the entry and syncs; device B edits the same entry concurrently.
        (self.library / "methods/doomed.md").unlink()
        self.cli("rebuild-index", "personal")
        self.assertEqual(self.cli("sync", "run", "personal")["results"][0]["status"], "ok")
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_upsert", "arguments": dict(
            layer="L1", name="doomed", title="将被删", description="d", body="B机的修订")}}
        subprocess.check_output([sys.executable, str(CODE / "mcp/server.py")], input=json.dumps(request) + "\n",
                                env=env_b, cwd=home_b, text=True)
        result = cli_b("sync", "run", "personal")["results"][0]
        self.assertEqual(result["status"], "needs_manual_merge", result)  # never silently resurrected
        conflict = result["pull"]["conflicts"][0]
        self.assertEqual(conflict["kind"], "remote_deleted")
        self.assertIn("B机的修订", (lib_b / "methods/doomed.md").read_text(encoding="utf-8"))
        self.assertIn(sync_git.REMOTE_DELETED_SENTINEL, (lib_b / conflict["remote_copy"]).read_text(encoding="utf-8"))
        remote_tree = subprocess.check_output(["git", "--git-dir", str(bare_remote), "ls-tree", "-r", "--name-only", "main"],
                                              text=True)
        self.assertNotIn("methods/doomed.md", remote_tree)  # nothing was pushed back
        # Accepting the remote deletion removes the entry (old content goes to local trash).
        with mock.patch.dict(os.environ, {"UNIFIED_MEMORY_STATE_DIR": str(self.home / "state-b")}):
            sync_git.resolve_conflict(lib_b, conflict["remote_copy"], "use_remote")
        self.assertFalse((lib_b / "methods/doomed.md").exists())
        self.assertTrue(cli_b("doctor")["ok"])
        self.assertEqual(cli_b("sync", "run", "personal")["results"][0]["status"], "ok")

        # A library nested inside another repository must not inherit the parent's repo.
        parent = self.home / "dotfiles"
        parent.mkdir()
        subprocess.check_call(["git", "init", "-b", "main", str(parent)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        nested = parent / "memory"
        nested.mkdir()
        self.assertFalse(sync_git.is_git_repo(nested))
        self.assertTrue(sync_git.is_git_repo(parent))
        sync_git.ensure_repo(nested)
        self.assertTrue(sync_git.is_git_repo(nested))

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

        # Search defaults to personal; explicit '*' or 'all' opts into mounted libraries.
        self.assertEqual(self.tool("memory_search", {"query": "排查"}), [])
        hits = self.tool("memory_search", {"query": "排查", "library": "*"})
        self.assertEqual(hits[0]["library"], "team-rw")
        self.assertEqual(hits[0]["ref"], "team-rw:methods/k8s-debug.md")
        self.assertEqual(hits[0]["source_library"], "team-rw")
        self.assertEqual((hits[0]["provenance"], hits[0]["trust"], hits[0]["read_only"]), ("shared", "shared", False))
        self.assertEqual(self.tool("memory_search", {"query": "排查", "library": "all"})[0]["ref"],
                         "team-rw:methods/k8s-debug.md")

        ro_hits = self.tool("memory_search", {"query": "核对", "library": "team-ro"})
        self.assertEqual((ro_hits[0]["source_library"], ro_hits[0]["trust"], ro_hits[0]["read_only"]),
                         ("team-ro", "shared", True))
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

    def test_default_personal_scope_mount_diagnostics_and_symlink_rejection(self):
        missing = self.home / "missing-library"
        self.cli("mount", "add", "broken", "--path", str(missing), "--label", "不可用", "--mode", "rw")
        # A mounted library becomes unavailable if its required index disappears after mounting.
        (missing / "MEMORY.md").unlink()
        config_path = self.home / ".config" / "unified-memory" / "mounts.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["mounts"].append(
            {"id": "bad-mode", "path": str(self.home / "bad-mode"), "mode": "admin"}
        )
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        self.tool("memory_upsert", dict(layer="L1", name="personal-only", title="个人方法",
                                        description="仅个人", body="个人专用方法"))

        libs = self.tool("memory_libraries", {})
        broken = next(lib for lib in libs if lib["id"] == "broken")
        self.assertFalse(broken["available"])
        self.assertTrue(broken["error"])
        bad_mode = next(lib for lib in libs if lib["id"] == "bad-mode")
        self.assertFalse(bad_mode["available"])
        self.assertIn("Invalid mount mode 'admin'", bad_mode["error"])
        self.assertEqual(bad_mode["issues"][0]["code"], "invalid_entry")
        self.assertEqual(self.tool("memory_search", {"query": "个人"})[0]["library"], "personal")

        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "memory_search", "arguments": {"query": "个人", "library": "broken"}}}
        output = subprocess.check_output([sys.executable, str(CODE / "mcp/server.py")],
                                         input=json.dumps(request) + "\n", env=self.environment(), text=True)
        result = json.loads(output)["result"]
        self.assertTrue(result.get("isError"), output)
        self.assertIn("Library 'broken' is unavailable", result["content"][0]["text"])

        request["params"]["arguments"] = {"query": "个人", "library": "bad-mode"}
        output = subprocess.check_output([sys.executable, str(CODE / "mcp/server.py")],
                                         input=json.dumps(request) + "\n", env=self.environment(), text=True)
        result = json.loads(output)["result"]
        self.assertTrue(result.get("isError"), output)
        self.assertIn("Invalid mount mode 'admin'", result["content"][0]["text"])

        outside = self.home / "outside.md"
        outside.write_text("# outside\n", encoding="utf-8")
        symlink = self.library / "methods" / "linked.md"
        symlink.symlink_to(outside)
        request["params"]["arguments"] = {"query": "个人"}
        output = subprocess.check_output([sys.executable, str(CODE / "mcp/server.py")],
                                         input=json.dumps(request) + "\n", env=self.environment(), text=True)
        result = json.loads(output)["result"]
        self.assertTrue(result.get("isError"), output)
        self.assertIn("Symlinked memory file is not allowed", result["content"][0]["text"])

        # A corrupt mounts.json must remain visible in the Web sync hub rather than
        # turning the whole page into a 500 or silently dropping the diagnostics.
        config_path.write_text("{not json", encoding="utf-8")
        command = [sys.executable, str(CODE / "manager/manager.py"), "--port", "0",
                   "--memory-root", str(self.library)]
        with subprocess.Popen(command, env=self.environment(), stdout=subprocess.PIPE, text=True) as process:
            try:
                address = process.stdout.readline().strip()
                hub_html = urlopen(address + "?view=sync_hub", timeout=5).read().decode()
                self.assertIn("配置问题：invalid_config", hub_html)
                self.assertIn("not valid JSON", hub_html)
            finally:
                process.terminate()

    def test_multi_host_credentials_and_failure_hints(self):
        sys.path.insert(0, str(CODE / "scripts"))
        import sync_git
        from unittest.mock import patch

        gitee = sync_git.https_remote("https://chenwenxin01@gitee.com/team/memory.git")
        self.assertEqual((gitee["host"], gitee["username"], gitee["provider"]), ("gitee.com", "chenwenxin01", "Gitee"))
        self.assertIsNone(sync_git.https_remote("git@gitee.com:team/memory.git"))
        self.assertEqual(sync_git.auth_method("git@gitee.com:team/memory.git"), "ssh")
        self.assertEqual(sync_git.auth_method("https://git.example.com/team/memory.git"), "https-credential")
        self.assertEqual(sync_git.auth_method(str(self.home)), "local")

        helper = self.home / "fake-credential-helper"
        helper.write_text("#!/bin/sh\nexit 0\n")
        helper.chmod(0o755)
        with patch.dict(os.environ, {"UNIFIED_MEMORY_GIT_CREDENTIAL_HELPER": str(helper)}):
            env = {}
            options = sync_git._credential_options("https://git.example.com/team/memory.git", False, env)
        self.assertIn("credential.https://git.example.com.helper=!" + str(helper), options)
        self.assertFalse(any("credential.https://github.com.interactive=true" == o for o in options))

        url = "https://git.example.com/team/memory.git"
        denied = sync_git._git_failure("git clone", "remote: \x1b[31mIncorrect username or password (access token)\x1b[0m\n"
                                       f"fatal: repository '{url}/' not found", url)
        self.assertIn("本机 Git 未能通过远端身份验证", str(denied))
        self.assertIn("git.example.com", str(denied))
        self.assertNotIn("GitHub", str(denied))
        self.assertNotIn("\x1b", str(denied))
        missing = sync_git._git_failure("git fetch", "remote: Repository not found.", url)
        self.assertIn("凭据不正确", str(missing))
        self.assertNotIn("GitHub", str(missing))
        self.assertIn("GitHub", str(sync_git._git_failure("git fetch", "remote: Repository not found.",
                                                           "https://github.com/a/b.git")))

        # A failed clone removes the directory it created, so the mount form can be retried.
        target = self.home / "Shared" / "team"
        failed = subprocess.CompletedProcess([], 128, "", "fatal: could not read Username: terminal prompts disabled")
        def fake_clone(root, args, **kwargs):
            target.mkdir()
            return failed
        with patch.object(sync_git, "_git_process", side_effect=fake_clone):
            with self.assertRaisesRegex(sync_git.GitSyncError, "未能通过远端身份验证"):
                sync_git.clone_repo(url, target)
        self.assertFalse(target.exists())

    def test_remote_read_probe_and_auth_failure(self):
        from unittest.mock import patch
        sys.path.insert(0, str(CODE / "scripts"))
        import sync_git

        remote = self.home / "empty.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        self.cli("sync", "setup", "personal", "--remote-url", str(remote))
        def refs(directory):
            return subprocess.run(["git", "-C", str(directory), "for-each-ref"],
                                  check=True, capture_output=True).stdout
        before = refs(self.library)
        sync_git.test_remote_access(self.library)
        self.assertEqual(refs(self.library), before)
        self.assertEqual(refs(remote), b"")
        denied = subprocess.CompletedProcess([], 128, "", "fatal: could not read Username: terminal prompts disabled")
        with patch.object(sync_git, "_git_process", return_value=denied):
            with self.assertRaisesRegex(sync_git.GitSyncError, "本机 Git 未能通过远端身份验证"):
                sync_git.test_remote_access(self.library)

    def test_github_network_route_and_transient_handshake(self):
        from unittest.mock import patch
        sys.path.insert(0, str(CODE / "scripts"))
        import sync_git

        env = {"HOME": str(self.home), "PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1"}
        url = "https://github.com/"
        proxy = "http://127.0.0.1:34567"
        with patch.object(sync_git.sys, "platform", "darwin"), patch.object(sync_git, "getproxies", return_value={"https": proxy}), \
                patch.object(sync_git, "proxy_bypass", return_value=False) as bypass:
            options = sync_git._github_proxy_options(self.home, env)
            self.assertEqual(options, ["-c", f"http.{url}.proxy={proxy}"])
            for variable in ("https_proxy", "HTTPS_PROXY", "ALL_PROXY"):
                self.assertEqual(sync_git._github_proxy_options(self.home, {**env, variable: ""}), [])
            bypass.return_value = True
            self.assertEqual(sync_git._github_proxy_options(self.home, env), [])
            bypass.return_value = False
            subprocess.run(["git", "config", "--global", "http.proxy", ""], env=env, check=True)
            self.assertEqual(sync_git._github_proxy_options(self.home, env), [])

        failure = subprocess.CompletedProcess([], 128, "", "LibreSSL SSL_connect: SSL_ERROR_SYSCALL")
        ok = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(sync_git.time, "sleep"), patch.object(sync_git, "_git_process", side_effect=[failure, ok]) as run:
            sync_git.test_remote_access(self.library)
            self.assertEqual(run.call_count, 2)
            self.assertTrue(all(c.args[1] == ("ls-remote", "--", "origin") for c in run.call_args_list))
        with patch.object(sync_git.time, "sleep"), patch.object(sync_git, "_git_process", return_value=failure) as run:
            with self.assertRaisesRegex(sync_git.GitSyncError, "HTTPS 安全连接时中断"):
                sync_git.test_remote_access(self.library)
            self.assertEqual(run.call_count, 2)
        for msg in ("SSL certificate problem: certificate has expired", "authentication failed"):
            with patch.object(sync_git, "_git_process", return_value=subprocess.CompletedProcess([], 128, "", msg)) as run:
                with self.assertRaises(sync_git.GitSyncError):
                    sync_git.test_remote_access(self.library)
                self.assertEqual(run.call_count, 1)

    def test_browser_onboarding_cancel_retry_sync_and_join(self):
        import shutil
        import time
        from urllib.error import HTTPError

        # Exercise the normal HTTP forms and real Git commits, clone, and push. Only the
        # identity provider and network destination are substituted; no real accounts are used.
        bundle = self.home / "Portable Git"
        bindir = bundle / "bin"
        core = bundle / "libexec/git-core"
        bindir.mkdir(parents=True)
        core.mkdir(parents=True)
        git = shutil.which("git")
        system_core = subprocess.check_output([git, "--exec-path"], text=True).strip()
        bare = self.home / "team.git"
        subprocess.run([git, "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        url = "https://github.com/example/team.git"
        helper = core / "git-credential-manager"
        helper.write_text("#!" + sys.executable + "\n" + r"""
import os, subprocess, sys, time
from pathlib import Path
home = Path(os.environ["HOME"])
fields = dict(line.split("=", 1) for line in sys.stdin.read().splitlines() if "=" in line)
if fields.get("host") != "github.com":
    sys.exit(1)
if sys.argv[1] == "store":
    (home / "session").touch()
elif sys.argv[1] == "get":
    if not (home / "session").exists():
        def setting(key):
            return subprocess.check_output([os.environ["REAL_GIT"], "config", "--get-urlmatch",
                                            "credential." + key, "https://github.com"], text=True).strip()
        assert setting("interactive") == "true" and setting("gitHubAuthModes") == "browser"
        (home / "opened").touch()
        for _ in range(300):
            if (home / "answer").exists(): break
            time.sleep(.05)
        if (home / "answer").read_text() != "allow":
            sys.stderr.write("access_denied\n")
            print("quit=true")
            sys.exit(0)
    print("username=fixture\npassword=test-only")
""")
        wrapper = bindir / "git"
        wrapper.write_text("#!" + sys.executable + "\n" + r"""
import os, subprocess, sys
git = os.environ["REAL_GIT"]
os.environ["GIT_EXEC_PATH"] = os.environ["REAL_GIT_CORE"]
os.environ.pop("GIT_TEMPLATE_DIR", None)
args = sys.argv[1:]
network = next((i for i, arg in enumerate(args) if arg in ("ls-remote", "fetch", "push", "clone")), None)
if network is not None:
    settings = args[:network]
    fill = subprocess.run([git, *settings, "credential", "fill"], input=b"protocol=https\nhost=github.com\n\n",
                          stdout=subprocess.PIPE)
    if fill.returncode: sys.exit(fill.returncode)
    subprocess.run([git, *settings, "credential", "approve"], input=fill.stdout, check=True)
    args = ["-c", "url." + os.environ["TEST_BARE"] + ".insteadOf=" + os.environ["TEST_URL"], *args]
os.execv(git, [git, *args])
""")
        helper.chmod(0o755)
        wrapper.chmod(0o755)
        env = {**self.environment(), "REAL_GIT": git, "REAL_GIT_CORE": system_core,
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(self.home / ".gitconfig"),
               "UNIFIED_MEMORY_GIT_HOME": str(bundle), "TEST_BARE": str(bare), "TEST_URL": url}
        command = [sys.executable, str(CODE / "manager/manager.py"), "--port", "0"]
        with subprocess.Popen(command, env=env, stdout=subprocess.PIPE, text=True) as process:
            try:
                address = process.stdout.readline().strip().rstrip("/")
                with urlopen(address + "/?view=sync_hub", timeout=10) as response:
                    csrf = re.search(r'name="csrf" value="([^"]+)"', response.read().decode())[1]

                def post(path, **fields):
                    req = Request(address + path, data=urlencode({"csrf": csrf, **fields}).encode())
                    with urlopen(req, timeout=10) as resp:
                        return resp.geturl(), resp.read().decode()

                def refresh_target(page, current_url):
                    target = re.search(r'<meta http-equiv="refresh" content="\d+;url=([^"]+)">', page)
                    self.assertIsNotNone(target, "progress must request its job URL even after a #feedback redirect")
                    next_url = urljoin(current_url, target[1])
                    self.assertEqual(next_url, urldefrag(current_url).url)
                    return next_url

                def finish(location):
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        try:
                            with urlopen(location, timeout=10) as response:
                                page = response.read().decode()
                                if "/sync_progress?" not in response.geturl():
                                    return page
                                location = refresh_target(page, response.geturl())
                        except HTTPError as error:
                            return error.read().decode()
                        time.sleep(.05)
                    self.fail("sync job did not finish")

                setup = dict(remote_url=url, target_library="personal", branch="main", after_save="sync")
                location, page = post("/sync_setup", **setup)
                self.assertIn("正在同步", page)
                self.assertEqual(refresh_target(page, location + "#feedback"), location)
                self.assertEqual(post("/sync", library="primary")[0], location)
                with self.assertRaises(HTTPError) as guard:
                    post("/sync_setup", **setup)
                self.assertEqual(guard.exception.code, 409)
                answer = self.home / "answer"
                answer.write_text("deny")
                self.assertIn("授权已取消", finish(location))
                self.assertTrue((self.home / "opened").exists())

                answer.write_text("allow")
                location, _ = post("/sync_test", library="primary")
                self.assertIn("读取连接成功", finish(location))
                self.assertEqual(subprocess.check_output([git, "-C", str(bare), "for-each-ref"]), b"")
                self.assertFalse((self.library / ".git/refs/heads/main").exists())
                answer.unlink()
                location, _ = post("/sync", library="primary", return_view="sync_hub")
                self.assertIn("同步完成", finish(location))
                subprocess.run([git, "-C", str(bare), "cat-file", "-e", "main:MEMORY.md"], check=True)

                # The same GitHub authorization can run inline while normal pages are usable.
                (self.home / "session").unlink()
                request = Request(address + "/sync", data=urlencode({"csrf": csrf, "library": "primary"}).encode(),
                                  headers={"Accept": "application/json"})
                def start_inline():
                    with urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 202)
                        return json.load(response)["id"]

                def inline_status():
                    with urlopen(address + "/sync_jobs", timeout=5) as response:
                        return json.load(response)[0]

                job_id = start_inline()
                self.assertEqual(start_inline(), job_id)
                with urlopen(address + "/?view=clients", timeout=5) as response:
                    self.assertIn('id="sync-notices"', response.read().decode())
                self.assertEqual(inline_status()["state"], "running")
                answer.write_text("allow")
                deadline = time.monotonic() + 15
                status = inline_status()
                while status["state"] == "running" and time.monotonic() < deadline:
                    time.sleep(.05)
                    status = inline_status()
                self.assertEqual((status["id"], status["state"]), (job_id, "success"))

                location, _ = post("/mount_add", git_url=url, mode="ro", layer_L1="1", layer_L3="1")
                self.assertIn("共享记忆库已挂载", finish(location))
                self.assertTrue((self.home / "UnifiedMemory-Shared/team-team/MEMORY.md").is_file())
                self.assertFalse((self.home / ".gitconfig").exists())
            finally:
                process.terminate()

    def test_first_sync_adopts_remote_history_and_preserves_local_edits(self):
        def git(*args):
            return subprocess.check_output(["git", *map(str, args)], text=True, env=self.environment())

        bare = self.home / "first.git"
        git("init", "-q", "--bare", "-b", "main", bare)
        team = self.home / "shared"
        self.cli("mount", "add", "empty", "--path", str(team), "--mode", "ro", "--git", str(bare))
        self.cli("sync", "run", "empty")
        self.assertEqual(git("-C", bare, "for-each-ref"), "")

        self.cli("init", root=self.other)
        self.cli("sync", "setup", "personal", "--remote-url", str(bare), root=self.other)
        self.tool("memory_upsert", dict(layer="L1", name="welcome", title="入门", description="欢迎", body="团队记忆"),
                  root=self.other)
        self.cli("sync", "run", "personal", root=self.other)
        revision = git("-C", bare, "rev-parse", "main")
        self.cli("sync", "run", "empty")
        self.assertEqual(git("-C", team, "rev-parse", "HEAD"), revision)
        self.assertEqual(git("-C", team, "status", "--porcelain"), "")
        self.assertIn("团队记忆", (team / "methods/welcome.md").read_text())

        self.cli("sync", "setup", "personal", "--remote-url", str(bare))
        self.cli("sync", "run", "personal")
        self.assertEqual(git("-C", self.library, "rev-parse", "HEAD"), revision)
        self.assertEqual(git("-C", bare, "rev-parse", "main"), revision)

        edited = self.home / "existing"
        self.cli("init", root=edited)
        (edited / "MEMORY.md").write_text("我的已有记录")
        self.cli("sync", "setup", "personal", "--remote-url", str(bare), root=edited)
        result = subprocess.run([sys.executable, str(CODE / "scripts/memoryctl.py"), "sync", "run", "personal"],
                                cwd=self.home, env=self.environment(edited), capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("独立的提交历史", result.stderr + result.stdout)
        self.assertEqual((edited / "MEMORY.md").read_text(), "我的已有记录")
        self.assertEqual(git("-C", bare, "rev-parse", "main"), revision)

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

    def test_web_p0_sync_hub_share_and_conflict_resolution(self):
        self.tool("memory_upsert", dict(layer="L1", name="deploy-tip", title="发布经验",
                                        description="灰度步骤", body="先切 10% 流量"))
        command = [sys.executable, str(CODE / "manager/manager.py"), "--port", "0",
                   "--memory-root", str(self.library)]
        with subprocess.Popen(command, env=self.environment(), stdout=subprocess.PIPE, text=True) as process:
            try:
                address = process.stdout.readline().strip().rstrip("/")

                def get_html(path):
                    with urlopen(address + path, timeout=5) as resp:
                        return resp.read().decode("utf-8")

                def post_form(path, data):
                    encoded = urlencode(data).encode("utf-8")
                    req = Request(
                        address + path,
                        data=encoded,
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Origin": address,
                        },
                        method="POST",
                    )
                    with urlopen(req, timeout=5) as resp:
                        return resp.read().decode("utf-8")

                home_html = get_html("/")
                self.assertIn("ops-strip", home_html)
                self.assertIn("ops-drawer", home_html)
                m_csrf = re.search(r'name="csrf"\s+value="([^"]+)"', home_html)
                self.assertIsNotNone(m_csrf)
                csrf = m_csrf.group(1)

                # 1. Mount a writable shared library via Sync Hub web form.
                hub_html = get_html("/?view=sync_hub")
                self.assertIn("共享记忆库与 Git 同步中心", hub_html)
                team_dir = self.home / "team-ui-kb"
                after_mount = post_form(
                    "/mount_add",
                    {
                        "csrf": csrf,
                        "library": "primary",
                        "mount_id": "team-ui",
                        "label": "前端团队库",
                        "path": str(team_dir),
                        "mode": "rw",
                        "layer_L1": "1",
                        "layer_L3": "1",
                        "layer_L4": "1",
                        "auto_commit": "1",
                    },
                )
                self.assertIn("前端团队库", after_mount)

                # A legacy URL without ".md" remains readable, while a topic
                # from the personal library is diagnosed instead of silently
                # opening under the selected shared library.
                legacy_topic_html = get_html("/?topic=methods/deploy-tip")
                self.assertIn("发布经验", legacy_topic_html)
                self.assertIn("topic=methods%2Fdeploy-tip.md", legacy_topic_html)
                cross_library_html = get_html("/?library=team-ui&topic=methods/deploy-tip.md")
                self.assertIn("当前记忆库「前端团队库」的索引中没有条目", cross_library_html)
                self.assertIn("返回当前库概览", cross_library_html)
                self.assertIn('href="/?library=team-ui"', cross_library_html)

                # 2. Share a personal topic to the mounted team library via web action.
                topic_html = get_html("/?topic=methods/deploy-tip.md")
                self.assertIn("分享到共享库", topic_html)
                after_share = post_form(
                    "/share_topic",
                    {
                        "csrf": csrf,
                        "library": "primary",
                        "topic": "methods/deploy-tip.md",
                        "target_library": "team-ui",
                        "target_layer": "L1",
                    },
                )
                self.assertIn("已将该个人记忆分享到目标共享库", after_share)
                self.assertTrue((team_dir / "methods/deploy-tip.md").is_file())

                # 3. Create a conflict copy and resolve it via visual conflict workbench.
                conflict_file = self.library / "methods/deploy-tip.conflict-20261120-010101.md"
                conflict_file.write_text(
                    "---\nname: deploy-tip\ndescription: \"远端灰度步骤\"\ntype: reference\n---\n\n# 发布经验\n\n先切 20% 流量\n",
                    encoding="utf-8",
                )
                conflict_html = get_html("/?view=conflicts")
                self.assertIn("diff-view", conflict_html)
                self.assertIn("先切 20% 流量", conflict_html)
                post_form(
                    "/resolve_conflict",
                    {
                        "csrf": csrf,
                        "library": "primary",
                        "conflict_file": "methods/deploy-tip.conflict-20261120-010101.md",
                        "strategy": "custom_merge",
                        "merged_content": (
                            "---\nname: deploy-tip\ndescription: \"合并后的灰度步骤\"\ntype: reference\n---\n\n"
                            "# 发布经验\n\n先切 10% 再扩到 20% 流量\n"
                        ),
                    },
                )
                self.assertFalse(conflict_file.exists())
                self.assertIn("扩到 20% 流量", (self.library / "methods/deploy-tip.md").read_text(encoding="utf-8"))
            finally:
                process.terminate()


if __name__ == "__main__":
    unittest.main()


class PackagedFrameworkTests(unittest.TestCase):
    """The packaged app only ships PUBLIC_FILES, so every runtime import must be in that list."""

    def test_staged_framework_answers_tool_calls(self):
        sys.path.insert(0, str(CODE / "scripts"))
        try:
            from package_release import PUBLIC_FILES
        finally:
            sys.path.pop(0)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        staged = Path(tmp.name) / "framework"
        for name in PUBLIC_FILES:
            if name.startswith("desktop/") or name.startswith("tests/"):
                continue
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((CODE / name).read_bytes())
        home = Path(tmp.name) / "home"
        home.mkdir()
        library = Path(tmp.name) / "library"
        environment = {**os.environ, "HOME": str(home), "UNIFIED_MEMORY_ROOT": str(library),
                       "PYTHONDONTWRITEBYTECODE": "1"}
        subprocess.check_output([sys.executable, str(staged / "scripts/memoryctl.py"), "init"],
                                env=environment, cwd=home, text=True)
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "memory_lint", "arguments": {}}}
        output = subprocess.check_output([sys.executable, str(staged / "mcp/server.py")],
                                         input=json.dumps(request) + "\n", env=environment,
                                         cwd=home, text=True)
        payload = json.loads(json.loads(output)["result"]["content"][0]["text"])
        self.assertIn("findings", payload)
