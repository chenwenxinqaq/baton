"""One unreadable file or library must not break search across every library."""

from pathlib import Path
import sys
import tempfile
import unittest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "scripts"))
sys.path.insert(0, str(CODE / "mcp"))

import server as mcp_server  # noqa: E402


class _NoopScheduler:
    def enqueue(self, root, **kwargs):
        return {"queued": False}

    def status(self, root):
        return {}

    def shutdown(self):
        pass


def _store(base, name):
    root = base / name
    root.mkdir()
    (root / "MEMORY.md").write_text("# M\n\n## L0\n\n## L1\n\n## L2\n\n## L3\n\n## L4\n", encoding="utf-8")
    for folder in ("constraints", "methods", "preferences", "projects", "archive", "user-managed"):
        (root / folder).mkdir()
    store = mcp_server.MemoryStore(root, library_id=name, async_scheduler=_NoopScheduler())
    store.state_dir = base / f"state-{name}"
    store.lock_path = store.state_dir / "library.lock"
    store.trash_dir = store.state_dir / "trash"
    return store


class SearchResilienceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def test_non_utf8_file_does_not_break_search(self):
        store = _store(self.base, "team")
        store.upsert_entry("L1", "good", "Good note", "good", "good body")
        (store.root / "methods" / "gbk-note.md").write_bytes("# 中文笔记\n\ngood 内容\n".encode("gbk"))
        paths = {hit["path"] for hit in store.search("good")}
        self.assertIn("methods/good.md", paths)
        self.assertIn("methods/gbk-note.md", paths)

    def test_broken_library_is_skipped_when_searching_all(self):
        good = _store(self.base, "personal")
        good.upsert_entry("L1", "good", "Good note", "good", "good body")
        bad = _store(self.base, "team")
        (bad.root / "methods" / "link.md").symlink_to(self.base / "elsewhere.md")
        (self.base / "elsewhere.md").write_text("# x\n", encoding="utf-8")
        results = mcp_server.WorkspaceStore._per_store([good, bad], lambda st: st.search("good"))
        self.assertEqual([st.library_id for st, _ in results], ["personal"])
        with self.assertRaises(ValueError):
            mcp_server.WorkspaceStore._per_store([bad], lambda st: st.search("good"))


if __name__ == "__main__":
    unittest.main()
