"""High-precision secret detection for memory writes and consolidation lint.

Memory files are plain Markdown, often Git-synced and injected into future
agent prompts. This guard stops raw credentials (API keys, PATs, private keys,
credentialed URLs, and concrete password/token assignments) from being written
into memory while avoiding false positives on placeholders, env-var references,
and key-file paths.
"""

import re

PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"
)

PROVIDER_PATTERNS = (
    ("github-token", "GitHub 访问令牌", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("github-token", "GitHub 细粒度访问令牌", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("gitlab-token", "GitLab 访问令牌", re.compile(r"\b(?:glpat|gloas)-[A-Za-z0-9_-]{16,}\b")),
    ("anthropic-key", "Anthropic API Key", re.compile(r"\bsk-ant-(?:api\d{2}-)?[A-Za-z0-9_-]{20,}\b")),
    ("openai-key", "OpenAI API Key", re.compile(r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}\b")),
    ("api-key", "sk- 开头 API Key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("aws-key", "AWS Access Key ID", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google-key", "Google API Key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack-token", "Slack Token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{16,}\b")),
    ("stripe-key", "Stripe Live Key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}\b")),
    ("hf-token", "HuggingFace Token", re.compile(r"\bhf_[A-Za-z0-9]{24,}\b")),
    ("pypi-token", "PyPI Token", re.compile(r"\bpypi-[A-Za-z0-9_-]{32,}\b")),
    ("npm-token", "npm Token", re.compile(r"\bnpm_[A-Za-z0-9]{24,}\b")),
    ("jwt-token", "JWT 令牌", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{16,}\b")),
)

CREDENTIAL_URL_RE = re.compile(
    r"\b(?:https?|ftps?|ssh|postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|rediss|amqp|amqps)://"
    r"(?P<user>[^/\s:@]{1,64}):(?P<secret>[^/\s@]{4,160})@(?P<host>[A-Za-z0-9._-]+)",
    re.IGNORECASE,
)

BEARER_RE = re.compile(
    r"(?i)\bAuthorization\s*[:=]\s*[\"']?Bearer\s+(?P<secret>[A-Za-z0-9._~+/=-]{20,})[\"']?"
)

ASSIGNMENT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])"
    r"(?:api[_-]?key|secret[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|db[_-]?password|database[_-]?password|private[_-]?key|password|passwd)"
    r"\s*[:=]\s*[\"']?(?P<secret>[^\s\"'`,;]{12,160})[\"']?"
)

_PLACEHOLDER_TOKENS = (
    "example",
    "sample",
    "placeholder",
    "changeme",
    "replace",
    "dummy",
    "fake",
    "redacted",
    "masked",
    "hidden",
    "your_",
    "your-",
    "my_",
    "my-",
    "test_",
    "test-",
    "keychain",
    "keyring",
    "1password",
    "bitwarden",
)

_DEV_DEFAULT_USERS = {"postgres", "root", "admin", "user", "username", "test", "guest", "dev"}
_DEV_DEFAULT_PASSWORDS = {
    "pass",
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "postgres",
    "root",
    "admin",
    "test",
    "guest",
    "1234",
    "12345",
    "123456",
    "12345678",
}


def mask_secret(value):
    """Return a display-safe preview that never exposes the raw credential."""
    raw = str(value or "").strip()
    if not raw:
        return "***"
    if raw.startswith("-----BEGIN ") and "PRIVATE KEY-----" in raw:
        return raw
    if len(raw) <= 10:
        return "***"
    return f"{raw[:4]}…{raw[-3:]}"


def _char_classes(value):
    classes = 0
    if any("a" <= ch <= "z" for ch in value):
        classes += 1
    if any("A" <= ch <= "Z" for ch in value):
        classes += 1
    if any("0" <= ch <= "9" for ch in value):
        classes += 1
    if any(not ch.isalnum() for ch in value):
        classes += 1
    return classes


