"""Central configuration. Secrets live in /etc/proxy-hub/proxy-hub.env (never in Git)."""
from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
SYSTEM_ENV_FILE = Path("/etc/proxy-hub/proxy-hub.env")


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(SYSTEM_ENV_FILE)
_load_env_file(REPO_DIR / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes", "on")


class Settings:
    """Values are read from the environment at instantiation (supports runtime reload)."""

    def __init__(self) -> None:
        self.host: str = os.getenv("PROXYHUB_HOST", "127.0.0.1")
        self.port: int = _int("PROXYHUB_PORT", 8899)
        self.data_dir: Path = Path(os.getenv("PROXYHUB_DATA_DIR", "/var/lib/proxy-hub"))
        self.db_path: Path = Path(os.getenv("PROXYHUB_DB_PATH", "/var/lib/proxy-hub/proxyhub.db"))
        self.subscription_url: str = os.getenv("PROXYHUB_SUBSCRIPTION_URL", "")
        self.admin_user: str = os.getenv("PROXYHUB_ADMIN_USER", "admin")
        self.admin_password: str = os.getenv("PROXYHUB_ADMIN_PASSWORD", "")
        self.secret_key: str = os.getenv("PROXYHUB_SECRET_KEY", "") or secrets.token_hex(32)

        self.xray_bin: str = os.getenv("PROXYHUB_XRAY_BIN", "/usr/local/bin/xray")
        self.xray_config: Path = Path(
            os.getenv("PROXYHUB_XRAY_CONFIG", "/var/lib/proxy-hub/generated/xray.json"))
        self.socks_port: int = _int("PROXYHUB_SOCKS_PORT", 2081)
        self.http_port: int = _int("PROXYHUB_HTTP_PORT", 1081)

        self.singbox_bin: str = os.getenv("PROXYHUB_SINGBOX_BIN", "/usr/local/bin/sing-box")
        self.singbox_config: Path = Path(
            os.getenv("PROXYHUB_SINGBOX_CONFIG", "/var/lib/proxy-hub/generated/sing-box.json"))
        self.singbox_enabled: bool = _bool("PROXYHUB_SINGBOX_ENABLED", False)

        self.sub_update_interval: int = _int("PROXYHUB_SUB_UPDATE_INTERVAL", 21600)
        self.health_interval: int = _int("PROXYHUB_HEALTH_INTERVAL", 300)
        self.health_url: str = os.getenv("PROXYHUB_HEALTH_URL", "https://api.ipify.org")
        self.health_timeout: int = _int("PROXYHUB_HEALTH_TIMEOUT", 15)

        self.failover_mode: str = os.getenv("PROXYHUB_FAILOVER_MODE", "auto")
        self.failover_threshold: int = _int("PROXYHUB_FAILOVER_THRESHOLD", 3)
        self.failover_cooldown: int = _int("PROXYHUB_FAILOVER_COOLDOWN", 600)

        self.gateway_enabled: bool = _bool("PROXYHUB_GATEWAY_ENABLED", False)
        self.gateway_bind: str = os.getenv("PROXYHUB_GATEWAY_BIND", "127.0.0.1")
        self.public_socks_port: int = _int("PROXYHUB_PUBLIC_SOCKS_PORT", 8388)
        self.public_http_port: int = _int("PROXYHUB_PUBLIC_HTTP_PORT", 8389)
        self.default_route: str = os.getenv("PROXYHUB_DEFAULT_ROUTE", "proxy")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def refresh_settings() -> Settings:
    get_settings.cache_clear()
    _load_env_file(SYSTEM_ENV_FILE)
    return get_settings()
