from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "scripts"))
sys.path.insert(0, str(CODE / "mcp"))

import audit_log  # noqa: E402
import memoryctl  # noqa: E402
import server as mcp_server  # noqa: E402
import sync_git  # noqa: E402


class SyncSecurityTests(unittest.TestCase):
    def test_sync_tolerates_leftover_index_conflict_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env = {
                "HOME": str(base / "home"),
                "UNIFIED_MEMORY_STATE_DIR": str(base / "state"),
                "UNIFIED_MEMORY_CONFIG_DIR": str(base / "config"),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            }
            with mock.patch.dict(os.environ, env):
                bare = base / "remote.git"
                subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
                lib = base / "lib"
                memoryctl.initialize_library(lib)
                sync_git.ensure_repo(lib, remote_url=str(bare), branch="main")
                sync_git.auto_commit(lib, "seed")
                sync_git.push(lib, branch="main")
                (lib / "MEMORY.conflict-20261122-130000.md").write_text("# old index\n", encoding="utf-8")
                (lib / "methods").mkdir(exist_ok=True)
                (lib / "methods/new.md").write_text("# new\n", encoding="utf-8")

                result = sync_git.sync(lib, branch="main", browser_login=False)

                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["push"]["status"], "pushed")
                self.assertIn("MEMORY.conflict-20261122-130000.md", result["pull"]["conflicts"])

    def test_blocking_conflicts_accepts_paths_and_dicts(self):
        self.assertEqual(
            sync_git._blocking_conflicts([
                "MEMORY.conflict-20261122-130000.md",
                "methods/a.conflict-20261122-130000.md",
                {"kind": "index", "conflict_copy": "MEMORY.conflict-20261122-130001.md"},
                {"kind": "both_modified", "conflict_copy": "methods/b.conflict-20261122-130000.md"},
            ]),
            [
                "methods/a.conflict-20261122-130000.md",
                {"kind": "both_modified", "conflict_copy": "methods/b.conflict-20261122-130000.md"},
            ],
        )

    def _two_clones_with_conflict(self, base):
        bare = base / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        a, b = base / "a", base / "b"
        memoryctl.initialize_library(a)
        sync_git.ensure_repo(a, remote_url=str(bare), branch="main")
        (a / "methods/x.md").write_text("# x\n\nbase\n", encoding="utf-8")
        sync_git.auto_commit(a, "seed")
        sync_git.push(a, branch="main")
        sync_git.clone_repo(str(bare), b, branch="main")
        (b / "methods/x.md").write_text("# x\n\nremote\n", encoding="utf-8")
        sync_git.auto_commit(b, "remote edit")
        sync_git.push(b, branch="main")
        (a / "methods/x.md").write_text("# x\n\nlocal\n", encoding="utf-8")
        sync_git.auto_commit(a, "local edit")
        return a

    def _isolated_git_env(self, base):
        return mock.patch.dict(os.environ, {
            "HOME": str(base / "home"),
            "UNIFIED_MEMORY_STATE_DIR": str(base / "state"),
            "UNIFIED_MEMORY_CONFIG_DIR": str(base / "config"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        })

    def test_failed_conflict_resolution_aborts_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self._isolated_git_env(base):
                a = self._two_clones_with_conflict(base)
                with mock.patch.object(sync_git, "rebuild_index", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        sync_git.pull(a, branch="main")
                self.assertFalse((a / ".git/MERGE_HEAD").exists())
                self.assertEqual((a / "methods/x.md").read_text(encoding="utf-8"), "# x\n\nlocal\n")
                self.assertEqual(sync_git.find_conflict_files(a), [])

                # A normal retry still preserves both sides.
                result = sync_git.pull(a, branch="main")
                self.assertEqual(result["status"], "merged_with_conflicts")

    def test_auto_commit_and_sync_refuse_unfinished_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self._isolated_git_env(base):
                a = self._two_clones_with_conflict(base)
                subprocess.run(["git", "-C", str(a), "fetch", "-q", "origin"], check=True)
                merge = subprocess.run(
                    ["git", "-C", str(a), "merge", "--no-edit", "origin/main"],
                    capture_output=True, text=True,
                )
                self.assertNotEqual(merge.returncode, 0)
                head = subprocess.check_output(["git", "-C", str(a), "rev-parse", "HEAD"], text=True)

                result = sync_git.auto_commit(a, "memory: upsert")
                self.assertFalse(result["committed"])
                self.assertEqual(result["reason"], "merge_in_progress")
                with self.assertRaisesRegex(sync_git.GitSyncError, "未完成的 Git 合并"):
                    sync_git.push(a, branch="main")
                with self.assertRaisesRegex(sync_git.GitSyncError, "未完成的 Git 合并"):
                    sync_git.pull(a, branch="main")
                self.assertEqual(
                    subprocess.check_output(["git", "-C", str(a), "rev-parse", "HEAD"], text=True),
                    head,
                )

    def test_mcp_auto_commit_failure_is_a_sync_event_and_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "MEMORY.md").write_text(
                "# Unified Memory\n\n## L0\n\n## L1\n\n## L2\n\n## L3\n\n## L4\n",
                encoding="utf-8",
            )
            for folder in ("constraints", "methods", "preferences", "projects", "archive", "user-managed"):
                (root / folder).mkdir()
            events = []
            with mock.patch.object(
                mcp_server.sync_git,
                "auto_commit",
                side_effect=RuntimeError("https://alice:secret@gitee.com/team/memory.git failed"),
            ), mock.patch.object(
                mcp_server.audit_log,
                "record",
                side_effect=lambda event, **_: events.append(event) or True,
            ):
                store = mcp_server.MemoryStore(
                    root,
                    sync_config={"driver": "git", "auto_commit": True},
                )
                result = store.upsert_entry("L1", "sync-demo", "同步失败", "测试", "正文")

            self.assertIn("sync_warning", result)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["source"], "sync")
            self.assertEqual(events[0]["event"], "git_auto_commit")
            self.assertEqual(events[0]["reason"], "sync_error")
            self.assertNotIn("secret", events[0]["error"])
            self.assertIn("https://***@gitee.com", events[0]["error"])

    def test_remote_passwords_are_rejected_and_errors_redacted(self):
        with self.assertRaisesRegex(ValueError, "不能包含内嵌密码"):
            sync_git.validate_remote_url("https://alice:secret@gitee.com/team/memory.git")
        with self.assertRaisesRegex(ValueError, "不能包含内嵌密码"):
            sync_git.validate_remote_url("alice:secret@git.example.com:team/memory.git")
        self.assertEqual(
            sync_git._redact_remote_urls("fatal: https://alice:secret@gitee.com/team/memory.git"),
            "fatal: https://***@gitee.com/team/memory.git",
        )
        self.assertEqual(
            sync_git._redact_remote_urls("fatal: alice:secret@git.example.com:team/memory.git"),
            "fatal: ***@git.example.com:team/memory.git",
        )
        self.assertEqual(
            audit_log._redact_remote_urls("alice:secret@git.example.com:team/memory.git"),
            "***@git.example.com:team/memory.git",
        )

    def test_auto_commit_stages_only_memory_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sync_git.ensure_repo(root)
            (root / "methods").mkdir()
            (root / "methods/good.md").write_text("# good\n", encoding="utf-8")
            (root / "credential.txt").write_text("token", encoding="utf-8")
            outside = root / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            (root / "methods/link.md").symlink_to(outside)

            result = sync_git.auto_commit(root, "test safe staging")
            self.assertTrue(result["committed"])
            tracked = subprocess.check_output(
                ["git", "-C", str(root), "ls-files"], text=True
            ).splitlines()
            self.assertIn("methods/good.md", tracked)
            self.assertNotIn("credential.txt", tracked)
            self.assertNotIn("methods/link.md", tracked)

    def test_ensure_repo_serializes_repository_metadata_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(
                sync_git,
                "_library_lock",
                wraps=sync_git._library_lock,
            ) as lock:
                info = sync_git.ensure_repo(root)

            self.assertTrue(info["initialized"])
            lock.assert_called_once_with(root.resolve())
            self.assertTrue((root / ".git").is_dir())

    def test_index_rebuild_keeps_titles_that_live_only_in_the_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "methods").mkdir()
            fm = "---\nname: {n}\ndescription: \"{d}\"\ntype: reference\n---\n\n"
            # Written by the Web editor: no H1, title only in the index.
            (root / "methods/mem-20261122.md").write_text(fm.format(n="mem-20261122", d="网页摘要") + "正文\n", encoding="utf-8")
            (root / "methods/h1.md").write_text(fm.format(n="h1", d="d") + "# 文件标题\n\n正文\n", encoding="utf-8")
            (root / "methods/new.md").write_text(fm.format(n="new", d="") + "正文\n", encoding="utf-8")
            (root / "MEMORY.md").write_text(
                "# §0\n\n## L0 操作约束\n\n## L1 可复用方法\n"
                "- [网页标题](methods/mem-20261122.md) — 旧摘要\n"
                "- [旧标题](methods/h1.md) — d\n",
                encoding="utf-8",
            )
            sync_git.rebuild_index(root, lock=False)
            text = (root / "MEMORY.md").read_text(encoding="utf-8")
            self.assertIn("- [网页标题](methods/mem-20261122.md) — 网页摘要", text)
            self.assertIn("- [文件标题](methods/h1.md) — d", text)
            self.assertIn("- [new](methods/new.md) — new", text)
            self.assertTrue(text.startswith("# §0\n"))

    def test_remote_url_rejects_tokens_in_the_username_slot(self):
        for url in (
            "https://ghp_abcdefghijklmnop0123456789@github.com/o/r.git",
            "https://github_pat_11ABCDEFG@github.com/o/r.git",
            "https://glpat-abcdefghij@gitlab.com/o/r.git",
            "https://x-access-token@github.com/o/r.git",
            "https://0123456789abcdef0123@gitee.com/o/r.git",
            "https://alice:secret@gitee.com/o/r.git",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                sync_git.validate_remote_url(url)
        for url in (
            "https://alice@gitee.com/o/r.git",
            "https://chenwenxin01@dev.kunlunxin.com/klx/x.git",
            "git@github.com:o/r.git",
            "ssh://git@github.com/o/r.git",
        ):
            self.assertEqual(sync_git.validate_remote_url(url), url)

    def test_index_rebuild_ignores_symlink_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "methods").mkdir()
            outside = root / "outside.md"
            outside.write_text("# secret\n", encoding="utf-8")
            (root / "methods/link.md").symlink_to(outside)

            sync_git.rebuild_index(root)
            self.assertNotIn("link.md", (root / "MEMORY.md").read_text(encoding="utf-8"))
            self.assertEqual(sync_git.find_conflict_files(root), [])

    def test_audit_log_is_private_locked_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertTrue(
                audit_log.record(
                    {"tool": "memory_search", "query": "https://alice:secret@gitee.com/x"},
                    home=home,
                )
            )
            path = audit_log.audit_path(home=home)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.with_name("audit.jsonl.lock").stat().st_mode & 0o777, 0o600)
            events = audit_log.read_events(home=home)
            self.assertEqual(events[0]["query"], "https://***@gitee.com/x")

    def test_pull_and_push_do_not_hold_library_lock_during_network_git_calls(self):
        import fcntl

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env = {
                "HOME": str(base / "home"),
                "UNIFIED_MEMORY_STATE_DIR": str(base / "state"),
                "UNIFIED_MEMORY_CONFIG_DIR": str(base / "config"),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            }
            with mock.patch.dict(os.environ, env):
                bare = base / "remote.git"
                subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
                lib = base / "lib"
                memoryctl.initialize_library(lib)
                sync_git.ensure_repo(lib, remote_url=str(bare), branch="main")
                sync_git.auto_commit(lib, "seed")

                observed = {}
                orig_run_git = sync_git._run_git

                def spy_run_git(repo, *args, **kwargs):
                    if args and args[0] in {"fetch", "push"}:
                        lock_path = sync_git.library_state_dir(repo) / "library.lock"
                        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            observed[args[0]] = "unlocked"
                            fcntl.flock(fd, fcntl.LOCK_UN)
                        except BlockingIOError:
                            observed[args[0]] = "locked"
                        finally:
                            os.close(fd)
                    return orig_run_git(repo, *args, **kwargs)

                with mock.patch.object(sync_git, "_run_git", side_effect=spy_run_git):
                    pushed = sync_git.push(lib, branch="main")
                    pulled = sync_git.pull(lib, branch="main")

                self.assertTrue(pushed["pushed"])
                self.assertEqual(observed.get("push"), "unlocked")
                self.assertEqual(observed.get("fetch"), "unlocked")
                self.assertEqual(pulled["status"], "up_to_date")
                upstream = subprocess.run(
                    ["git", "-C", str(lib), "rev-parse", "--abbrev-ref", "main@{upstream}"],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()
                self.assertEqual(upstream, "origin/main")

    def test_macos_pac_proxy_parsing_and_fallback(self):
        self.assertEqual(
            sync_git._parse_pac_result("HTTPS proxy.example.com:8894; DIRECT"),
            "https://proxy.example.com:8894",
        )
        self.assertEqual(
            sync_git._parse_pac_result("PROXY 127.0.0.1:7890; DIRECT"),
            "http://127.0.0.1:7890",
        )
        self.assertEqual(
            sync_git._parse_pac_result("SOCKS5 127.0.0.1:1080; DIRECT"),
            "socks5h://127.0.0.1:1080",
        )
        self.assertEqual(sync_git._parse_pac_result("DIRECT"), "")
        self.assertEqual(sync_git._parse_pac_result("HTTPS bad host:80"), "")

        if sys.platform == "darwin":
            pac_js = (
                "function FindProxyForURL(url, host) {"
                "  if (dnsDomainIs(host, 'github.com')) return 'HTTPS pac.example.com:8894; DIRECT';"
                "  return 'DIRECT';"
                "}"
            )
            self.assertEqual(
                sync_git._eval_pac_script(pac_js, "https://github.com/", "github.com"),
                "HTTPS pac.example.com:8894; DIRECT",
            )
            self.assertEqual(
                sync_git._eval_pac_script(pac_js, "https://example.org/", "example.org"),
                "DIRECT",
            )
            # Infinite loop must be cut off cleanly instead of hanging.
            self.assertEqual(
                sync_git._eval_pac_script("function FindProxyForURL(){ while(true){} }", "https://github.com/", "github.com"),
                "",
            )

        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp, "PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1"}
            with mock.patch.object(sync_git.sys, "platform", "darwin"), \
                    mock.patch.object(sync_git, "getproxies", return_value={}), \
                    mock.patch.object(sync_git, "proxy_bypass", return_value=False), \
                    mock.patch.object(sync_git, "_resolve_macos_pac_proxy", return_value="https://pac.example.com:8894"):
                opts = sync_git._github_proxy_options(Path(tmp), env, "https://github.com/o/r.git")
                self.assertEqual(opts, ["-c", "http.https://github.com/.proxy=https://pac.example.com:8894"])
                # Respect explicit env override.
                self.assertEqual(
                    sync_git._github_proxy_options(Path(tmp), {**env, "HTTPS_PROXY": ""}, "https://github.com/o/r.git"),
                    [],
                )


if __name__ == "__main__":
    unittest.main()
