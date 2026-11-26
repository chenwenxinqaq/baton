"""The Web manager search box and MCP memory_search must agree on matches and scores."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "scripts"))
sys.path.insert(0, str(CODE / "mcp"))
sys.path.insert(0, str(CODE / "manager"))
sys.path.insert(0, str(CODE / "tests"))

import server as mcp_server  # noqa: E402
from manager import Library, filter_and_score, top_hit_citations  # noqa: E402
from test_search_resilience import _store  # noqa: E402


class SearchParityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"UNIFIED_MEMORY_STATE_DIR": str(Path(tmp.name) / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.store = _store(Path(tmp.name), "personal")
        write = self.store.upsert_entry
        write("L1", "recipe", "打包步骤", "生成包", "记录构建过程。")
        write("L3", "handbook", "版本手册", "使用方式", "见 methods/recipe.md。")
        write("L3", "deploy-notes", "部署记录", "上线情况", "本周完成 docker 镜像构建。")
        write("L2", "tone", "回复风格", "简洁", "先给结论。")
        self.library = Library(self.store.root, "demo")

    def manager_rows(self, q):
        _, topics = self.library.read_index()
        return topics, filter_and_score(self.library, topics, [], q, "", "active", "")

    def manager_hits(self, q):
        return sorted((item.path, score) for score, _, item in self.manager_rows(q)[1])

    def mcp_hits(self, q):
        return sorted((hit["path"], hit["score"]) for hit in self.store.search(q, limit=50, ranker="legacy"))

    def test_same_matches_and_scores_as_mcp_search(self):
        for q in ("版本手册", "打包流程", "docker", "构建", "最近 构建", "recipe", "不存在的词组"):
            with self.subTest(q=q):
                self.assertEqual(self.manager_hits(q), self.mcp_hits(q))

    def test_related_procedure_title_is_found(self):
        self.assertEqual([path for path, _ in self.manager_hits("打包流程")], ["methods/recipe.md"])

    def test_frontmatter_keys_are_not_searched_as_body(self):
        self.assertEqual(self.manager_hits("active"), [])
        self.assertEqual(self.mcp_hits("active"), [])

    def test_top_hit_citations_match_see_also(self):
        topics, rows = self.manager_rows("版本手册")
        cited = [topic.path for topic in top_hit_citations(self.library, topics, rows, "版本手册")]
        see_also = self.store.search("版本手册", ranker="legacy")[0]["see_also"]
        self.assertEqual(cited, ["methods/recipe.md"])
        self.assertEqual([ref["ref"] for ref in see_also], cited)


if __name__ == "__main__":
    unittest.main()
