"""Deterministic MEMORY.md index rebuilder and zero-dependency Git sync driver."""

from contextlib import contextmanager
from datetime import datetime
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import sys
import subprocess
from tempfile import NamedTemporaryFile

from library_paths import FRAMEWORK_ROOT, library_state_dir


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


def _atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".tmp-", delete=False
        ) as handle:
            staged = Path(handle.name)
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
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


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
    conflict_path = (root / conflict_rel_path).resolve()
    if not conflict_path.is_relative_to(root) or not conflict_path.is_file():
        raise ValueError(f"Conflict copy not found: {conflict_rel_path}")
    if not is_conflict_filename(conflict_path.name):
        raise ValueError(f"Not a conflict copy: {conflict_rel_path}")

    orig_rel = original_path_for_conflict(conflict_path.relative_to(root).as_posix())
    orig_path = (root / orig_rel).resolve()
    if not orig_path.is_relative_to(root):
        raise ValueError("Resolved original path escapes library root.")
    if strategy not in {"keep_local", "use_remote", "custom_merge"}:
        raise ValueError(f"Unknown conflict resolution strategy: {strategy}")

    trash_dir = library_state_dir(root) / "trash"

    def _backup(src, tag):
        if not src.is_file():
            return
        trash_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", tag)[:80] or "conflict"
        shutil.copy2(src, trash_dir / f"{stamp}-{safe}-{secrets.token_hex(2)}.md")

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
    scan_dirs = [folder for _, folder, _ in LAYER_HEADINGS] + ["user-managed"]
    for folder in scan_dirs:
        d = root / folder
        if not d.is_dir():
            continue
        for md in sorted(d.glob("*.md")):
            if is_conflict_filename(md.name):
                found.append(md.relative_to(root).as_posix())
    return found


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
            if dir_path.is_dir():
                for md in sorted(dir_path.glob("*.md"), key=lambda p: p.name.casefold()):
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
    return bool(re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?",
                             remote_url, re.IGNORECASE) and github_helper())


def _git_process(root, args, *, browser_login=False, timeout=None, env_extra=None):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    bundle = _git_bundle()
    if bundle:
        env.update(PATH=str(bundle / "bin") + os.pathsep + env.get("PATH", os.defpath),
                   GIT_EXEC_PATH=str(bundle / "libexec/git-core"),
                   GIT_TEMPLATE_DIR=str(bundle / "share/git-core/templates"))
    cmd = ["git", "-c", "core.quotepath=false"]
    if args[0] in {"ls-remote", "fetch", "push", "clone"}:
        helper = github_helper()
        if helper:
            cmd += ["-c", "credential.https://github.com.helper=!" + shlex.quote(helper),
                    "-c", "credential.https://github.com.gitHubAuthModes=browser",
                    "-c", "credential.https://github.com.interactive=" + ("true" if browser_login else "false")]
    if env_extra:
        env.update(env_extra)
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
            raise GitSyncError("等待 GitHub 授权或网络连接超时，请返回同步页重试。") from exc
    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)


def _git_failure(action, detail):
    lowered = detail.lower()
    if "repository not found" in lowered or re.search(r"(?:http |error: )403\b", lowered) or "write access to repository not granted" in lowered:
        return GitSyncError("仓库地址有误或当前账号权限不足。请在 GitHub 确认仓库归属、接受协作邀请，并核对读写权限。")
    if any(word in lowered for word in ("user_cancel", "access_denied", "authentication was canceled", "user canceled", "user cancelled")):
        return GitSyncError("GitHub 授权已取消，可以重新点击连接。")
    if ("terminal prompts disabled" in lowered or "authentication failed" in lowered
            or "permission denied (publickey" in lowered):
        return GitSyncError(
            "本机 Git 未能通过远端身份验证。"
            "使用 GitHub HTTPS 地址时，可在桌面版「共享与同步」点击「连接 GitHub」重新授权。其他地址请检查 Git 凭据。"
        )
    if "refusing to merge unrelated histories" in lowered:
        return GitSyncError("本地库与远端已有独立的提交历史。要加入已有团队仓库，请在「共享与同步」挂载共享库，个人记忆会保留在原目录。")
    return GitSyncError(f"{action} failed: {detail}")


def _run_git(root, *args, check=True, env_extra=None, timeout=None, browser_login=False):
    proc = _git_process(root, args, env_extra=env_extra, timeout=timeout, browser_login=browser_login)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise _git_failure(f"git {args[0]}", detail)
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


def ensure_repo(root, remote_url="", remote="origin", branch="main"):
    """Ensure root is a Git repo with sane .gitignore, local identity, and optional remote."""
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    initialized = False
    if not is_git_repo(root):
        _run_git(root, "init", "-b", branch or "main")
        initialized = True
    _ensure_git_identity(root)
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
        "remote_url": _run_git(root, "remote", "get-url", remote, check=False).stdout.strip(),
    }


