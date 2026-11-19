"""Filesystem layout, local state isolation, and multi-library mount configuration."""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile


FRAMEWORK_ROOT = Path(__file__).resolve().parent.parent
ALL_LAYERS = ("L0", "L1", "L2", "L3", "L4")
DEFAULT_SHARED_LAYERS = ("L1", "L3", "L4")
MOUNT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
RESERVED_MOUNT_IDS = frozenset({"personal", "primary", "all", "local"})


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


def _atomic_write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".tmp-", delete=False
        ) as handle:
            staged = Path(handle.name)
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        staged.replace(path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


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


def load_mounts_config(home=None):
    """Load local multi-library mount and sync configuration."""
    path = mounts_config_path(home=home)
    if not path.is_file():
        return {"version": 1, "personal_sync": None, "mounts": []}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid mounts config format in {path}")
    mounts = raw.get("mounts") or []
    if not isinstance(mounts, list):
        raise ValueError(f"Invalid 'mounts' list in {path}")
    return {
        "version": int(raw.get("version") or 1),
        "personal_sync": normalize_sync_config(raw.get("personal_sync")) if raw.get("personal_sync") else None,
        "mounts": mounts,
    }


def save_mounts_config(cfg, home=None):
    """Atomically persist mounts.json."""
    path = mounts_config_path(home=home)
    payload = {
        "version": 1,
        "personal_sync": normalize_sync_config(cfg.get("personal_sync")) if cfg.get("personal_sync") else None,
        "mounts": list(cfg.get("mounts") or []),
    }
    _atomic_write_json(path, payload)
    return path


def validate_mount_id(mount_id):
    mid = str(mount_id or "").strip()
    if not MOUNT_ID_RE.fullmatch(mid) or mid in RESERVED_MOUNT_IDS:
        raise ValueError(
            f"Invalid mount id '{mount_id}'. Use lowercase letters, digits, '-' or '_' "
            f"(1-40 chars) and avoid reserved names: {', '.join(sorted(RESERVED_MOUNT_IDS))}."
        )
    return mid


def resolve_libraries(primary_root=None, home=None, include_disabled=False):
    """Return ordered list of library descriptors: personal first, followed by configured mounts."""
    primary = data_root(primary_root)
    cfg = load_mounts_config(home=home)
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
    seen_paths = {primary}
    seen_ids = {"personal"}
    for entry in cfg.get("mounts") or []:
        if not isinstance(entry, dict):
            continue
        enabled = bool(entry.get("enabled", True))
        if not enabled and not include_disabled:
            continue
        mid = validate_mount_id(entry.get("id", ""))
        if mid in seen_ids:
            continue
        mpath = validate_library_path(entry.get("path", ""))
        if mpath in seen_paths:
            continue
        mode = str(entry.get("mode") or "ro").strip().lower()
        if mode not in {"ro", "rw"}:
            raise ValueError(f"Invalid mount mode '{mode}' for '{mid}'. Expected 'ro' or 'rw'.")
        role = str(entry.get("role") or "shared").strip().lower()
        if role not in {"shared", "personal"}:
            role = "shared"
        layers = normalize_layers(
            entry.get("layers"),
            default=ALL_LAYERS if role == "personal" else DEFAULT_SHARED_LAYERS,
        )
        sync_cfg = normalize_sync_config(entry.get("sync")) if entry.get("sync") else None
        libs.append(
            {
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
        )
        seen_ids.add(mid)
        seen_paths.add(mpath)
    return libs
