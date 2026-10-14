"""Scheduled Lifecycle Manager: automatic node rotation with fairness and safety."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from ..config import get_settings
from ..db import get_session_factory
from ..models import ProxyNode

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
        """The single authoritative rotation scheduler.

        NOTE: main.py must NOT run a second loop calling _check_and_rotate;
        all scheduled rotation flows through this task, and manual rotation
        through force_rotation_sync. Both funnel into failover.attempt_transition
        (serialized by transition_lock).
        """
        while self._running:
            try:
                await asyncio.to_thread(self._check_and_rotate_sync)
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
        """Async wrapper kept for backward compatibility; runs the sync core."""
        await asyncio.to_thread(self._check_and_rotate_sync)

    def _check_and_rotate_sync(self) -> dict:
        """Scheduled rotation core (synchronous; runs in a worker thread)."""
        from ..models import AuditLog as _Audit
        from .failover import attempt_transition, eligible_candidates, session_set

        db = get_session_factory()()
        try:
            config = self._get_config(db)
            if not config["enabled"]:
                return {"switched": False, "reason": "rotation disabled"}

            active = db.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).first()
            if not active:
                log.warning("no active node for rotation check")
                return {"switched": False, "reason": "no active node"}

            max_duration = timedelta(hours=config["max_duration_hours"])
            now_utc = datetime.now(timezone.utc)
            last_change = active.last_state_change
            if last_change and last_change.tzinfo is None:
                last_change = last_change.replace(tzinfo=timezone.utc)
            active_duration = now_utc - (last_change or now_utc)
            if active_duration < max_duration:
                return {"switched": False, "reason": "max duration not reached"}

            candidate = self._select_rotation_candidate(db, active, config)
            if candidate is None:
                log.info("rotation: no suitable candidate for scheduled rotation of %s",
                         active.name)
                return {"switched": False, "reason": "no suitable candidate"}

            if config["dry_run"]:
                log.info("rotation (dry-run): would switch %s -> %s",
                         active.name, candidate.name)
                return {"switched": False, "reason": "dry-run mode",
                        "would_switch_to": candidate.name}

            log.info("rotation: scheduled switch %s (duration %.1fh) -> %s",
                     active.name, active_duration.total_seconds() / 3600, candidate.name)

            def audit(actor, action, detail):
                db.add(_Audit(actor=actor, action=action, detail=detail))
                db.commit()

            ok, detail = attempt_transition(
                db, candidate, active, actor="rotation", audit=audit,
                reason_kind="rotation")
            if ok:
                session_set(db, "last_rotation_at", datetime.now(timezone.utc).isoformat())
                session_set(db, "last_rotation_reason", detail)
                return {"switched": True, "reason": detail, "new_node_id": candidate.id}
            return {"switched": False, "reason": detail}
        finally:
            db.close()

    def _select_rotation_candidate(self, db, active: ProxyNode, config: dict) -> Optional[ProxyNode]:
        """Best eligible candidate, preferring nodes outside the use window.

        Eligibility (health, verified egress, circuit, quality) is shared
        with failover via eligible_candidates; the 24h exclusion window only
        reorders, and falls back to the full eligible set so rotation never
        fails merely because every candidate was used recently.
        """
        from .failover import eligible_candidates

        exclusion_window = timedelta(hours=config["exclusion_window_hours"])
        now = datetime.now(timezone.utc)
        eligible, _rejected = eligible_candidates(
            db, exclude_id=active.id if active else None)
        if not eligible:
            return None
        fresh = []
        for node in eligible:
            last_change = node.last_state_change
            if last_change and last_change.tzinfo is None:
                last_change = last_change.replace(tzinfo=timezone.utc)
            if last_change is None or (now - last_change) >= exclusion_window:
                fresh.append(node)
        pool = fresh or eligible
        if not fresh:
            log.info("rotation: all candidates recently used, falling back to full set")
        return pool[0]

    async def force_rotation(self) -> dict:
        """Manually trigger a rotation check (for API)."""
        return await asyncio.to_thread(self.force_rotation_sync)

    def force_rotation_sync(self) -> dict:
        """Manual rotation core (synchronous; runs in a worker thread)."""
        from ..models import AuditLog as _Audit
        from .failover import attempt_transition, session_set

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
                return {"switched": False, "reason": "dry-run mode",
                        "would_switch_to": candidate.name}

            def audit(actor, action, detail):
                db.add(_Audit(actor=actor, action=action, detail=detail))
                db.commit()

            ok, detail = attempt_transition(
                db, candidate, active, actor="rotation", audit=audit,
                reason_kind="manual rotation")
            if not ok:
                return {"switched": False, "reason": detail}
            session_set(db, "last_rotation_at", datetime.now(timezone.utc).isoformat())
            session_set(db, "last_rotation_reason", detail)
            return {"switched": True, "reason": detail, "new_node_id": candidate.id}
        finally:
            db.close()

    def get_rotation_status(self, db) -> dict:
        """Get current rotation status."""
        from .failover import session_get

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

        # NOTE: never return the raw ORM object here — FastAPI would
        # serialize every column including secret credentials.
        return {
            "enabled": config["enabled"],
            "dry_run": config["dry_run"],
            "active_node": {
                "id": active.id,
                "name": active.name,
                "protocol": active.protocol,
                "quality_score": active.quality_score,
                "duration_hours": round(active_duration.total_seconds() / 3600, 1),
                "max_duration_hours": config["max_duration_hours"],
                "remaining_hours": round(max(0, remaining.total_seconds() / 3600), 1),
            },
            "next_check_in_seconds": config["check_interval_seconds"],
            "last_rotation_at": session_get(db, "last_rotation_at", ""),
            "last_rotation_reason": session_get(db, "last_rotation_reason", ""),
        }


lifecycle_manager = LifecycleManager()


async def get_lifecycle_manager() -> LifecycleManager:
    return lifecycle_manager