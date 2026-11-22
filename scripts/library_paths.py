"""Filesystem layout, local state isolation, and multi-library mount configuration."""

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
import threading
import time

try:  # POSIX advisory locking; the Windows fallback below uses msvcrt.
    import fcntl
except ImportError:  # pragma: no cover - platform specific
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None


FRAMEWORK_ROOT = Path(__file__).resolve().parent.parent
ALL_LAYERS = ("L0", "L1", "L2", "L3", "L4")
DEFAULT_SHARED_LAYERS = ("L1", "L3", "L4")
MOUNT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
RESERVED_MOUNT_IDS = frozenset({"personal", "primary", "all", "local"})
# Schema 2 adds a monotonically increasing ``generation`` and a ``quarantine`` list
# that keeps unusable entries instead of dropping them. Schema 1 files migrate in memory
# on load and are rewritten as schema 2 on the next save.
MOUNTS_SCHEMA_VERSION = 2
MOUNTS_LOCK_TIMEOUT = 10.0


class MountsConfigError(ValueError):
    """The mounts config cannot be read, validated or written safely."""

    code = "invalid_config"


class MountsConfigConflict(MountsConfigError):
    """The file changed on disk after it was loaded; reload and retry."""

    code = "conflict"


class MountsConfigLockTimeout(MountsConfigError):
    """Another process held the mounts config lock for too long."""

    code = "locked"


def library_seed_files(shared=False):
    files = {"MEMORY.md": FRAMEWORK_ROOT / "templates/library.md"}
    for name in ("entry", "project", "incident"):
        relative = f"templates/{name}.md"
        files[relative] = FRAMEWORK_ROOT / relative
    if not shared:
        skill = "skills/unified-memory/SKILL.md"
        files[skill] = FRAMEWORK_ROOT / skill
    return files


def _resolved_home(home=None):
    return Path(home).expanduser().resolve() if home else Path.home().resolve()


def data_root(path=None):
    configured = path or os.environ.get("UNIFIED_MEMORY_ROOT") or Path.home() / "UnifiedMemory"
    root = Path(configured).expanduser().resolve()
    if root == FRAMEWORK_ROOT or FRAMEWORK_ROOT in root.parents:
        raise ValueError("Choose a private memory directory outside the framework checkout.")
    return root


def validate_library_path(path):
    """Validate and resolve a memory library root outside the framework checkout."""
    root = Path(path).expanduser().resolve()
    if root == FRAMEWORK_ROOT or FRAMEWORK_ROOT in root.parents:
        raise ValueError("Choose a memory directory outside the framework checkout.")
    return root


def state_base_dir(home=None):
    """Return local-only runtime state root (kept outside synced memory folders)."""
    env_dir = os.environ.get("UNIFIED_MEMORY_STATE_DIR")
    if env_dir and home is None:
        return Path(env_dir).expanduser().resolve()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg and home is None:
        return (Path(xdg).expanduser().resolve() / "unified-memory")
    return _resolved_home(home) / ".local" / "state" / "unified-memory"


def global_state_dir(home=None):
    """Return machine-local state dir for client integrations, backups, and logs."""
    return state_base_dir(home=home) / "global"


def library_state_dir(root, home=None):
    """Return machine-local state dir (locks, trash) isolated per library path."""
    resolved = Path(root).expanduser().resolve()
    digest = sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    return state_base_dir(home=home) / "libraries" / digest


def config_base_dir(home=None):
    """Return machine-local configuration directory (not synced with memory repos)."""
    env_dir = os.environ.get("UNIFIED_MEMORY_CONFIG_DIR")
    if env_dir and home is None:
        return Path(env_dir).expanduser().resolve()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and home is None:
        return (Path(xdg).expanduser().resolve() / "unified-memory")
    return _resolved_home(home) / ".config" / "unified-memory"


def mounts_config_path(home=None):
    return config_base_dir(home=home) / "mounts.json"


