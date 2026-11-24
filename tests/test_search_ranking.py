"""Search ranking: BM25F tokens/scores, ranker selection, and the audit-log eval."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "scripts"))
sys.path.insert(0, str(CODE / "mcp"))
sys.path.insert(0, str(CODE / "tests"))

import search_eval  # noqa: E402
import server as mcp_server  # noqa: E402
from test_search_resilience import _store  # noqa: E402


def _write(store, layer, name, title, description, body):
    store.upsert_entry(layer, name, title, description, body)


class SearchTokensTests(unittest.TestCase):
    def test_compound_parts_and_cjk_bigrams(self):
        self.assertEqual(
            mcp_server.search_tokens("docker-build-cache-fix 构建缓存"),
            ["docker-build-cache-fix", "docker", "build", "cache", "fix", "构建", "建缓", "缓存"],
        )

    def test_single_letter_parts_dropped(self):
        self.assertEqual(mcp_server.search_tokens("a b-c lb"), ["b-c", "lb"])


class Bm25fScoresTests(unittest.TestCase):
    def test_title_match_beats_body_match(self):
        docs = [
            {"name": "other", "title": "other", "description": "", "body": "docker appears in body text"},
            {"name": "docker-notes", "title": "docker notes", "description": "", "body": "notes"},
        ]
        scores = mcp_server.bm25f_scores(docs, ["docker"])
        self.assertGreater(scores[1], scores[0])

    def test_rare_term_weighs_more(self):
        docs = [
            {"name": "a", "title": "common rare", "description": "", "body": ""},
            {"name": "b", "title": "common", "description": "", "body": ""},
            {"name": "c", "title": "common", "description": "", "body": ""},
        ]
        scores = mcp_server.bm25f_scores(docs, ["common", "rare"])
        self.assertEqual(max(range(3), key=scores.__getitem__), 0)

    def test_empty_inputs(self):
        self.assertEqual(mcp_server.bm25f_scores([], ["x"]), [])
        self.assertEqual(mcp_server.bm25f_scores([{"name": "a"}], []), [0.0])


class RankerSelectionTests(unittest.TestCase):
    def test_default_env_and_unknown(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UNIFIED_MEMORY_SEARCH_RANKER", None)
            self.assertEqual(mcp_server.search_ranker(), "legacy")
            self.assertEqual(mcp_server.search_ranker("HYBRID"), "hybrid")
            self.assertEqual(mcp_server.search_ranker("nope"), "legacy")
        with mock.patch.dict(os.environ, {"UNIFIED_MEMORY_SEARCH_RANKER": "bm25f"}):
            self.assertEqual(mcp_server.search_ranker(), "bm25f")


class StoreRankingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _store(Path(self.tmp.name), "personal")
        _write(self.store, "L1", "docker-build-cache-fix", "Docker 构建缓存失效排查", "COPY 顺序导致缓存失效", "调整 COPY 顺序。")
        _write(self.store, "L1", "python-release", "Python 发布清单", "发布前检查", "偶尔提到 docker 镜像。")
        _write(self.store, "L3", "unrelated", "无关条目", "别的事情", "完全不相关。")

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_rankers_keep_match_filter_and_order(self):
        for ranker in mcp_server.SEARCH_RANKERS:
            hits = self.store.search("docker 缓存", ranker=ranker)
            names = [h["path"].rsplit("/", 1)[-1] for h in hits]
            self.assertEqual(names[0], "docker-build-cache-fix.md", ranker)
            self.assertNotIn("unrelated.md", names, ranker)
            self.assertTrue(all(h["score"] > 0 for h in hits), ranker)

    def test_eval_builds_cases_from_shown_reads(self):
        events = [
            {"t": 100.0, "source": "mcp", "client": "codex", "tool": "memory_search", "ok": True, "query": "docker 缓存",
             "library": "personal", "refs": ["methods/python-release.md", "methods/docker-build-cache-fix.md"]},
            {"t": 110.0, "source": "mcp", "client": "codex", "tool": "memory_read", "ok": True, "ref": "MEMORY.md"},
            {"t": 120.0, "source": "mcp", "client": "codex", "tool": "memory_read", "ok": True, "ref": "methods/docker-build-cache-fix.md"},
        ]
        cases = search_eval.build_cases(events)
        self.assertEqual(cases, [{"query": "docker 缓存", "library": "personal",
                                  "relevant": ["methods/docker-build-cache-fix.md"]}])

        class _Wrap:
            def __init__(self, store):
                self.store = store

            def search(self, query, limit=10, library="personal", ranker=None):
                return self.store.search(query, limit=limit, ranker=ranker)

        report = search_eval.evaluate(_Wrap(self.store), cases)
        self.assertEqual(report["evaluated"], 1)
        for ranker in search_eval.RANKERS:
            self.assertEqual(report["rankers"][ranker]["hit@1"], 1.0, ranker)


if __name__ == "__main__":
    unittest.main()
