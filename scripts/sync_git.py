"""Deterministic MEMORY.md index rebuilder and zero-dependency Git sync driver."""

from contextlib import contextmanager
from datetime import datetime
import fcntl
import getpass
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import sys
import subprocess
import time
from tempfile import NamedTemporaryFile
from urllib.parse import unquote, urlsplit
from urllib.request import getproxies, proxy_bypass

from library_paths import FRAMEWORK_ROOT, library_seed_files, library_state_dir


LAYER_HEADINGS = (
    ("L0", "constraints", "## L0 操作约束"),
    ("L1", "methods", "## L1 可复用方法"),
    ("L2", "preferences", "## L2 协作偏好"),
    ("L3", "projects", "## L3 项目快照"),
    ("L4", "archive", "## L4 事故归档"),
)
CONFLICT_MARKER_RE = re.compile(r"\.conflict-\d{8}-\d{6}(?:-\d+|-[a-f0-9]+)?\.md$", re.IGNORECASE)
REMOTE_DELETED_SENTINEL = "<!-- unified-memory:remote-deleted -->"
REMOTE_DELETED_TEXT = (
    REMOTE_DELETED_SENTINEL
    + "\n\n另一台设备已删除此条目，但本机在同一时间修改了它。本机版本已保留在原位。"
    "选择“采用远端版本”将删除本条目（旧内容会先放入本机回收站）；选择“保留本机”则继续保留并推送。\n"
)
DEFAULT_GITIGNORE = ".DS_Store\n.state/\n__pycache__/\n*.pyc\n*.conflict-*.md\n"
SYNC_ROOT_FILES = frozenset({"MEMORY.md", ".gitignore"})
SYNC_ENTRY_DIRS = frozenset(folder for _, folder, _ in LAYER_HEADINGS) | {"user-managed"}
SYNC_SEED_FILES = frozenset(library_seed_files().keys())
REMOTE_URL_WITH_USERINFO_RE = re.compile(
    r"(?P<prefix>(?:https?|ssh)://)(?P<userinfo>[^/\s@]+)@",
    re.IGNORECASE,
)
REMOTE_SCP_WITH_PASSWORD_RE = re.compile(
    r"(?P<prefix>^|[\s(])(?P<userinfo>[^/\s:@]+):(?P<password>[^/\s@]+)"
    r"@(?P<host>[^/\s:]+):",
    re.IGNORECASE,
)


def _redact_remote_urls(text):
    """Hide URL userinfo (including tokens) before returning/logging Git output."""
    value = REMOTE_URL_WITH_USERINFO_RE.sub(r"\g<prefix>***@", str(text or ""))
    return REMOTE_SCP_WITH_PASSWORD_RE.sub(r"\g<prefix>***@\g<host>:", value)


def redact_remote_url(remote_url):
    """Return a display-safe remote URL without userinfo credentials."""
    return _redact_remote_urls(remote_url)


def validate_remote_url(remote_url):
    """Reject HTTP(S)/SSH URLs that embed a password or access token in userinfo.

    A username-only HTTPS URL remains supported for providers that use it as a
    credential lookup hint (for example ``https://alice@gitee.com/...``), while
    ``https://alice:token@...`` is rejected before it can enter Git config,
    mounts.json, logs, or a subprocess argument.
    """
    raw = str(remote_url or "").strip()
    if not raw:
        return raw
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValueError("远端仓库地址格式无效。") from exc
    if parsed.scheme.lower() in {"http", "https", "ssh"} and parsed.netloc:
        userinfo = parsed.netloc.rsplit("@", 1)[0] if "@" in parsed.netloc else ""
        if userinfo and ":" in unquote(userinfo):
            raise ValueError("远端地址不能包含内嵌密码或访问令牌，请改用钥匙串、凭据助手或 SSH 密钥。")
    if REMOTE_SCP_WITH_PASSWORD_RE.match(raw):
        raise ValueError("远端地址不能包含内嵌密码或访问令牌，请改用钥匙串、凭据助手或 SSH 密钥。")
    return raw


def _normalize_git_relpath(rel_path):
    """Normalize a Git path without allowing absolute paths or traversal."""
    if not isinstance(rel_path, str) or not rel_path or "\\" in rel_path:
        return None
    path = PurePosixPath(rel_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _is_safe_sync_relpath(rel_path, *, include_conflict=False):
    """Return whether a relative path belongs to the Markdown sync allowlist."""
    normalized = _normalize_git_relpath(rel_path)
    if not normalized:
        return False
    if normalized in SYNC_ROOT_FILES or normalized in SYNC_SEED_FILES:
        return True
    path = PurePosixPath(normalized)
    if not path.parts or path.parts[0] not in SYNC_ENTRY_DIRS:
        return False
    if path.suffix.lower() != ".md":
        return False
    return include_conflict or not is_conflict_filename(path.name)


def _safe_library_path(root, rel_path, *, allow_missing=True):
    """Resolve a library-relative path while refusing symlink components."""
    normalized = _normalize_git_relpath(rel_path)
    if not normalized:
        return None
    root = Path(root)
    candidate = root.joinpath(*PurePosixPath(normalized).parts)
    current = root
    try:
        for part in PurePosixPath(normalized).parts:
            current = current / part
            if current.is_symlink():
                return None
        resolved = candidate.resolve(strict=False)
    except OSError:
        return None
    try:
        if not resolved.is_relative_to(root):
            return None
    except AttributeError:
        if root not in resolved.parents and resolved != root:
            return None
    if candidate.exists() and not candidate.is_file():
        return None
    if not allow_missing and not candidate.is_file():
        return None
    return candidate


def _safe_markdown_files(root, folder):
    """Yield regular top-level Markdown files, excluding symlink entries."""
    root = Path(root)
    directory = root / folder
    if directory.is_symlink() or not directory.is_dir():
        return
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name.casefold())
    except OSError:
        return
    for entry in entries:
        if entry.suffix.lower() != ".md" or entry.is_symlink() or not entry.is_file():
            continue
        safe = _safe_library_path(root, entry.relative_to(root).as_posix(), allow_missing=False)
        if safe is not None:
            yield safe


