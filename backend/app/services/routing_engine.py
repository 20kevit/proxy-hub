"""Traffic routing engine: validation, matching, simulation, Xray compilation.

Semantics (documented, deterministic):
- Rules evaluate by (priority ASC, id ASC); first match wins.
- domain_exact: 'telegram.org' matches only telegram.org.
- domain_suffix: '*.telegram.org' matches telegram.org AND *.telegram.org.
- domain_wildcard: '*' matches any char sequence ('*.google.*' etc.), anchored.
- ip: exact address. cidr: prefix membership (ipaddress module).
- port: '443' or '80-443' as the rule's primary match; any rule may ALSO carry
  an optional port/network constraint (e.g. SSH stays direct only for TCP/22).
- client_scope 'all' or a ProxyClient username (enforced via dedicated inbound).
- No rule matches -> DEFAULT route (configurable: direct|proxy|block).

Xray has no per-user matcher for SOCKS accounts (verified empirically), so
client-scoped rules compile with inboundTag pointing at that client's
dedicated SOCKS inbound. One table, no duplication.
"""
from __future__ import annotations

import ipaddress
import re

MATCH_TYPES = ("domain_exact", "domain_suffix", "domain_wildcard", "ip", "cidr", "port")
ACTIONS = ("direct", "proxy", "block")
NETWORKS = ("any", "tcp", "udp")

FA_PORT_RE = re.compile(r"^\d{1,5}(-\d{1,5})?$")
FA_HOST_LABEL = re.compile(r"^[A-Za-z0-9*_-]+$")
FA_IP = re.compile(r"^[0-9a-fA-F.:]+$")


class RuleError(ValueError):
    pass


def norm_domain(value: str) -> str:
    v = value.strip().lower().rstrip(".")
    try:
        v = v.encode("idna").decode("ascii")
    except Exception:
        pass
    return v


def validate_rule(match_type: str, pattern: str, port: str = "",
                  network: str = "any", action: str = "") -> tuple[str, str]:
    """Returns (normalized_pattern, normalized_port). Raises RuleError (Persian msg)."""
    if match_type not in MATCH_TYPES:
        raise RuleError("نوع تطبیق نامعتبر است.")
    if action not in ACTIONS:
        raise RuleError("عمل نامعتبر است (DIRECT/PROXY/BLOCK).")
    if network not in NETWORKS:
        raise RuleError("پروتکل نامعتبر است.")
    p = pattern.strip()
    if not p:
        raise RuleError("الگو خالی است.")
    if match_type == "domain_exact":
        p = norm_domain(p)
        if "*" in p or not re.fullmatch(r"[a-z0-9_.-]+", p) or "." not in p:
            raise RuleError("دامنه‌ی دقیق نامعتبر است (مثال: telegram.org).")
    elif match_type == "domain_suffix":
        if not p.startswith("*."):
            raise RuleError("پسوند دامنه باید با *. شروع شود (مثال: *.telegram.org).")
        base = norm_domain(p[2:])
        if "*" in base or not re.fullmatch(r"[a-z0-9_.-]+", base) or "." not in base:
            raise RuleError("پسوند دامنه نامعتبر است.")
        p = "*." + base
    elif match_type == "domain_wildcard":
        p = norm_domain(p)
        if "*" not in p:
            raise RuleError("الگوی wildcard باید حداقل یک * داشته باشد.")
        if not all(FA_HOST_LABEL.match(label) for label in p.split(".")):
            raise RuleError("الگوی wildcard نامعتبر است.")
    elif match_type == "ip":
        try:
            p = str(ipaddress.ip_address(p))
        except ValueError:
            raise RuleError("آدرس IP نامعتبر است.")
    elif match_type == "cidr":
        try:
            p = str(ipaddress.ip_network(p, strict=False))
        except ValueError:
            raise RuleError("CIDR نامعتبر است (مثال: 10.0.0.0/8).")
    elif match_type == "port":
        if not FA_PORT_RE.match(p):
            raise RuleError("پورت نامعتبر است (مثال: 443 یا 80-443).")
        lo, _, hi = p.partition("-")
        lo_i, hi_i = int(lo), int(hi or lo)
        if not (1 <= lo_i <= 65535 and 1 <= hi_i <= 65535 and lo_i <= hi_i):
            raise RuleError("بازه‌ی پورت خارج از محدوده است.")
        p = str(lo_i) if lo_i == hi_i else f"{lo_i}-{hi_i}"
    port = port.strip()
    if port:
        if not FA_PORT_RE.match(port):
            raise RuleError("قید پورت نامعتبر است.")
        lo, _, hi = port.partition("-")
        lo_i, hi_i = int(lo), int(hi or lo)
        if not (1 <= lo_i <= 65535 and 1 <= hi_i <= 65535 and lo_i <= hi_i):
            raise RuleError("بازه‌ی قید پورت خارج از محدوده است.")
        port = str(lo_i) if lo_i == hi_i else f"{lo_i}-{hi_i}"
    return p, port


