"""Scheduled Lifecycle Manager: automatic node rotation with fairness and safety."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from ..config import get_settings
from ..db import get_session_factory
from ..models import ProxyNode
from .failover import activate_node, pick_failover_candidate

log = logging.getLogger("proxyhub.lifecycle")


class LifecycleManager:
    """Manages scheduled node rotation with fairness and safety constraints."""

    def __init__(self):
        self.settings = get_settings()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._rotation_loop())
        log.info("LifecycleManager started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("LifecycleManager stopped")

    def _get_config(self, db) -> dict:
        """Get rotation configuration from database settings."""
        from .failover import session_get
        return {
            "enabled": session_get(db, "rotation_enabled", "true").lower() in ("1", "true", "yes", "on"),
            "max_duration_hours": int(session_get(db, "rotation_max_duration_hours", "6")),
            "exclusion_window_hours": int(session_get(db, "rotation_exclusion_hours", "24")),
            "dry_run": session_get(db, "rotation_dry_run", "false").lower() in ("1", "true", "yes", "on"),
            "check_interval_seconds": int(session_get(db, "rotation_check_interval", "300")),
        }

    async def _rotation_loop(self):
        while self._running:
            try:
                await self._check_and_rotate()
            except Exception as exc:
                log.warning("rotation_loop error: %s", exc)
            await asyncio.sleep(self._get_config_check_interval())

    def _get_config_check_interval(self) -> int:
        try:
            db = get_session_factory()()
            config = self._get_config(db)
            db.close()
            return config.get("check_interval_seconds", 300)
        except Exception:
            return 300

    async def _check_and_rotate(self):
        db = get_session_factory()()
        try:
            config = self._get_config(db)
            if not config["enabled"]:
                return

            active = db.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).first()
            if not active:
                log.warning("no active node for rotation check")
                return

            max_duration = timedelta(hours=config["max_duration_hours"])
            now_utc = datetime.now(timezone.utc)
            last_change = active.last_state_change
            if last_change and last_change.tzinfo is None:
                last_change = last_change.replace(tzinfo=timezone.utc)
            active_duration = now_utc - (last_change or now_utc)
            if active_duration < max_duration:
                # Not yet time for scheduled rotation
                return

            # Check if we should rotate due to scheduled lifecycle
            candidate = self._select_rotation_candidate(db, active, config)
            if candidate is None:
                log.info("rotation: no suitable candidate for scheduled rotation of %s", active.name)
                return

            if config["dry_run"]:
                log.info("rotation (dry-run): would switch %s -> %s", active.name, candidate.name)
                return

            log.info("rotation: scheduled switch %s (duration %.1fh) -> %s",
                     active.name, active_duration.total_seconds() / 3600, candidate.name)

            # Use the same activation mechanism as failover
            def _audit(actor, action, detail):
                from ..models import AuditLog
                db.add(AuditLog(actor=actor, action=action, detail=detail))
                db.commit()

            from ..models import AuditLog as _Audit
            def audit(actor, action, detail):
                db.add(_Audit(actor=actor, action=action, detail=detail))
                db.commit()

            from .failover import session_set
            reason = f"rotation: {active.name} reached max duration ({config['max_duration_hours']}h) -> {candidate.name}"
            activate_node(db, candidate, actor="rotation", audit=audit, reason=reason)
            session_set(db, "last_rotation_at", datetime.now(timezone.utc).isoformat())
            session_set(db, "last_rotation_reason", reason)

        finally:
            db.close()

    def _select_rotation_candidate(self, db, active: ProxyNode, config: dict) -> Optional[ProxyNode]:
        """Select best candidate for scheduled rotation."""
        exclusion_window = timedelta(hours=config["exclusion_window_hours"])
        now = datetime.now(timezone.utc)

        # Find eligible nodes
        candidates = db.query(ProxyNode).filter(
            ProxyNode.enabled.is_(True),
            ProxyNode.removed_upstream.is_(False),
            ProxyNode.circuit_open.is_(False),
            ProxyNode.is_active.is_(False),
            ProxyNode.health == "healthy",
        ).all()

        if not candidates:
            return None

        # Filter out recently used nodes
        eligible = []
        for node in candidates:
            recently_used = False
            # Check if node was active within exclusion window
            # We track this via last_state_change when it was last activated
            if node.last_state_change:
                last_change = node.last_state_change
                if last_change.tzinfo is None:
                    last_change = last_change.replace(tzinfo=timezone.utc)
                if now - last_change < exclusion_window:
                    # Check if it was activated (became active) recently
                    # We approximate: if it was active recently, last_state_change would be recent
                    # For now, we'll use a simpler heuristic
                    recently_used = True

            if not recently_used:
                eligible.append(node)

        if not eligible:
            # If all candidates were recently used, fall back to all candidates
            log.info("rotation: all candidates recently used, falling back to full set")
            eligible = candidates

        if not eligible:
            return None

        # Rank by quality score, preferring nodes with good egress and stability
        eligible.sort(key=lambda n: (-n.quality_score, n.rank, n.id))
        return eligible[0]

    async def force_rotation(self) -> dict:
        """Manually trigger a rotation check (for API)."""
        db = get_session_factory()()
        try:
            config = self._get_config(db)
            if not config["enabled"]:
                return {"switched": False, "reason": "rotation disabled"}

            active = db.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).first()
            if not active:
                return {"switched": False, "reason": "no active node"}

            candidate = self._select_rotation_candidate(db, active, config)
            if candidate is None:
                return {"switched": False, "reason": "no suitable candidate"}

            if config["dry_run"]:
                return {"switched": False, "reason": "dry-run mode", "would_switch_to": candidate.name}

            def _audit(actor, action, detail):
                from ..models import AuditLog
                db.add(AuditLog(actor=actor, action=action, detail=detail))
                db.commit()

            from ..models import AuditLog as _Audit
            def audit(actor, action, detail):
                db.add(_Audit(actor=actor, action=action, detail=detail))
                db.commit()

            from .failover import session_set
            reason = f"manual rotation: {active.name} -> {candidate.name}"
            activate_node(db, candidate, actor="rotation", audit=audit, reason=reason)
            session_set(db, "last_rotation_at", datetime.now(timezone.utc).isoformat())
            session_set(db, "last_rotation_reason", reason)

            return {"switched": True, "reason": reason, "new_node_id": candidate.id}

        finally:
            db.close()

    def get_rotation_status(self, db) -> dict:
        """Get current rotation status."""
        config = self._get_config(db)
        active = db.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).first()

        if not active:
            return {"enabled": config["enabled"], "active_node": None}

        max_duration = timedelta(hours=config["max_duration_hours"])
        now_utc = datetime.now(timezone.utc)
        last_change = active.last_state_change
        if last_change and last_change.tzinfo is None:
            last_change = last_change.replace(tzinfo=timezone.utc)
        active_duration = now_utc - (last_change or now_utc)
        remaining = max_duration - active_duration

        return {
            "enabled": config["enabled"],
            "dry_run": config["dry_run"],
            "active_node": {
                "id": active.id,
                "name": active.name,
                "duration_hours": round(active_duration.total_seconds() / 3600, 1),
                "max_duration_hours": config["max_duration_hours"],
                "remaining_hours": round(max(0, remaining.total_seconds() / 3600), 1),
            },
            "next_check_in_seconds": config["check_interval_seconds"],
            "last_rotation": db.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).first(),
        }


lifecycle_manager = LifecycleManager()


async def get_lifecycle_manager() -> LifecycleManager:
    return lifecycle_manager