"""Autonomous Failover: quality-based, hysteresis-guarded, circuit-breaker protected."""
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
            ProxyNode.circuit_open.is_(False),
            ProxyNode.is_active.is_(False),
        )
    )
    if exclude_id is not None:
        q = q.filter(ProxyNode.id != exclude_id)
    candidates = q.all()
    if not candidates:
        return None
    candidates.sort(key=lambda n: (-n.quality_score, n.rank, n.id))
    return candidates[0]


def maybe_failover(session, active: ProxyNode, audit) -> dict:
    settings = get_settings()
    mode = session_get(session, "failover_mode", settings.failover_mode)
    if mode != "auto":
        return {"switched": False, "reason": "manual mode"}

    threshold = int(session_get(session, "failover_threshold", "3"))
    cooldown = int(session_get(session, "failover_cooldown", "600"))
    min_healthy_duration = int(session_get(session, "failover_min_healthy", "30"))

    if active.consecutive_failures < threshold:
        return {"switched": False, "reason": f"consecutive_failures {active.consecutive_failures}/{threshold}"}

    last_sw = session_get(session, "last_failover_at", "")
    if last_sw:
        try:
            ago = (datetime.now(timezone.utc) - datetime.fromisoformat(last_sw)).total_seconds()
            if ago < cooldown:
                return {"switched": False, "reason": f"cooldown {int(cooldown - ago)}s left"}
        except ValueError:
            pass

    if active.last_state_change:
        try:
            healthy_for = (datetime.now(timezone.utc) - active.last_state_change).total_seconds()
            if healthy_for < min_healthy_duration:
                return {"switched": False, "reason": f"min_healthy {int(min_healthy_duration - healthy_for)}s left"}
        except (ValueError, TypeError):
            pass

    candidate = pick_failover_candidate(session, exclude_id=active.id)
    if candidate is None:
        return {"switched": False, "reason": "no healthy candidate"}

    if candidate.quality_score < 30:
        return {"switched": False, "reason": f"candidate quality too low: {candidate.quality_score}"}

    reason = f"failover: {active.name} (q={active.quality_score}) failed {active.consecutive_failures}x -> {candidate.name} (q={candidate.quality_score})"
    activate_node(session, candidate, actor="failover", audit=audit, reason=reason)
    session_set(session, "last_failover_at", datetime.now(timezone.utc).isoformat())
    session_set(session, "last_failover_reason", reason)
    return {"switched": True, "reason": reason, "node_id": candidate.id}


def activate_node(session, node: ProxyNode, actor: str = "user", audit=None, reason: str = "") -> None:
    if node.removed_upstream:
        raise ValueError("node was removed upstream and cannot be activated")
    if not node.enabled:
        raise ValueError("node is disabled")
    if node.circuit_open:
        raise ValueError("node circuit breaker is open")
    adapter = adapter_for(node)
    name = type(adapter).__name__
    if name == "SingBoxAdapter" and not adapter.installed():
        raise ValueError("node needs sing-box which is not installed (see System page)")
    adapter.apply(node, session=session)
    prev = session.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).first()
    session.query(ProxyNode).update({ProxyNode.is_active: False})
    node.is_active = True
    node.consecutive_failures = 0
    node.consecutive_successes = 0
    node.last_state_change = datetime.now(timezone.utc)
    session_set(session, "active_node_id", str(node.id))
    if prev is not None and prev.id != node.id:
        session_set(session, "last_prev_node", f"{prev.name} (#{prev.id})")
    session_set(session, "active_core", name.replace("Adapter", "").lower())
    session.commit()
    if audit:
        audit(actor, "node.activate", f"{node.name} via {name}" + (f" ({reason})" if reason else ""))
    log.info("activated node %s (%s) quality=%.1f", node.id, node.protocol, node.quality_score)


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


def get_failover_config(session) -> dict:
    settings = get_settings()
    return {
        "mode": session_get(session, "failover_mode", settings.failover_mode),
        "threshold": int(session_get(session, "failover_threshold", str(settings.failover_threshold))),
        "cooldown": int(session_get(session, "failover_cooldown", str(settings.failover_cooldown))),
        "min_healthy": int(session_get(session, "failover_min_healthy", "30")),
    }


def set_failover_config(session, **kwargs) -> dict:
    allowed = {"failover_mode", "failover_threshold", "failover_cooldown", "failover_min_healthy"}
    for key, value in kwargs.items():
        if key in allowed and value is not None:
            if key == "failover_mode" and value not in ("auto", "manual"):
                raise ValueError("failover_mode must be auto|manual")
            session_set(session, key, str(value))
    return get_failover_config(session)