"""Integration checks with synthetic libraries and isolated client configurations."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import urlencode
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
        launcher = self.home / "Library/Application Support/Unified Memory/bin/umem-mcp"
        self.assertIn(str(launcher), config.read_text())  # clients never point at the source tree / app bundle
        self.assertNotIn(str(CODE / "mcp/server.py"), config.read_text())
        self.assertIn(f"FW={CODE}", launcher.read_text())
        self.assertTrue(report["ok"], report["findings"])  # doctor probes MCP through the launcher
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
                for expected in ("AI 读写动态", "今日写入 3 次", "不存在的主题", "沉淀提醒 2 次"):
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

        def initialize(command):
            result = subprocess.run([command, "--memory-root", str(self.library)], env=self.environment(), cwd=self.home,
                                    input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
                                    capture_output=True, text=True)
            return result

        self.assertIn("unified-memory", initialize(str(launcher)).stdout)
        # Simulate the app being moved/replaced: the recorded runtime path disappears.
        broken = launcher.read_text().replace(str(CODE), str(self.home / "gone"))
        launcher.write_text(broken)
        result = initialize(str(launcher))
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
                    self.assertIn('class="desk"', page)
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
