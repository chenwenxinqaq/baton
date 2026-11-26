from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CODE = Path(__file__).resolve().parents[1]


class ManagerConcurrencyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.library = base / "library"
        (base / "home").mkdir()
        env = dict(os.environ)
        env.update({
            "HOME": str(base / "home"),
            "UNIFIED_MEMORY_ROOT": str(self.library),
            "UNIFIED_MEMORY_STATE_DIR": str(base / "state"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        subprocess.check_output([sys.executable, str(CODE / "scripts/memoryctl.py"), "init"], env=env, cwd=base)
        command = [sys.executable, str(CODE / "manager/manager.py"), "--port", "0"]
        self.manager = subprocess.Popen(command, stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(self.stop_manager)
        self.base = self.manager.stdout.readline().strip().rstrip("/")
        self.port = int(self.base.rsplit(":", 1)[1])

    def stop_manager(self):
        self.manager.terminate()
        self.manager.wait(timeout=10)
        self.manager.stdout.close()

    def test_stalled_client_does_not_block_other_requests(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as stalled:
            # Hold a connection open with headers that never finish.
            stalled.sendall(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n".encode())
            time.sleep(0.2)
            started = time.monotonic()
            with urlopen(self.base + "/", timeout=10) as response:
                self.assertEqual(response.status, 200)
                response.read()
            self.assertLess(time.monotonic() - started, 3)

    def test_parallel_saves_all_land_in_files_and_index(self):
        with urlopen(self.base + "/", timeout=5) as response:
            token = re.search(r'name="csrf" value="([^"]+)"', response.read().decode()).group(1)

        def save(number):
            fields = {
                "csrf": token, "library": "primary", "scope": "index", "layer": "L3",
                "title": f"并发条目 {number}", "description": f"并发写入 {number}",
                "body": f"正文 {number}", "slug": f"parallel-{number}",
            }
            request = Request(self.base + "/save", data=urlencode(fields).encode(), headers={"Origin": self.base})
            with urlopen(request, timeout=30) as response:
                response.read()
                return response.status

        with ThreadPoolExecutor(max_workers=12) as pool:
            statuses = list(pool.map(save, range(12)))

        self.assertEqual(statuses, [200] * 12)
        index = (self.library / "MEMORY.md").read_text(encoding="utf-8")
        for number in range(12):
            self.assertTrue((self.library / "projects" / f"parallel-{number}.md").is_file())
            self.assertEqual(index.count(f"](projects/parallel-{number}.md)"), 1)


if __name__ == "__main__":
    unittest.main()
