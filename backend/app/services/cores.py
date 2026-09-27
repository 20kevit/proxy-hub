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


class XrayAdapter:
    """Generates/validates the managed Xray config and controls proxyhub-xray.service."""

    name = "xray"

    def capable(self, node: ProxyNode) -> bool:
        return node.protocol in XRAY_CAPABLE

    def generate(self, node: ProxyNode) -> dict:
        builder = XRAY_BUILDERS.get(node.protocol)
        if builder is None:
            raise CoreError(f"xray cannot handle protocol: {node.protocol}")
        settings = get_settings()
        return {
            "log": {"loglevel": "warning"},
            "inbounds": [
                {"listen": "127.0.0.1", "port": settings.socks_port,
                 "protocol": "socks", "settings": {"udp": True}, "tag": "socks-in"},
                {"listen": "127.0.0.1", "port": settings.http_port,
                 "protocol": "http", "tag": "http-in"},
            ],
            "outbounds": [
                builder(node),
                {"protocol": "freedom", "tag": "direct"},
                {"protocol": "blackhole", "tag": "block"},
            ],
            "routing": {
                "domainStrategy": "AsIs",
                "rules": [
                    {"type": "field",
                     "ip": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"],
                     "outboundTag": "direct"},
                    {"type": "field", "port": "0-65535", "outboundTag": "proxy"},
                ],
            },
        }

    def write_config(self, node: ProxyNode) -> Path:
        settings = get_settings()
        cfg = self.generate(node)
        settings.xray_config.parent.mkdir(parents=True, exist_ok=True)
        tmp = settings.xray_config.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        tmp.replace(settings.xray_config)
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

    def apply(self, node: ProxyNode) -> None:
        self.write_config(node)
        self.validate()
        try:
            self._svc("restart")
        except CoreError:
            self._svc("start")

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
        tmp = settings.singbox_config.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.generate(node), indent=2), encoding="utf-8")
        tmp.replace(settings.singbox_config)
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
