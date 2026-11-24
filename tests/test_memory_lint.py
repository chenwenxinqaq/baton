"""Consolidation checks must find real mess and stay quiet about the rest."""

from pathlib import Path
import sys
import tempfile
import time
import unittest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "scripts"))
sys.path.insert(0, str(CODE / "mcp"))
sys.path.insert(0, str(CODE / "tests"))

import memory_lint  # noqa: E402
from test_search_resilience import _store  # noqa: E402

NOW = 1764000000.0


def _entry(path, **over):
    item = {
        "path": path,
        "source": "index",
        "layer": "L1",
        "name": Path(path).stem,
        "title": "Docker 构建缓存排查",
        "description": "缓存失效的根因是 BuildKit 的挂载顺序",
        "status": "active",
        "updated_at": "2026-11-20",
        "updated_epoch": NOW - 86400,
        "body": "2026-11-20 验证：改挂载顺序后缓存命中。",
    }
    item.update(over)
    return item


def _checks(findings):
    return {f["check"] for f in findings}


class LintEntryTests(unittest.TestCase):
    def test_clean_library_has_no_findings(self):
        entries = [_entry("methods/docker-cache.md")]
        index = {"methods/docker-cache.md": {"layer": "L1", "title": "t", "description": "d"}}
        self.assertEqual(memory_lint.lint_entries(entries, index, now=NOW), [])

    def test_index_and_body_problems_are_reported(self):
        entries = [
            _entry("methods/docker-cache.md", body="---\nname: x\n---\n正文 2026-11-20。"),
            _entry("methods/no-summary.md", title="另一个主题", description="", body="2026-11-20 结论。"),
        ]
        index = {"methods/docker-cache.md": {"layer": "L1", "title": "t", "description": "d"},
                 "methods/gone.md": {"layer": "L1", "title": "删掉的条目", "description": "d"}}
        findings = memory_lint.lint_entries(entries, index, now=NOW)
        self.assertEqual(
            _checks(findings),
            {"duplicate-frontmatter", "missing-description", "index-missing", "index-orphan"},
        )
        orphan = [f for f in findings if f["check"] == "index-orphan"][0]
        self.assertEqual(orphan["ref"], "methods/gone.md")
        self.assertEqual(orphan["severity"], "warning")

    def test_stale_snapshots_and_missing_dates(self):
        entries = [_entry(
            "projects/demo-status.md",
            layer="L3",
            title="demo 项目进展",
            description="当前卡在构建",
            body="当前卡在构建，没写日期。",
            updated_epoch=NOW - 200 * 86400,
            updated_at="2026-05-08",
        )]
        index = {"projects/demo-status.md": {"layer": "L3", "title": "t", "description": "d"}}
        findings = memory_lint.lint_entries(entries, index, now=NOW, stale_days=90)
        self.assertEqual(_checks(findings), {"stale-snapshot", "missing-date"})
        self.assertIn("2026-05-08", [f["detail"] for f in findings if f["check"] == "stale-snapshot"][0])

    def test_long_entry_is_flagged_once(self):
        entries = [_entry("methods/docker-cache.md", body="2026-11-20。" + "细节。" * 3000)]
        index = {"methods/docker-cache.md": {"layer": "L1", "title": "t", "description": "d"}}
        findings = memory_lint.lint_entries(entries, index, now=NOW)
        self.assertEqual([f["check"] for f in findings], ["oversized"])

    def test_two_entries_on_one_topic_are_paired(self):
        entries = [
            _entry("methods/docker-cache-a.md"),
            _entry("methods/docker-cache-b.md"),
        ]
        index = {p["path"]: {"layer": "L1", "title": "t", "description": "d"} for p in entries}
        findings = memory_lint.lint_entries(entries, index, now=NOW)
        pairs = [f for f in findings if f["check"] == "duplicate-topic"]
        self.assertEqual(len(pairs), 1)
        self.assertIn("methods/docker-cache-b.md", pairs[0]["detail"])

    def test_archived_entries_are_not_paired(self):
        entries = [
            _entry("archive/docker-cache-a.md", layer="L4"),
            _entry("archive/docker-cache-b.md", layer="L4"),
        ]
        index = {p["path"]: {"layer": "L4", "title": "t", "description": "d"} for p in entries}
        findings = memory_lint.lint_entries(entries, index, now=NOW)
        self.assertNotIn("duplicate-topic", _checks(findings))

    def test_summary_counts_by_check_and_severity(self):
        findings = [
            {"check": "oversized", "severity": "info"},
            {"check": "oversized", "severity": "info"},
            {"check": "duplicate-frontmatter", "severity": "error"},
        ]
        self.assertEqual(
            memory_lint.summarize(findings),
            {"total": 3, "by_check": {"oversized": 2, "duplicate-frontmatter": 1},
             "by_severity": {"info": 2, "error": 1}},
        )


class LintStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = _store(Path(tmp.name), "personal")

    def test_fresh_entry_passes_and_stale_snapshot_is_caught(self):
        self.store.upsert_entry("L1", "docker-cache", "Docker 构建缓存排查",
                                "缓存失效的根因", "2026-11-20 验证：改挂载顺序后缓存命中。")
        clean = self.store.lint()
        self.assertEqual(clean["findings"], [])
        self.assertEqual(clean["entries"], 1)
        self.assertIn("notice", clean)

        self.store.upsert_entry("L3", "demo-status", "demo 项目进展",
                                "当前卡在构建", "2026-11-20 当前卡在构建。")
        aged = self.store.lint(stale_days=0)
        self.assertEqual({f["check"] for f in aged["findings"]}, {"stale-snapshot"})
        self.assertEqual(aged["summary"]["by_check"], {"stale-snapshot": 1})
        self.assertEqual(aged["library"], "personal")

    def test_leftover_frontmatter_in_a_file_is_reported(self):
        self.store.upsert_entry("L1", "docker-cache", "Docker 构建缓存排查",
                                "缓存失效的根因", "2026-11-20 验证通过。")
        target = self.store.root / "methods" / "docker-cache.md"
        raw = target.read_text(encoding="utf-8")
        target.write_text(raw + "\n---\nname: stray\n---\n", encoding="utf-8")
        findings = self.store.lint()["findings"]
        self.assertEqual([f["check"] for f in findings], ["duplicate-frontmatter"])
        self.assertEqual(findings[0]["ref"], "methods/docker-cache.md")


if __name__ == "__main__":
    unittest.main()