def clone_repo(remote_url, dest_path, branch=""):
    """Clone a remote Git repository into dest_path."""
    dest = Path(dest_path).expanduser().resolve()
    if dest.exists() and any(dest.iterdir()):
        raise GitSyncError(f"Target directory already exists and is not empty: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["clone"]
    if branch:
        args.extend(["--branch", branch])
    args.extend(["--", remote_url, str(dest)])
    interactive = github_browser_login(remote_url)
    proc = _git_process(dest.parent, args, browser_login=interactive, timeout=300 if interactive else None)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise _git_failure("git clone", detail)
    _ensure_git_identity(dest)
    return {
        "path": str(dest),
        "branch": current_branch(dest, fallback=branch or "main"),
        "remote_url": remote_url,
    }


def test_remote_access(root, remote="origin", *, browser_login=False):
    """Read advertised refs without fetching, committing, or pushing, including for empty remotes."""
    _run_git(root, "ls-remote", "--", remote, browser_login=browser_login, timeout=180 if browser_login else 15)


def _has_commits(root):
    return _run_git(root, "rev-parse", "--verify", "HEAD", check=False).returncode == 0


def _porcelain_changes(root):
    proc = _run_git(root, "status", "--porcelain", check=False)
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


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
        "remote_url": remote_url,
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
    changes = _porcelain_changes(root)
    if not changes:
        return {"committed": False, "reason": "clean"}
    _ensure_git_identity(root)
    _ensure_gitignore(root)
    _run_git(root, "add", "-A")
    # Re-check staged diff (e.g., if only ignored files existed)
    if _has_commits(root):
        staged = _run_git(root, "diff", "--cached", "--quiet", check=False)
        if staged.returncode == 0:
            return {"committed": False, "reason": "clean"}
    _run_git(root, "commit", "-m", message)
    rev = _run_git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    return {"committed": True, "revision": rev, "files": len(changes)}


def _extract_theirs_conflict_copy(root, rel_path, stamp, text=None):
    """Save remote version (:3:<path>) beside the local file without losing either side.

    `text` overrides the content (used for the remote-deleted tombstone).
    """
    if text is None:
        show = _run_git(root, "show", f":3:{rel_path}", check=False)
        if show.returncode != 0:
            return None
        text = show.stdout
    orig = Path(root) / rel_path
    conflict_name = f"{orig.stem}.conflict-{stamp}{orig.suffix}"
    conflict_path = orig.with_name(conflict_name)
    if conflict_path.exists():
        idx = 1
        while conflict_path.exists():
            conflict_path = orig.with_name(f"{orig.stem}.conflict-{stamp}-{idx}{orig.suffix}")
            idx += 1
    _atomic_write(conflict_path, text)
    return conflict_path.relative_to(root).as_posix()


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

        changes = _porcelain_changes(root)
        if changes:
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
            _run_git(root, "checkout", "-B", br, remote_ref)
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

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        saved_conflicts = []
        restored_remote = []
        index_conflicted = False

        for rel in unmerged:
            if rel == "MEMORY.md":
                index_conflicted = True
                _run_git(root, "checkout", "--ours", "MEMORY.md", check=False)
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
        _run_git(root, "add", "-A")
        msg = (
            "sync: merge remote (auto-rebuilt MEMORY.md)"
            if not saved_conflicts
            else f"sync: merge remote ({len(saved_conflicts)} entry conflict copy preserved)"
        )
        _run_git(root, "commit", "--no-edit", "-m", msg)
        return {
            "pulled": True,
            "status": "merged_with_conflicts" if saved_conflicts else "merged_index_rebuilt",
            "branch": br,
            "remote": remote,
            "index_rebuilt": index_conflicted,
            "conflicts": saved_conflicts,
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

        existing_conflicts = find_conflict_files(root)
        if existing_conflicts:
            raise GitSyncError(
                f"Unresolved conflict copies exist ({', '.join(existing_conflicts)}); merge and remove them before pushing."
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


def sync(root, remote="origin", branch="", mode="rw", pull_only=False, push_only=False, browser_login=True):
    """Run bidirectional (or one-way) Git synchronization for a library."""
    result = {"path": str(Path(root).expanduser().resolve()), "mode": mode, "pull": None, "push": None}
    if browser_login:
        remote_url = _run_git(root, "remote", "get-url", remote, check=False).stdout.strip()
        if github_browser_login(remote_url):
            # Finish the interactive step before taking the library write lock or making a snapshot.
            test_remote_access(root, remote, browser_login=True)
            if _run_git(root, "remote", "get-url", remote).stdout.strip() != remote_url:
                raise GitSyncError("授权期间仓库绑定已变更，请对新的仓库重新同步。")
    if not push_only:
        pull_res = pull(root, remote=remote, branch=branch, mode=mode)
        result["pull"] = pull_res
        if pull_res.get("conflicts"):
            result["status"] = "needs_manual_merge"
            return result
    if mode == "rw" and not pull_only:
        push_res = push(root, remote=remote, branch=branch, mode=mode, browser_login=browser_login)
        result["push"] = push_res
    result["status"] = "ok"
    return result
