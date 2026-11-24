"""memory_read must support outline and single-section reads (progressive disclosure)."""

from pathlib import Path
import sys
import tempfile
import unittest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "scripts"))
sys.path.insert(0, str(CODE / "mcp"))
sys.path.insert(0, str(CODE / "tests"))

import server as mcp_server  # noqa: E402
from test_search_resilience import _store  # noqa: E402

LONG = "细节说明。" * 300


class ReadSectionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.store = _store(self.base, "personal")
        self.store.upsert_entry(
            layer="L1",
            name="guide",
            title="部署手册",
            description="部署步骤",
            body=f"开头说明。\n\n## 前置条件\n\n需要 Node 22。\n\n## 步骤\n\n{LONG}\n\n## 验证\n\n跑测试。",
        )

    def test_full_read_includes_section_outline_for_long_entries(self):
        res = self.store.read_entry("methods/guide.md")
        self.assertFalse(res["partial"])
        titles = [sec["section"] for sec in res["sections"]]
        self.assertEqual(titles, ["部署手册", "前置条件", "步骤", "验证"])
        self.assertTrue(all(sec["tokens"] > 0 for sec in res["sections"]))
        self.assertIn("## 步骤", res["content"])

    def test_outline_is_much_smaller_than_the_entry(self):
        full = self.store.read_entry("methods/guide.md")
        outline = self.store.read_entry("methods/guide.md", outline=True)
        self.assertTrue(outline["partial"])
        self.assertNotIn("细节说明", outline["content"])
        self.assertLess(len(outline["content"]), len(full["content"]) / 4)
        self.assertEqual(outline["chars"], full["chars"])
        self.assertIn("section=", outline["notice"])

    def test_section_read_returns_only_that_section(self):
        res = self.store.read_entry("methods/guide.md", section="验证")
        self.assertTrue(res["partial"])
        self.assertEqual(res["section"], "验证")
        self.assertIn("跑测试", res["content"])
        self.assertNotIn("细节说明", res["content"])
        self.assertIn("# 部署手册", res["content"])

    def test_section_read_accepts_a_substring_and_reports_choices(self):
        res = self.store.read_entry("methods/guide.md", section="前置")
        self.assertEqual(res["section"], "前置条件")
        with self.assertRaises(ValueError) as caught:
            self.store.read_entry("methods/guide.md", section="没有这节")
        self.assertIn("前置条件", str(caught.exception))

    def test_intro_before_the_first_heading_is_a_readable_section(self):
        res = self.store.read_entry("methods/guide.md", section="部署手册")
        self.assertIn("开头说明", res["content"])
        self.assertNotIn("## 前置条件", res["content"])

    def test_short_entries_stay_plain(self):
        self.store.upsert_entry(
            layer="L2",
            name="style",
            title="回复风格",
            description="简短",
            body="回复要短。",
        )
        res = self.store.read_entry("preferences/style.md")
        self.assertNotIn("sections", res)
        self.assertFalse(res["partial"])
        outline = self.store.read_entry("preferences/style.md", outline=True)
        self.assertIn("回复要短", outline["content"])


if __name__ == "__main__":
    unittest.main()


class EntryLinkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = _store(Path(tmp.name), "personal")
        self.store.upsert_entry("L1", "docker-cache", "Docker 构建缓存排查",
                                "缓存失效的根因", "2026-11-20 验证：改挂载顺序后缓存命中。")
        self.store.upsert_entry("L3", "demo-status", "demo 项目进展",
                                "构建已通过",
                                "构建问题的根因见 methods/docker-cache.md，以及 methods/missing.md。")

    def test_read_shows_cited_entries_and_citing_entries(self):
        cited = self.store.read_entry("methods/docker-cache.md")
        self.assertNotIn("links", cited)
        self.assertEqual(cited["backlinks"], [{"ref": "projects/demo-status.md", "title": "demo 项目进展"}])

        citing = self.store.read_entry("projects/demo-status.md")
        self.assertEqual(citing["links"], [{"ref": "methods/docker-cache.md", "title": "Docker 构建缓存排查"}])
        self.assertNotIn("backlinks", citing)

    def test_index_links_are_skipped(self):
        res = self.store.read_entry("MEMORY.md")
        self.assertNotIn("links", res)
        self.assertNotIn("backlinks", res)
