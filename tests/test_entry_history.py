"""Previous versions must be listable and restorable, not just sitting in trash."""

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


class EntryHistoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.store = _store(self.base, "personal")

    def _write(self, body, layer="L1"):
        return self.store.upsert_entry(
            layer=layer,
            name="docker-cache",
            title="Docker 构建缓存排查",
            description="缓存失效的根因",
            body=body,
        )

    def test_history_lists_versions_newest_first(self):
        self._write("第一版结论。")
        self._write("第二版结论。")
        self._write("第三版结论。")
        res = self.store.entry_history("docker-cache")
        self.assertEqual(res["current"]["path"], "methods/docker-cache.md")
        self.assertEqual(res["total_versions"], 2)
        self.assertEqual([v["reason"] for v in res["versions"]], ["update", "update"])
        self.assertGreaterEqual(res["versions"][0]["saved_at"], res["versions"][-1]["saved_at"])
        self.assertTrue(all(v["title"] == "Docker 构建缓存排查" for v in res["versions"]))

    def test_history_is_empty_for_a_fresh_entry(self):
        self._write("只写了一次。")
        res = self.store.entry_history("docker-cache")
        self.assertEqual(res["versions"], [])
        self.assertNotIn("notice", res)

    def test_dry_run_shows_a_version_without_writing(self):
        self._write("第一版结论。")
        self._write("第二版结论。")
        version = self.store.entry_history("docker-cache")["versions"][0]["version"]
        preview = self.store.restore_entry(version, dry_run=True)
        self.assertFalse(preview["restored"])
        self.assertIn("第一版结论", preview["content"])
        self.assertEqual(preview["would_replace"], "methods/docker-cache.md")
        self.assertIn("第二版结论", self.store.read_entry("methods/docker-cache.md")["content"])

    def test_restore_puts_the_old_version_back_and_keeps_the_replaced_one(self):
        self._write("第一版结论。")
        self._write("第二版结论。")
        version = self.store.entry_history("docker-cache")["versions"][0]["version"]
        res = self.store.restore_entry(version)
        self.assertTrue(res["restored"])
        self.assertEqual(res["restored_from"], version)
        content = self.store.read_entry("methods/docker-cache.md")["content"]
        self.assertIn("第一版结论", content)
        self.assertNotIn("第二版结论", content)
        saved = [v["version"] for v in self.store.entry_history("docker-cache")["versions"]]
        self.assertEqual(len(saved), 2)

    def test_restore_after_a_layer_change_uses_the_current_layer(self):
        self._write("第一版结论。")
        self._write("搬到 L3 的结论。", layer="L3")
        versions = self.store.entry_history("docker-cache")["versions"]
        reasons = {v["reason"] for v in versions}
        self.assertIn("layer-change", reasons)
        old_version = [v for v in versions if v["reason"] == "layer-change"][0]["version"]
        res = self.store.restore_entry(old_version)
        self.assertEqual(res["path"], "projects/docker-cache.md")

    def test_unknown_version_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.restore_entry("../../etc/passwd")
        with self.assertRaises(ValueError) as caught:
            self.store.restore_entry("20200101-000000-docker-cache-abcd.md")
        self.assertIn("No saved version", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
