"""Build a source distribution using an explicit list of public framework files."""

import argparse
from hashlib import sha256
import tarfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from tempfile import NamedTemporaryFile

from library_paths import FRAMEWORK_ROOT


PUBLIC_FILES = (
    ".gitignore",
    "VERSION",
    "bin/umem",
    "desktop/licenses/dugite-LICENSE.md",
    "desktop/licenses/git-COPYING",
    "desktop/licenses/gcm-LICENSE",
    "desktop/icon.svg",
    "desktop/tray.svg",
    "desktop/main.cjs",
    "desktop/preload.cjs",
    "desktop/updater.cjs",
    "desktop/updater.test.cjs",
    "desktop/package.json",
    "desktop/package-lock.json",
    "desktop/package.mjs",
    "desktop/runtime.mjs",
    "desktop/umem-python",
    "manager/desktop.css",
    "manager/desktop.html",
    "README.md",
    "manager/app.js",
    "manager/manager.py",
    "manager/page.html",
    "manager/style.css",
    "mcp/server.py",
    "scripts/library_paths.py",
    "scripts/memory_import.py",
    "scripts/memory_lint.py",
    "scripts/memoryctl.py",
    "scripts/cli_updater.py",
    "scripts/package_release.py",
    "scripts/sync_git.py",
    "scripts/async_sync.py",
    "scripts/audit_log.py",
    "scripts/cursor_hook.py",
    "scripts/secret_guard.py",
    "skills/unified-memory/SKILL.md",
    "templates/entry.md",
    "templates/incident.md",
    "templates/library.md",
    "templates/project.md",
    "tests/test_public_framework.py",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", action="store_true", help="Build a CLI distribution without Electron.")
    args = parser.parse_args()
    if args.cli:
        directory = FRAMEWORK_ROOT / "dist"
        directory.mkdir(exist_ok=True)
        output = directory / "unified-memory-cli.tar.gz"
        with NamedTemporaryFile(dir=directory, delete=False) as temp:
            staged = Path(temp.name)
        try:
            with tarfile.open(staged, "w:gz") as archive:
                for name in PUBLIC_FILES:
                    if name.startswith(("desktop/", "tests/")) or name in {".gitignore", "scripts/package_release.py"}:
                        continue
                    source = FRAMEWORK_ROOT / name
                    if source.resolve() != source or not source.is_file():
                        raise ValueError(f"Expected a regular source file: {name}")
                    archive.add(source, arcname="unified-memory-cli/" + name, recursive=False)
            staged.replace(output)
            output.chmod(0o644)
            digest = sha256(output.read_bytes()).hexdigest()
            sha_file = directory / (output.name + ".sha256")
            sha_file.write_text(f"{digest}  {output.name}\n", encoding="utf-8")
            sha_file.chmod(0o644)
        finally:
            staged.unlink(missing_ok=True)
        print(output)
        return
    output = FRAMEWORK_ROOT / "unified-memory-starter.zip"
    with NamedTemporaryFile(dir=FRAMEWORK_ROOT, delete=False) as temp:
        staged = Path(temp.name)
    try:
        with ZipFile(staged, "w", compression=ZIP_DEFLATED) as archive:
            for name in PUBLIC_FILES:
                source = FRAMEWORK_ROOT / name
                if source.resolve() != source or not source.is_file():
                    raise ValueError(f"Expected a regular source file: {name}")
                archive.write(source, "unified-memory-starter/" + name)
        staged.replace(output)
    finally:
        staged.unlink(missing_ok=True)
    print(output)


if __name__ == "__main__":
    main()
