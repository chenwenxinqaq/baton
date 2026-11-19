"""Filesystem layout shared by the CLI, web console and MCP server."""

import os
from pathlib import Path


FRAMEWORK_ROOT = Path(__file__).resolve().parent.parent


def data_root(path=None):
    configured = path or os.environ.get("UNIFIED_MEMORY_ROOT") or Path.home() / "UnifiedMemory"
    root = Path(configured).expanduser().resolve()
    if root == FRAMEWORK_ROOT or FRAMEWORK_ROOT in root.parents:
        raise ValueError("Choose a private memory directory outside the framework checkout.")
    return root
