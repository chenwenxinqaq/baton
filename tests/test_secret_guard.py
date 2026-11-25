"""Tests for write-time and lint-time credential leak protection."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))
sys.path.insert(0, str(ROOT / "manager"))
sys.path.insert(0, str(ROOT / "scripts"))

import manager as manager_app  # noqa: E402
import memory_lint  # noqa: E402
import secret_guard  # noqa: E402
import server as mcp_server  # noqa: E402


def _make_library(root: Path):
    for folder in ("constraints", "methods", "preferences", "projects", "archive", "user-managed"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "MEMORY.md").write_text(
        "# 记忆索引\n\n"
        "## L0 操作约束\n\n"
        "## L1 方法\n\n"
        "## L2 偏好\n\n"
        "## L3 项目\n\n"
        "## L4 归档\n",
        encoding="utf-8",
    )


class SecretGuardTests(unittest.TestCase):
    def test_detects_provider_tokens_private_keys_bearer_and_credentialed_urls(self):
        samples = [
            ("GitHub token", "token: ghp_AbCdEf0123456789GhIjKlMnOpQrStUvWxYz"),
            ("Anthropic API key", "key = sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"),
            ("AWS access key", "aws: AKIA7K9M2P4Q8R6T3V5W"),
            ("PEM private key", "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"),
            ("Bearer token", "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"),
            ("URL with embedded credentials", "postgres://admin:S3cr3tPass99@db.internal.example:5432/app"),
            ("inline credential", "api_key = \"aB9$kL2mN5pQ8rT1vW4xY7z\""),
        ]
        for expected_kind, text in samples:
            with self.subTest(expected_kind=expected_kind):
                findings = secret_guard.find_secrets(text)
                self.assertTrue(findings, f"expected finding for {expected_kind}")
                # Masked output must never leak the raw secret payload.
                rendered = secret_guard.format_findings(findings)
                self.assertNotIn("S3cr3tPass99", rendered)
                self.assertNotIn("GhIjKlMnOpQrStUvWxYz", rendered)

    def test_allows_safe_credential_locations_and_placeholders(self):
        safe_texts = [
            "凭据存放在 ~/.config/demo/token（权限 0600），不要把明文写入记忆。",
            "环境变量 OPENAI_API_KEY 或 GITHUB_TOKEN 由系统钥匙串注入。",
            "示例：Authorization: Bearer <your-token-here>",
            "示例：api_key = \"your-api-key-placeholder\"",
            "本地开发数据库：postgres://postgres:postgres@localhost:5432/dev",
            "密码字段名 password = PASSWORD_ENV_NAME",
            "配置路径 token_file = /Users/demo/.secrets/app.key",
            "环境变量引用 api_key = $OPENAI_API_KEY",
            "AWS 文档示例 AKIAIOSFODNN7EXAMPLE",
        ]
        for text in safe_texts:
            with self.subTest(text=text):
                self.assertEqual(secret_guard.find_secrets(text), [])

    def test_mcp_upsert_blocks_secrets_and_leaves_library_clean(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "lib"
            _make_library(root)
            store = mcp_server.MemoryStore(root)
            with self.assertRaises(ValueError) as ctx:
                store.upsert_entry(
                    layer="L1",
                    name="deploy-note",
                    title="部署脚本",
                    description="记录部署方式",
                    body="验证日期：2026-11-26\nGITHUB_TOKEN=ghp_1234567890abcdefghijklmnopqrstuvwx",
                )
            self.assertIn("明文凭据", str(ctx.exception))
            self.assertFalse((root / "methods" / "deploy-note.md").exists())

    def test_manager_save_topic_and_user_entry_block_secrets(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "lib"
            _make_library(root)
            lib = manager_app.Library(root, "个人记忆")
            with self.assertRaises(manager_app.InvalidEntry):
                lib.save_topic(
                    orig_path="",
                    expected_revision="",
                    layer="L1",
                    title="API 说明",
                    description="包含密钥",
                    body="sk-ant-api03-1234567890abcdefghijklmnopqrstuvwxyz",
                )
            with self.assertRaises(manager_app.InvalidEntry):
                lib.save(
                    manager_app.Entry(
                        name="user-secret",
                        title="私有记录",
                        description="测试",
                        entry_type="reference",
                        layer="L1",
                        status="active",
                        body="-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
                    )
                )

    def test_memory_lint_flags_existing_secret_leak_on_disk(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "lib"
            _make_library(root)
            # Simulate a pre-existing file written outside MCP/Manager.
            (root / "methods" / "legacy-cred.md").write_text(
                "---\nname: legacy-cred\ndescription: 旧记录\ntype: reference\n---\n\n"
                "# 旧记录\n\n验证日期：2026-11-26\n\n"
                "Legacy token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n",
                encoding="utf-8",
            )
            report = mcp_server.MemoryStore(root).lint()
            checks = [f["check"] for f in report["findings"]]
            self.assertIn("secret-leak", checks)
            secret_finding = next(f for f in report["findings"] if f["check"] == "secret-leak")
            self.assertEqual(secret_finding["severity"], "error")
            self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ", secret_finding["detail"])


if __name__ == "__main__":
    unittest.main()
