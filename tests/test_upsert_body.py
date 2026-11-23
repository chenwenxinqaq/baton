"""memory_upsert must not nest the frontmatter and title it generates itself."""

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


class UpsertBodyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "lib"
        root.mkdir()
        (root / "MEMORY.md").write_text("# M\n\n## L0\n\n## L1\n\n## L2\n\n## L3\n\n## L4\n", encoding="utf-8")
        for folder in ("constraints", "methods", "preferences", "projects", "archive", "user-managed"):
            (root / folder).mkdir()
        self.store = mcp_server.MemoryStore(root, async_scheduler=_NoopScheduler())
        self.store.state_dir = Path(tmp.name) / "state"
        self.store.lock_path = self.store.state_dir / "library.lock"
        self.store.trash_dir = self.store.state_dir / "trash"

    def test_read_edit_upsert_keeps_one_header(self):
        self.store.upsert_entry("L3", "fm-demo", "FM demo", "v1", "Body v1.")
        content = self.store.read_entry("projects/fm-demo.md")["content"]
        self.store.upsert_entry("L3", "fm-demo", "FM demo", "v2", content + "\nAdded line.")
        text = self.store.read_entry("projects/fm-demo.md")["content"]
        self.assertEqual(text.count("name: fm-demo"), 1, text)
        self.assertEqual(text.count("# FM demo"), 1, text)
        self.assertIn('description: "v2"', text)
        self.assertIn("Body v1.\n\nAdded line.", text)

    def test_frontmatter_only_body_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.upsert_entry("L3", "fm-empty", "X", "d", "---\nname: x\n---\n")

    def test_plain_horizontal_rules_are_kept(self):
        body = "---\nplain text between rules\n---\nmore"
        self.assertEqual(mcp_server.strip_generated_header(body), body)


if __name__ == "__main__":
    unittest.main()
