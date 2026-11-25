"""Context-cost metrics: tool results record their size, and usage_stats measures read-through."""

from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest import mock


CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE / "scripts"))
sys.path.insert(0, str(CODE / "mcp"))

import audit_log  # noqa: E402
import server as mcp_server  # noqa: E402


class EstimateTokensTests(unittest.TestCase):
    def test_cjk_counts_per_character_and_ascii_per_four_chars(self):
        self.assertEqual(audit_log.estimate_tokens(""), 0)
        self.assertEqual(audit_log.estimate_tokens("中文记忆"), 4)
        self.assertEqual(audit_log.estimate_tokens("abcdefgh"), 2)
        self.assertEqual(audit_log.estimate_tokens("记忆 abcd"), 2 + 2)


class UsageStatsTests(unittest.TestCase):
    def ev(self, t, tool, client="Codex", **extra):
        return {"t": t, "source": "mcp", "ok": True, "client": client, "tool": tool, **extra}

    def test_read_rate_ranks_and_unused_searches(self):
        events = [
            self.ev(0, "memory_search", hits=3, refs=["a.md", "b.md", "c.md"], tokens=300, bytes=900),
            self.ev(1, "memory_read", ref="b.md", tokens=500, bytes=1500),
            self.ev(2, "memory_read", ref="b.md", tokens=500, bytes=1500),  # same ref twice counts once
            self.ev(3, "memory_read", ref="other.md", tokens=100, bytes=300),  # not from the search
            self.ev(4, "memory_search", hits=2, refs=["d.md", "e.md"], tokens=200, bytes=600),
            self.ev(5, "memory_read", ref="d.md", client="Cursor"),  # other client does not count
            self.ev(6, "memory_search", hits=1, refs=["f.md"], tokens=100, bytes=300),
            self.ev(6 + audit_log.FOLLOW_WINDOW_SECONDS + 1, "memory_read", ref="f.md"),  # too late
            {"t": 7, "source": "hook", "event": "sessionStart", "tokens": 400},
            self.ev(8, "memory_search", hits=0, refs=[]),  # old line without tokens
        ]
        stats = audit_log.usage_stats(events)
        self.assertEqual(stats["searches"], 4)
        self.assertEqual(stats["results_returned"], 6)
        self.assertEqual(stats["results_read"], 1)
        self.assertEqual(stats["read_rate"], round(1 / 6, 3))
        self.assertEqual(stats["searches_without_read"], 3)
        self.assertEqual(stats["read_ranks"], {"2": 1})
        self.assertEqual(stats["by_tool"]["memory_search"], {"calls": 3, "tokens": 600, "bytes": 1800, "avg_tokens": 200})
        self.assertEqual(stats["by_tool"]["memory_read"]["calls"], 3)
        self.assertEqual(stats["tokens_total"], 600 + 1100)
        self.assertEqual((stats["session_injections"], stats["session_injection_tokens"]), (1, 400))

    def test_empty_log(self):
        stats = audit_log.usage_stats([])
        self.assertEqual(stats["tokens_total"], 0)
        self.assertIsNone(stats["read_rate"])

    def test_batch_memory_read_counts_all_read_refs(self):
        events = [
            self.ev(0, "memory_search", hits=3, refs=["a.md", "b.md", "c.md"], tokens=300, bytes=900),
            self.ev(1, "memory_read", refs=["a.md", "c.md"], hits=2, tokens=400, bytes=1200),
        ]
        stats = audit_log.usage_stats(events)
        self.assertEqual(stats["results_read"], 2)
        self.assertEqual(stats["read_rate"], round(2 / 3, 3))
        self.assertEqual(stats["read_ranks"], {"1": 1, "3": 1})
        self.assertEqual(stats["searches_without_read"], 0)

    def test_batch_memory_read_audit_event_records_refs_without_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "lib"
            root.mkdir()
            (root / "MEMORY.md").write_text("# Unified Memory\n\n## L0\n\n## L1\n\n## L2\n\n## L3\n\n## L4\n", encoding="utf-8")
            for folder in ("constraints", "methods", "preferences", "projects", "archive", "user-managed"):
                (root / folder).mkdir()
            events = []
            with mock.patch.object(mcp_server.audit_log, "record", side_effect=lambda event, **_: events.append(event) or True):
                store = mcp_server.MemoryStore(root)
                store.upsert_entry("L1", "one", "条目一", "摘要一", "机密正文一")
                store.upsert_entry("L1", "two", "条目二", "摘要二", "机密正文二")
                mcp_server.handle_tool_call(store, "memory_read", {"paths": ["methods/one.md", "methods/two.md"]})
            read_ev = events[-1]
            self.assertEqual(read_ev["tool"], "memory_read")
            self.assertEqual(read_ev["refs"], ["methods/one.md", "methods/two.md"])
            self.assertEqual(read_ev["hits"], 2)
            self.assertNotIn("机密正文", json.dumps(events, ensure_ascii=False))


class ToolCallAuditSizeTests(unittest.TestCase):
    def test_tool_call_records_size_of_returned_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "lib"
            root.mkdir()
            (root / "MEMORY.md").write_text("# Unified Memory\n\n## L0\n\n## L1\n\n## L2\n\n## L3\n\n## L4\n", encoding="utf-8")
            for folder in ("constraints", "methods", "preferences", "projects", "archive", "user-managed"):
                (root / folder).mkdir()
            events = []
            with mock.patch.object(mcp_server.audit_log, "record", side_effect=lambda event, **_: events.append(event) or True):
                store = mcp_server.MemoryStore(root)
                store.upsert_entry("L1", "csv-report", "周报用 CSV", "周报一律用 CSV", "正文内容")
                result = mcp_server.handle_tool_call(store, "memory_search", {"query": "CSV"})
            text = result["content"][0]["text"]
            search = events[-1]
            self.assertEqual(search["tool"], "memory_search")
            self.assertEqual(search["bytes"], len(text.encode("utf-8")))
            self.assertEqual(search["tokens"], audit_log.estimate_tokens(text))
            self.assertEqual(search["refs"], [h["ref"] for h in json.loads(text)])
            self.assertNotIn("正文内容", json.dumps(events, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