def _wildcard_re(pattern: str) -> re.Pattern:
    return re.compile("^" + re.escape(pattern).replace(r"\*", ".*") + "$", re.IGNORECASE)


def _port_in(constraint: str, port: int | None) -> bool:
    if not constraint:
        return True
    if port is None:
        return False
    lo, _, hi = constraint.partition("-")
    return int(lo) <= port <= int(hi or lo)


def rule_matches(rule, domain: str = "", ip: str = "",
                 port: int | None = None, network: str = "any",
                 client: str = "all") -> bool:
    if not rule.enabled:
        return False
    if rule.client_scope != "all" and rule.client_scope != client:
        return False
    if rule.network != "any" and network != "any" and rule.network != network:
        return False
    if not _port_in(rule.port or "", port):
        return False
    mt, pat = rule.match_type, rule.pattern
    if mt == "domain_exact":
        return bool(domain) and norm_domain(domain) == pat
    if mt == "domain_suffix":
        if not domain:
            return False
        d = norm_domain(domain)
        base = pat[2:]
        return d == base or d.endswith("." + base)
    if mt == "domain_wildcard":
        return bool(domain) and _wildcard_re(pat).match(norm_domain(domain)) is not None
    if mt == "ip":
        if not ip:
            return False
        try:
            return ipaddress.ip_address(ip) == ipaddress.ip_address(pat)
        except ValueError:
            return False
    if mt == "cidr":
        if not ip:
            return False
        try:
            return ipaddress.ip_address(ip) in ipaddress.ip_network(pat)
        except ValueError:
            return False
    if mt == "port":
        return _port_in(pat, port)
    return False


def ordered_rules(session, only_enabled: bool = False):
    from ..models import RouteRule

    q = session.query(RouteRule).order_by(RouteRule.priority, RouteRule.id)
    if only_enabled:
        q = q.filter(RouteRule.enabled.is_(True))
    return q.all()


def simulate(session, domain: str = "", ip: str = "", port: int | None = None,
             network: str = "any", client: str = "all",
             default_route: str = "proxy", active_node_name: str = "") -> dict:
    for rule in ordered_rules(session, only_enabled=True):
        if rule_matches(rule, domain=domain, ip=ip, port=port, network=network, client=client):
            return {
                "decision": rule.action,
                "matched_rule_id": rule.id,
                "matched_rule": f"#{rule.priority} {rule.pattern}",
                "client_scope": rule.client_scope,
                "active_node": active_node_name,
                "is_default": False,
            }
    return {
        "decision": default_route,
        "matched_rule_id": None,
        "matched_rule": "DEFAULT",
        "client_scope": "all",
        "active_node": active_node_name,
        "is_default": True,
    }


def to_xray_rule(rule, default_tag: str = "proxy") -> dict:
    """Compile one RouteRule to an Xray routing rule (field type)."""
    tag = {"direct": "direct", "proxy": "proxy", "block": "block"}[rule.action]
    out: dict = {"type": "field", "outboundTag": tag}
    if rule.client_scope != "all":
        out["inboundTag"] = [f"gw-{rule.client_scope}"]
    mt, pat = rule.match_type, rule.pattern
    if mt == "domain_exact":
        out["domain"] = [f"full:{pat}"]
    elif mt == "domain_suffix":
        out["domain"] = [f"domain:{pat[2:]}"]
    elif mt == "domain_wildcard":
        out["domain"] = [f"regexp:^{re.escape(pat).replace('\\*', '.*')}$"]
    elif mt in ("ip", "cidr"):
        out["ip"] = [pat]
    elif mt == "port":
        out["port"] = pat
    if rule.port and mt != "port":
        out["port"] = rule.port
    if rule.network != "any":
        out["network"] = rule.network
    return out


SEED_RULES: list[tuple[int, str, str, str, str, str, str, str]] = [
    # priority, match_type, pattern, port, network, action, client_scope, note
    (1, "cidr", "10.0.0.0/8", "", "any", "direct", "all", "شبکه‌ی خصوصی"),
    (2, "cidr", "172.16.0.0/12", "", "any", "direct", "all", "شبکه‌ی خصوصی"),
    (3, "cidr", "192.168.0.0/16", "", "any", "direct", "all", "شبکه‌ی خصوصی"),
    (4, "cidr", "127.0.0.0/8", "", "any", "direct", "all", "لوکال‌هاست"),
    (10, "domain_suffix", "*.ir", "", "any", "direct", "all", "دامنه‌های داخلی"),
    (20, "port", "22", "", "tcp", "direct", "all", "SSH همیشه مستقیم"),
]


def ensure_seed_rules(session) -> None:
    from ..models import RouteRule

    if session.query(RouteRule).count():
        return
    for prio, mt, pat, port, net, action, scope, note in SEED_RULES:
        session.add(RouteRule(priority=prio, match_type=mt, pattern=pat, port=port,
                              network=net, action=action, client_scope=scope, note=note))
    session.commit()
