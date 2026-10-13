"""Parsers for common proxy URI schemes -> normalized node dicts.

Supported: vless, vmess, trojan, ss, hysteria2/hy2.
One malformed URI must never break a whole subscription (raise NodeParseError).
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse


class NodeParseError(ValueError):
    pass


@dataclass
class ParsedNode:
    protocol: str
    server: str
    port: int
    name: str = ""
    transport: str = ""
    security: str = ""
    secret: str = ""  # uuid / password / private payload (stored server-side only)
    params: dict = field(default_factory=dict)

    def fingerprint(self) -> str:
        raw = "|".join(
            [
                self.protocol,
                self.server.lower(),
                str(self.port),
                self.transport,
                self.security,
                hashlib.sha256(self.secret.encode()).hexdigest()[:16],
                json.dumps(self.params, sort_keys=True, default=str),
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _b64decode(data: str) -> str:
    s = "".join(data.split())
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s).decode("utf-8", errors="replace")


def _first(qs: dict, key: str, default: str = "") -> str:
    vals = qs.get(key)
    return vals[0] if vals else default


def parse_vless(uri: str) -> ParsedNode:
    u = urlparse(uri)
    if not u.hostname or not u.port:
        raise NodeParseError("vless: missing host/port")
    qs = parse_qs(u.query)
    params = {
        "encryption": _first(qs, "encryption", "none"),
        "flow": _first(qs, "flow"),
        "sni": _first(qs, "sni"),
        "fp": _first(qs, "fp"),
        "pbk": _first(qs, "pbk"),
        "sid": _first(qs, "sid"),
        "spx": _first(qs, "spx"),
        "type": _first(qs, "type", "tcp"),
        "path": _first(qs, "path"),
        "host": _first(qs, "host"),
        "serviceName": _first(qs, "serviceName"),
        "alpn": _first(qs, "alpn"),
    }
    return ParsedNode(
        protocol="vless",
        server=u.hostname,
        port=u.port,
        name=unquote(u.fragment or ""),
        transport=params["type"],
        security=_first(qs, "security"),
        secret=unquote(u.username or ""),
        params={k: v for k, v in params.items() if v},
    )


def parse_vmess(uri: str) -> ParsedNode:
    try:
        payload = json.loads(_b64decode(uri[len("vmess://"):]))
    except Exception as exc:
        raise NodeParseError(f"vmess: bad payload: {exc}") from exc
    if not payload.get("add") or not payload.get("port"):
        raise NodeParseError("vmess: missing address/port")
    try:
        port = int(str(payload["port"]))
    except ValueError as exc:
        raise NodeParseError("vmess: bad port") from exc
    params = {
        "aid": str(payload.get("aid", "0")),
        "net": str(payload.get("net", "tcp")),
        "tls": str(payload.get("tls", "")),
        "sni": str(payload.get("sni", "") or payload.get("host", "")),
        "path": str(payload.get("path", "")),
        "host": str(payload.get("host", "")),
    }
    return ParsedNode(
        protocol="vmess",
        server=str(payload["add"]),
        port=port,
        name=str(payload.get("ps", "")),
        transport=params["net"],
        security="tls" if params["tls"] == "tls" else "",
        secret=str(payload.get("id", "")),
        params={k: v for k, v in params.items() if v},
    )


def parse_trojan(uri: str) -> ParsedNode:
    u = urlparse(uri)
    if not u.hostname or not u.port:
        raise NodeParseError("trojan: missing host/port")
    qs = parse_qs(u.query)
    params = {
        "sni": _first(qs, "sni"),
        "fp": _first(qs, "fp"),
        "type": _first(qs, "type", "tcp"),
        "path": _first(qs, "path"),
        "host": _first(qs, "host"),
        "alpn": _first(qs, "alpn"),
    }
    return ParsedNode(
        protocol="trojan",
        server=u.hostname,
        port=u.port,
        name=unquote(u.fragment or ""),
        transport=params["type"],
        security="tls",
        secret=unquote(u.username or ""),
        params={k: v for k, v in params.items() if v},
    )


def parse_ss(uri: str) -> ParsedNode:
    body = uri[len("ss://"):]
    fragment = ""
    if "#" in body:
        body, fragment = body.split("#", 1)
    if "@" not in body:
        try:
            body = _b64decode(body)
        except Exception as exc:
            raise NodeParseError(f"ss: bad payload: {exc}") from exc
    else:
        head, _, tail = body.partition("@")
        if ":" not in tail:
            try:
                decoded = _b64decode(body)
                if "@" in decoded and ":" in decoded:
                    body = decoded
                    head, _, tail = body.partition("@")
            except Exception as exc:
                raise NodeParseError(f"ss: bad payload: {exc}") from exc
        if ":" not in (head or "") or head and len(head) < 3 and "=" not in head:
            pass
        try:
            if ":" not in head and head:
                head = _b64decode(head)
        except Exception:
            pass
        body = f"{head}@{tail}"
    try:
        userinfo, _, hostport = body.rpartition("@")
        method, _, password = userinfo.partition(":")
        host, _, port_s = hostport.partition(":")
        port = int(port_s)
    except Exception as exc:
        raise NodeParseError(f"ss: bad address: {exc}") from exc
    if not host or not method:
        raise NodeParseError("ss: missing host/method")
    return ParsedNode(
        protocol="ss",
        server=host,
        port=port,
        name=unquote(fragment or ""),
        transport="tcp",
        security="",
        secret=password,
        params={"method": method},
    )


def parse_hysteria2(uri: str) -> ParsedNode:
    u = urlparse(uri)
    if not u.hostname or not u.port:
        raise NodeParseError("hysteria2: missing host/port")
    qs = parse_qs(u.query)
    params = {
        "obfs": _first(qs, "obfs"),
        "obfs_password": _first(qs, "obfs-password") or _first(qs, "obfs_password"),
        "sni": _first(qs, "sni") or u.hostname,
        "insecure": _first(qs, "insecure"),
        "pinSHA256": _first(qs, "pinSHA256"),
    }
    return ParsedNode(
        protocol="hysteria2",
        server=u.hostname,
        port=u.port,
        name=unquote(u.fragment or ""),
        transport="udp",
        security="tls",
        secret=unquote(u.username or ""),
        params={k: v for k, v in params.items() if v},
    )


SCHEME_PARSERS = {
    "vless": parse_vless,
    "vmess": parse_vmess,
    "trojan": parse_trojan,
    "ss": parse_ss,
    "shadowsocks": parse_ss,
    "hysteria2": parse_hysteria2,
    "hy2": parse_hysteria2,
}


def parse_uri(uri: str) -> ParsedNode:
    scheme = uri.split("://", 1)[0].strip().lower() if "://" in uri else ""
    parser = SCHEME_PARSERS.get(scheme)
    if parser is None:
        raise NodeParseError(f"unsupported scheme: {scheme or '?'}")
    return parser(uri.strip())


def looks_like_base64(text: str) -> bool:
    s = "".join(text.split())
    if len(s) < 8 or len(s) % 4 != 0:
        return False
    try:
        base64.b64decode(s, validate=True)
        decoded = base64.b64decode(s).decode("utf-8")
        return "://" in decoded
    except Exception:
        return False


def split_subscription_text(text: str) -> list[str]:
    """Decode common subscription response formats into candidate URI lines."""
    t = text.strip()
    if not t:
        return []
    if t.startswith("{"):
        # Possibly sing-box / JSON subscription: extract any embedded URIs.
        import re

        uris = re.findall(r"[a-zA-Z0-9+_-]+://[^\s\"'<>]+", t)
        return uris
    if looks_like_base64(t):
        try:
            t = base64.b64decode("".join(t.split())).decode("utf-8", errors="replace")
        except Exception:
            pass
    out: list[str] = []
    for line in t.replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        if "://" not in line:
            # Line itself may be base64-encoded single node (common in some panels).
            try:
                decoded = _b64decode(line)
                if "://" in decoded:
                    line = decoded.strip()
                else:
                    continue
            except Exception:
                continue
        out.append(line)
    return out
