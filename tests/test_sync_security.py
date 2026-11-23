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


if __name__ == "__main__":
    unittest.main()
