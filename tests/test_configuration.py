"""Configuration is external, validated, and never echoed with credentials."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "config.toml"

    def load(self, text="", env=None):
        self.assertIsNotNone(importlib.util.find_spec("configuration"),
                             "external configuration loader is not implemented")
        from configuration import load_config
        self.path.write_text(text)
        return load_config(self.path, environ={} if env is None else env)

    def test_defaults_never_enable_delivery(self):
        cfg = self.load()
        self.assertFalse(cfg.delivery_enabled)
        self.assertEqual(cfg.channel, "codexreset")
        self.assertEqual(cfg.poll_interval_seconds, 60)
        self.assertEqual(cfg.state_dir, self.root / "data")

    def test_toml_configures_webhook_without_modifying_source(self):
        cfg = self.load('''[service]
delivery_enabled = true
poll_interval_seconds = 90
[wecom]
webhook_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=TEST_ONLY"
''')
        self.assertTrue(cfg.delivery_enabled)
        self.assertEqual(cfg.poll_interval_seconds, 90)
        self.assertIn("TEST_ONLY", cfg.webhook_url)
        self.assertNotIn("TEST_ONLY", repr(cfg))

    def test_environment_overrides_config(self):
        cfg = self.load('[service]\ndelivery_enabled = true\n[telegram]\nchannel = "example"\n',
                        {"DELIVERY_ENABLED": "false", "TELEGRAM_CHANNEL": "codexreset",
                         "POLL_INTERVAL_SECONDS": "45", "WECOM_WEBHOOK_URL": "TEST_ENV_ONLY"})
        self.assertFalse(cfg.delivery_enabled)
        self.assertEqual(cfg.channel, "codexreset")
        self.assertEqual(cfg.poll_interval_seconds, 45)
        self.assertEqual(cfg.webhook_url, "TEST_ENV_ONLY")

    def test_secret_files_are_relative_to_config_file(self):
        (self.root / "secrets").mkdir()
        (self.root / "secrets/webhook").write_text("TEST_FILE_ONLY\n")
        cfg = self.load('[wecom]\nwebhook_url_file = "secrets/webhook"\n')
        self.assertEqual(cfg.webhook_url, "TEST_FILE_ONLY")

    def test_legacy_secret_mount_still_works(self):
        secret_dir = self.root / "secrets"
        secret_dir.mkdir()
        for name, value in (("wecom-webhook-url", "WEBHOOK_TEST"),
                            ("relay-authorization", "Basic RELAY_TEST"),
                            ("telegram-proxy-url", "http://proxy.example.com:3128")):
            (secret_dir / name).write_text(value)
        cfg = self.load(env={"SECRETS_DIR": str(secret_dir)})
        self.assertEqual(cfg.webhook_url, "WEBHOOK_TEST")
        self.assertEqual(cfg.relay_authorization, "Basic RELAY_TEST")
        self.assertEqual(cfg.proxy_url, "http://proxy.example.com:3128")

    def test_explicit_empty_value_disables_legacy_secret_fallback(self):
        secret_dir = self.root / "secrets"
        secret_dir.mkdir()
        (secret_dir / "telegram-proxy-url").write_text("http://proxy.example.com:3128")
        cfg = self.load('[telegram]\nproxy_url = ""\n', {"SECRETS_DIR": str(secret_dir)})
        self.assertEqual(cfg.proxy_url, "")

    def test_env_secret_does_not_require_unused_file(self):
        cfg = self.load('[wecom]\nwebhook_url_file = "missing"\n', {"WECOM_WEBHOOK_URL": "ENV_ONLY"})
        self.assertEqual(cfg.webhook_url, "ENV_ONLY")

    def test_unknown_keys_and_invalid_types_fail_closed(self):
        for content in ('[service]\ndelivery_enabld = true', '[unknown]\nx = 1',
                        '[service]\ndelivery_enabled = "false"', '[service]\npoll_interval_seconds = true',
                        '[service]\npoll_interval_seconds = 0', '[service]\npoll_interval_seconds = 900',
                        '[telegram]\nchannel = "bad/path"'):
            with self.subTest(content=content):
                self.assertIsNotNone(importlib.util.find_spec("configuration"), "configuration loader missing")
                from configuration import ConfigError
                with self.assertRaises(ConfigError):
                    self.load(content)

    def test_missing_explicit_secret_does_not_echo_path(self):
        self.assertIsNotNone(importlib.util.find_spec("configuration"), "configuration loader missing")
        from configuration import ConfigError
        with self.assertRaises(ConfigError) as ctx:
            self.load('[wecom]\nwebhook_url_file = "PRIVATE_PATH"')
        self.assertNotIn("PRIVATE_PATH", str(ctx.exception))

    def test_inline_and_file_secret_are_ambiguous(self):
        self.assertIsNotNone(importlib.util.find_spec("configuration"), "configuration loader missing")
        from configuration import ConfigError
        with self.assertRaises(ConfigError):
            self.load('[wecom]\nwebhook_url = "TEST"\nwebhook_url_file = "secret.txt"')

    def test_invalid_boolean_environment_is_rejected(self):
        self.assertIsNotNone(importlib.util.find_spec("configuration"), "configuration loader missing")
        from configuration import ConfigError
        with self.assertRaises(ConfigError):
            self.load(env={"DELIVERY_ENABLED": "flase"})

    def cli(self, content, *args):
        self.path.write_text(content)
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "SSL_CERT_FILE")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run([sys.executable, str(ROOT / "notifier.py"), "--config", str(self.path), *args],
                              capture_output=True, text=True, env=env, timeout=10)

    def test_check_config_is_offline_and_redacts_credentials(self):
        result = self.cli('''[service]
delivery_enabled = true
[wecom]
webhook_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=PRIVATE_TEST_SENTINEL"
''', "--check-config")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["webhook_configured"])
        self.assertNotIn("PRIVATE_TEST_SENTINEL", result.stdout + result.stderr)
        self.assertFalse((self.root / "data").exists())

    def test_toml_error_does_not_echo_secret_or_traceback(self):
        result = self.cli('[wecom]\nwebhook_url = "PRIVATE_TEST_SENTINEL', "--check-config")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("PRIVATE_TEST_SENTINEL", result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_enabled_delivery_requires_webhook(self):
        result = self.cli('[service]\ndelivery_enabled = true', "--check-config")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("webhook_missing", result.stderr)

    def test_test_send_missing_webhook_fails_before_network(self):
        result = self.cli('', "--send-test", "LOCAL-TEST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("webhook_missing", result.stderr)

    def test_check_config_rejects_malformed_network_endpoints(self):
        for snippet in ('[telegram]\nproxy_url = "http://"',
                        '[telegram]\nproxy_url = "http://proxy.example.com:notaport"',
                        '[telegram]\nproxy_url = "http://proxy.example.com:0"',
                        '[telegram]\nrelay_url = "https://relay.example.com:notaport/codex-reset-feed"\nrelay_authorization = "Basic TEST_ONLY"',
                        '[wecom]\nwebhook_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=PRIVATE\\nTEST"'):
            with self.subTest(snippet=snippet):
                result = self.cli(snippet, "--check-config")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("PRIVATE", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
