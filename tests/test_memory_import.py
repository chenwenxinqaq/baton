"""Review-queue imports must survive stopped saves, odd input, and changing sources."""

import os
from pathlib import Path
import sys
import tempfile
import unittest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "manager"))
sys.path.insert(0, str(CODE / "scripts"))
sys.path.insert(0, str(CODE / "mcp"))

import memory_import  # noqa: E402
from manager import InvalidEntry, Library, approve_import  # noqa: E402


class MemoryImportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
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
        self.library = Library(self.root, "demo")
        self.home = self.base / "home"
        self.home.mkdir()

    @staticmethod
    def _restore_env(previous):
        if previous is None:
            os.environ.pop("UNIFIED_MEMORY_STATE_DIR", None)
        else:
            os.environ["UNIFIED_MEMORY_STATE_DIR"] = previous

    def queue_text(self, body, title="样本", client="cursor"):
        memory_import.import_text(self.root, client, title, body)
        (item,) = [entry for entry in memory_import.pending(self.root, client) if entry["title"] == title]
        return item

    def approve(self, item, client="cursor", **draft):
        fields = dict(layer="L3", title=item["title"], description="摘要", body=item["body"])
        fields.update(draft)
        return approve_import(self.library, client, item["id"], memory_import.revision(item), fields)

    def test_approval_recovers_from_file_left_without_index_line(self):
        item = self.queue_text("白鹭候选记忆")
        orphan = self.root / "projects" / f'{item["id"]}.md'
        orphan.write_text("---\nname: x\n---\n\n上次保存中断留下的正文\n", encoding="utf-8")

        saved = self.approve(item)

        self.assertEqual(saved, f'projects/{item["id"]}.md')
        self.assertIn("白鹭候选记忆", orphan.read_text(encoding="utf-8"))
        self.assertIn(f"]({saved})", (self.root / "MEMORY.md").read_text(encoding="utf-8"))
        backups = [path.read_text(encoding="utf-8") for path in self.library.trash_dir.glob("*.md")]
        self.assertTrue(any("上次保存中断留下的正文" in text for text in backups))
        self.assertEqual(memory_import.pending(self.root, "cursor"), [])

    def test_indexed_topic_with_the_same_slug_is_not_discarded(self):
        item = self.queue_text("杜鹃候选记忆")
        self.library.save_topic("", "", "L3", "已有条目", "摘要", "用户自己的正文", slug=item["id"])

        with self.assertRaises(InvalidEntry):
            self.approve(item)

        kept = (self.root / "projects" / f'{item["id"]}.md').read_text(encoding="utf-8")
        self.assertIn("用户自己的正文", kept)


if __name__ == "__main__":
    unittest.main()