def _looks_like_placeholder(value, *, strict_entropy=False, allow_all_caps=False):
    raw = str(value or "").strip()
    if not raw:
        return True
    if any(marker in raw for marker in ("<", ">", "{", "}", "***", "...", "…")):
        return True
    if raw.startswith("$") or re.search(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Z][A-Z0-9_]{2,}\b", raw):
        return True
    # File paths and URLs without embedded credentials are pointers, not secrets.
    if raw.startswith(("/", "~/", "./", "../", "http://", "https://", "file://")):
        return True
    if re.fullmatch(r"[A-Za-z]:\\.*", raw):
        return True
    # Environment variable identifiers (e.g. OPENAI_API_KEY) are references, not values.
    if not allow_all_caps and re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", raw):
        return True
    lowered = raw.casefold()
    if any(tok in lowered for tok in _PLACEHOLDER_TOKENS):
        return True
    if len(set(lowered)) < 5:
        return True
    if strict_entropy:
        # Avoid flagging readable hyphenated/underscored identifiers or prose phrases.
        if re.fullmatch(r"[a-z]+(?:[-_][a-z]+)+", raw):
            return True
        if re.fullmatch(r"[a-zA-Z0-9._-]+/[a-zA-Z0-9._/-]+", raw) and not any(ch.isdigit() for ch in raw):
            return True
        if _char_classes(raw) < 2:
            return True
        # Require either digits or mixed-case alphanumeric entropy for generic assignments.
        has_digit = any("0" <= ch <= "9" for ch in raw)
        has_upper = any("A" <= ch <= "Z" for ch in raw)
        has_lower = any("a" <= ch <= "z" for ch in raw)
        if not (has_digit or (has_upper and has_lower)):
            return True
    return False


def find_secrets(*texts):
    """Scan one or more strings and return redacted secret findings."""
    combined = "\n".join(str(t or "") for t in texts if t)
    if not combined.strip():
        return []
    findings = []
    seen = set()

    def add(kind, label, raw):
        masked = mask_secret(raw)
        key = (kind, masked)
        if key in seen:
            return
        seen.add(key)
        findings.append({"kind": kind, "label": label, "masked": masked})

    for match in PEM_PRIVATE_KEY_RE.finditer(combined):
        add("private-key", "私钥 PEM 内容", match.group(0))

    for kind, label, pattern in PROVIDER_PATTERNS:
        for match in pattern.finditer(combined):
            raw = match.group(0)
            if not _looks_like_placeholder(raw, strict_entropy=False, allow_all_caps=True):
                add(kind, label, raw)

    for match in CREDENTIAL_URL_RE.finditer(combined):
        user = match.group("user").strip()
        secret = match.group("secret").strip()
        if _looks_like_placeholder(secret, strict_entropy=False):
            continue
        if secret.casefold() in _DEV_DEFAULT_PASSWORDS:
            continue
        if user.casefold() == secret.casefold() and user.casefold() in _DEV_DEFAULT_USERS:
            continue
        add("credential-url", "包含密码或令牌的连接 URL", secret)

    for match in BEARER_RE.finditer(combined):
        secret = match.group("secret").strip()
        if not _looks_like_placeholder(secret, strict_entropy=True):
            add("bearer-token", "Bearer 认证令牌", secret)

    for match in ASSIGNMENT_RE.finditer(combined):
        secret = match.group("secret").strip()
        if not _looks_like_placeholder(secret, strict_entropy=True):
            add("secret-assignment", "疑似明文密钥或密码赋值", secret)

    return findings


def format_findings(findings, limit=3):
    """Format findings as a short human-readable masked summary."""
    parts = [f"{item['label']}（{item['masked']}）" for item in findings[:limit]]
    if len(findings) > limit:
        parts.append(f"等共 {len(findings)} 处")
    return "、".join(parts)


def ensure_no_secrets(*texts):
    """Raise ValueError with a redacted explanation when raw credentials appear."""
    findings = find_secrets(*texts)
    if not findings:
        return
    summary = format_findings(findings)
    raise ValueError(
        f"检测到疑似明文凭据或私钥，已阻止写入记忆库：{summary}。"
        "请将真实密钥放入系统钥匙串或权限为 0600 的私有文件，记忆条目中只保留位置说明或环境变量名。"
    )