def _discover_sync_paths(root):
    """Discover existing regular files that are safe to stage for a memory library."""
    root = Path(root)
    found = set()
    for rel in SYNC_ROOT_FILES | SYNC_SEED_FILES:
        path = _safe_library_path(root, rel, allow_missing=False)
        if path is not None:
            found.add(rel)
    for folder in SYNC_ENTRY_DIRS:
        base = root / folder
        if base.is_symlink() or not base.is_dir():
            continue
        for current, directories, files in os.walk(base, followlinks=False):
            current_path = Path(current)
            directories[:] = sorted(
                name for name in directories
                if not (current_path / name).is_symlink()
            )
            for name in sorted(files):
                if not name.lower().endswith(".md") or is_conflict_filename(name):
                    continue
                path = current_path / name
                if path.is_symlink() or not path.is_file():
                    continue
                rel = path.relative_to(root).as_posix()
                if _is_safe_sync_relpath(rel) and _safe_library_path(root, rel, allow_missing=False):
                    found.add(rel)
    return found


def _atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".tmp-", delete=False
        ) as handle:
            staged = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        staged.replace(path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


@contextmanager
def _library_lock(root):
    lock_path = library_state_dir(root) / "library.lock"
    if lock_path.parent.is_symlink() or lock_path.is_symlink():
        raise GitSyncError("本地同步状态目录不能是符号链接。")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(lock_path.parent, 0o700)
    except OSError:
        pass
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _parse_frontmatter(text):
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    header, sep, body = normalized[4:].partition("\n---\n")
    if not sep:
        return {}, normalized
    fields = {}
    for line in header.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, colon, raw_val = line.partition(":")
        if not colon:
            continue
        key = key.strip()
        val = raw_val.strip()
        if val.startswith('"') and val.endswith('"'):
            try:
                val = json.loads(val)
            except json.JSONDecodeError:
                val = val[1:-1]
        fields[key] = val
    return fields, body.lstrip("\n")


def _extract_title(body, fallback):
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return fallback


def is_conflict_filename(name):
    return bool(CONFLICT_MARKER_RE.search(name))


def original_path_for_conflict(conflict_rel_path):
    """Derive the original entry relative path from a *.conflict-*.md relative path."""
    p = Path(conflict_rel_path)
    if not is_conflict_filename(p.name):
        raise ValueError(f"Not a conflict copy filename: {conflict_rel_path}")
    orig_name = CONFLICT_MARKER_RE.sub(".md", p.name)
    return p.with_name(orig_name).as_posix()


def resolve_conflict(
    root,
    conflict_rel_path,
    strategy,
    *,
    merged_content="",
    auto_commit_enabled=False,
    library_id="personal",
):
    """Resolve one *.conflict-*.md copy safely by keeping local, taking remote, or saving merged content."""
    import secrets
    import shutil

    root = Path(root).expanduser().resolve()
    conflict_path = _safe_library_path(root, conflict_rel_path, allow_missing=False)
    if conflict_path is None or not conflict_path.is_file():
        raise ValueError(f"Conflict copy not found: {conflict_rel_path}")
    if not is_conflict_filename(conflict_path.name):
        raise ValueError(f"Not a conflict copy: {conflict_rel_path}")

    orig_rel = original_path_for_conflict(conflict_path.relative_to(root).as_posix())
    orig_path = _safe_library_path(root, orig_rel, allow_missing=True)
    if orig_path is None:
        raise ValueError("Resolved original path escapes library root.")
    if strategy not in {"keep_local", "use_remote", "custom_merge"}:
        raise ValueError(f"Unknown conflict resolution strategy: {strategy}")

    trash_dir = library_state_dir(root) / "trash"

    def _backup(src, tag):
        if src.is_symlink() or not src.is_file():
            return
        if trash_dir.is_symlink():
            raise ValueError("本地回收站不能是符号链接。")
        trash_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(trash_dir, 0o700)
        except OSError:
            pass
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", tag)[:80] or "conflict"
        target = trash_dir / f"{stamp}-{safe}-{secrets.token_hex(2)}.md"
        shutil.copy2(src, target, follow_symlinks=False)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass

    with _library_lock(root):
        if strategy == "keep_local":
            _backup(conflict_path, f"conflict-discard-{conflict_path.stem}")
            conflict_path.unlink()
        elif strategy == "use_remote":
            remote_text = conflict_path.read_text(encoding="utf-8")
            _backup(orig_path, f"conflict-local-{orig_path.stem}")
            _backup(conflict_path, f"conflict-remote-{conflict_path.stem}")
            if remote_text.lstrip().startswith(REMOTE_DELETED_SENTINEL):
                orig_path.unlink(missing_ok=True)  # the other device deleted it; honour that choice
            else:
                _atomic_write(orig_path, remote_text)
            conflict_path.unlink()
            rebuild_index(root, lock=False)
        else:
            cleaned = merged_content.replace("\r\n", "\n").replace("\r", "\n").strip()
            if not cleaned:
                raise ValueError("合并后的正文不能为空。")
            _backup(orig_path, f"conflict-local-{orig_path.stem}")
            _backup(conflict_path, f"conflict-remote-{conflict_path.stem}")
            _atomic_write(orig_path, cleaned + "\n")
            conflict_path.unlink()
            rebuild_index(root, lock=False)

        if auto_commit_enabled and is_git_repo(root):
            auto_commit(root, f"sync({library_id}): resolve conflict for {orig_rel} ({strategy})")

    return {
        "resolved": conflict_rel_path,
        "original": orig_rel,
        "strategy": strategy,
        "remaining_conflicts": find_conflict_files(root),
    }


def find_conflict_files(memory_root):
    """Return sorted relative paths of unresolved *.conflict-*.md files."""
    root = Path(memory_root).expanduser().resolve()
    found = []
    # MEMORY.md conflicts live at the library root; entry conflicts live only in
    # the managed Markdown directories.  Do not follow symlinks while scanning.
    root_conflicts = [
        path for path in sorted(root.glob("*.conflict-*.md"))
        if not path.is_symlink() and path.is_file() and is_conflict_filename(path.name)
    ]
    found.extend(path.relative_to(root).as_posix() for path in root_conflicts)
    scan_dirs = [folder for _, folder, _ in LAYER_HEADINGS] + ["user-managed"]
    for folder in scan_dirs:
        for md in _safe_markdown_files(root, folder):
            if is_conflict_filename(md.name):
                found.append(md.relative_to(root).as_posix())
    return sorted(set(found))


def _is_index_conflict_path(rel_path):
    path = PurePosixPath(str(rel_path))
    return len(path.parts) == 1 and path.name.startswith("MEMORY.conflict-") and path.suffix == ".md"


def rebuild_index(memory_root, policy_template=None, lock=True):
    """Deterministically rebuild L0-L4 index sections in MEMORY.md from disk entries.

    Preserves the existing §0 policy preamble before `## L0 ` if present; falls back
    to the public library template otherwise.
    """
    root = Path(memory_root).expanduser().resolve()
    index_path = root / "MEMORY.md"
    template_path = Path(policy_template) if policy_template else (FRAMEWORK_ROOT / "templates" / "library.md")

    def _do_rebuild():
        preamble = ""
        if index_path.is_file():
            existing = index_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
            head, sep, _ = existing.partition("\n## L0 ")
            if sep and "<<<<<<" not in head and "======" not in head and ">>>>>>" not in head:
                preamble = head.rstrip()
        if not preamble:
            tpl_text = template_path.read_text(encoding="utf-8").replace("\r\n", "\n")
            head, _, _ = tpl_text.partition("\n## L0 ")
            preamble = head.rstrip()

        sections = [preamble]
        total = 0
        per_layer = {}
        for layer, folder, heading in LAYER_HEADINGS:
            lines = [heading]
            dir_path = root / folder
            count = 0
            for md in _safe_markdown_files(root, folder):
                if md.name == "README.md" or is_conflict_filename(md.name):
                    continue
                raw = md.read_text(encoding="utf-8", errors="replace")
                meta, body = _parse_frontmatter(raw)
                title = str(meta.get("title") or "").strip() or _extract_title(body, md.stem)
                desc = str(meta.get("description") or "").strip() or title
                title = " ".join(title.splitlines()).strip() or md.stem
                desc = " ".join(desc.splitlines()).strip() or title
                rel = f"{folder}/{md.name}"
                lines.append(f"- [{title}]({rel}) — {desc}")
                count += 1
                total += 1
            per_layer[layer] = count
            sections.append("\n".join(lines))

        new_text = "\n\n".join(sections) + "\n"
        changed = (not index_path.is_file()) or (index_path.read_text(encoding="utf-8", errors="replace") != new_text)
        if changed:
            _atomic_write(index_path, new_text)
        return {
            "memory_root": str(root),
            "indexed_topics": total,
            "per_layer": per_layer,
            "changed": changed,
        }

    if lock:
        with _library_lock(root):
            return _do_rebuild()
    return _do_rebuild()


class GitSyncError(RuntimeError):
    """Raised when a Git synchronization operation fails."""


def _git_bundle():
    """Find the desktop Git distribution, including when a source-backed manager is in use."""
    explicit = os.environ.get("UNIFIED_MEMORY_GIT_HOME")
    candidates = [Path(explicit)] if explicit else []
    if (FRAMEWORK_ROOT.name == "framework" and FRAMEWORK_ROOT.parent.name == "Resources"
            and FRAMEWORK_ROOT.parent.parent.parent.suffix == ".app"):
        candidates.append(FRAMEWORK_ROOT.parent / "git")
    if sys.platform == "darwin":
        for apps in (Path.home() / "Applications", Path("/Applications")):
            candidates.append(apps / "Unified Memory.app/Contents/Resources/git")
    return next((p for p in candidates if os.access(p / "bin/git", os.X_OK)), None)


def github_helper():
    bundle = _git_bundle()
    if bundle:
        helper = bundle / "libexec/git-core/git-credential-manager"
        if os.access(helper, os.X_OK):
            return str(helper)
    return shutil.which("git-credential-manager")


def github_browser_login(remote_url):
    try:
        validate_remote_url(remote_url)
    except ValueError:
        return False
    return bool(re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?",
                             remote_url, re.IGNORECASE) and github_helper())


