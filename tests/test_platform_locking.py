import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import library_paths  # noqa: E402


class FakeMsvcrt:
    LK_NBLCK = 2
    LK_UNLCK = 0

    def __init__(self, busy=0):
        self.busy = busy
        self.calls = []

    def locking(self, fd, mode, nbytes):
        self.calls.append((mode, os.lseek(fd, 0, os.SEEK_CUR), nbytes))
        if mode == self.LK_NBLCK and self.busy:
            self.busy -= 1
            raise OSError("locked")


class PlatformLockingTests(unittest.TestCase):
    def test_every_entry_module_imports_without_fcntl(self):
        code = (
            "import sys\n"
            "sys.modules['fcntl'] = None\n"
            f"sys.path[:0] = [{str(ROOT / 'scripts')!r}, {str(ROOT / 'mcp')!r}, {str(ROOT / 'manager')!r}]\n"
            "import audit_log, sync_git, memory_import, memoryctl, server, manager\n"
            "import library_paths\n"
            "assert library_paths.fcntl is None\n"
        )
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, HOME=temp, UNIFIED_MEMORY_ROOT=str(Path(temp) / "lib"),
                       UNIFIED_MEMORY_STATE_DIR=str(Path(temp) / "state"))
            result = subprocess.run([sys.executable, "-B", "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_lock_file_waits_and_locks_from_the_start_of_the_file_without_fcntl(self):
        fake = FakeMsvcrt(busy=2)
        with tempfile.TemporaryDirectory() as temp:
            with (Path(temp) / "x.lock").open("a+", encoding="utf-8") as handle:
                handle.write("held by 123")
                handle.flush()
                with patch.object(library_paths, "fcntl", None), patch.object(library_paths, "msvcrt", fake), \
                        patch.object(library_paths.time, "sleep") as sleep:
                    library_paths.lock_file(handle.fileno())
                    library_paths.unlock_file(handle.fileno())
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(fake.calls, [(2, 0, 1)] * 3 + [(0, 0, 1)])


if __name__ == "__main__":
    unittest.main()
