from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "scripts"))

import audit_log  # noqa: E402
import sync_git  # noqa: E402


class SyncSecurityTests(unittest.TestCase):
    def test_remote_passwords_are_rejected_and_errors_redacted(self):
        with self.assertRaisesRegex(ValueError, "不能包含内嵌密码"):
            sync_git.validate_remote_url("https://alice:secret@gitee.com/team/memory.git")
        self.assertEqual(
            sync_git._redact_remote_urls("fatal: https://alice:secret@gitee.com/team/memory.git"),
            "fatal: https://***@gitee.com/team/memory.git",
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
