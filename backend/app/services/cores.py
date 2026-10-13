"""Proxy core adapters: XrayAdapter (primary) + SingBoxAdapter (Hysteria2 only).

Managed cores run as SEPARATE instances with their own generated configs, so the
pre-existing system Xray service (/usr/local/etc/xray/config.json) is never touched.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ..config import get_settings
from ..models import ProxyNode

log = logging.getLogger("proxyhub.cores")


class CoreError(RuntimeError):
    pass


def _db_setting(session, key: str, default: str) -> str:
    from ..models import Setting

    row = session.query(Setting).filter(Setting.key == key).first()
    return row.value if row else default


def _check_port(port: int, extra: set | None = None) -> None:
    blocked = set(RESERVED_PORTS) | (extra or set())
    if port in blocked:
        raise CoreError(f"port {port} is reserved for internal services")
    if not 1024 <= port <= 65535:
        raise CoreError(f"port {port} out of range 1024-65535")


def _lock_down(path: Path) -> None:
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _chown_proxyhub(path: Path) -> None:
    """Ensure file/directory is owned by proxyhub user for service access."""
    try:
        import os, pwd, grp
        uid = pwd.getpwnam("proxyhub").pw_uid
        gid = grp.getgrnam("proxyhub").gr_gid
        os.chown(path, uid, gid)
    except (KeyError, OSError, PermissionError):
        pass


def managed_socks_url() -> str:
    s = get_settings()
    return f"socks5h://127.0.0.1:{s.socks_port}"


def managed_http_url() -> str:
    s = get_settings()
    return f"http://127.0.0.1:{s.http_port}"


def _vless_outbound(node: ProxyNode) -> dict:
    p = node.params or {}
    stream: dict = {
        "network": p.get("type", "tcp"),
        "security": node.security or "none",
    }
    if node.security == "reality":
        stream["realitySettings"] = {
            "serverName": p.get("sni", ""),
            "fingerprint": p.get("fp", "chrome"),
            "publicKey": p.get("pbk", ""),
            "shortId": p.get("sid", ""),
            "spiderX": p.get("spx", "/"),
        }
    elif node.security == "tls":
        tls_settings: dict = {"serverName": p.get("sni", "") or node.server}
        if p.get("fp"):
            tls_settings["fingerprint"] = p["fp"]
        if p.get("alpn"):
            tls_settings["alpn"] = [a.strip() for a in p["alpn"].split(",") if a.strip()]
        stream["tlsSettings"] = tls_settings
    if stream["network"] == "ws":
        stream["wsSettings"] = {"path": p.get("path", "/"), "headers": {"Host": p.get("host", "")}}
    elif stream["network"] == "grpc":
        stream["grpcSettings"] = {"serviceName": p.get("serviceName", "")}
    return {
        "protocol": "vless",
        "tag": "proxy",
        "settings": {
            "vnext": [
                {
                    "address": node.server,
                    "port": node.port,
                    "users": [
                        {
                            "id": node.secret,
                            "encryption": p.get("encryption", "none"),
                            "flow": p.get("flow", ""),
                        }
                    ],
                }
            ]
        },
        "streamSettings": stream,
    }


def _vmess_outbound(node: ProxyNode) -> dict:
    p = node.params or {}
    network = p.get("net", "tcp")
    stream: dict = {"network": network, "security": node.security or "none"}
    if node.security == "tls":
        tls_settings: dict = {"serverName": p.get("sni", "") or node.server}
        stream["tlsSettings"] = tls_settings
    if network == "ws":
        stream["wsSettings"] = {"path": p.get("path", "/"), "headers": {"Host": p.get("host", "")}}
    elif network == "grpc":
        stream["grpcSettings"] = {"serviceName": p.get("serviceName", "")}
    return {
        "protocol": "vmess",
        "tag": "proxy",
        "settings": {
            "vnext": [
                {
                    "address": node.server,
                    "port": node.port,
                    "users": [{"id": node.secret, "alterId": int(p.get("aid", 0) or 0)}],
                }
            ]
        },
        "streamSettings": stream,
    }


def _trojan_outbound(node: ProxyNode) -> dict:
    p = node.params or {}
    network = p.get("type", "tcp")
    tls_settings: dict = {"serverName": p.get("sni", "") or node.server}
    if p.get("fp"):
        tls_settings["fingerprint"] = p["fp"]
    if p.get("alpn"):
        tls_settings["alpn"] = [a.strip() for a in p["alpn"].split(",") if a.strip()]
    stream: dict = {"network": network, "security": "tls", "tlsSettings": tls_settings}
    if network == "ws":
        stream["wsSettings"] = {"path": p.get("path", "/"), "headers": {"Host": p.get("host", "")}}
    elif network == "grpc":
        stream["grpcSettings"] = {"serviceName": p.get("serviceName", "")}
    return {
        "protocol": "trojan",
        "tag": "proxy",
        "settings": {"servers": [{"address": node.server, "port": node.port, "password": node.secret}]},
        "streamSettings": stream,
    }


def _ss_outbound(node: ProxyNode) -> dict:
    p = node.params or {}
    return {
        "protocol": "shadowsocks",
        "tag": "proxy",
        "settings": {
            "servers": [
                {
                    "address": node.server,
                    "port": node.port,
                    "method": p.get("method", "aes-256-gcm"),
                    "password": node.secret,
                }
            ]
        },
    }


XRAY_BUILDERS = {
    "vless": _vless_outbound,
    "vmess": _vmess_outbound,
    "trojan": _trojan_outbound,
    "ss": _ss_outbound,
}

XRAY_CAPABLE = set(XRAY_BUILDERS)

RESERVED_PORTS = {22, 80, 443, 3306, 3389, 4096, 5001, 631, 8000, 8001,
                  8899, 9011, 1080, 1081, 2080, 2081, 12345}

PRIVATE_DIRECT = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"]


class XrayAdapter:
    """Generates/validates the managed Xray config and controls proxyhub-xray.service."""

    name = "xray"

    def capable(self, node: ProxyNode) -> bool:
        return node.protocol in XRAY_CAPABLE

    def generate(self, node: ProxyNode, gateway: dict | None = None) -> dict:
        """gateway dict (or None): bind, socks_port, http_port, accounts [(u,p)],
        dedicated [(username, port, [(u,p)])], route_rules [xray rules], default_route.
        """
        builder = XRAY_BUILDERS.get(node.protocol)
        if builder is None:
            raise CoreError(f"xray cannot handle protocol: {node.protocol}")
        settings = get_settings()
        inbounds = [
            {"listen": "127.0.0.1", "port": settings.socks_port,
             "protocol": "socks", "settings": {"udp": True}, "tag": "socks-in"},
            {"listen": "127.0.0.1", "port": settings.http_port,
             "protocol": "http", "tag": "http-in"},
        ]
        rules: list[dict] = []
        if gateway:
            bind = gateway["bind"]
            accts = [{"user": u, "pass": p} for u, p in gateway["accounts"]]
            if accts:
                inbounds.append(
                    {"listen": bind, "port": gateway["socks_port"], "protocol": "socks",
                     "settings": {"auth": "password", "accounts": accts, "udp": True},
                     "tag": "gw-socks"})
                inbounds.append(
                    {"listen": bind, "port": gateway["http_port"], "protocol": "http",
                     "settings": {"accounts": accts},
                     "tag": "gw-http"})
            for username, port, sub_accts in gateway.get("dedicated", []):
                inbounds.append(
                    {"listen": bind, "port": port, "protocol": "socks",
                     "settings": {"auth": "password",
                                  "accounts": [{"user": u, "pass": p} for u, p in sub_accts],
                                  "udp": True},
                     "tag": f"gw-{username}"})
            rules.extend(gateway.get("route_rules", []))
            default = gateway.get("default_route", "proxy")
        else:
            rules.append({"type": "field", "ip": PRIVATE_DIRECT, "outboundTag": "direct"})
            default = "proxy"
        rules.append({"type": "field", "port": "0-65535", "outboundTag": default})
        return {
            "log": {"loglevel": "warning"},
            "inbounds": inbounds,
            "outbounds": [
                builder(node),
                {"protocol": "freedom", "tag": "direct"},
                {"protocol": "blackhole", "tag": "block"},
            ],
            "routing": {"domainStrategy": "AsIs", "rules": rules},
        }

    def gateway_context(self, session) -> dict | None:
        """Build gateway context from DB. Returns None when gateway disabled."""
        from ..models import ProxyClient
        from .clients import client_usable, open_password
        from .routing_engine import ensure_seed_rules, ordered_rules, to_xray_rule

        settings = get_settings()
        enabled = _db_setting(session, "gateway_enabled", str(settings.gateway_enabled))
        if enabled.lower() not in ("1", "true", "yes", "on"):
            return None
        bind = _db_setting(session, "gateway_bind", settings.gateway_bind)
        if bind not in ("127.0.0.1", "0.0.0.0"):
            raise CoreError("gateway bind must be 127.0.0.1 or 0.0.0.0")
        socks_port = int(_db_setting(session, "public_socks_port", str(settings.public_socks_port)))
        http_port = int(_db_setting(session, "public_http_port", str(settings.public_http_port)))
        default_route = _db_setting(session, "default_route", settings.default_route)
        if default_route not in ("direct", "proxy", "block"):
            raise CoreError("default route must be direct|proxy|block")
        for port in (socks_port, http_port):
            _check_port(port)
        ensure_seed_rules(session)
        accounts: list[tuple[str, str]] = []
        dedicated: list[tuple[str, int, list]] = []
        for client in session.query(ProxyClient).all():
            usable, _ = client_usable(client)
            if not usable:
                continue
            password = open_password(client.password_enc)
            accounts.append((client.username, password))
            if client.dedicated_port:
                _check_port(client.dedicated_port, extra={socks_port, http_port})
                dedicated.append((client.username, client.dedicated_port,
                                  [(client.username, password)]))
        for _, port, _ in dedicated:
            _check_port(port, extra={socks_port, http_port})
        dedicated_users = {username for username, _port, _accts in dedicated}
        for rule in ordered_rules(session, only_enabled=True):
            if rule.client_scope != "all" and rule.client_scope not in dedicated_users:
                raise CoreError(
                    f"rule #{rule.id} targets client '{rule.client_scope}' which has "
                    "no dedicated inbound port; give the client a dedicated port "
                    "or change the rule scope to 'all'")
        xr_rules = [to_xray_rule(r) for r in ordered_rules(session, only_enabled=True)]
        return {"bind": bind, "socks_port": socks_port, "http_port": http_port,
                "accounts": accounts, "dedicated": dedicated,
                "route_rules": xr_rules, "default_route": default_route}

    def write_config(self, node: ProxyNode, session=None) -> Path:
        settings = get_settings()
        gateway = self.gateway_context(session) if session is not None else None
        cfg = self.generate(node, gateway=gateway)
        settings.xray_config.parent.mkdir(parents=True, exist_ok=True)
        _lock_down(settings.xray_config.parent)
        _chown_proxyhub(settings.xray_config.parent)
        tmp = settings.xray_config.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(settings.xray_config)
        _chown_proxyhub(settings.xray_config)
        return settings.xray_config

    def validate(self) -> None:
        settings = get_settings()
        self._validate_path(settings.xray_config)

    def _validate_path(self, path: Path) -> None:
        """Validate exactly the given file with the installed binary.

        Error strings contain only the validator's stderr (never config
        content, which holds secrets).
        """
        settings = get_settings()
        if not shutil.which(settings.xray_bin) and not Path(settings.xray_bin).exists():
            raise CoreError(f"xray binary not found: {settings.xray_bin}")
        proc = subprocess.run(
            [settings.xray_bin, "run", "-test", "-config", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            raise CoreError(f"xray config invalid: {proc.stderr.strip()[:400]}")

    def _write_candidate(self, node: ProxyNode, session=None) -> Path:
        """Write the candidate config to a unique temp file (NOT live yet).

        Same directory/filesystem as the live file (atomic rename later),
        restrictive permissions and ownership applied before validation.
        Caller must unlink the temp file (success and failure paths).
        """
        settings = get_settings()
        gateway = self.gateway_context(session) if session is not None else None
        cfg = self.generate(node, gateway=gateway)
        settings.xray_config.parent.mkdir(parents=True, exist_ok=True)
        _lock_down(settings.xray_config.parent)
        _chown_proxyhub(settings.xray_config.parent)
        fd, tmp = tempfile.mkstemp(prefix="xray-", suffix=".json",
                                   dir=str(settings.xray_config.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
            os.chmod(tmp, 0o600)
            _chown_proxyhub(Path(tmp))
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return Path(tmp)

    def _backup_live(self) -> Path | None:
        """Copy the current live config aside; best-effort, may return None."""
        settings = get_settings()
        live = settings.xray_config
        if not live.exists():
            return None
        backup = live.with_suffix(".prev")
        try:
            shutil.copy2(live, backup)
            try:
                backup.chmod(0o600)
            except OSError:
                pass
            _chown_proxyhub(backup)
            return backup
        except OSError as exc:
            log.warning("config backup failed: %s", type(exc).__name__)
            return None

    def _svc(self, action: str) -> str:
        proc = subprocess.run(
            ["systemctl", action, "proxyhub-xray"], capture_output=True, text=True, timeout=60
        )
        out = (proc.stdout + proc.stderr).strip()
        if proc.returncode != 0:
            raise CoreError(f"proxyhub-xray {action} failed: {out[:300]}")
        return out

    def reload(self) -> str:
        """Send SIGHUP to proxyhub-xray for zero-downtime config reload."""
        proc = subprocess.run(
            ["systemctl", "reload", "proxyhub-xray"], capture_output=True, text=True, timeout=30
        )
        out = (proc.stdout + proc.stderr).strip()
        if proc.returncode != 0:
            raise CoreError(f"proxyhub-xray reload failed: {out[:300]}")
        return out

    def apply(self, node: ProxyNode, session=None, force_restart: bool = False) -> None:
        if session is not None and node.protocol not in XRAY_CAPABLE:
            try:
                if self.gateway_context(session) is not None:
                    raise CoreError(
                        "gateway is enabled: activate an Xray-capable node "
                        "(VLESS/VMess/Trojan/Shadowsocks), not Hysteria2")
            except CoreError:
                raise
            except Exception:
                pass
        prev_active = None
        if session is not None:
            prev_active = session.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).first()

        # 1-3. candidate temp file -> validate the EXACT file -> then replace.
        # The known-good live config is never touched before validation passes.
        candidate = self._write_candidate(node, session=session)
        try:
            self._validate_path(candidate)
        except Exception:
            try:
                candidate.unlink()
            except OSError:
                pass
            raise
        backup = self._backup_live()
        try:
            candidate.replace(get_settings().xray_config)
        except OSError as exc:
            try:
                candidate.unlink()
            except OSError:
                pass
            raise CoreError(f"config replace failed: {type(exc).__name__}")
        _chown_proxyhub(get_settings().xray_config)

        # 4. apply with rollback: on failure restore the previous config.
        try:
            if force_restart or prev_active is None or prev_active.id != node.id:
                try:
                    self._svc("restart")
                except CoreError:
                    self._svc("start")
            else:
                try:
                    self.reload()
                except CoreError:
                    self._svc("restart")
        except CoreError as exc:
            if backup is not None:
                try:
                    shutil.copy2(backup, get_settings().xray_config)
                    self._svc("restart")
                    log.warning("activation failed; restored previous config")
                except (OSError, CoreError) as rb_exc:
                    log.error("rollback failed: %s", type(rb_exc).__name__)
            raise CoreError(f"activation failed, previous config restored: {exc}") from exc

    def status(self) -> dict:
        """Read-only status of the managed proxyhub-xray service.

        Never raises: returns {"active": bool, "state": str} where state is
        one of active|inactive|failed|unknown|missing.
        """
        try:
            proc = subprocess.run(
                ["systemctl", "is-active", "proxyhub-xray"],
                capture_output=True, text=True, timeout=10,
            )
        except FileNotFoundError:
            return {"active": False, "state": "missing"}
        except subprocess.TimeoutExpired:
            return {"active": False, "state": "unknown"}
        except OSError:
            return {"active": False, "state": "unknown"}
        state = (proc.stdout or "").strip().lower() or "unknown"
        if state not in ("active", "inactive", "failed", "activating", "deactivating"):
            state = "unknown"
        return {"active": state == "active", "state": state}


def rebuild_gateway(session) -> dict:
    """Regenerate managed config for the CURRENT active node and restart.

    Used after client/rule/gateway-setting changes (no node switch).
    """
    from ..models import ProxyNode

    active = session.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).first()
    if active is None:
        raise CoreError("no active node to rebuild gateway for")
    XrayAdapter().apply(active, session=session)
    return {"ok": True, "node_id": active.id}


class SingBoxAdapter:
    """Optional managed sing-box instance — only required for Hysteria2 nodes."""

    name = "sing-box"

    def installed(self) -> bool:
        settings = get_settings()
        return Path(settings.singbox_bin).exists() or shutil.which("sing-box") is not None

    def capable(self, node: ProxyNode) -> bool:
        return node.protocol == "hysteria2"

    def generate(self, node: ProxyNode) -> dict:
        if node.protocol != "hysteria2":
            raise CoreError(f"sing-box adapter asked for protocol: {node.protocol}")
        p = node.params or {}
        # Fail closed on unsupported TLS modes rather than silently changing
        # verification semantics: insecure=1 would disable verification, and
        # pinSHA256 requires pinning support we do not implement.
        if str(p.get("insecure", "")).lower() in ("1", "true", "yes"):
            raise CoreError("hysteria2 with insecure TLS is not supported (verification stays on)")
        if p.get("pinSHA256"):
            raise CoreError("hysteria2 with pinSHA256 is not supported")
        hy: dict = {
            "type": "hysteria2",
            "tag": "proxy",
            "server": node.server,
            "server_port": node.port,
            "password": node.secret,
            "tls": {"enabled": True, "server_name": p.get("sni") or node.server},
        }
        if p.get("obfs"):
            hy["obfs"] = {"type": p["obfs"], "password": p.get("obfs_password", "")}
        return {
            "log": {"level": "warn"},
            "inbounds": [
                {"type": "socks", "tag": "socks-in", "listen": "127.0.0.1",
                 "listen_port": get_settings().socks_port},
                {"type": "http", "tag": "http-in", "listen": "127.0.0.1",
                 "listen_port": get_settings().http_port},
            ],
            "outbounds": [hy, {"type": "direct", "tag": "direct"}],
            "route": {"rules": [], "final": "proxy"},
        }

    def write_config(self, node: ProxyNode) -> Path:
        settings = get_settings()
        settings.singbox_config.parent.mkdir(parents=True, exist_ok=True)
        _lock_down(settings.singbox_config.parent)
        _chown_proxyhub(settings.singbox_config.parent)
        tmp = settings.singbox_config.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.generate(node), indent=2), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(settings.singbox_config)
        _chown_proxyhub(settings.singbox_config)
        return settings.singbox_config

    def validate(self) -> None:
        settings = get_settings()
        self._validate_path(settings.singbox_config)

    def _validate_path(self, path: Path) -> None:
        settings = get_settings()
        if not self.installed():
            raise CoreError("sing-box is not installed")
        proc = subprocess.run(
            [settings.singbox_bin, "check", "-c", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            raise CoreError(f"sing-box config invalid: {proc.stderr.strip()[:400]}")

    def apply(self, node: ProxyNode) -> None:
        """Validate the candidate file before replacing the live config.

        On restart failure the previous config is restored, so a bad
        candidate never destroys the last known-good working config.
        """
        settings = get_settings()
        settings.singbox_config.parent.mkdir(parents=True, exist_ok=True)
        _lock_down(settings.singbox_config.parent)
        _chown_proxyhub(settings.singbox_config.parent)
        fd, tmp = tempfile.mkstemp(prefix="singbox-", suffix=".json",
                                   dir=str(settings.singbox_config.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.generate(node), fh, indent=2)
            os.chmod(tmp, 0o600)
            _chown_proxyhub(Path(tmp))
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        candidate = Path(tmp)
        try:
            self._validate_path(candidate)
        except Exception:
            try:
                candidate.unlink()
            except OSError:
                pass
            raise
        backup: Path | None = None
        if settings.singbox_config.exists():
            backup = settings.singbox_config.with_suffix(".prev")
            try:
                shutil.copy2(settings.singbox_config, backup)
                backup.chmod(0o600)
            except OSError:
                backup = None
        try:
            candidate.replace(settings.singbox_config)
        except OSError as exc:
            try:
                candidate.unlink()
            except OSError:
                pass
            raise CoreError(f"config replace failed: {type(exc).__name__}")
        _chown_proxyhub(settings.singbox_config)
        proc = subprocess.run(
            ["systemctl", "restart", "proxyhub-singbox"], capture_output=True, text=True, timeout=60
        )
        if proc.returncode != 0:
            if backup is not None:
                try:
                    shutil.copy2(backup, settings.singbox_config)
                    subprocess.run(["systemctl", "restart", "proxyhub-singbox"],
                                   capture_output=True, text=True, timeout=60)
                except OSError:
                    pass
            raise CoreError(f"proxyhub-singbox restart failed: {proc.stderr.strip()[:300]}")

    def status(self) -> dict:
        """Read-only status of the managed proxyhub-singbox service.

        Never raises: returns {"active": bool, "state": str} where state is
        one of active|inactive|failed|unknown|missing|not-installed.
        """
        if not self.installed():
            return {"active": False, "state": "not-installed"}
        try:
            proc = subprocess.run(
                ["systemctl", "is-active", "proxyhub-singbox"],
                capture_output=True, text=True, timeout=10,
            )
        except FileNotFoundError:
            return {"active": False, "state": "missing"}
        except subprocess.TimeoutExpired:
            return {"active": False, "state": "unknown"}
        except OSError:
            return {"active": False, "state": "unknown"}
        state = (proc.stdout or "").strip().lower() or "unknown"
        if state not in ("active", "inactive", "failed", "activating", "deactivating"):
            state = "unknown"
        return {"active": state == "active", "state": state}


def adapter_for(node: ProxyNode):
    if node.protocol in XRAY_CAPABLE:
        return XrayAdapter()
    if node.protocol == "hysteria2":
        return SingBoxAdapter()
    raise CoreError(f"no core adapter for protocol: {node.protocol}")


@dataclasses.dataclass
class EgressResult:
    """Outcome of a genuine per-node egress verification.

    ok=True means an HTTP request reached the test URL *through this node's
    own outbound*. Any other outcome leaves the node's verified-egress state
    untouched by the caller.
    """

    ok: bool
    app_latency_ms: float | None = None
    error: str = ""
    error_type: str = ""  # timeout|tls_error|reset|connect_error|http_error|unverified|internal


class EphemeralXray:
    """Temporary Xray instance routing a single node, for egress verification.

    Binds 127.0.0.1 on an OS-assigned port; config lives in a 0600 temp file
    next to the managed config and is always removed. Never touches the
    running managed core or its configuration file.
    """

    def __init__(self, node: ProxyNode) -> None:
        builder = XRAY_BUILDERS.get(node.protocol)
        if builder is None:
            raise CoreError(f"ephemeral check cannot handle protocol: {node.protocol}")
        self._node = node
        self._builder = builder
        self._proc: subprocess.Popen | None = None
        self._cfg_path: Path | None = None
        self.port: int = 0

    def _free_port(self) -> int:
        import socket as _socket

        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def __enter__(self) -> str:
        settings = get_settings()
        if not shutil.which(settings.xray_bin) and not Path(settings.xray_bin).exists():
            raise CoreError(f"xray binary not found: {settings.xray_bin}")
        self.port = self._free_port()
        cfg = {
            "log": {"loglevel": "warning"},
            "inbounds": [
                {"listen": "127.0.0.1", "port": self.port, "protocol": "socks",
                 "settings": {"udp": True}, "tag": "verify-in"},
            ],
            "outbounds": [
                self._builder(self._node),
                {"protocol": "freedom", "tag": "direct"},
            ],
        }
        settings.xray_config.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="verify-", suffix=".json",
                                   dir=str(settings.xray_config.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh)
            os.chmod(tmp, 0o600)
            self._cfg_path = Path(tmp)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        check = subprocess.run(
            [settings.xray_bin, "run", "-test", "-config", tmp],
            capture_output=True, text=True, timeout=30,
        )
        if check.returncode != 0:
            self._cleanup()
            raise CoreError(f"verify config invalid: {check.stderr.strip()[:200]}")
        self._proc = subprocess.Popen(
            [settings.xray_bin, "run", "-config", tmp],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 8.0
        import socket as _socket

        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                self._cleanup()
                raise CoreError("verify xray exited during startup")
            try:
                with _socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return f"socks5h://127.0.0.1:{self.port}"
            except OSError:
                time.sleep(0.2)
        self._cleanup()
        raise CoreError("verify xray did not listen in time")

    def __exit__(self, *exc_info) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        if self._proc is not None:
            proc, self._proc = self._proc, None
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
            except OSError:
                pass
        if self._cfg_path is not None:
            path, self._cfg_path = self._cfg_path, None
            try:
                path.unlink()
            except OSError:
                pass


def verify_node_egress(node: ProxyNode, test_url: str, timeout: int = 15) -> EgressResult:
    """Genuinely verify one node's egress via an isolated ephemeral Xray.

    Never raises for probe failures (returns EgressResult); raises CoreError
    only for local environment problems (missing binary), which callers treat
    as 'unverified'. No secrets are included in error strings.
    """
    import time as _time

    started = _time.monotonic()
    try:
        with EphemeralXray(node) as proxy_url:
            try:
                import httpx as _httpx

                with _httpx.Client(proxy=proxy_url, timeout=timeout) as client:
                    resp = client.get(test_url)
                    resp.raise_for_status()
                    ms = round((_time.monotonic() - started) * 1000, 1)
                    return EgressResult(ok=True, app_latency_ms=ms)
            except Exception as exc:  # noqa: BLE001 - mapped below, never re-raised
                name = type(exc).__name__
                text = str(exc)
                if "Timeout" in name or "timeout" in text.lower():
                    return EgressResult(ok=False, error="http_timeout", error_type="timeout")
                if "SSL" in text or "TLS" in text:
                    return EgressResult(ok=False, error="tls_error", error_type="tls_error")
                if "reset" in text.lower():
                    return EgressResult(ok=False, error="reset", error_type="reset")
                if "HTTPStatusError" in name or "Status" in name:
                    return EgressResult(ok=False, error="http_error", error_type="http_error")
                return EgressResult(ok=False, error=name, error_type="connect_error")
    except CoreError as exc:
        return EgressResult(ok=False, error=str(exc)[:200], error_type="unverified")


def system_xray_status() -> dict:
    """Read-only status of the pre-existing system Xray (never managed by ProxyHub)."""
    proc = subprocess.run(
        ["systemctl", "is-active", "xray"], capture_output=True, text=True, timeout=10
    )
    return {"active": proc.stdout.strip() == "active", "state": proc.stdout.strip()}
