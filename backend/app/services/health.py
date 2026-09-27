"""Health checking: TCP dial latency for every node + full egress test for the active node.

No credentials are logged. Test endpoint is configurable (default: https://api.ipify.org).
"""
from __future__ import annotations

import logging
import socket
import time
from datetime import datetime, timezone

import httpx

from ..config import get_settings
from ..models import ProxyNode

log = logging.getLogger("proxyhub.health")


def tcp_latency(host: str, port: int, timeout: float = 8.0) -> tuple[bool, float, str]:
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, round((time.monotonic() - started) * 1000, 1), ""
    except OSError as exc:
        return False, 0.0, f"{type(exc).__name__}"


def egress_ip_via(proxy_url: str, test_url: str, timeout: int) -> tuple[bool, str, float]:
    started = time.monotonic()
    try:
        with httpx.Client(proxy=proxy_url, timeout=timeout) as client:
            resp = client.get(test_url)
            resp.raise_for_status()
            body = resp.text.strip()
            return True, body[:64], round((time.monotonic() - started) * 1000, 1)
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}", 0.0


def check_node(session, node: ProxyNode, full_egress: bool = False,
               socks_proxy: str = "") -> ProxyNode:
    settings = get_settings()
    ok, latency, err = tcp_latency(node.server, node.port)
    if ok and full_egress and socks_proxy:
        egress_ok, egress_info, egress_ms = egress_ip_via(
            socks_proxy, settings.health_url, settings.health_timeout
        )
        if egress_ok:
            latency = egress_ms
            err = ""
            node.egress_ok = True
        else:
            ok, err = False, f"egress: {egress_info}"
            node.egress_ok = False
    node.latency_ms = latency if ok else None
    node.last_checked = datetime.now(timezone.utc)
    node.last_error = "" if ok else err[:500]
    if ok:
        node.health = "healthy"
        node.fail_count = 0
    else:
        node.fail_count = (node.fail_count or 0) + 1
        node.health = "unhealthy"
    session.commit()
    return node


def check_all(session, limit: int = 200, egress_for_active: bool = True) -> dict:
    """Check enabled, non-removed nodes. Active node additionally gets an egress test."""
    from .cores import managed_socks_url  # local import: avoid cycle

    settings = get_settings()
    nodes = (
        session.query(ProxyNode)
        .filter(ProxyNode.enabled.is_(True), ProxyNode.removed_upstream.is_(False))
        .order_by(ProxyNode.id)
        .limit(limit)
        .all()
    )
    active = session.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).first()
    healthy = unhealthy = 0
    try:
        socks = managed_socks_url() if egress_for_active else ""
    except Exception:
        socks = ""
    for node in nodes:
        try:
            check_node(
                session, node,
                full_egress=egress_for_active and active is not None and node.id == active.id,
                socks_proxy=socks,
            )
        except Exception as exc:  # one bad node must not stop the sweep
            log.warning("health check failed for node %s: %s", node.id, type(exc).__name__)
            continue
        if node.health == "healthy":
            healthy += 1
        else:
            unhealthy += 1
    return {"checked": len(nodes), "healthy": healthy, "unhealthy": unhealthy}
