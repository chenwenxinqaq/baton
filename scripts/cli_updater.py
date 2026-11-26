"""Verified self-check and in-place upgrade for standalone Unified Memory CLI installs."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from library_paths import FRAMEWORK_ROOT, _atomic_write_json, global_state_dir
import sync_git


REPO_SLUG = "chenwenxinqaq/baton"
REPO_URL = f"https://github.com/{REPO_SLUG}"
RELEASES_URL = f"{REPO_URL}/releases"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPO_SLUG}/releases/latest"
LATEST_RELEASE_WEB = f"{RELEASES_URL}/latest"
CLI_ASSET_NAME = "unified-memory-cli.tar.gz"
SHA256_RE = re.compile(r"\b([a-fA-F0-9]{64})\b")
SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


def normalize_version(tag: str) -> str:
    text = str(tag or "").strip()
    if text.lower().startswith("v"):
        text = text[1:]
    return text


def parse_version(tag: str):
    match = SEMVER_RE.fullmatch(str(tag or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def is_newer_version(latest: str, current: str) -> bool:
    a = parse_version(latest)
    b = parse_version(current)
    if a is None or b is None:
        return False
    return a > b


def parse_sha256(text: str) -> str:
    raw = str(text or "").strip()
    if raw.lower().startswith("sha256:"):
        raw = raw[7:].strip()
    match = SHA256_RE.search(raw)
    return match.group(1).lower() if match else ""


def is_allowed_download_url(url: str) -> bool:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return False
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return host == "github.com" or host == "githubusercontent.com" or host.endswith(".githubusercontent.com")


def current_version(framework_root: Path = FRAMEWORK_ROOT) -> str:
    root = Path(framework_root).expanduser().resolve()
    version_file = root / "VERSION"
    if version_file.is_file():
        val = normalize_version(version_file.read_text(encoding="utf-8").strip())
        if parse_version(val):
            return val
    pkg_json = root / "desktop" / "package.json"
    if pkg_json.is_file():
        try:
            val = normalize_version(json.loads(pkg_json.read_text(encoding="utf-8")).get("version", ""))
            if parse_version(val):
                return val
        except (OSError, ValueError):
            pass
    # When running from inside Unified Memory.app/Contents/Resources/framework
    info_plist = root.parent.parent / "Info.plist"
    if info_plist.is_file():
        try:
            with info_plist.open("rb") as fh:
                val = normalize_version(plistlib.load(fh).get("CFBundleShortVersionString", ""))
            if parse_version(val):
                return val
        except Exception:
            pass
    return "0.0.0"


def inspect_install_target(framework_root: Path = FRAMEWORK_ROOT) -> dict:
    root = Path(framework_root).expanduser().resolve()
    as_posix = root.as_posix()
    if (root / ".git").exists():
        return {
            "install_kind": "git-checkout",
            "can_replace": False,
            "target": str(root),
            "reason": "当前运行于 Git 源码仓库，请使用 git pull 更新，避免覆盖本地改动。",
        }
    if ".app/Contents/Resources/framework" in as_posix:
        return {
            "install_kind": "desktop-bundle",
            "can_replace": False,
            "target": str(root),
            "reason": "当前 CLI 由桌面版 Unified Memory.app 提供，请在桌面应用菜单栏点击「检查更新…」完成升级。",
        }
    if not os.access(root, os.W_OK) or not os.access(root.parent, os.W_OK):
        return {
            "install_kind": "cli-standalone",
            "can_replace": False,
            "target": str(root),
            "reason": f"安装目录缺少写权限：{root}。请检查目录权限或手动重新解压新版。",
        }
    return {
        "install_kind": "cli-standalone",
        "can_replace": True,
        "target": str(root),
        "reason": "",
    }


def _parse_curl_headers(raw_headers: bytes) -> tuple[int, dict]:
    blocks = re.split(rb"\r?\n\r?\n", raw_headers.strip())
    status = 0
    headers = {}
    for block in blocks:
        lines = block.splitlines()
        if not lines or not lines[0].startswith(b"HTTP/"):
            continue
        parts = lines[0].decode("latin-1", errors="replace").split()
        status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        headers = {}
        for line in lines[1:]:
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.decode("latin-1", errors="replace").strip().lower()] = v.decode("latin-1", errors="replace").strip()
    return status, headers


def _default_http_get(url: str, headers: dict | None = None, timeout: int = 20, dest_path: Path | None = None):
    """Perform HTTPS GET honoring env and macOS static/PAC proxies via curl (or urllib fallback)."""
    req_headers = {"User-Agent": "UnifiedMemory-CLI"}
    if headers:
        req_headers.update(headers)
    proxy = sync_git.resolve_system_proxy(url)
    curl_bin = "/usr/bin/curl" if os.access("/usr/bin/curl", os.X_OK) else shutil.which("curl")
    if curl_bin:
        with tempfile.TemporaryDirectory(prefix="umem-http-") as tmp:
            hdr_file = Path(tmp) / "headers.txt"
            out_file = Path(dest_path) if dest_path is not None else Path(tmp) / "body.bin"
            cmd = [
                curl_bin,
                "--silent",
                "--show-error",
                "--http1.1",
                "--location",
                "--max-redirs",
                "5",
                "--connect-timeout",
                "15",
                "--max-time",
                str(max(10, int(timeout))),
                "--dump-header",
                str(hdr_file),
                "--output",
                str(out_file),
                "--write-out",
                "%{url_effective}",
            ]
            if proxy:
                cmd += ["--proxy", proxy]
            for k, v in req_headers.items():
                cmd += ["-H", f"{k}: {v}"]
            cmd.append(url)
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or f"curl exit {proc.returncode}").strip()
                raise RuntimeError(f"网络请求失败：{detail}")
            status, resp_headers = _parse_curl_headers(hdr_file.read_bytes() if hdr_file.is_file() else b"")
            final_url = (proc.stdout or "").strip() or url
            body = b"" if dest_path is not None else (out_file.read_bytes() if out_file.is_file() else b"")
            return status, resp_headers, body, final_url

    req = Request(url, headers=req_headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            final_url = resp.geturl() or url
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            if dest_path is not None:
                with Path(dest_path).open("wb") as fh:
                    shutil.copyfileobj(resp, fh)
                body = b""
            else:
                body = resp.read()
            return resp.status, resp_headers, body, final_url
    except HTTPError as exc:
        resp_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return exc.code, resp_headers, exc.read() if exc.fp else b"", exc.geturl() or url
    except URLError as exc:
        raise RuntimeError(f"网络请求失败：{exc.reason}") from exc


def _cache_path(home=None) -> Path:
    return global_state_dir(home=home) / "cli-update-cache.json"


def _load_cache(home=None) -> dict:
    path = _cache_path(home=home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _save_cache(payload: dict, home=None) -> None:
    try:
        _atomic_write_json(_cache_path(home=home), payload)
    except OSError:
        pass


def _release_from_api(data: dict) -> dict | None:
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name") or "").strip()
    version = normalize_version(tag)
    if not parse_version(version):
        return None
    assets = data.get("assets") if isinstance(data.get("assets"), list) else []
    cli_asset = next((a for a in assets if isinstance(a, dict) and a.get("name") == CLI_ASSET_NAME), None)
    sha_asset = next((a for a in assets if isinstance(a, dict) and a.get("name") == CLI_ASSET_NAME + ".sha256"), None)
    download_url = str((cli_asset or {}).get("browser_download_url") or "")
    if not download_url or not is_allowed_download_url(download_url):
        return None
    sha_url = str((sha_asset or {}).get("browser_download_url") or "")
    if sha_url and not is_allowed_download_url(sha_url):
        sha_url = ""
    return {
        "tag": tag,
        "version": version,
        "release_url": str(data.get("html_url") or f"{RELEASES_URL}/tag/{tag}"),
        "download_url": download_url,
        "sha256": parse_sha256((cli_asset or {}).get("digest") or ""),
        "sha256_url": sha_url,
        "size": int((cli_asset or {}).get("size") or 0),
        "notes": str(data.get("body") or "").strip()[:1200],
        "source": "github-api",
    }


def fetch_latest_release(*, http_get=None, home=None) -> dict:
    """Fetch latest Unified Memory CLI release with ETag cache and github.com JSON fallback."""
    getter = http_get or _default_http_get
    cache = _load_cache(home=home)
    headers = {"Accept": "application/vnd.github+json"}
    if cache.get("etag") and isinstance(cache.get("release"), dict):
        headers["If-None-Match"] = str(cache["etag"])

    api_error = None
    try:
        status, resp_headers, body, _ = getter(LATEST_RELEASE_API, headers, 15, None)
        if status == 304 and isinstance(cache.get("release"), dict):
            return {**cache["release"], "from_cache": True}
        if 200 <= status < 300:
            parsed = json.loads(body.decode("utf-8"))
            rel = _release_from_api(parsed)
            if rel is None:
                raise RuntimeError("最新发布尚未包含 CLI 安装包，请稍后重试。")
            etag = resp_headers.get("etag") or ""
            _save_cache({"etag": etag, "release": rel, "checked_at": int(time.time())}, home=home)
            return {**rel, "from_cache": False}
        api_error = RuntimeError(f"GitHub API 返回 HTTP {status}")
    except Exception as exc:
        api_error = exc

    # Fallback: GitHub web JSON endpoint avoids the 60 req/hr unauthenticated API limit.
    try:
        status, _, body, _ = getter(LATEST_RELEASE_WEB, {"Accept": "application/json"}, 15, None)
        if 200 <= status < 300:
            parsed = json.loads(body.decode("utf-8"))
            tag = str(parsed.get("tag_name") or "").strip()
            version = normalize_version(tag)
            if parse_version(version):
                return {
                    "tag": tag,
                    "version": version,
                    "release_url": f"{RELEASES_URL}/tag/{tag}",
                    "download_url": f"{RELEASES_URL}/download/{tag}/{CLI_ASSET_NAME}",
                    "sha256": "",
                    "sha256_url": f"{RELEASES_URL}/download/{tag}/{CLI_ASSET_NAME}.sha256",
                    "size": 0,
                    "notes": "",
                    "source": "github-web",
                    "from_cache": False,
                }
    except Exception:
        pass
    raise RuntimeError(str(api_error or "无法获取最新版本信息"))


def _sha256_file(path: Path) -> str:
    h = sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _safe_extract_cli_tarball(tarball: Path, dest_dir: Path) -> Path:
    """Safely extract unified-memory-cli.tar.gz without traversal or symlinks."""
    dest_resolved = dest_dir.resolve()
    top_prefix = "unified-memory-cli"
    extracted_files = 0
    with tarfile.open(tarball, "r:gz") as archive:
        for member in archive.getmembers():
            posix = PurePosixPath(member.name)
            if posix.is_absolute() or ".." in posix.parts or not posix.parts:
                raise ValueError(f"安装包包含非法路径：{member.name}")
            if posix.parts[0] != top_prefix:
                raise ValueError(f"安装包根目录异常：{member.name}")
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"安装包包含不支持的特殊文件：{member.name}")
            target = (dest_resolved / Path(*posix.parts)).resolve()
            if not target.is_relative_to(dest_resolved):
                raise ValueError(f"安装包路径越界：{member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = archive.extractfile(member)
            if src is None:
                continue
            with src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            mode = member.mode & 0o755
            target.chmod(0o755 if (mode & 0o111) else 0o644)
            extracted_files += 1
    staged = dest_resolved / top_prefix
    required = ("bin/umem", "scripts/memoryctl.py", "mcp/server.py", "manager/manager.py")
    missing = [rel for rel in required if not (staged / rel).is_file()]
    if extracted_files == 0 or missing:
        raise ValueError(f"安装包内容不完整，缺少：{', '.join(missing) or '核心文件'}")
    return staged


def _verify_staged_cli(staged_root: Path, expected_version: str) -> None:
    ver_file = staged_root / "VERSION"
    if ver_file.is_file():
        actual = normalize_version(ver_file.read_text(encoding="utf-8").strip())
        if expected_version and parse_version(actual) and actual != expected_version:
            raise ValueError(f"安装包版本号不匹配（期望 {expected_version}，实际 {actual}）。")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [sys.executable, "-B", str(staged_root / "scripts" / "memoryctl.py"), "interface"],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise ValueError(f"新版 CLI 自检失败：{detail[:240]}")
    info = json.loads(proc.stdout)
    if not isinstance(info, dict) or not info.get("protocol_version"):
        raise ValueError("新版 CLI 自检输出异常。")


def check_or_upgrade(
    *,
    framework_root: Path = FRAMEWORK_ROOT,
    apply: bool = False,
    force: bool = False,
    http_get=None,
    home=None,
    refresh_shims: bool = True,
) -> dict:
    """Check for a newer CLI release, or download, verify, and atomically upgrade in place."""
    root = Path(framework_root).expanduser().resolve()
    cur = current_version(root)
    target_info = inspect_install_target(root)
    release = fetch_latest_release(http_get=http_get, home=home)
    latest = release["version"]
    update_available = is_newer_version(latest, cur)
    summary = {
        "current_version": cur,
        "latest_version": latest,
        "latest_tag": release["tag"],
        "update_available": update_available,
        "source": release["source"],
        "from_cache": bool(release.get("from_cache", False)),
        "release_url": release["release_url"],
        "download_url": release["download_url"],
        "sha256": release.get("sha256") or "",
        "install_kind": target_info["install_kind"],
        "can_replace": target_info["can_replace"],
        "target": target_info["target"],
        "reason": target_info["reason"],
    }
    if not apply:
        return {"status": "update_available" if update_available else "up_to_date", **summary}

    if not target_info["can_replace"]:
        raise ValueError(target_info["reason"] or "当前安装方式不支持原地升级。")
    if not update_available and not force:
        return {"status": "up_to_date", **summary}

    getter = http_get or _default_http_get
    expected_sha = release.get("sha256") or ""
    if not expected_sha and release.get("sha256_url"):
        sha_url = release["sha256_url"]
        if not is_allowed_download_url(sha_url):
            raise ValueError("校验文件地址不在允许的 GitHub 域名范围内。")
        st, _, body, final_sha_url = getter(sha_url, {}, 15, None)
        if not is_allowed_download_url(final_sha_url):
            raise ValueError("校验文件重定向到非信任域名，已中止升级。")
        if 200 <= st < 300:
            expected_sha = parse_sha256(body.decode("utf-8", errors="replace"))
    if not expected_sha:
        raise ValueError("未获取到发布包的 SHA-256 校验值，已拒绝原地覆盖。")

    download_url = release["download_url"]
    if not is_allowed_download_url(download_url):
        raise ValueError("下载地址不在允许的 GitHub 域名范围内。")

    with tempfile.TemporaryDirectory(prefix="umem-cli-upgrade-") as tmp:
        tmp_dir = Path(tmp)
        tarball = tmp_dir / CLI_ASSET_NAME
        st, _, _, final_url = getter(download_url, {}, 120, tarball)
        if not is_allowed_download_url(final_url):
            raise ValueError("安装包重定向到非信任域名，已中止升级。")
        if not (200 <= st < 300) or not tarball.is_file() or tarball.stat().st_size == 0:
            raise ValueError(f"下载 CLI 安装包失败（HTTP {st}）。")
        actual_sha = _sha256_file(tarball)
        if actual_sha != expected_sha:
            raise ValueError(f"SHA-256 校验失败（期望 {expected_sha[:12]}…，实际 {actual_sha[:12]}…）。")

        staged = _safe_extract_cli_tarball(tarball, tmp_dir / "extracted")
        _verify_staged_cli(staged, latest)

        parent = root.parent
        pid = os.getpid()
        staged_sibling = parent / f".{root.name}.new-{pid}"
        backup_sibling = parent / f".{root.name}.bak-{pid}"
        if staged_sibling.exists():
            shutil.rmtree(staged_sibling)
        if backup_sibling.exists():
            shutil.rmtree(backup_sibling)

        shutil.copytree(staged, staged_sibling, symlinks=False)
        swapped_backup = False
        try:
            os.replace(root, backup_sibling)
            swapped_backup = True
            os.replace(staged_sibling, root)
        except Exception as exc:
            if swapped_backup and backup_sibling.exists() and not root.exists():
                try:
                    os.replace(backup_sibling, root)
                except OSError:
                    pass
            if staged_sibling.exists():
                shutil.rmtree(staged_sibling, ignore_errors=True)
            raise RuntimeError(f"原子替换失败，已自动回滚：{exc}") from exc
        else:
            shutil.rmtree(backup_sibling, ignore_errors=True)

    refreshed = []
    if refresh_shims:
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        if home is not None:
            env["HOME"] = str(home)
        proc = subprocess.run(
            [sys.executable, "-B", str(root / "scripts" / "memoryctl.py"), "install-shims"],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        if proc.returncode == 0:
            try:
                refreshed = json.loads(proc.stdout).get("changed", [])
            except ValueError:
                refreshed = []

    return {
        "status": "upgraded",
        **summary,
        "sha256": actual_sha,
        "from_version": cur,
        "to_version": current_version(root),
        "refreshed_shims": refreshed,
    }
