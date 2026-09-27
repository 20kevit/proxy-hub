"""Subscription engine: fetch -> decode -> detect -> parse -> normalize -> dedupe -> validate -> persist."""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone

import httpx

from ..config import get_settings
from ..models import ProxyNode, SubscriptionState
from .parsers import NodeParseError, ParsedNode, parse_uri, split_subscription_text

log = logging.getLogger("proxyhub.subscription")

SUPPORTED = {"vless", "vmess", "trojan", "ss", "hysteria2"}


def mask_url(url: str) -> str:
    if not url or "://" not in url:
        return "****"
    head, _, tail = url.partition("://")
    host = tail.split("/", 1)[0]
    rest = tail[len(host):]
    if len(rest) > 12:
        rest = rest[:4] + "/************"
    return f"{head}://{host}{rest}"


def _utcnow():
    return datetime.now(timezone.utc)


def fetch_subscription(url: str, timeout: int = 30) -> str:
    try:
        resp = httpx.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "ProxyHub/1.0", "Accept": "text/plain,*/*"},
            follow_redirects=True,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"fetch failed: {type(exc).__name__}") from exc
    text = resp.text
    if not text.strip():
        raise RuntimeError("empty subscription body")
    if len(text) > 5_000_000:
        raise RuntimeError("subscription body too large")
    return text


def parse_subscription_text(text: str) -> tuple[list[ParsedNode], dict]:
    """Returns (nodes, stats). Never raises on individual bad lines."""
    lines = split_subscription_text(text)
    nodes: list[ParsedNode] = []
    seen: set[str] = set()
    stats = {"lines": len(lines), "parsed": 0, "duplicates": 0, "unsupported": 0, "errors": 0}
    for line in lines:
        try:
            node = parse_uri(line)
        except NodeParseError as exc:
            if "unsupported scheme" in str(exc):
                stats["unsupported"] += 1
            else:
                stats["errors"] += 1
            continue
        if node.protocol not in SUPPORTED:
            stats["unsupported"] += 1
            continue
        if not node.server or not (1 <= node.port <= 65535):
            stats["errors"] += 1
            continue
        fp = node.fingerprint()
        if fp in seen:
            stats["duplicates"] += 1
            continue
        seen.add(fp)
        nodes.append(node)
        stats["parsed"] += 1
    return nodes, stats


def sync_nodes(session, nodes: list[ParsedNode], source: str = "subscription") -> dict:
    """Persist normalized nodes; mark vanished subscription nodes as removed_upstream."""
    now = _utcnow()
    fps = {n.fingerprint() for n in nodes}
    existing: dict[str, ProxyNode] = {
        n.fingerprint: n
        for n in session.query(ProxyNode).filter(ProxyNode.source == source).all()
    }
    added = updated = 0
    for node in nodes:
        fp = node.fingerprint()
        row = existing.get(fp)
        if row is None:
            session.add(
                ProxyNode(
                    fingerprint=fp,
                    name=node.name or f"{node.protocol} {node.server}:{node.port}",
                    protocol=node.protocol,
                    server=node.server,
                    port=node.port,
                    transport=node.transport,
                    security=node.security,
                    secret=node.secret,
                    params=node.params,
                    source=source,
                    removed_upstream=False,
                    last_seen=now,
                )
            )
            added += 1
        else:
            row.removed_upstream = False
            row.last_seen = now
            if node.name and row.name != node.name and row.source == source and not row.name:
                row.name = node.name
            updated += 1
    removed = 0
    for fp, row in existing.items():
        if fp not in fps and not row.removed_upstream:
            row.removed_upstream = True
            if row.is_active:
                row.is_active = False  # never keep a deleted node active
            removed += 1
    session.commit()
    return {"added": added, "updated": updated, "removed": removed, "total": len(nodes)}


def update_subscription(session, url: str | None = None, timeout: int = 30) -> dict:
    """Full pipeline with history tracking. Keeps last-known-good on failure."""
    settings = get_settings()
    url = url or settings.subscription_url
    if not url:
        raise RuntimeError("subscription URL is not configured")
    state = session.query(SubscriptionState).first()
    if state is None:
        state = SubscriptionState(update_interval=settings.sub_update_interval)
        session.add(state)
        session.commit()
    state.last_attempt_at = _utcnow()
    session.commit()
    started = time.monotonic()
    try:
        text = fetch_subscription(url, timeout=timeout)
        nodes, stats = parse_subscription_text(text)
        result = sync_nodes(session, nodes)
    except Exception as exc:
        state.last_error = str(exc)[:500]
        session.commit()
        log.warning("subscription update failed: %s", mask_url(url))
        raise
    state.last_ok_at = _utcnow()
    state.last_error = ""
    state.node_count = result["total"]
    state.url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
    session.commit()
    elapsed = round(time.monotonic() - started, 2)
    log.info("subscription update ok: %s in %ss", result, elapsed)
    return {**result, **stats, "elapsed_s": elapsed}
