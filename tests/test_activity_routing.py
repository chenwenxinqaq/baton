"""Regression tests for cross-library links in the global activity dashboard."""

from pathlib import Path
import sys
import tempfile
import unittest


CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE / "manager"))
sys.path.insert(0, str(CODE / "scripts"))

import audit_log  # noqa: E402
from manager import _ref_link  # noqa: E402


class ActivityRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def record(self, **event):
        audit_log.record({"source": "mcp", "ok": True, **event}, home=self.home)

    def test_top_hits_keep_library_ownership_for_bare_and_qualified_refs(self):
        # Personal refs are intentionally bare for backward compatibility,
        # while mounted-library refs carry their library id in the ref itself.
        self.record(tool="memory_search", library="personal", refs=["projects/shared.md"])
        self.record(tool="memory_search", library="team-mymem", refs=["team-mymem:projects/shared.md"])
        self.record(tool="memory_read", ref="projects/legacy.md")

        summary = audit_log.summarize(home=self.home)
        self.assertEqual(
            {(hit["library"], hit["ref"]) for hit in summary["top_hits"]},
            {
                ("personal", "projects/shared.md"),
                ("team-mymem", "team-mymem:projects/shared.md"),
                ("personal", "projects/legacy.md"),
            },
        )

    def test_ref_link_uses_event_library_instead_of_selected_page_library(self):
        personal = _ref_link(
            "projects/personal-only.md",
            key="team-mymem",
            event_library="personal",
        )
        self.assertIn("library=primary", personal)
        self.assertNotIn("library=team-mymem", personal)

        shared = _ref_link(
            "team-mymem:projects/shared.md",
            key="primary",
            event_library="personal",
        )
        self.assertIn("library=team-mymem", shared)
        self.assertNotIn("library=primary", shared)

        # Old audit events without a library field retain the current fallback.
        legacy = _ref_link("projects/legacy.md", key="team-mymem")
        self.assertIn("library=team-mymem", legacy)


if __name__ == "__main__":
    unittest.main()
