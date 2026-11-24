"""Writes must not silently lose another session's edit or the previous version."""

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

LONG = "排查步骤与证据。" * 80


class WriteSafetyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.store = _store(self.base, "personal")

    def _write(self, name, body, **kwargs):
        return self.store.upsert_entry(
            layer=kwargs.pop("layer", "L1"),
            name=name,
            title=kwargs.pop("title", "Docker 构建缓存排查"),
            description=kwargs.pop("description", "缓存失效的根因"),
            body=body,
            **kwargs,
        )

    def test_read_reports_updated_at_for_compare_and_set(self):
        self._write("docker-cache", LONG)
        read = self.store.read_entry("methods/docker-cache.md")
        self.assertTrue(read["updated_at"])

    def test_stale_write_is_rejected_and_fresh_one_succeeds(self):
        first = self._write("docker-cache", LONG)
        with self.assertRaises(ValueError) as caught:
            self._write("docker-cache", LONG + "新结论。", expected_updated_at="2020-01-01T00:00:00+08:00")
        self.assertIn("changed since you read it", str(caught.exception))
        self.assertIn(first["updated_at"], str(caught.exception))
        again = self._write("docker-cache", LONG + "新结论。", expected_updated_at=first["updated_at"])
        self.assertEqual(again["path"], "methods/docker-cache.md")
        self.assertIn("新结论", self.store.read_entry("methods/docker-cache.md")["content"])

    def test_update_reports_the_version_it_replaced(self):
        self._write("docker-cache", LONG)
        res = self._write("docker-cache", LONG + "补充一句。")
        self.assertTrue(res["previous"]["updated_at"])
        self.assertGreater(res["previous"]["chars"], 400)
        self.assertTrue(Path(res["previous"]["backup"]).is_file())
        self.assertNotIn("content_warning", res)

    def test_shrinking_write_warns_and_keeps_the_backup(self):
        self._write("docker-cache", LONG)
        res = self._write("docker-cache", "一句话。")
        self.assertIn("content_warning", res)
        backup = Path(res["previous"]["backup"]).read_text(encoding="utf-8")
        self.assertIn("排查步骤与证据", backup)

    def test_new_entry_points_at_existing_entries_on_the_same_topic(self):
        self._write("docker-cache", LONG)
        res = self._write("docker-cache-notes", LONG, layer="L3", title="Docker 构建缓存排查记录")
        refs = [item["ref"] for item in res["related"]]
        self.assertIn("methods/docker-cache.md", refs)
        self.assertIn("related", res)
        self.assertIn("one current conclusion", res["notice"])

    def test_updates_do_not_get_duplicate_hints(self):
        self._write("docker-cache", LONG)
        res = self._write("docker-cache", LONG + "再补充。")
        self.assertNotIn("related", res)


if __name__ == "__main__":
    unittest.main()
