"""Tests for Sleep-time Upkeep / Morning Note generation, application, and dismissal."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "scripts"))
sys.path.insert(0, str(CODE / "mcp"))
sys.path.insert(0, str(CODE / "manager"))

import server as mcp_server  # noqa: E402
import upkeep  # noqa: E402
from manager import Library, render_upkeep_panel  # noqa: E402


class UpkeepTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.root = self.base / "library"
        self.root.mkdir()
        for folder in ("constraints", "methods", "preferences", "projects", "archive", "user-managed"):
            (self.root / folder).mkdir()
        (self.root / "MEMORY.md").write_text(
            "# M\n\n## L0\n\n## L1\n\n## L2\n\n## L3\n\n## L4\n",
            encoding="utf-8",
        )
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.addCleanup(self._restore_home)
        self.library = Library(self.root, "个人库")
        self.store = self.library.store

    def _restore_home(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home

    def _env(self):
        return {
            **os.environ,
            "HOME": str(self.home),
            "UNIFIED_MEMORY_ROOT": str(self.root),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def test_duplicate_merge_stale_archive_and_oversized_trim_apply_cleanly(self):
        # 1. Duplicate pair on the same topic
        self.store.upsert_entry(
            "L1",
            "docker-cache-old",
            "Docker 构建缓存排查",
            "Docker BuildKit 构建缓存失效排查步骤",
            "2026-11-10 旧笔记：检查 COPY 顺序与锁文件。",
        )
        self.store.upsert_entry(
            "L1",
            "docker-cache-new",
            "Docker 构建缓存排查",
            "Docker BuildKit 构建缓存失效排查步骤",
            "2026-11-26 新结论：改用 --mount=type=cache 后命中率提升。",
        )

        # 2. Expired L3 snapshot
        self.store.upsert_entry(
            "L3",
            "old-migration",
            "一次性迁移快照",
            "2026-01 临时迁移计划",
            "2026-01-01 已完成的旧迁移快照。",
            verified_at="2026-01-01",
            valid_until="2026-01-15",
        )

        # 3. Oversized multi-section entry (> 6000 chars)
        long_sections = "\n\n".join(
            f"## 阶段 {i}\n\n"
            + "\n".join(
                f"- 第 {i}-{j} 条详细日志、构建耗时与参数验证记录（含中间产物校验与回滚对照），用于测试超长快照精简。"
                for j in range(1, 28)
            )
            for i in range(1, 7)
        )
        self.store.upsert_entry(
            "L3",
            "huge-project",
            "超长项目快照",
            "包含多个历史阶段的详细快照",
            f"2026-11-26 项目总览。\n\n{long_sections}",
        )

        note = upkeep.public_morning_note(self.store, audit_home=self.home)
        by_kind = {item["kind"]: item for item in note["suggestions"]}
        self.assertEqual(set(by_kind), {"merge_duplicate", "archive_stale", "trim_oversized"})
        self.assertTrue(by_kind["merge_duplicate"]["diff"])
        self.assertTrue(by_kind["trim_oversized"]["diff"])
        self.assertGreater(by_kind["trim_oversized"]["saved_chars"], 1000)

        # Desktop UI renders all suggestions with Apply / Dismiss controls.
        html = render_upkeep_panel("tok", "primary", self.library, agent_home=self.home)
        self.assertIn("合并重复", html)
        self.assertIn("归档过期", html)
        self.assertIn("精简超长", html)
        self.assertIn("应用", html)
        self.assertIn("忽略", html)

        # Apply duplicate merge: folds older into newer and archives older to L4.
        merge_item = by_kind["merge_duplicate"]
        merge_res = upkeep.apply_suggestion(
            self.store, merge_item["id"], expected_fingerprint=merge_item["fingerprint"], audit_home=self.home
        )
        self.assertEqual(merge_res["path"], "methods/docker-cache-new.md")
        self.assertEqual(merge_res["archived_path"], "archive/docker-cache-old.md")
        self.assertTrue((self.root / "archive/docker-cache-old.md").is_file())
        merged_text = (self.root / "methods/docker-cache-new.md").read_text(encoding="utf-8")
        self.assertIn("改用 --mount=type=cache", merged_text)
        self.assertIn("检查 COPY 顺序与锁文件", merged_text)

        # Apply stale archive: moves expired L3 snapshot to L4.
        arch_item = by_kind["archive_stale"]
        arch_res = upkeep.apply_suggestion(
            self.store, arch_item["id"], expected_fingerprint=arch_item["fingerprint"], audit_home=self.home
        )
        self.assertEqual(arch_res["path"], "archive/old-migration.md")
        self.assertTrue((self.root / "archive/old-migration.md").is_file())

        # Apply oversized trim: shrinks body and keeps full backup in trash.
        trim_item = by_kind["trim_oversized"]
        before_len = len((self.root / "projects/huge-project.md").read_text(encoding="utf-8"))
        trim_res = upkeep.apply_suggestion(
            self.store, trim_item["id"], expected_fingerprint=trim_item["fingerprint"], audit_home=self.home
        )
        after_len = len((self.root / "projects/huge-project.md").read_text(encoding="utf-8"))
        self.assertLess(after_len, before_len)
        self.assertTrue(Path(trim_res["backup"]).is_file())
        self.assertIn("阶段 6", (self.root / "projects/huge-project.md").read_text(encoding="utf-8"))

        # Morning Note is now clean.
        clean_note = upkeep.public_morning_note(self.store, audit_home=self.home)
        self.assertEqual(clean_note["suggestions"], [])

    def test_dismiss_hides_suggestion_until_content_changes_and_cli_works(self):
        self.store.upsert_entry(
            "L3",
            "temp-note",
            "过期临时条目",
            "临时截止策略",
            "2026-01-01 临时记录。",
            verified_at="2026-01-01",
            valid_until="2026-01-10",
        )
        rep = json.loads(
            subprocess.check_output(
                [sys.executable, str(CODE / "scripts/memoryctl.py"), "upkeep", "report"],
                env=self._env(),
                text=True,
            )
        )
        self.assertEqual(len(rep["suggestions"]), 1)
        item = rep["suggestions"][0]

        subprocess.check_output(
            [
                sys.executable,
                str(CODE / "scripts/memoryctl.py"),
                "upkeep",
                "dismiss",
                item["id"],
                "--fingerprint",
                item["fingerprint"],
            ],
            env=self._env(),
            text=True,
        )
        hidden = upkeep.public_morning_note(self.store, audit_home=self.home)
        self.assertEqual(hidden["suggestions"], [])
        self.assertEqual(hidden["dismissed_count"], 1)

        # Updating the entry changes its fingerprint so the suggestion resurfaces if still expired.
        self.store.upsert_entry("L3", "temp-note", body="补充说明。", mode="append")
        resurfaced = upkeep.public_morning_note(self.store, audit_home=self.home)
        self.assertEqual(len(resurfaced["suggestions"]), 1)

        # Dismiss again then reset via CLI.
        upkeep.dismiss_suggestion(self.root, resurfaced["suggestions"][0]["id"], resurfaced["suggestions"][0]["fingerprint"])
        self.assertEqual(upkeep.public_morning_note(self.store, audit_home=self.home)["suggestions"], [])
        subprocess.check_output(
            [sys.executable, str(CODE / "scripts/memoryctl.py"), "upkeep", "reset"],
            env=self._env(),
            text=True,
        )
        self.assertEqual(len(upkeep.public_morning_note(self.store, audit_home=self.home)["suggestions"]), 1)


if __name__ == "__main__":
    unittest.main()
