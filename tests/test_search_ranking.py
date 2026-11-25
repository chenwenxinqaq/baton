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

    def test_particles_split_cjk_runs(self):
        # 的 is a grammatical particle: 版的/的打 would match unrelated entries.
        self.assertEqual(
            mcp_server.cjk_chunks("桌面版的打包方法"), ["桌面版", "打包方法"]
        )
        self.assertNotIn("版的", mcp_server.search_tokens("桌面版的打包"))
        self.assertEqual(
            mcp_server.build_query_features("桌面版的打包方法")[1],
            ["桌面", "面版", "打包", "包方", "方法"],
        )

    def test_particles_inside_real_terms_are_kept(self):
        # 地址/了解 are real terms here, so 地 and 了 are not separators.
        for term in ("地址编码", "了解打包"):
            self.assertIn(term[:2], mcp_server.build_query_features(term)[1])


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
                                  "relevant": ["methods/docker-build-cache-fix.md"], "axis": "extraction"}])

        class _Wrap:
            def __init__(self, store):
                self.store = store

            def search(self, query, limit=10, library="personal", ranker=None):
                return self.store.search(query, limit=limit, ranker=ranker)

        report = search_eval.evaluate(_Wrap(self.store), cases)
        self.assertEqual(report["evaluated"], 1)
        for ranker in search_eval.RANKERS:
            self.assertEqual(report["rankers"][ranker]["hit@1"], 1.0, ranker)



class MatchStrengthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _store(Path(self.tmp.name), "personal")
        _write(self.store, "L1", "docker-build-cache-fix", "Docker 构建缓存失效排查", "COPY 顺序导致缓存失效", "调整 COPY 顺序。")
        _write(self.store, "L1", "python-release", "Python 发布清单", "发布前检查", "偶尔提到 docker 镜像。")

    def tearDown(self):
        self.tmp.cleanup()

    def test_helper_levels(self):
        self.assertEqual(mcp_server.match_strength(["a", "b"], ["a", "b"]), "full")
        self.assertEqual(mcp_server.match_strength(["a", "b"], ["a"]), "partial")
        self.assertEqual(mcp_server.match_strength(["构建方法"], ["构建", "方法"]), "fuzzy")

    def test_search_reports_match_level(self):
        hits = {h["path"]: h["match"] for h in self.store.search("docker 缓存")}
        self.assertEqual(hits["methods/docker-build-cache-fix.md"], "full")
        self.assertEqual(hits["methods/python-release.md"], "partial")
        fuzzy = self.store.search("构建缓存问题")
        self.assertTrue(fuzzy)
        self.assertTrue(all(h["match"] == "fuzzy" for h in fuzzy))
        self.assertEqual(self.store.search("最近")[0]["match"], "recent")

    def test_eval_axes_and_abstention(self):
        class _Wrap:
            def __init__(self, store):
                self.store = store

            def search(self, query, limit=10, library="personal", ranker=None):
                return self.store.search(query, limit=limit, ranker=ranker)

        cases = search_eval.load_cases_from_rows([
            ["docker 缓存", ["methods/docker-build-cache-fix.md"]],
            {"query": "python 发布", "relevant": ["methods/python-release.md"], "axis": "update"},
            {"query": "python 报销", "relevant": []},
            {"query": "docker 缓存", "relevant": [], "axis": "abstention"},
        ])
        self.assertEqual([c["axis"] for c in cases], ["extraction", "update", "abstention", "abstention"])
        report = search_eval.evaluate(_Wrap(self.store), cases, rankers=("legacy",))
        self.assertEqual(report["evaluated"], 4)
        self.assertEqual(report["rankers"]["legacy"]["abstention"], 0.5)
        self.assertEqual(report["axes"]["update"]["legacy"]["hit@1"], 1.0)
        self.assertEqual(report["axes"]["abstention"]["legacy"], {"pass": 0.5, "cases": 2})


class ProcedurePhraseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = _store(Path(tmp.name), "personal")
        _write(self.store, "L1", "packaging", "桌面版打包步骤", "生成安装包", "先构建应用。")

    def test_title_variant_recovers_missing_phrase_without_claiming_exact_match(self):
        _write(self.store, "L3", "questions", "会议记录", "未验证的想法", "有人问打包方法。")
        for query in ("打包方法", "打包流程"):
            with self.subTest(query=query):
                hit = self.store.search(query, ranker="legacy")[0]
                self.assertEqual(hit["ref"], "methods/packaging.md")
                self.assertEqual(hit["match"], "fuzzy")
                self.assertEqual(hit["matched_phrase"], "打包步骤")
                self.assertNotIn("see_also", hit)
        literal = self.store.search("打包步骤", ranker="legacy")[0]
        self.assertEqual(literal["match"], "full")
        self.assertNotIn("matched_phrase", literal)

    def test_literal_title_beats_variant(self):
        _write(self.store, "L1", "packaging-flow", "打包流程", "新版规范", "后台签名。")
        hits = self.store.search("打包流程", ranker="legacy")
        self.assertEqual([h["ref"] for h in hits], ["methods/packaging-flow.md", "methods/packaging.md"])

    def test_variants_require_the_same_topic_in_the_title(self):
        _write(self.store, "L3", "notes", "工作记录", "没有结论", "提到备份步骤。")
        for query in ("备份流程", "打包部署流程", "方法"):
            with self.subTest(query=query):
                self.assertFalse(any("matched_phrase" in h for h in self.store.search(query)))


if __name__ == "__main__":
    unittest.main()
