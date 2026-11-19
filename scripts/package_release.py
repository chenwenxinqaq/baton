"""Build a source distribution using an explicit list of public framework files."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from tempfile import NamedTemporaryFile

from library_paths import FRAMEWORK_ROOT


PUBLIC_FILES = (
    ".gitignore",
    "README.md",
    "manager/app.js",
    "manager/manager.py",
    "manager/page.html",
    "manager/style.css",
    "mcp/server.py",
    "scripts/library_paths.py",
    "scripts/memoryctl.py",
    "scripts/package_release.py",
    "scripts/sync_git.py",
    "scripts/audit_log.py",
    "scripts/cursor_hook.py",
    "skills/unified-memory/SKILL.md",
    "templates/entry.md",
    "templates/incident.md",
    "templates/library.md",
    "templates/project.md",
    "tests/test_public_framework.py",
)


def main():
    output = FRAMEWORK_ROOT / "unified-memory-starter.zip"
    with NamedTemporaryFile(dir=FRAMEWORK_ROOT, delete=False) as temp:
        staged = Path(temp.name)
    try:
        with ZipFile(staged, "w", compression=ZIP_DEFLATED) as archive:
            for name in PUBLIC_FILES:
                source = FRAMEWORK_ROOT / name
                if source.resolve() != source or not source.is_file():
                    raise ValueError(f"Expected a regular source file: {name}")
                archive.writestr("unified-memory-starter/" + name, source.read_bytes())
        staged.replace(output)
    finally:
        staged.unlink(missing_ok=True)
    print(output)


if __name__ == "__main__":
    main()
