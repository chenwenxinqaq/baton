"""The desktop UI must expose the same version history the agents can restore."""

import os
from pathlib import Path
import sys
import tempfile
import unittest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "manager"))
sys.path.insert(0, str(CODE / "scripts"))
sys.path.insert(0, str(CODE / "mcp"))

import server as mcp_server  # noqa: E402
from manager import InvalidEntry, Library, render_topic_versions_html  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