NETWORK_COMMANDS = {"ls-remote", "fetch", "push", "clone"}
# Non-interactive network commands must not hang a sync job forever.
NETWORK_TIMEOUT = 120
KNOWN_HTTPS_PROVIDERS = {
    "github.com": "GitHub",
    "gitee.com": "Gitee",
    "gitlab.com": "GitLab",
    "bitbucket.org": "Bitbucket",
}
_HTTPS_URL_RE = re.compile(r"https://(?:(?P<user>[^@/\s]+)@)?(?P<host>[A-Za-z0-9.-]+(?::\d+)?)(?:[/?#]|$)",
                           re.IGNORECASE)
_SSH_URL_RE = re.compile(r"(?:ssh://|[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:)")
_MAC_KEYCHAIN_HELPERS = (
    "/Library/Developer/CommandLineTools/usr/libexec/git-core/git-credential-osxkeychain",
    "/Applications/Xcode.app/Contents/Developer/usr/libexec/git-core/git-credential-osxkeychain",
    "/opt/homebrew/opt/git/libexec/git-core/git-credential-osxkeychain",
    "/usr/local/opt/git/libexec/git-core/git-credential-osxkeychain",
)


def https_remote(remote_url):
    """Return host details for an HTTPS remote, or None for SSH and local paths."""
    validate_remote_url(remote_url)
    match = _HTTPS_URL_RE.match(remote_url or "")
    if not match:
        return None
    host = match["host"].lower()
    return {
        "host": host,
        "username": unquote(match["user"] or ""),
        "scope": f"https://{host}",
        "provider": KNOWN_HTTPS_PROVIDERS.get(host, ""),
    }


def auth_method(remote_url):
    """Describe how the desktop Git authenticates to a remote."""
    info = https_remote(remote_url)
    if info and info["host"] == "github.com" and github_helper():
        return "github-browser"
    if info:
        return "https-credential"
    if _SSH_URL_RE.match(remote_url or ""):
        return "ssh"
    return "local"


def https_credential_helper():
    """Find a credential store for non-GitHub HTTPS hosts.

    The bundled Git does not read the system gitconfig, so the macOS keychain helper
    configured by Apple's Git is invisible unless it is passed explicitly.
    Returns (helper_path, kind) or (None, "").
    """
    explicit = os.environ.get("UNIFIED_MEMORY_GIT_CREDENTIAL_HELPER")
    if explicit:
        return (explicit, "custom") if os.access(explicit, os.X_OK) else (None, "")
    if sys.platform == "darwin":
        for candidate in (shutil.which("git-credential-osxkeychain"), *_MAC_KEYCHAIN_HELPERS):
            if candidate and os.access(candidate, os.X_OK):
                return candidate, "osxkeychain"
        helper = github_helper()
        if helper:
            return helper, "gcm"
    return None, ""


