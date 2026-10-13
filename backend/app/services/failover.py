"""Autonomous Failover: quality-based, hysteresis-guarded, circuit-breaker protected.

Eligibility policy (documented, enforced by node_eligibility):
  1. enabled and not removed_upstream
  2. circuit breaker closed
  3. health == "healthy" (never "unknown": unprobed nodes are ineligible)
  4. verified egress: egress_ok is True AND last_success is fresh
     (within egress_freshness_seconds, default 1800)
  5. quality_score >= minimum (default 30)
A candidate that fails a fresh pre-switch verification is skipped (never
selected blindly). When no candidate verifies, the active node is preserved
and the failure is reported — never a silent unverified switch.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from ..config import get_settings
from ..models import ProxyNode
from .cores import XRAY_CAPABLE, adapter_for

log = logging.getLogger("proxyhub.failover")

# Single coordinated transition mechanism: every active-node change
# (failover, scheduled rotation, manual API activation) funnels through
# activate_node(), which holds this lock. threading.Lock is used because
# callers run in both asyncio tasks (via to_thread) and FastAPI threadpool
# workers; the critical section is short except for the core restart itself,
# during which competing transitions must wait rather than interleave.
transition_lock = threading.Lock()

MIN_QUALITY_DEFAULT = 30.0
EGRESS_FRESHNESS_DEFAULT = 1800


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def egress_freshness_seconds(session) -> int:
    try:
        return max(60, int(session_get(session, "egress_freshness_seconds",
                                      str(EGRESS_FRESHNESS_DEFAULT))))
    except (TypeError, ValueError):
        return EGRESS_FRESHNESS_DEFAULT


def node_eligibility(node: ProxyNode, now: datetime, freshness: int,
                     min_quality: float = MIN_QUALITY_DEFAULT) -> tuple[bool, str]:
    """Explicit eligibility check. Returns (eligible, reason)."""
    if not node.enabled:
        return False, "disabled"
    if node.removed_upstream:
        return False, "removed upstream"
    if node.circuit_open:
        return False, "circuit open"
    if (node.health or "unknown") != "healthy":
        return False, f"health={node.health or 'unknown'} (needs verified healthy)"
    if not node.egress_ok:
        return False, "no verified egress"
    last_ok = _aware(node.last_success)
    if last_ok is None or (now - last_ok).total_seconds() > freshness:
        return False, "stale egress verification"
    if (node.quality_score or 0.0) < min_quality:
        return False, f"quality {node.quality_score} below minimum {min_quality}"
    return True, "eligible"


def eligible_candidates(session, exclude_id: int | None = None,
                        freshness: int | None = None,
                        min_quality: float = MIN_QUALITY_DEFAULT
                        ) -> tuple[list[ProxyNode], dict[str, int]]:
    """All eligible non-active nodes, best-first, plus rejection tallies."""
    if freshness is None:
        freshness = egress_freshness_seconds(session)
    now = datetime.now(timezone.utc)
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
    eligible: list[ProxyNode] = []
    rejected: dict[str, int] = {}
    for node in q.all():
        ok, reason = node_eligibility(node, now, freshness, min_quality)
        if ok:
            eligible.append(node)
        else:
            rejected[reason] = rejected.get(reason, 0) + 1
    # Deprioritize (never exclude) the most recently failed node to break
    # A->B->A oscillation when several nodes are degraded at once.
    penalized_id: int | None = None
    try:
        raw = session_get(session, "last_failed_node_id", "")
        at = session_get(session, "last_failed_at", "")
        if raw and at:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(at)).total_seconds()
            if age < 3600:
                penalized_id = int(raw)
    except (ValueError, TypeError):
        penalized_id = None
    eligible.sort(key=lambda n: (
        1 if (penalized_id is not None and n.id == penalized_id) else 0,
        -(n.quality_score or 0.0), n.rank, n.id))
    return eligible, rejected


def pick_failover_candidate(session, exclude_id: int | None = None) -> ProxyNode | None:
    eligible, _rejected = eligible_candidates(session, exclude_id=exclude_id)
    return eligible[0] if eligible else None


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

    freshness = egress_freshness_seconds(session)
    eligible, rejected = eligible_candidates(session, exclude_id=active.id,
                                             freshness=freshness)
    if not eligible:
        detail = ", ".join(f"{k}:{v}" for k, v in sorted(rejected.items())) or "none"
        return {"switched": False,
                "reason": f"no eligible candidate (rejected: {detail})"}

    from .cores import CoreError

    failures: list[str] = []
    for candidate in eligible:
        ok, detail = attempt_transition(
            session, candidate, active, actor="failover", audit=audit,
            reason_kind="failover")
        if ok:
            failed_id = active.id
            now_iso = datetime.now(timezone.utc).isoformat()
            session_set(session, "last_failover_at", now_iso)
            session_set(session, "last_failover_reason", detail)
            session_set(session, "last_failed_node_id", str(failed_id))
            session_set(session, "last_failed_at", now_iso)
            return {"switched": True, "reason": detail, "node_id": candidate.id}
        failures.append(detail)
    return {"switched": False,
            "reason": "no eligible candidate (all failed verification: "
                      + "; ".join(failures[:5]) + ")"}


def attempt_transition(session, candidate: ProxyNode, active: ProxyNode | None,
                       actor: str, audit, reason_kind: str) -> tuple[bool, str]:
    """Shared verify-then-activate transition used by failover, rotation and API.

    Verifies the candidate through its OWN outbound, then activates under
    transition_lock. Returns (switched, reason). Never leaves a false active
    record: activate_node commits only after the core accepts the config.
    """
    from .cores import CoreError, verify_node_egress

    settings = get_settings()
    test_url = getattr(settings, "health_url", "https://api.ipify.org")
    timeout = int(getattr(settings, "health_timeout", 15))
    try:
        eg = verify_node_egress(candidate, test_url, timeout)
    except Exception as exc:  # never let verification crash the caller
        eg = None
        log.warning("pre-switch verification error for node %s: %s",
                    candidate.id, type(exc).__name__)
    if eg is None or not eg.ok:
        candidate.egress_ok = False
        candidate.consecutive_failures = (candidate.consecutive_failures or 0) + 1
        candidate.consecutive_successes = 0
        candidate.last_error = (eg.error if eg else "verification error")[:500]
        session.commit()
        return False, f"#{candidate.id} unverified ({candidate.last_error})"
    candidate.egress_ok = True
    candidate.consecutive_failures = 0
    candidate.consecutive_successes = (candidate.consecutive_successes or 0) + 1
    candidate.last_success = datetime.now(timezone.utc)
    session.commit()
    prev_name = active.name if active is not None else "none"
    reason = (f"{reason_kind}: {prev_name} -> {candidate.name} "
              f"(q={candidate.quality_score}, verified egress {eg.app_latency_ms}ms)")
    try:
        activate_node(session, candidate, actor=actor, audit=audit, reason=reason)
    except (ValueError, CoreError) as exc:
        errmsg = str(exc)[:300]
        candidate.egress_ok = False
        session.commit()
        if audit:
            audit(actor, "node.activate.failed", f"{candidate.name}: {errmsg}")
        log.warning("%s activation failed for node %s: %s",
                    reason_kind, candidate.id, errmsg)
        return False, f"#{candidate.id} activation failed ({errmsg})"
    return True, reason


def check_gateway_compatible(session, node: ProxyNode) -> None:
    """Central protocol/gateway compatibility gate. Raises ValueError.

    Must run BEFORE any database mutation, config write or core restart, on
    every activation path (failover, rotation, manual API).
    """
    if node.protocol in XRAY_CAPABLE:
        return
    from ..models import Setting

    row = session.query(Setting).filter(Setting.key == "gateway_enabled").first()
    enabled = (row.value if row else str(get_settings().gateway_enabled)).lower()
    if enabled in ("1", "true", "yes", "on"):
        raise ValueError(
            f"node protocol {node.protocol} is incompatible with the enabled "
            "gateway (gateway requires an Xray protocol: vless/vmess/trojan/ss)")


def activate_node(session, node: ProxyNode, actor: str = "user", audit=None, reason: str = "") -> None:
    """Single coordinated transition mechanism for ALL active-node changes.

    Serialized by transition_lock. Validates (incl. gateway compatibility)
    before mutating anything; the database is only updated after the core
    accepts the new configuration, so a failed activation never leaves a
    false active record.
    """
    with transition_lock:
        if node.removed_upstream:
            raise ValueError("node was removed upstream and cannot be activated")
        if not node.enabled:
            raise ValueError("node is disabled")
        if node.circuit_open:
            raise ValueError("node circuit breaker is open")
        check_gateway_compatible(session, node)
        adapter = adapter_for(node)
        name = type(adapter).__name__
        if name == "SingBoxAdapter" and not adapter.installed():
            raise ValueError("node needs sing-box which is not installed (see System page)")
        adapter.apply(node, session=session)
        # Re-check nothing else switched nodes while we were applying.
        prev = session.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).first()
        session.query(ProxyNode).update({ProxyNode.is_active: False})
        fresh = session.query(ProxyNode).filter(ProxyNode.id == node.id).first()
        if fresh is None or fresh.removed_upstream or not fresh.enabled:
            raise ValueError("node became ineligible during activation")
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