"""TOML + environment + mounted-secret configuration, without value logging."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import tomllib


class ConfigError(ValueError):
    """Messages are fixed error codes; never include user-supplied values."""


@dataclass(frozen=True)
class Settings:
    channel: str = "codexreset"
    poll_interval_seconds: int = 60
    delivery_enabled: bool = False
    state_dir: Path = Path("data")
    webhook_url: str = field(default="", repr=False)
    relay_url: str = field(default="", repr=False)
    relay_authorization: str = field(default="", repr=False)
    proxy_url: str = field(default="", repr=False)


SCHEMA = {
    "service": {"delivery_enabled": bool, "poll_interval_seconds": int,
                "state_dir": str, "secrets_dir": str},
    "telegram": {"channel": str, "relay_url": str, "relay_authorization": str,
                 "relay_authorization_file": str, "proxy_url": str, "proxy_url_file": str},
    "wecom": {"webhook_url": str, "webhook_url_file": str},
}


def load_config(config_path=None, environ=None):
    env = os.environ if environ is None else environ
    path = config_path or env.get("NOTIFIER_CONFIG")
    base = Path(path).resolve().parent if path else Path.cwd()
    document = {}
    if path:
        try:
            with open(path, "rb") as handle:
                document = tomllib.load(handle)
        except (tomllib.TOMLDecodeError, UnicodeError):
            raise ConfigError("config_invalid_toml") from None
        except OSError:
            raise ConfigError("config_file_unreadable") from None
    for section, values in document.items():
        if section not in SCHEMA or type(values) is not dict:
            raise ConfigError("unknown_configuration_section")
        for name, value in values.items():
            expected = SCHEMA[section].get(name)
            if expected is None:
                raise ConfigError("unknown_configuration_key")
            if type(value) is not expected:
                raise ConfigError("invalid_configuration_type")

    def value(section, key, variable, default):
        return env.get(variable, document.get(section, {}).get(key, default))

    def resolve(raw, root):
        if not raw:
            raise ConfigError("empty_configuration_path")
        p = Path(raw).expanduser()
        return p if p.is_absolute() else root / p

    def setting_path(key, variable, default):
        raw = value("service", key, variable, default)
        return resolve(raw, Path.cwd() if variable in env else base)

    secret_dir = setting_path("secrets_dir", "SECRETS_DIR", "/run/notifier-secrets")

    def read_secret(path, required):
        try:
            with path.open("rb") as handle:
                raw = handle.read(16385)
            if len(raw) > 16384:
                raise ConfigError("secret_file_too_large")
            return raw.decode("utf-8").strip()
        except FileNotFoundError:
            if not required:
                return ""
            raise ConfigError("secret_file_unreadable") from None
        except (OSError, UnicodeError):
            raise ConfigError("secret_file_unreadable") from None

    def secret(section, key, variable, legacy_name):
        values = document.get(section, {})
        file_key = key + "_file"
        file_variable = variable + "_FILE"
        if variable in env and file_variable in env:
            raise ConfigError("ambiguous_environment_secret")
        if variable in env:
            return env[variable].strip()
        if file_variable in env:
            return read_secret(resolve(env[file_variable], Path.cwd()), True)
        if key in values and file_key in values:
            raise ConfigError("ambiguous_configuration_secret")
        if key in values:
            return values[key].strip()
        if file_key in values:
            return read_secret(resolve(values[file_key], base), True)
        return read_secret(secret_dir / legacy_name, False)

    enabled = value("service", "delivery_enabled", "DELIVERY_ENABLED", False)
    if isinstance(enabled, str):
        if enabled.lower() not in ("true", "false"):
            raise ConfigError("invalid_delivery_enabled")
        enabled = enabled.lower() == "true"
    interval = value("service", "poll_interval_seconds", "POLL_INTERVAL_SECONDS", 60)
    try:
        interval = int(interval)
    except (ValueError, TypeError):
        raise ConfigError("invalid_poll_interval") from None
    if not 30 <= interval <= 120:
        raise ConfigError("poll_interval_must_be_30_to_120")
    channel = value("telegram", "channel", "TELEGRAM_CHANNEL", "codexreset")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", channel):
        raise ConfigError("invalid_channel")
    return Settings(
        channel=channel,
        poll_interval_seconds=interval,
        delivery_enabled=enabled,
        state_dir=setting_path("state_dir", "STATE_DIR", "data"),
        webhook_url=secret("wecom", "webhook_url", "WECOM_WEBHOOK_URL", "wecom-webhook-url"),
        relay_url=value("telegram", "relay_url", "TELEGRAM_RELAY_URL", ""),
        relay_authorization=secret("telegram", "relay_authorization", "TELEGRAM_RELAY_AUTHORIZATION", "relay-authorization"),
        proxy_url=secret("telegram", "proxy_url", "TELEGRAM_PROXY_URL", "telegram-proxy-url"),
    )