def _fsync_dir(directory):
    """Flush a directory entry so a completed rename survives a crash (best effort)."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".tmp-", delete=False) as handle:
            staged = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        staged.replace(path)
        _fsync_dir(path.parent)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _atomic_write_json(path, payload):
    _atomic_write_bytes(path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


_lock_state = threading.local()


def _try_lock(fd):
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True
    if msvcrt is not None:  # pragma: no cover - Windows only
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
    return True


def _unlock(fd):
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - Windows only
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextmanager
def mounts_config_lock(home=None, timeout=None):
    """Exclusive cross-process lock around mounts.json read-modify-write cycles.

    Reentrant within a thread. The lock lives next to the config file and is released by
    the OS if the holder crashes.
    """
    path = mounts_config_path(home=home)
    lock_path = path.with_name(path.name + ".lock")
    key = str(lock_path)
    held = getattr(_lock_state, "held", None)
    if held is None:
        held = _lock_state.held = set()
    if key in held:
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + (MOUNTS_LOCK_TIMEOUT if timeout is None else timeout)
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise MountsConfigLockTimeout(
                    f"Timed out waiting for another process to finish updating {path}. Try again."
                )
            time.sleep(0.05)
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)
            _unlock(fd)
    finally:
        os.close(fd)


def config_signature(home=None):
    """Cheap change token for mounts.json: None when absent, else (mtime_ns, size, inode, sha256).

    mtime alone misses same-timestamp rewrites; the content hash and inode (atomic replace
    allocates a new one) close that gap. An unreadable file yields ("error", errno) so a
    transition to or from an error state is also a change.
    """
    path = mounts_config_path(home=home)
    try:
        stat = path.stat()
        digest = sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return ("error", exc.errno)
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino, digest)


def normalize_layers(layers, default=DEFAULT_SHARED_LAYERS):
    if layers is None:
        return list(default)
    if isinstance(layers, str):
        raw = [part.strip().upper() for part in layers.split(",") if part.strip()]
    else:
        raw = [str(part).strip().upper() for part in layers if str(part).strip()]
    ordered = []
    for item in raw:
        if item not in ALL_LAYERS:
            raise ValueError(f"Invalid layer '{item}'. Expected subset of {', '.join(ALL_LAYERS)}.")
        if item not in ordered:
            ordered.append(item)
    if not ordered:
        raise ValueError("At least one layer must be enabled.")
    return ordered


def normalize_sync_config(sync_cfg):
    if not sync_cfg:
        return None
    if not isinstance(sync_cfg, dict):
        raise ValueError("sync configuration must be an object.")
    driver = str(sync_cfg.get("driver") or "git").strip().lower()
    if driver not in {"git"}:
        raise ValueError(f"Unsupported sync driver: {driver}")
    return {
        "driver": driver,
        "remote": str(sync_cfg.get("remote") or "origin").strip() or "origin",
        "branch": str(sync_cfg.get("branch") or "").strip(),
        "remote_url": str(sync_cfg.get("remote_url") or "").strip(),
        "auto_commit": bool(sync_cfg.get("auto_commit", True)),
        "auto_push": bool(sync_cfg.get("auto_push", False)),
    }


_CORE_KEYS = ("version", "generation", "personal_sync", "mounts", "quarantine")


def _read_config_bytes(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise MountsConfigError(f"Cannot read mounts config {path}: {exc}") from exc


def _digest(data):
    return None if data is None else sha256(data).hexdigest()


def _parse_config_bytes(data, path):
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MountsConfigError(
            f"Mounts config {path} is not valid JSON ({exc}). Fix or remove it; the last good "
            f"copy, if any, is {path.name}.bak next to it."
        ) from exc
    if not isinstance(raw, dict):
        raise MountsConfigError(f"Invalid mounts config format in {path}: expected a JSON object.")
    return raw


def _schema_version(raw, path):
    version = raw.get("version")
    if version is None:
        return 1
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise MountsConfigError(f"Invalid 'version' in {path}: {version!r}.")
    if version > MOUNTS_SCHEMA_VERSION:
        raise MountsConfigError(
            f"{path} uses schema version {version}, newer than this build supports "
            f"({MOUNTS_SCHEMA_VERSION}). Update Unified Memory; the file was left untouched."
        )
    return version


def _migrate_v1_to_v2(raw):
    """Start the generation counter; non-object entries are quarantined by _build_config."""
    migrated = dict(raw)
    migrated.update(version=2, generation=0)
    return migrated


_MIGRATIONS = {1: _migrate_v1_to_v2}


def _empty_config():
    return {
        "version": MOUNTS_SCHEMA_VERSION,
        "generation": 0,
        "personal_sync": None,
        "mounts": [],
        "quarantine": [],
        "extra": {},
        "migrated_from": None,
        "_digest": None,
    }


def _build_config(raw, path, digest):
    version = _schema_version(raw, path)
    migrated_from = version if version < MOUNTS_SCHEMA_VERSION else None
    mounts = raw.get("mounts") or []
    if not isinstance(mounts, list):
        raise MountsConfigError(f"Invalid 'mounts' list in {path}")
    while version < MOUNTS_SCHEMA_VERSION:
        raw = _MIGRATIONS[version](raw)
        version += 1
    generation = raw.get("generation", 0)
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        generation = 0
    quarantine = raw.get("quarantine") or []
    quarantine = list(quarantine) if isinstance(quarantine, list) else []
    # Non-object entries cannot be edited safely; keep them aside instead of dropping them.
    quarantine += [
        {"value": item, "reason": "mount entry is not an object"}
        for item in mounts
        if not isinstance(item, dict)
    ]
    try:
        personal_sync = normalize_sync_config(raw.get("personal_sync")) if raw.get("personal_sync") else None
    except ValueError as exc:
        raise MountsConfigError(f"Invalid 'personal_sync' in {path}: {exc}") from exc
    cfg = _empty_config()
    cfg.update(
        generation=generation,
        personal_sync=personal_sync,
        mounts=[item for item in mounts if isinstance(item, dict)],
        quarantine=quarantine,
        extra={key: value for key, value in raw.items() if key not in _CORE_KEYS},
        migrated_from=migrated_from,
        _digest=digest,
    )
    return cfg


def load_mounts_config(home=None):
    """Load, migrate and validate the mounts config.

    Raises MountsConfigError (a ValueError) for unreadable, corrupt or too-new files.
    Entries that are objects are returned as-is, even if invalid, so edits never drop
    them; resolve_libraries() reports which ones are unusable.
    """
    path = mounts_config_path(home=home)
    data = _read_config_bytes(path)
    if data is None:
        return _empty_config()
    return _build_config(_parse_config_bytes(data, path), path, _digest(data))


def save_mounts_config(cfg, home=None):
    """Atomically persist mounts.json under the config lock.

    A cfg obtained from load_mounts_config() remembers the content it was read from; if the
    file changed since (even by a writer that ignores the lock) MountsConfigConflict is
    raised instead of overwriting. The previous valid file is kept as mounts.json.bak, and
    an unreadable file is moved aside as mounts.json.corrupt-<timestamp> rather than lost.
    """
    path = mounts_config_path(home=home)
    with mounts_config_lock(home=home):
        data = _read_config_bytes(path)
        if "_digest" in cfg and cfg["_digest"] != _digest(data):
            raise MountsConfigConflict(
                f"{path} changed while it was being edited. Reload it and repeat the change."
            )
        mounts = list(cfg.get("mounts") or [])
        if any(not isinstance(item, dict) for item in mounts):
            raise MountsConfigError("Mount entries must be objects.")
        personal_sync = normalize_sync_config(cfg.get("personal_sync")) if cfg.get("personal_sync") else None
        previous_generation = int(cfg.get("generation") or 0)
        if data is not None:
            try:
                existing = _parse_config_bytes(data, path)
            except MountsConfigError:
                suffix = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1000000:06d}"
                path.replace(path.with_name(f"{path.name}.corrupt-{suffix}"))
            else:
                _schema_version(existing, path)  # refuse to overwrite a newer schema
                generation = existing.get("generation")
                if isinstance(generation, int) and not isinstance(generation, bool):
                    previous_generation = max(previous_generation, generation)
                _atomic_write_bytes(path.with_name(path.name + ".bak"), data)
        payload = {key: value for key, value in (cfg.get("extra") or {}).items() if key not in _CORE_KEYS}
        payload.update(
            version=MOUNTS_SCHEMA_VERSION,
            generation=previous_generation + 1,
            personal_sync=personal_sync,
            mounts=mounts,
            quarantine=list(cfg.get("quarantine") or []),
        )
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        _atomic_write_bytes(path, encoded)
        cfg.update(version=MOUNTS_SCHEMA_VERSION, generation=payload["generation"], migrated_from=None)
        cfg["_digest"] = _digest(encoded)
    return path


def update_mounts_config(mutator, home=None):
    """Run ``mutator(cfg)`` against a fresh config and save it, all under the config lock.

    Do slow work (cloning, network) before calling this and keep the mutator quick and
    free of side effects; raising from the mutator leaves the file untouched.
    """
    with mounts_config_lock(home=home):
        cfg = load_mounts_config(home=home)
        result = mutator(cfg)
        save_mounts_config(cfg, home=home)
    return result


def validate_mount_id(mount_id):
    mid = str(mount_id or "").strip()
    if not MOUNT_ID_RE.fullmatch(mid) or mid in RESERVED_MOUNT_IDS:
        raise ValueError(
            f"Invalid mount id '{mount_id}'. Use lowercase letters, digits, '-' or '_' "
            f"(1-40 chars) and avoid reserved names: {', '.join(sorted(RESERVED_MOUNT_IDS))}."
        )
    return mid


def _issue(code, message, mount_id=None, path=None):
    return {
        "code": code,
        "severity": "error",
        "id": mount_id,
        "path": str(path) if path is not None else None,
        "message": message,
    }


def library_unavailable_reason(path):
    """Return why a library directory cannot be opened, or None when it looks usable."""
    target = Path(path)
    if not target.exists():
        return "directory does not exist (disk not mounted or path moved?)"
    if not target.is_dir():
        return "path is not a directory"
    if not (target / "MEMORY.md").is_file():
        return "MEMORY.md not found in the directory"
    if not os.access(target, os.R_OK | os.X_OK):
        return "directory is not readable"
    return None


def unavailable_issue(desc, reason):
    return _issue("unavailable", reason, mount_id=desc.get("id"), path=desc.get("path"))


def _describe_mount(entry):
    """Validate one mounts.json entry and return its library descriptor (ValueError if unusable)."""
    enabled = entry.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("'enabled' must be true or false.")
    mid = validate_mount_id(entry.get("id", ""))
    raw_path = entry.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(f"Mount '{mid}' has no 'path'.")
    mpath = validate_library_path(raw_path)
    mode = entry.get("mode") or "ro"
    mode = mode.strip().lower() if isinstance(mode, str) else mode
    if mode not in {"ro", "rw"}:
        raise ValueError(f"Invalid mount mode '{mode}' for '{mid}'. Expected 'ro' or 'rw'.")
    role = entry.get("role") or "shared"
    role = role.strip().lower() if isinstance(role, str) else role
    if role not in {"shared", "personal"}:
        raise ValueError(f"Invalid role '{role}' for '{mid}'. Expected 'shared' or 'personal'.")
    layers = normalize_layers(
        entry.get("layers"),
        default=ALL_LAYERS if role == "personal" else DEFAULT_SHARED_LAYERS,
    )
    sync_cfg = normalize_sync_config(entry.get("sync")) if entry.get("sync") else None
    return {
        "id": mid,
        "label": str(entry.get("label") or mid).strip() or mid,
        "path": mpath,
        "role": role,
        "mode": mode,
        "layers": layers,
        "allow_user_managed": role == "personal",
        "enabled": enabled,
        "sync": sync_cfg,
    }


def resolve_libraries(primary_root=None, home=None, include_disabled=False, issues=None):
    """Return ordered library descriptors: personal first, followed by configured mounts.

    Without ``issues`` the behaviour is strict: an unreadable config or invalid entry raises
    ValueError and duplicates are skipped. Pass a list as ``issues`` to degrade gracefully
    instead: bad config or entries are skipped and described there (dicts with code,
    severity, id, path, message), so one broken mount cannot take every library down.
    """
    primary = data_root(primary_root)
    try:
        cfg = load_mounts_config(home=home)
    except MountsConfigError as exc:
        if issues is None:
            raise
        issues.append(_issue(exc.code, str(exc), path=mounts_config_path(home=home)))
        cfg = _empty_config()
    libs = [
        {
            "id": "personal",
            "label": "统一本地记忆",
            "path": primary,
            "role": "personal",
            "mode": "rw",
            "layers": list(ALL_LAYERS),
            "allow_user_managed": True,
            "enabled": True,
            "sync": cfg.get("personal_sync"),
        }
    ]
    for item in cfg.get("quarantine") or []:
        if issues is not None:
            issues.append(_issue("invalid_entry", f"Ignored quarantined entry: {item.get('reason')}"))
    seen_paths = {primary}
    seen_ids = {"personal"}
    for entry in cfg.get("mounts") or []:
        if entry.get("enabled", True) is False and not include_disabled:
            continue
        try:
            desc = _describe_mount(entry)
        except ValueError as exc:
            if issues is None:
                raise
            raw_id = entry.get("id")
            raw_path = entry.get("path")
            issues.append(
                _issue(
                    "invalid_entry",
                    str(exc),
                    mount_id=raw_id if isinstance(raw_id, str) else None,
                    path=raw_path if isinstance(raw_path, str) else None,
                )
            )
            continue
        if desc["id"] in seen_ids:
            if issues is not None:
                issues.append(_issue("duplicate_id", f"Duplicate mount id '{desc['id']}' ignored.", desc["id"], desc["path"]))
            continue
        if desc["path"] in seen_paths:
            if issues is not None:
                issues.append(
                    _issue("duplicate_path", f"Mount '{desc['id']}' reuses an already mounted path and was ignored.", desc["id"], desc["path"])
                )
            continue
        libs.append(desc)
        seen_ids.add(desc["id"])
        seen_paths.add(desc["path"])
    return libs
