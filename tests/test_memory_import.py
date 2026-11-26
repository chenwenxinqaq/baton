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

    def test_crlf_input_is_stored_with_lf_line_endings(self):
        item = self.queue_text("第一行\r\n第二行\r第三行")
        self.assertEqual(item["body"], "第一行\n第二行\n第三行")

        saved = self.approve(item, body="甲\r\n乙\r丙")

        text = (self.root / saved).read_bytes().decode("utf-8")
        self.assertNotIn("\r", text)
        self.assertIn("甲\n乙\n丙", text)

    def test_candidate_at_the_size_limit_can_be_approved_unchanged(self):
        item = self.queue_text("字" * memory_import.MAX_TEXT)

        saved = self.approve(item)

        text = (self.root / saved).read_text(encoding="utf-8")
        self.assertIn("字" * memory_import.MAX_TEXT, text)
        self.assertIn("> 来源：", text)

    def claude_note(self, text, stamp):
        note = self.home / ".claude" / "projects" / "demo" / "memory" / "note.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(text, encoding="utf-8")
        os.utime(note, (stamp, stamp))
        return note

    def scan_claude(self):
        return memory_import.import_memories(self.root, "claude", home=self.home)

    def test_source_edited_after_approval_reopens_and_updates_the_same_entry(self):
        self.claude_note("白鹭旧结论", 1_700_000_000)
        self.assertEqual(self.scan_claude()["added"], 1)
        (item,) = memory_import.pending(self.root, "claude")
        saved = self.approve(item, client="claude")

        # Scanning the unchanged source again must not reopen it.
        report = self.scan_claude()
        self.assertEqual((report["added"], report["updated"]), (0, 0))
        self.assertEqual(memory_import.pending(self.root, "claude"), [])

        self.claude_note("白鹭新结论", 1_700_000_500)
        self.assertEqual(self.scan_claude()["updated"], 1)
        (reopened,) = memory_import.pending(self.root, "claude")
        self.assertEqual(reopened["previously_approved"], saved)
        self.assertEqual(reopened["body"], "白鹭新结论")

        self.assertEqual(self.approve(reopened, client="claude"), saved)
        text = (self.root / saved).read_text(encoding="utf-8")
        self.assertIn("白鹭新结论", text)
        self.assertNotIn("白鹭旧结论", text)
        index = (self.root / "MEMORY.md").read_text(encoding="utf-8")
        self.assertEqual(index.count(f"]({saved})"), 1)
        self.assertEqual(memory_import.pending(self.root, "claude"), [])

    def test_touched_but_identical_source_and_legacy_receipts_stay_closed(self):
        note = self.claude_note("杜鹃结论", 1_700_000_000)
        self.scan_claude()
        (item,) = memory_import.pending(self.root, "claude")
        self.approve(item, client="claude")

        os.utime(note, (1_700_000_900, 1_700_000_900))
        self.assertEqual(self.scan_claude()["updated"], 0)

        receipt = memory_import.inbox(self.root, "claude") / (item["id"] + ".json")
        receipt.write_text('{"approved": "projects/x.md"}', encoding="utf-8")
        self.claude_note("杜鹃改动", 1_700_001_000)
        self.assertEqual(self.scan_claude()["updated"], 0)
        self.assertEqual(memory_import.pending(self.root, "claude"), [])

    def test_stray_scan_follows_the_importer_and_clears_after_approval(self):
        import memoryctl

        note = self.claude_note("海燕结论", 1_700_000_000)
        index = note.with_name("MEMORY.md")
        index.write_text("- [note](note.md)\n", encoding="utf-8")
        # The importer skips MEMORY.md when topic files sit next to it; so must the warning.
        self.assertEqual(memoryctl.scan_stray_agent_memories(home=self.home, root=self.root), [str(note)])
        self.assertEqual(memoryctl.scan_stray_agent_memories(home=self.home), [str(note)])

        self.scan_claude()
        (item,) = memory_import.pending(self.root, "claude")
        # Queued but unreviewed content is still outside the library.
        self.assertEqual(memoryctl.scan_stray_agent_memories(home=self.home, root=self.root), [str(note)])

        self.approve(item, client="claude")
        self.assertEqual(memoryctl.scan_stray_agent_memories(home=self.home, root=self.root), [])

        self.claude_note("海燕新结论", 1_700_000_500)
        self.assertEqual(memoryctl.scan_stray_agent_memories(home=self.home, root=self.root), [str(note)])


if __name__ == "__main__":
    unittest.main()