def _credential_options(remote_url, browser_login, env):
    """Host-scoped credential settings: GCM browser login for GitHub, keychain for other HTTPS hosts."""
    validate_remote_url(remote_url)
    options = []
    helper = github_helper()
    if helper:
        options += ["-c", "credential.https://github.com.helper=!" + shlex.quote(helper),
                    "-c", "credential.https://github.com.gitHubAuthModes=browser",
                    "-c", "credential.https://github.com.interactive=" + ("true" if browser_login else "false")]
    info = https_remote(remote_url)
    if info and info["host"] != "github.com":
        store, kind = https_credential_helper()
        if store:
            options += ["-c", f"credential.{info['scope']}.helper=!" + shlex.quote(store)]
            if kind == "gcm":
                options += ["-c", f"credential.{info['scope']}.interactive=false"]
                env.setdefault("GCM_CREDENTIAL_STORE", "keychain")
    return options


def _network_url(root, args, env):
    """Resolve the URL a network command talks to, so credentials can be scoped to its host."""
    rest = list(args[1:])
    if "--" in rest:
        candidates = rest[rest.index("--") + 1:]
    else:
        candidates = [arg for arg in rest if not arg.startswith("-")]
    target = candidates[0] if candidates else "origin"
    if args[0] == "clone" or "://" in target or _SSH_URL_RE.match(target) or target.startswith(("/", ".", "~")):
        return target
    proc = subprocess.run(["git", "remote", "get-url", target], cwd=str(root), env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _github_proxy_options(root, env):
    """Supply macOS's static HTTPS proxy as a host-scoped Git default."""
    if sys.platform != "darwin" or any(key.lower() in {"http_proxy", "https_proxy", "all_proxy"}
                                       for key in env):
        return []
    proxy = getproxies().get("https")
    if not proxy or proxy_bypass("github.com"):
        return []
    # Even an explicitly empty http.proxy means the user chose their own route.
    url = "https://github.com/"
    configured = subprocess.run(["git", "config", "--get-urlmatch", "http.proxy", url],
                                cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if configured.returncode != 1:
        return []
    return ["-c", f"http.{url}.proxy={proxy}"]


def _git_process(root, args, *, browser_login=False, timeout=None, env_extra=None):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    bundle = _git_bundle()
    if bundle:
        env.update(PATH=str(bundle / "bin") + os.pathsep + env.get("PATH", os.defpath),
                   GIT_EXEC_PATH=str(bundle / "libexec/git-core"),
                   GIT_TEMPLATE_DIR=str(bundle / "share/git-core/templates"))
    if env_extra:
        env.update(env_extra)
    cmd = ["git", "-c", "core.quotepath=false"]
    if args[0] in NETWORK_COMMANDS:
        network_url = _network_url(root, args, env)
        try:
            validate_remote_url(network_url)
        except ValueError as exc:
            raise GitSyncError(str(exc)) from exc
        cmd += _github_proxy_options(root, env)
        cmd += _credential_options(network_url, browser_login, env)
        if timeout is None and not browser_login:
            timeout = NETWORK_TIMEOUT
    # A timed-out Git can leave its credential helper listening for an OAuth callback.
    with subprocess.Popen(cmd + list(args), cwd=str(root), env=env, text=True, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            if browser_login:
                raise GitSyncError("等待 GitHub 授权或网络连接超时，请返回同步页重试。") from exc
            raise GitSyncError(f"连接远端仓库超时（{int(timeout)} 秒），请检查网络、代理或仓库地址后重试。") from exc
    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)


class _InterruptedTlsHandshake(GitSyncError):
    """Git lost the transport while establishing TLS with the remote."""


def _detail_tail(detail, lines=3):
    plain = _redact_remote_urls(re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", detail))
    kept = [line.strip() for line in plain.splitlines()
            if line.strip() and not line.strip().startswith("Cloning into")]
    return " / ".join(kept[-lines:])[:400]


def _auth_hint(remote_url):
    try:
        info = https_remote(remote_url)
    except ValueError:
        info = None
    if info and info["host"] == "github.com":
        return "可在桌面版「共享与同步」点击「连接 GitHub」重新授权。"
    if info:
        name = info["provider"] or info["host"]
        user = "（当前地址指定了用户名）" if info["username"] else ""
        return (f"{name} 使用 HTTPS 用户名 + 访问令牌认证{user}。请在钥匙串中保存 {info['host']} 的凭据，"
                f"用户名需与平台账号一致；也可把地址写成 https://用户名@{info['host']}/... 后重试。")
    if _SSH_URL_RE.match(remote_url or ""):
        return "SSH 地址使用本机 SSH 密钥，请确认公钥已添加到代码平台，且 ssh-agent 已加载私钥。"
    return "请检查 Git 凭据。"


def _git_failure(action, detail, remote_url=""):
    lowered = detail.lower()
    try:
        info = https_remote(remote_url)
    except ValueError:
        info = None
    is_github = not remote_url or (info is not None and info["host"] == "github.com")
    raw = _detail_tail(detail)
    if "ssl_connect" in lowered and "ssl_error_syscall" in lowered:
        return _InterruptedTlsHandshake("与远端仓库建立 HTTPS 安全连接时中断。请检查网络或代理后重试。")
    if any(word in lowered for word in ("user_cancel", "access_denied", "authentication was canceled", "user canceled", "user cancelled")):
        return GitSyncError(("GitHub " if is_github else "") + "授权已取消，可以重新点击连接。")
    auth_failed = any(word in lowered for word in (
        "terminal prompts disabled", "authentication failed", "permission denied (publickey",
        "incorrect username or password", "invalid username or password", "could not read username",
        "could not read password", "http basic: access denied"))
    if auth_failed:
        return GitSyncError(f"本机 Git 未能通过远端身份验证。{_auth_hint(remote_url)}（Git：{raw}）")
    if "repository not found" in lowered or re.search(r"(?:http |error: )403\b", lowered) or "write access to repository not granted" in lowered:
        if is_github:
            return GitSyncError("仓库地址有误或当前账号权限不足。请在 GitHub 确认仓库归属、接受协作邀请，并核对读写权限。")
        # Gitee and several self-hosted platforms answer "not found" for wrong credentials too.
        return GitSyncError(f"仓库地址有误、账号权限不足，或凭据不正确。{_auth_hint(remote_url)}（Git：{raw}）")
    if "refusing to merge unrelated histories" in lowered:
        return GitSyncError("本地库与远端已有独立的提交历史。要加入已有团队仓库，请在「共享与同步」挂载共享库，个人记忆会保留在原目录。")
    return GitSyncError(f"{action} failed: {_redact_remote_urls(detail)}")


def _run_git(root, *args, check=True, env_extra=None, timeout=None, browser_login=False):
    proc = _git_process(root, args, env_extra=env_extra, timeout=timeout, browser_login=browser_login)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        remote_url = _network_url(root, args, dict(os.environ)) if args[0] in NETWORK_COMMANDS else ""
        raise _git_failure(f"git {args[0]}", detail, remote_url)
    return proc


def is_git_repo(root):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        return False
    # Require the library itself to be the work-tree top level: a library living inside some other
    # repository (e.g. a dotfiles repo) must not inherit that repo's remote or history.
    proc = _run_git(root, "rev-parse", "--show-toplevel", check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    try:
        return Path(proc.stdout.strip()).resolve() == root
    except OSError:
        return False


def _ensure_git_identity(root):
    name = _run_git(root, "config", "user.name", check=False).stdout.strip()
    email = _run_git(root, "config", "user.email", check=False).stdout.strip()
    if not name:
        _run_git(root, "config", "user.name", getpass.getuser() or "unified-memory")
    if not email:
        user = getpass.getuser() or "unified-memory"
        _run_git(root, "config", "user.email", f"{user}@local")


def _ensure_gitignore(root):
    gi = Path(root) / ".gitignore"
    if gi.is_symlink():
        raise GitSyncError("库内 .gitignore 不能是符号链接。")
    if not gi.is_file():
        _atomic_write(gi, DEFAULT_GITIGNORE)
        return
    existing = gi.read_text(encoding="utf-8", errors="replace")
    missing = [line for line in DEFAULT_GITIGNORE.splitlines() if line not in existing.splitlines()]
    if missing:
        prefix = existing.rstrip("\n")
        updated = (prefix + "\n" if prefix else "") + "\n".join(missing) + "\n"
        _atomic_write(gi, updated)


def current_branch(root, fallback="main"):
    proc = _run_git(root, "symbolic-ref", "--short", "HEAD", check=False)
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    proc = _run_git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if proc.returncode == 0 and proc.stdout.strip() and proc.stdout.strip() != "HEAD":
        return proc.stdout.strip()
    return fallback


def _ensure_repo_unlocked(root, remote_url="", remote="origin", branch="main", *, writable=True):
    """Ensure root is a Git repo while the caller already holds the library lock."""
    try:
        validate_remote_url(remote_url)
    except ValueError as exc:
        raise GitSyncError(str(exc)) from exc
    root.mkdir(parents=True, exist_ok=True)
    initialized = False
    if not is_git_repo(root):
        _run_git(root, "init", "-b", branch or "main")
        initialized = True
    _ensure_git_identity(root)
    if writable:
        _ensure_gitignore(root)

    if remote_url:
        existing = _run_git(root, "remote", "get-url", remote, check=False)
        if existing.returncode == 0:
            if existing.stdout.strip() != remote_url:
                _run_git(root, "remote", "set-url", remote, remote_url)
        else:
            _run_git(root, "remote", "add", remote, remote_url)

    return {
        "path": str(root),
        "initialized": initialized,
        "branch": current_branch(root, fallback=branch or "main"),
        "remote": remote,
        # validate_remote_url() has already rejected embedded passwords/tokens. Keep
        # username-only hints (and SSH URLs) intact because callers persist this value
        # in mounts.json and Git needs the original remote to work.
        "remote_url": _run_git(root, "remote", "get-url", remote, check=False).stdout.strip(),
    }


def ensure_repo(root, remote_url="", remote="origin", branch="main", *, writable=True):
    """Ensure root is a Git repo with sane .gitignore, local identity, and optional remote.

    Repository metadata and the managed ``.gitignore`` are shared mutable state just like
    memory files.  Serialize initialization/reconfiguration with pulls, pushes, and writes;
    the unlocked helper is used only by callers that already hold ``_library_lock``.
    """
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    with _library_lock(root):
        return _ensure_repo_unlocked(
            root,
            remote_url=remote_url,
            remote=remote,
            branch=branch,
            writable=writable,
        )


def clone_repo(remote_url, dest_path, branch=""):
    """Clone a remote Git repository into dest_path."""
    try:
        validate_remote_url(remote_url)
    except ValueError as exc:
        raise GitSyncError(str(exc)) from exc
    dest = Path(dest_path).expanduser().resolve()
    if dest.is_symlink():
        raise GitSyncError(f"Target directory cannot be a symlink: {dest}")
    if dest.exists() and any(dest.iterdir()):
        raise GitSyncError(f"Target directory already exists and is not empty: {dest}")
    created = not dest.exists()
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["clone"]
    if branch:
        args.extend(["--branch", branch])
    args.extend(["--", remote_url, str(dest)])
    interactive = github_browser_login(remote_url)
    try:
        proc = _git_process(dest.parent, args, browser_login=interactive, timeout=300 if interactive else None)
    except GitSyncError:
        if created and dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        raise
    if proc.returncode != 0:
        if created and dest.exists():
            # Only remove what this clone created; a pre-existing empty directory stays.
            shutil.rmtree(dest, ignore_errors=True)
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise _git_failure("git clone", detail, remote_url)
    _ensure_git_identity(dest)
    return {
        "path": str(dest),
        "branch": current_branch(dest, fallback=branch or "main"),
        # The URL was validated before clone; returning it is safe and preserves
        # username-only HTTPS hints used by credential helpers.
        "remote_url": remote_url,
    }


def test_remote_access(root, remote="origin", *, browser_login=False):
    """Read advertised refs without fetching, committing, or pushing, including for empty remotes."""
    timeout = 180 if browser_login else 15
    deadline = time.monotonic() + timeout
    try:
        _run_git(root, "ls-remote", "--", remote, browser_login=browser_login, timeout=timeout)
    except _InterruptedTlsHandshake:
        # Only replay this read-only probe. Keep both attempts within the original time budget.
        remaining = deadline - time.monotonic()
        if remaining < 1:
            raise
        time.sleep(0.5)
        _run_git(root, "ls-remote", "--", remote, browser_login=browser_login, timeout=remaining - 0.5)


def _has_commits(root):
    return _run_git(root, "rev-parse", "--verify", "HEAD", check=False).returncode == 0


def _porcelain_changes(root):
    proc = _run_git(root, "status", "--porcelain", check=False)
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _git_path_list(root, *args):
    """Read a NUL-delimited Git path list without interpreting path contents."""
    proc = _run_git(root, *args, check=False)
    if proc.returncode != 0:
        return []
    return [item for item in proc.stdout.split("\0") if item]


def _merge_in_progress(root):
    """True while a merge is unfinished (MERGE_HEAD present or unmerged index entries)."""
    if _run_git(root, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
        return True
    return bool(_git_path_list(root, "diff", "--name-only", "--diff-filter=U", "-z"))


MERGE_IN_PROGRESS_MESSAGE = (
    "记忆库有未完成的 Git 合并，已停止自动提交和同步。"
    "请在库目录运行 git merge --abort，或解决冲突后手动提交。"
)


def _stage_allowed_paths(root):
    """Stage only memory Markdown and framework seed files.

    Git's ``add -A`` is intentionally avoided: an accidental credential file,
    attachment, or unrelated artifact inside the library must never be swept
    into a memory commit or pushed to a shared remote.  Existing disallowed
    staged paths are explicitly reset (the worktree is left untouched).
    """
    root = Path(root).expanduser().resolve()
    tracked = _git_path_list(root, "ls-files", "-z")
    candidates = _discover_sync_paths(root)
    candidates.update(
        rel for rel in tracked
        if _is_safe_sync_relpath(rel) and _safe_library_path(root, rel, allow_missing=True) is not None
    )

    staged = _git_path_list(root, "diff", "--cached", "--name-only", "-z")
    disallowed = [rel for rel in staged if rel not in candidates]
    if disallowed:
        reset = _run_git(root, "reset", "--quiet", "--", *sorted(disallowed), check=False)
        if reset.returncode != 0:
            raise GitSyncError("无法撤销非记忆文件的暂存状态，已停止自动提交。")

    if candidates:
        _run_git(root, "add", "--", *sorted(candidates))

    # A symlink may have appeared between discovery and `git add`; verify the
    # final index before allowing commit/push to continue.
    staged_after = _git_path_list(root, "diff", "--cached", "--name-only", "-z")
    unsafe_after = [
        rel for rel in staged_after
        if not _is_safe_sync_relpath(rel)
        or _safe_library_path(root, rel, allow_missing=True) is None
    ]
    if unsafe_after:
        _run_git(root, "reset", "--quiet", "--", *sorted(unsafe_after), check=False)
        raise GitSyncError("暂存区包含不受支持或符号链接路径，已阻止自动提交。")
    return staged_after


def git_status(root, remote="origin", branch=""):
    """Inspect local Git sync state without hitting the network."""
    root = Path(root).expanduser().resolve()
    conflicts = find_conflict_files(root)
    if not is_git_repo(root):
        return {
            "is_repo": False,
            "branch": "",
            "remote": remote,
            "remote_url": "",
            "has_commits": False,
            "dirty": False,
            "dirty_count": 0,
            "ahead": 0,
            "behind": 0,
            "conflicts": conflicts,
            "in_merge": False,
        }

    br = branch or current_branch(root)
    remote_url = _run_git(root, "remote", "get-url", remote, check=False).stdout.strip()
    has_head = _has_commits(root)
    changes = _porcelain_changes(root)
    ahead = 0
    behind = 0
    if has_head and remote_url:
        ref = f"{remote}/{br}"
        if _run_git(root, "rev-parse", "--verify", ref, check=False).returncode == 0:
            lr = _run_git(root, "rev-list", "--left-right", "--count", f"HEAD...{ref}", check=False)
            if lr.returncode == 0:
                parts = lr.stdout.strip().split()
                if len(parts) == 2:
                    ahead, behind = int(parts[0]), int(parts[1])

    git_dir = Path(_run_git(root, "rev-parse", "--git-dir").stdout.strip())
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    in_merge = (git_dir / "MERGE_HEAD").exists()

    return {
        "is_repo": True,
        "branch": br,
        "remote": remote,
        "remote_url": _redact_remote_urls(remote_url),
        "has_commits": has_head,
        "dirty": bool(changes),
        "dirty_count": len(changes),
        "ahead": ahead,
        "behind": behind,
        "conflicts": conflicts,
        "in_merge": in_merge,
    }


def auto_commit(root, message):
    """Commit local changes if root is a Git repo and has staged/unstaged changes."""
    root = Path(root).expanduser().resolve()
    if not is_git_repo(root):
        return {"committed": False, "reason": "not_a_repo"}
    if _merge_in_progress(root):
        # Committing now would conclude the merge with conflict markers staged as resolved.
        return {"committed": False, "reason": "merge_in_progress", "error": MERGE_IN_PROGRESS_MESSAGE}
    changes = _porcelain_changes(root)
    if not changes:
        return {"committed": False, "reason": "clean"}
    _ensure_git_identity(root)
    _ensure_gitignore(root)
    staged_paths = _stage_allowed_paths(root)
    if not staged_paths:
        return {"committed": False, "reason": "nothing_allowed", "files": 0}
    _run_git(root, "commit", "-m", message)
    rev = _run_git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    return {"committed": True, "revision": rev, "files": len(staged_paths)}


def _extract_theirs_conflict_copy(root, rel_path, stamp, text=None):
    """Save remote version (:3:<path>) beside the local file without losing either side.

    `text` overrides the content (used for the remote-deleted tombstone).
    """
    normalized = _normalize_git_relpath(rel_path)
    if not normalized or not _is_safe_sync_relpath(normalized):
        raise GitSyncError(f"远端冲突路径不在记忆 Markdown 允许列表中：{_redact_remote_urls(rel_path)}")
    if text is None:
        show = _run_git(root, "show", f":3:{normalized}", check=False)
        if show.returncode != 0:
            return None
        text = show.stdout
    orig = _safe_library_path(root, normalized, allow_missing=True)
    if orig is None:
        raise GitSyncError(f"冲突路径包含符号链接或越界：{_redact_remote_urls(normalized)}")
    conflict_name = f"{orig.stem}.conflict-{stamp}{orig.suffix}"
    conflict_path = orig.with_name(conflict_name)
    if conflict_path.exists():
        idx = 1
        while conflict_path.exists():
            conflict_path = orig.with_name(f"{orig.stem}.conflict-{stamp}-{idx}{orig.suffix}")
            idx += 1
    if conflict_path.is_symlink():
        raise GitSyncError("冲突副本目标不能是符号链接。")
    _atomic_write(conflict_path, text)
    return conflict_path.relative_to(root).as_posix()


def _unmodified_seeds(root):
    """Identify a new working tree made entirely of the files shipped by init."""
    if _has_commits(root) or _run_git(root, "ls-files", "--cached", "-z").stdout:
        return None
    names = _run_git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0")
    sources = library_seed_files()
    for name in filter(None, names):
        path = root / name
        if name != ".gitignore" and name not in sources:
            return None
        expected = DEFAULT_GITIGNORE.encode() if name == ".gitignore" else sources[name].read_bytes()
        if (path.is_symlink() or not path.is_file() or path.stat().st_size != len(expected)
                or path.read_bytes() != expected):
            return None
    return [name for name in names if name]


def _checkout_initial_branch(root, branch, remote_ref, seeds, mode):
    # Let Git check for local modifications and collisions while advancing the seed files
    # to the remote tree. In a personal library, preserve templates absent from the remote.
    remote_files = _run_git(root, "ls-tree", "-r", "--name-only", "-z", remote_ref).stdout.split("\0")
    covered = [name for name in seeds if mode == "ro" or name in remote_files]
    if covered:
        _run_git(root, "add", "--", *covered)
        base = _run_git(root, "write-tree").stdout.strip()
        try:
            _run_git(root, "read-tree", "-m", "-u", base, remote_ref)
        except GitSyncError:
            # Only originally untracked bootstrap files were staged above.
            _run_git(root, "rm", "--cached", "--quiet", "--", *covered, check=False)
            raise
    _run_git(root, "checkout", "--no-overwrite-ignore", "-B", branch, remote_ref)


def pull(root, remote="origin", branch="", mode="rw"):
    """Fetch and merge remote updates, auto-healing MEMORY.md conflicts and preserving entry conflicts."""
    root = Path(root).expanduser().resolve()
    if not is_git_repo(root):
        raise GitSyncError(f"Not a Git repository: {root}")

    with _library_lock(root):
        br = branch or current_branch(root)
        remote_url = _run_git(root, "remote", "get-url", remote, check=False).stdout.strip()
        if not remote_url:
            raise GitSyncError(f"Git remote '{remote}' is not configured for {root}")
        try:
            validate_remote_url(remote_url)
        except ValueError as exc:
            raise GitSyncError(str(exc)) from exc
        if _merge_in_progress(root):
            raise GitSyncError(MERGE_IN_PROGRESS_MESSAGE)

        changes = _porcelain_changes(root)
        seeds = _unmodified_seeds(root) if changes else []
        if changes and seeds is None:
            if mode == "ro":
                raise GitSyncError(
                    f"Read-only library '{root}' has local uncommitted changes; revert them before pulling."
                )
            auto_commit(root, "sync: local snapshot before pull")

        _run_git(root, "fetch", remote)
        remote_ref = f"{remote}/{br}"
        if _run_git(root, "rev-parse", "--verify", remote_ref, check=False).returncode != 0:
            return {
                "pulled": False,
                "status": "remote_branch_missing",
                "branch": br,
                "remote": remote,
                "conflicts": [],
            }

        if not _has_commits(root):
            _checkout_initial_branch(root, br, remote_ref, seeds or [], mode)
            return {
                "pulled": True,
                "status": "checked_out_remote",
                "branch": br,
                "remote": remote,
                "conflicts": [],
            }

        before_rev = _run_git(root, "rev-parse", "HEAD").stdout.strip()
        if mode == "ro":
            ff = _run_git(root, "merge", "--ff-only", remote_ref, check=False)
            if ff.returncode != 0:
                raise GitSyncError(
                    f"Read-only library diverged from {remote_ref}; clean local commits before pulling."
                )
            after_rev = _run_git(root, "rev-parse", "HEAD").stdout.strip()
            return {
                "pulled": after_rev != before_rev,
                "status": "fast_forward" if after_rev != before_rev else "up_to_date",
                "branch": br,
                "remote": remote,
                "conflicts": find_conflict_files(root),
            }

        merge = _run_git(root, "merge", "--no-edit", remote_ref, check=False)
        if merge.returncode == 0:
            after_rev = _run_git(root, "rev-parse", "HEAD").stdout.strip()
            return {
                "pulled": after_rev != before_rev,
                "status": "merged" if after_rev != before_rev else "up_to_date",
                "branch": br,
                "remote": remote,
                "conflicts": find_conflict_files(root),
            }

        # Resolve conflicts safely without losing either side or leaving the repo stuck in MERGING
        unmerged = [
            line.strip()
            for line in _run_git(root, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
            if line.strip()
        ]
        if not unmerged:
            detail = (merge.stderr or merge.stdout or "merge failed").strip()
            raise _git_failure(f"git merge {remote_ref}", detail)

        unsafe_unmerged = [rel for rel in unmerged if not _is_safe_sync_relpath(rel)]
        if unsafe_unmerged:
            _run_git(root, "merge", "--abort", check=False)
            raise GitSyncError(
                "远端合并包含不在记忆 Markdown 允许列表中的文件，已中止合并："
                + ", ".join(_redact_remote_urls(rel) for rel in unsafe_unmerged[:5])
            )
        unsafe_paths = [
            rel for rel in unmerged
            if _safe_library_path(root, rel, allow_missing=True) is None
        ]
        if unsafe_paths:
            _run_git(root, "merge", "--abort", check=False)
            raise GitSyncError(
                "合并路径包含符号链接或越界路径，已中止合并："
                + ", ".join(_redact_remote_urls(rel) for rel in unsafe_paths[:5])
            )

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        saved_conflicts = []
        restored_remote = []
        index_conflicted = False

        try:
            for rel in unmerged:
                if rel == "MEMORY.md":
                    index_conflicted = True
                    copy_rel = _extract_theirs_conflict_copy(root, rel, stamp)
                    if not copy_rel:
                        raise GitSyncError("无法保存远端 MEMORY.md 冲突副本，已停止自动合并。")
                    saved_conflicts.append({"original": rel, "remote_copy": copy_rel, "kind": "index"})
                    _run_git(root, "checkout", "--ours", "MEMORY.md")
                    continue
                # Keep local file at original path and preserve remote copy beside it
                has_ours = _run_git(root, "show", f":2:{rel}", check=False).returncode == 0
                has_theirs = _run_git(root, "show", f":3:{rel}", check=False).returncode == 0
                if has_ours and not has_theirs:
                    # Remote deleted what we edited: keep our copy and ask, instead of silently resurrecting it.
                    copy_rel = _extract_theirs_conflict_copy(root, rel, stamp, text=REMOTE_DELETED_TEXT)
                    saved_conflicts.append({"original": rel, "remote_copy": copy_rel, "kind": "remote_deleted"})
                    _run_git(root, "checkout", "--ours", rel)
                elif has_theirs and not has_ours:
                    # We deleted what the remote edited: never drop their edit; restore it and report.
                    _run_git(root, "checkout", "--theirs", rel)
                    restored_remote.append(rel)
                else:
                    copy_rel = _extract_theirs_conflict_copy(root, rel, stamp)
                    if copy_rel:
                        saved_conflicts.append({"original": rel, "remote_copy": copy_rel, "kind": "both_modified"})
                    _run_git(root, "checkout", "--ours", rel)

            # Deterministically rebuild MEMORY.md so additions from both sides are indexed cleanly
            if index_conflicted or saved_conflicts or restored_remote:
                rebuild_index(root, lock=False)

            _ensure_git_identity(root)
            _ensure_gitignore(root)
            _stage_allowed_paths(root)
            still_unmerged = _git_path_list(root, "diff", "--name-only", "--diff-filter=U", "-z")
            if still_unmerged:
                raise GitSyncError(
                    "合并后仍有未解决冲突，已阻止自动提交：" + ", ".join(still_unmerged[:5])
                )
            msg = (
                "sync: merge remote (auto-rebuilt MEMORY.md)"
                if not saved_conflicts
                else f"sync: merge remote ({len(saved_conflicts)} conflict copies preserved)"
            )
            _run_git(root, "commit", "--no-edit", "-m", msg)
        except BaseException:
            # Never leave the library MERGING: a later auto-commit would conclude the merge
            # with conflict markers staged as resolved and push them.
            _run_git(root, "merge", "--abort", check=False)
            for conflict in saved_conflicts:
                copy = _safe_library_path(root, conflict.get("remote_copy") or "", allow_missing=True)
                if copy is not None and copy.is_file() and is_conflict_filename(copy.name):
                    copy.unlink()
            raise
        blocking_conflicts = [
            conflict for conflict in saved_conflicts
            if conflict.get("kind") != "index"
        ]
        preserved_index_conflicts = [
            conflict for conflict in saved_conflicts
            if conflict.get("kind") == "index"
        ]
        return {
            "pulled": True,
            "status": "merged_with_conflicts" if saved_conflicts else "merged_index_rebuilt",
            "branch": br,
            "remote": remote,
            "index_rebuilt": index_conflicted,
            "conflicts": blocking_conflicts,
            "preserved_conflicts": preserved_index_conflicts,
            "restored_remote": restored_remote,
        }


def push(root, remote="origin", branch="", mode="rw", *, browser_login=False):
    """Commit pending changes and push to the configured remote branch."""
    if mode == "ro":
        raise GitSyncError("Cannot push a read-only mounted library.")
    root = Path(root).expanduser().resolve()
    if not is_git_repo(root):
        raise GitSyncError(f"Not a Git repository: {root}")

    with _library_lock(root):
        br = branch or current_branch(root)
        remote_url = _run_git(root, "remote", "get-url", remote, check=False).stdout.strip()
        if not remote_url:
            raise GitSyncError(f"Git remote '{remote}' is not configured for {root}")
        try:
            validate_remote_url(remote_url)
        except ValueError as exc:
            raise GitSyncError(str(exc)) from exc
        if _merge_in_progress(root):
            raise GitSyncError(MERGE_IN_PROGRESS_MESSAGE)

        existing_conflicts = find_conflict_files(root)
        blocking_conflicts = [rel for rel in existing_conflicts if not _is_index_conflict_path(rel)]
        if blocking_conflicts:
            raise GitSyncError(
                f"Unresolved conflict copies exist ({', '.join(blocking_conflicts)}); merge and remove them before pushing."
            )

        commit_res = auto_commit(root, "sync: commit local memory updates")
        if not _has_commits(root):
            return {"pushed": False, "status": "no_commits", "branch": br, "remote": remote}

        interactive = browser_login and github_browser_login(remote_url)
        _run_git(root, "push", "-u", remote, f"HEAD:{br}", browser_login=interactive,
                 timeout=300 if interactive else None)
        return {
            "pushed": True,
            "status": "pushed",
            "branch": br,
            "remote": remote,
            "auto_commit": commit_res,
        }


def _blocking_conflicts(conflicts):
    """Return conflicts that must stop a push.

    pull() reports conflict copies either as dicts (conflict branch, with a
    ``kind``) or as relative path strings (clean merge / fast-forward, from
    find_conflict_files). Root ``MEMORY.conflict-*.md`` copies never block:
    the index is rebuilt from entries, matching push().
    """
    blocking = []
    for conflict in conflicts or []:
        if isinstance(conflict, dict):
            if conflict.get("kind") != "index":
                blocking.append(conflict)
        elif not _is_index_conflict_path(Path(str(conflict))):
            blocking.append(conflict)
    return blocking


def sync(root, remote="origin", branch="", mode="rw", pull_only=False, push_only=False, browser_login=True):
    """Run bidirectional (or one-way) Git synchronization for a library."""
    result = {"path": str(Path(root).expanduser().resolve()), "mode": mode, "pull": None, "push": None}
    if browser_login:
        remote_url = _run_git(root, "remote", "get-url", remote, check=False).stdout.strip()
        try:
            validate_remote_url(remote_url)
        except ValueError as exc:
            raise GitSyncError(str(exc)) from exc
        if github_browser_login(remote_url):
            # Finish the interactive step before taking the library write lock or making a snapshot.
            test_remote_access(root, remote, browser_login=True)
            if _run_git(root, "remote", "get-url", remote).stdout.strip() != remote_url:
                raise GitSyncError("授权期间仓库绑定已变更，请对新的仓库重新同步。")
    if not push_only:
        pull_res = pull(root, remote=remote, branch=branch, mode=mode)
        result["pull"] = pull_res
        blocking_conflicts = _blocking_conflicts(pull_res.get("conflicts"))
        if blocking_conflicts:
            result["status"] = "needs_manual_merge"
            return result
    if mode == "rw" and not pull_only:
        push_res = push(root, remote=remote, branch=branch, mode=mode, browser_login=browser_login)
        result["push"] = push_res
    result["status"] = "ok"
    return result
