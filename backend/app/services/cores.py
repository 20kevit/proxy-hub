"""Proxy core adapters: XrayAdapter (primary) + SingBoxAdapter (Hysteria2 only).

Managed cores run as SEPARATE instances with their own generated configs, so the
pre-existing system Xray service (/usr/local/etc/xray/config.json) is never touched.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
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
        stream["tlsSettings"] = {"serverName": p.get("sni", "") or node.server}
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
        "streamSettings": {"network": p.get("net", "tcp"), "security": node.security or "none"},
    }


def _trojan_outbound(node: ProxyNode) -> dict:
    return {
        "protocol": "trojan",
        "tag": "proxy",
        "settings": {"servers": [{"address": node.server, "port": node.port, "password": node.secret}]},
        "streamSettings": {"network": "tcp", "security": "tls"},
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
        if not shutil.which(settings.xray_bin) and not Path(settings.xray_bin).exists():
            raise CoreError(f"xray binary not found: {settings.xray_bin}")
        proc = subprocess.run(
            [settings.xray_bin, "run", "-test", "-config", str(settings.xray_config)],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            raise CoreError(f"xray config invalid: {proc.stderr.strip()[:400]}")

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
        
        self.write_config(node, session=session)
        self.validate()
        
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

    def status(self) -> dict:
        proc = subprocess.run(
            ["systemctl", "is-active", "proxyhub-xray"], capture_output=True, text=True, timeout=10
        )
        return {"active": proc.stdout.strip() == "active", "state": proc.stdout.strip()}


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
        hy: dict = {
            "type": "hysteria2",
            "tag": "proxy",
            "server": node.server,
            "server_port": node.port,
            "password": node.secret,
            "tls": {"enabled": True, "server_name": p.get("sni") or node.server},
        }
        if p.get("obfs"):
            hy["obfs"] = {"type": p["obfs"], "password": ""}
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
        if not self.installed():
            raise CoreError("sing-box is not installed")
        proc = subprocess.run(
            [settings.singbox_bin, "check", "-c", str(settings.singbox_config)],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            raise CoreError(f"sing-box config invalid: {proc.stderr.strip()[:400]}")

    def apply(self, node: ProxyNode) -> None:
        self.write_config(node)
        self.validate()
        proc = subprocess.run(
            ["systemctl", "restart", "proxyhub-singbox"], capture_output=True, text=True, timeout=60
        )
        if proc.returncode != 0:
            raise CoreError(f"proxyhub-singbox restart failed: {proc.stderr.strip()[:300]}")


def adapter_for(node: ProxyNode):
    if node.protocol in XRAY_CAPABLE:
        return XrayAdapter()
    if node.protocol == "hysteria2":
        return SingBoxAdapter()
    raise CoreError(f"no core adapter for protocol: {node.protocol}")


def system_xray_status() -> dict:
    """Read-only status of the pre-existing system Xray (never managed by ProxyHub)."""
    proc = subprocess.run(
        ["systemctl", "is-active", "xray"], capture_output=True, text=True, timeout=10
    )
    return {"active": proc.stdout.strip() == "active", "state": proc.stdout.strip()}
