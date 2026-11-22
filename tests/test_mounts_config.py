"""Reliability checks for the mounts.json config: locking, schema, validation, change detection."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE / "scripts"))

import library_paths as lp  # noqa: E402


class MountsConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.primary = self.base / "personal"
        self.primary.mkdir()
        self.path = lp.mounts_config_path(home=self.home)

    def write_raw(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        self.path.write_text(text, encoding="utf-8")

    def entry(self, mid, **extra):
        item = {"id": mid, "path": str(self.base / mid), "mode": "ro", "role": "shared"}
        item.update(extra)
        return item

    def test_concurrent_updates_do_not_lose_entries(self):
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]); import library_paths as lp\n"
            "worker, home, base = sys.argv[2], sys.argv[3], sys.argv[4]\n"
            "for i in range(5):\n"
            "    mid = f'w{worker}-{i}'\n"
            "    lp.update_mounts_config(lambda cfg: cfg['mounts'].append("
            "{'id': mid, 'path': base + '/' + mid}), home=home)\n"
        )
        procs = [
            subprocess.Popen([sys.executable, "-c", script, str(CODE / "scripts"), str(n), str(self.home), str(self.base)])
            for n in range(6)
        ]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=60), 0)
        cfg = lp.load_mounts_config(home=self.home)
        self.assertEqual(len(cfg["mounts"]), 30)
        self.assertEqual(len({item["id"] for item in cfg["mounts"]}), 30)
        self.assertEqual(cfg["generation"], 30)

    def test_lock_is_reentrant_and_times_out_for_other_holders(self):
        with lp.mounts_config_lock(home=self.home):
            with lp.mounts_config_lock(home=self.home):
                lp.save_mounts_config({"mounts": []}, home=self.home)
            other = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]); import library_paths as lp\n"
                    "try:\n"
                    "    with lp.mounts_config_lock(home=sys.argv[2], timeout=0.2): pass\n"
                    "except lp.MountsConfigLockTimeout: sys.exit(7)\n",
                    str(CODE / "scripts"),
                    str(self.home),
                ]
            )
            self.assertEqual(other.returncode, 7)

    def test_v1_migrates_and_preserves_unknown_data(self):
        self.write_raw({"version": 1, "future_flag": True, "mounts": [self.entry("team"), "oops", 3]})
        cfg = lp.load_mounts_config(home=self.home)
        self.assertEqual((cfg["version"], cfg["migrated_from"]), (2, 1))
        self.assertEqual([m["id"] for m in cfg["mounts"]], ["team"])
        self.assertEqual([q["value"] for q in cfg["quarantine"]], ["oops", 3])
        before = self.path.read_text(encoding="utf-8")
        self.assertIn('"version": 1', before)  # loading never rewrites the file
        lp.save_mounts_config(cfg, home=self.home)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual((saved["version"], saved["generation"], saved["future_flag"]), (2, 1, True))
        self.assertEqual([q["value"] for q in saved["quarantine"]], ["oops", 3])
        self.assertEqual(self.path.with_name("mounts.json.bak").read_text(encoding="utf-8"), before)
        self.assertIsNone(lp.load_mounts_config(home=self.home)["migrated_from"])

    def test_newer_schema_is_rejected_and_never_overwritten(self):
        self.write_raw({"version": 99, "mounts": []})
        original = self.path.read_text(encoding="utf-8")
        with self.assertRaises(lp.MountsConfigError):
            lp.load_mounts_config(home=self.home)
        with self.assertRaises(lp.MountsConfigError):
            lp.save_mounts_config({"mounts": []}, home=self.home)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)
        issues = []
        libs = lp.resolve_libraries(primary_root=self.primary, home=self.home, issues=issues)
        self.assertEqual([lib["id"] for lib in libs], ["personal"])
        self.assertEqual(issues[0]["code"], "invalid_config")

    def test_corrupt_config_degrades_and_is_moved_aside_on_save(self):
        self.write_raw("{not json")
        with self.assertRaises(lp.MountsConfigError):
            lp.resolve_libraries(primary_root=self.primary, home=self.home)
        issues = []
        libs = lp.resolve_libraries(primary_root=self.primary, home=self.home, issues=issues)
        self.assertEqual([lib["id"] for lib in libs], ["personal"])
        self.assertEqual(len(issues), 1)
        self.assertIn("not valid JSON", issues[0]["message"])
        lp.save_mounts_config({"mounts": [self.entry("team")]}, home=self.home)
        self.assertEqual(len(list(self.path.parent.glob("mounts.json.corrupt-*"))), 1)
        self.assertEqual(lp.load_mounts_config(home=self.home)["mounts"][0]["id"], "team")

    def test_stale_save_raises_conflict_instead_of_losing_changes(self):
        lp.save_mounts_config({"mounts": [self.entry("a")]}, home=self.home)
        stale = lp.load_mounts_config(home=self.home)
        # A writer that ignores the lock (an older build) rewrites the file.
        self.write_raw({"version": 1, "mounts": [self.entry("a"), self.entry("b")]})
        stale["mounts"].append(self.entry("c"))
        with self.assertRaises(lp.MountsConfigConflict):
            lp.save_mounts_config(stale, home=self.home)
        self.assertEqual([m["id"] for m in lp.load_mounts_config(home=self.home)["mounts"]], ["a", "b"])

    def test_invalid_entries_are_reported_and_kept(self):
        good = self.entry("good", layers=["L1"])
        self.write_raw(
            {
                "version": 2,
                "mounts": [
                    good,
                    self.entry("good"),
                    self.entry("samepath", path=good["path"]),
                    self.entry("badmode", mode="admin"),
                    self.entry("badrole", role="root"),
                    self.entry("flag", enabled="no"),
                    {"id": "nopath"},
                    {"id": "Bad ID", "path": str(self.base / "x")},
                    self.entry("off", enabled=False),
                ],
            }
        )
        issues = []
        libs = lp.resolve_libraries(primary_root=self.primary, home=self.home, issues=issues)
        self.assertEqual([lib["id"] for lib in libs], ["personal", "good"])
        self.assertEqual(
            {(i["id"], i["code"]) for i in issues},
            {
                ("good", "duplicate_id"),
                ("samepath", "duplicate_path"),
                ("badmode", "invalid_entry"),
                ("badrole", "invalid_entry"),
                ("flag", "invalid_entry"),
                ("nopath", "invalid_entry"),
                ("Bad ID", "invalid_entry"),
            },
        )
        with_disabled = lp.resolve_libraries(primary_root=self.primary, home=self.home, include_disabled=True, issues=[])
        self.assertIn("off", [lib["id"] for lib in with_disabled])
        with self.assertRaises(ValueError):  # strict mode keeps failing loudly
            lp.resolve_libraries(primary_root=self.primary, home=self.home)
        # Editing through the config API must not drop the unusable entries.
        lp.update_mounts_config(lambda cfg: cfg["mounts"].append(self.entry("extra")), home=self.home)
        self.assertEqual(len(lp.load_mounts_config(home=self.home)["mounts"]), 10)

    def test_config_signature_detects_same_mtime_and_size_rewrites(self):
        self.assertIsNone(lp.config_signature(home=self.home))
        self.write_raw('{"version": 2, "mounts": [], "x": "aaaa"}')
        first = lp.config_signature(home=self.home)
        stat = self.path.stat()
        self.write_raw('{"version": 2, "mounts": [], "x": "bbbb"}')
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        second = lp.config_signature(home=self.home)
        self.assertEqual(self.path.stat().st_size, stat.st_size)
        self.assertNotEqual(first, second)
        self.assertEqual(second, lp.config_signature(home=self.home))
        self.path.unlink()
        self.assertIsNone(lp.config_signature(home=self.home))

    def test_unavailable_reasons(self):
        missing = self.base / "gone"
        self.assertIn("does not exist", lp.library_unavailable_reason(missing))
        empty = self.base / "empty"
        empty.mkdir()
        self.assertIn("MEMORY.md", lp.library_unavailable_reason(empty))
        (empty / "MEMORY.md").write_text("# m\n", encoding="utf-8")
        self.assertIsNone(lp.library_unavailable_reason(empty))
        issue = lp.unavailable_issue({"id": "team", "path": missing}, "x")
        self.assertEqual((issue["code"], issue["id"]), ("unavailable", "team"))


if __name__ == "__main__":
    unittest.main()
