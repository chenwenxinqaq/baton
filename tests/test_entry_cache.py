"""The in-memory entry cache must never serve content that is no longer on disk."""

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "scripts"))
sys.path.insert(0, str(CODE / "mcp"))
sys.path.insert(0, str(CODE / "tests"))

import server as mcp_server  # noqa: E402
from test_search_resilience import _store  # noqa: E402


class EntryCacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.note = self.base / "note.md"
        self.note.write_text("---\nname: note\nstatus: active\n---\n# 标题\n\n第一版\n", encoding="utf-8")
        mcp_server._ENTRY_CACHE.clear()
        self.addCleanup(mcp_server._ENTRY_CACHE.clear)
        parser = mock.patch.object(mcp_server, "parse_frontmatter", wraps=mcp_server.parse_frontmatter)
        self.parse = parser.start()
        self.addCleanup(parser.stop)

    def later(self, seconds=10):
        """Pretend the clock moved on so files written by the test count as settled."""
        clock = mock.patch.object(mcp_server.time, "time_ns", return_value=time.time_ns() + seconds * 10**9)
        clock.start()
        self.addCleanup(clock.stop)

    def test_unchanged_file_is_parsed_once(self):
        self.later()
        first = mcp_server.load_entry(self.note)
        second = mcp_server.load_entry(self.note)
        self.assertEqual(first, second)
        self.assertIn("第一版", second[1])
        self.assertEqual(self.parse.call_count, 1)

    def test_caller_changes_do_not_reach_the_cache(self):
        self.later()
        meta, _ = mcp_server.load_entry(self.note)
        meta["status"] = "archived"
        self.assertEqual(mcp_server.load_entry(self.note)[0]["status"], "active")

    def test_rewrite_with_same_size_and_restored_mtime_is_seen(self):
        self.later()
        before = self.note.stat()
        mcp_server.load_entry(self.note)
        self.note.write_text(self.note.read_text(encoding="utf-8").replace("第一版", "第二版"), encoding="utf-8")
        os.utime(self.note, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = self.note.stat()
        self.assertEqual((after.st_size, after.st_mtime_ns), (before.st_size, before.st_mtime_ns))
        self.assertIn("第二版", mcp_server.load_entry(self.note)[1])

    def test_atomic_replace_is_seen(self):
        self.later()
        mcp_server.load_entry(self.note)
        fresh = self.base / "fresh.md"
        fresh.write_text(self.note.read_text(encoding="utf-8").replace("第一版", "第三版"), encoding="utf-8")
        os.replace(fresh, self.note)
        self.assertIn("第三版", mcp_server.load_entry(self.note)[1])

    def test_recently_changed_file_is_not_cached(self):
        mcp_server.load_entry(self.note)
        mcp_server.load_entry(self.note)
        self.assertEqual(self.parse.call_count, 2)
        self.assertEqual(mcp_server._ENTRY_CACHE, {})

    def test_search_and_list_follow_writes_and_archives(self):
        store = _store(self.base, "personal")
        store.upsert_entry("L3", "plan", "发布计划", "计划", "灰度阶段")
        self.later()
        self.assertEqual([hit["path"] for hit in store.search("灰度")], ["projects/plan.md"])
        self.assertEqual([hit["path"] for hit in store.search("灰度")], ["projects/plan.md"])

        store.upsert_entry("L3", "plan", "发布计划", "计划", "全量阶段", mode="replace")
        self.assertEqual(store.search("灰度"), [])
        self.assertEqual([hit["path"] for hit in store.search("全量")], ["projects/plan.md"])

        (store.root / "projects" / "plan.md").unlink()
        self.assertEqual(store.search("全量"), [])


if __name__ == "__main__":
    unittest.main()
