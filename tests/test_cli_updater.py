"""Tests for standalone CLI version reporting, release checking, and verified upgrades."""

from __future__ import annotations

from hashlib import sha256
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "scripts"))

import cli_updater  # noqa: E402
from package_release import PUBLIC_FILES  # noqa: E402


def _make_cli_tarball(dest_tar: Path, version: str, *, extra_member: tuple[str, bytes] | None = None) -> str:
    """Build a valid unified-memory-cli.tar.gz from current PUBLIC_FILES with a custom VERSION."""
    with tarfile.open(dest_tar, "w:gz") as archive:
        for name in PUBLIC_FILES:
            if name.startswith(("desktop/", "tests/")) or name in {".gitignore", "scripts/package_release.py"}:
                continue
            arcname = "unified-memory-cli/" + name
            if name == "VERSION":
                payload = (version + "\n").encode("utf-8")
                info = tarfile.TarInfo(arcname)
                info.size = len(payload)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(payload))
            else:
                archive.add(CODE / name, arcname=arcname, recursive=False)
        if extra_member is not None:
            name, payload = extra_member
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
    return sha256(dest_tar.read_bytes()).hexdigest()


def _stage_cli_dir(dest: Path, version: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in PUBLIC_FILES:
        if name.startswith(("desktop/", "tests/")) or name in {".gitignore", "scripts/package_release.py"}:
            continue
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name == "VERSION":
            target.write_text(version + "\n", encoding="utf-8")
        else:
            shutil.copy2(CODE / name, target)


class CliUpdaterTests(unittest.TestCase):
    def test_version_parsing_and_url_allowlist(self):
        self.assertTrue(cli_updater.is_newer_version("v0.1.23", "0.1.22"))
        self.assertFalse(cli_updater.is_newer_version("0.1.22", "0.1.22"))
        self.assertFalse(cli_updater.is_newer_version("0.1.21", "0.1.22"))
        self.assertTrue(cli_updater.is_allowed_download_url("https://github.com/chenwenxinqaq/baton/releases/download/v0.1.23/unified-memory-cli.tar.gz"))
        self.assertTrue(cli_updater.is_allowed_download_url("https://release-assets.githubusercontent.com/abc"))
        self.assertFalse(cli_updater.is_allowed_download_url("http://github.com/a/b"))
        self.assertFalse(cli_updater.is_allowed_download_url("https://evil.example.com/unified-memory-cli.tar.gz"))

    def test_fetch_latest_uses_etag_cache_and_falls_back_on_api_403(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            calls = []
            api_body = json.dumps({
                "tag_name": "v0.1.23",
                "html_url": "https://github.com/chenwenxinqaq/baton/releases/tag/v0.1.23",
                "assets": [
                    {
                        "name": "unified-memory-cli.tar.gz",
                        "browser_download_url": "https://github.com/chenwenxinqaq/baton/releases/download/v0.1.23/unified-memory-cli.tar.gz",
                        "digest": "sha256:" + ("a" * 64),
                        "size": 12345,
                    }
                ],
            }).encode("utf-8")

            def http_ok(url, headers, timeout, dest):
                calls.append((url, dict(headers or {})))
                if len(calls) == 1:
                    return 200, {"etag": '"etag-v23"'}, api_body, url
                self.assertEqual((headers or {}).get("If-None-Match"), '"etag-v23"')
                return 304, {"etag": '"etag-v23"'}, b"", url

            first = cli_updater.fetch_latest_release(http_get=http_ok, home=home)
            self.assertEqual(first["version"], "0.1.23")
            self.assertFalse(first["from_cache"])
            second = cli_updater.fetch_latest_release(http_get=http_ok, home=home)
            self.assertEqual(second["version"], "0.1.23")
            self.assertTrue(second["from_cache"])

            def http_fallback(url, headers, timeout, dest):
                if url == cli_updater.LATEST_RELEASE_API:
                    return 403, {}, b'{"message":"rate limit"}', url
                self.assertEqual(url, cli_updater.LATEST_RELEASE_WEB)
                return 200, {}, json.dumps({"tag_name": "v0.1.24"}).encode("utf-8"), url

            fb = cli_updater.fetch_latest_release(http_get=http_fallback, home=home)
            self.assertEqual(fb["version"], "0.1.24")
            self.assertEqual(fb["source"], "github-web")
            self.assertTrue(fb["sha256_url"].endswith(".tar.gz.sha256"))

    def test_inspect_install_target_guards_git_and_desktop_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            git_dir = base / "repo"
            (git_dir / ".git").mkdir(parents=True)
            self.assertFalse(cli_updater.inspect_install_target(git_dir)["can_replace"])
            self.assertEqual(cli_updater.inspect_install_target(git_dir)["install_kind"], "git-checkout")

            bundle_dir = base / "Unified Memory.app" / "Contents" / "Resources" / "framework"
            bundle_dir.mkdir(parents=True)
            self.assertFalse(cli_updater.inspect_install_target(bundle_dir)["can_replace"])
            self.assertEqual(cli_updater.inspect_install_target(bundle_dir)["install_kind"], "desktop-bundle")

            cli_dir = base / "unified-memory-cli"
            cli_dir.mkdir()
            self.assertTrue(cli_updater.inspect_install_target(cli_dir)["can_replace"])
            self.assertEqual(cli_updater.inspect_install_target(cli_dir)["install_kind"], "cli-standalone")

    def test_verified_in_place_upgrade_and_rejects_tampered_or_traversal_tarball(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            home.mkdir()
            install_dir = base / "unified-memory-cli"
            _stage_cli_dir(install_dir, "0.1.22")
            self.assertEqual(cli_updater.current_version(install_dir), "0.1.22")

            good_tar = base / "good.tar.gz"
            good_sha = _make_cli_tarball(good_tar, "0.1.23")
            bad_tar = base / "bad.tar.gz"
            bad_sha = _make_cli_tarball(bad_tar, "0.1.23", extra_member=("../escape.txt", b"boom"))

            def make_http(tar_path: Path, digest_hex: str, final_dl_url: str | None = None):
                dl_url = "https://github.com/chenwenxinqaq/baton/releases/download/v0.1.23/unified-memory-cli.tar.gz"
                api_payload = json.dumps({
                    "tag_name": "v0.1.23",
                    "html_url": "https://github.com/chenwenxinqaq/baton/releases/tag/v0.1.23",
                    "assets": [{
                        "name": "unified-memory-cli.tar.gz",
                        "browser_download_url": dl_url,
                        "digest": "sha256:" + digest_hex,
                        "size": tar_path.stat().st_size,
                    }],
                }).encode("utf-8")

                def getter(url, headers, timeout, dest):
                    if url == cli_updater.LATEST_RELEASE_API:
                        return 200, {}, api_payload, url
                    if url == dl_url and dest is not None:
                        shutil.copy2(tar_path, dest)
                        return 200, {}, b"", final_dl_url or "https://objects.githubusercontent.com/cli.tar.gz"
                    raise AssertionError(f"Unexpected URL: {url}")

                return getter

            # 1. Wrong SHA-256 is rejected without modifying install_dir.
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                cli_updater.check_or_upgrade(
                    framework_root=install_dir,
                    apply=True,
                    http_get=make_http(good_tar, "0" * 64),
                    home=home,
                    refresh_shims=False,
                )
            self.assertEqual(cli_updater.current_version(install_dir), "0.1.22")

            # 2. Untrusted redirect target is rejected.
            with self.assertRaisesRegex(ValueError, "非信任域名"):
                cli_updater.check_or_upgrade(
                    framework_root=install_dir,
                    apply=True,
                    http_get=make_http(good_tar, good_sha, final_dl_url="https://evil.example.com/cli.tar.gz"),
                    home=home,
                    refresh_shims=False,
                )

            # 3. Path traversal member inside tarball is rejected.
            with self.assertRaisesRegex(ValueError, "非法路径"):
                cli_updater.check_or_upgrade(
                    framework_root=install_dir,
                    apply=True,
                    http_get=make_http(bad_tar, bad_sha),
                    home=home,
                    refresh_shims=False,
                )
            self.assertFalse((base / "escape.txt").exists())

            # 4. Valid signed-by-SHA tarball upgrades cleanly and refreshes shims.
            result = cli_updater.check_or_upgrade(
                framework_root=install_dir,
                apply=True,
                http_get=make_http(good_tar, good_sha),
                home=home,
                refresh_shims=True,
            )
            self.assertEqual(result["status"], "upgraded")
            self.assertEqual(result["from_version"], "0.1.22")
            self.assertEqual(result["to_version"], "0.1.23")
            self.assertEqual(cli_updater.current_version(install_dir), "0.1.23")
            self.assertTrue(len(result["refreshed_shims"]) >= 3)


if __name__ == "__main__":
    unittest.main()
