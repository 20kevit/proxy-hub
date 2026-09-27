"""Controlled automatic failover: deterministic, cooldown-guarded, no flapping."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..config import get_settings
from ..models import ProxyNode
from .cores import adapter_for

log = logging.getLogger("proxyhub.failover")


def pick_failover_candidate(session, exclude_id: int | None = None) -> ProxyNode | None:
    q = (
        session.query(ProxyNode)
        .filter(
            ProxyNode.enabled.is_(True),
            ProxyNode.removed_upstream.is_(False),
            ProxyNode.health == "healthy",
            ProxyNode.is_active.is_(False),
        )
    )
    if exclude_id is not None:
        q = q.filter(ProxyNode.id != exclude_id)
    candidates = q.all()
    # Deterministic order: proven egress first, then lowest latency (NULLs last), then id.
    candidates.sort(key=lambda n: (not n.egress_ok, n.latency_ms is None, n.latency_ms or 0, n.id))
    return candidates[0] if candidates else None


def maybe_failover(session, active: ProxyNode, audit) -> dict:
    """Called after a failed health check of the active node.

    Switches only when: mode==auto AND fail_count >= threshold AND cooldown passed.
    Returns a status dict describing what happened.
    """
    settings = get_settings()
    mode = session_get(session, "failover_mode", settings.failover_mode)
    threshold = int(session_get(session, "failover_threshold", str(settings.failover_threshold)))
    cooldown = int(session_get(session, "failover_cooldown", str(settings.failover_cooldown)))
    if mode != "auto":
        return {"switched": False, "reason": "manual mode"}
    if (active.fail_count or 0) < threshold:
        return {"switched": False, "reason": f"fail_count {active.fail_count}/{threshold}"}
    last_sw = session_get(session, "last_failover_at", "")
    if last_sw:
        try:
            ago = (datetime.now(timezone.utc) - datetime.fromisoformat(last_sw)).total_seconds()
            if ago < cooldown:
                return {"switched": False, "reason": f"cooldown {int(cooldown - ago)}s left"}
        except ValueError:
            pass
    candidate = pick_failover_candidate(session, exclude_id=active.id)
    if candidate is None:
        return {"switched": False, "reason": "no healthy candidate"}
    reason = f"failover: {active.name} failed {active.fail_count}x -> {candidate.name}"
    activate_node(session, candidate, actor="failover", audit=audit, reason=reason)
    session_set(session, "last_failover_at", datetime.now(timezone.utc).isoformat())
    session_set(session, "last_failover_reason", reason)
    return {"switched": True, "reason": reason, "node_id": candidate.id}


def activate_node(session, node: ProxyNode, actor: str = "user", audit=None, reason: str = "") -> None:
    if node.removed_upstream:
        raise ValueError("node was removed upstream and cannot be activated")
    if not node.enabled:
        raise ValueError("node is disabled")
    adapter = adapter_for(node)
    name = type(adapter).__name__
    if name == "SingBoxAdapter" and not adapter.installed():
        raise ValueError("node needs sing-box which is not installed (see System page)")
    adapter.apply(node)  # validates + restarts managed core; raises CoreError on failure
    session.query(ProxyNode).update({ProxyNode.is_active: False})
    node.is_active = True
    node.fail_count = 0
    session_set(session, "active_node_id", str(node.id))
    session_set(session, "active_core", name.replace("Adapter", "").lower())
    session.commit()
    if audit:
        audit(actor, "node.activate", f"{node.name} via {name}" + (f" ({reason})" if reason else ""))
    log.info("activated node %s (%s)", node.id, node.protocol)


def session_get(session, key: str, default: str = "") -> str:
    from ..models import Setting

    row = session.query(Setting).filter(Setting.key == key).first()
    return row.value if row else default


def session_set(session, key: str, value: str) -> None:
    from ..models import Setting

    row = session.query(Setting).filter(Setting.key == key).first()
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    session.commit()
