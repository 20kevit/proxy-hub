"""Independent Watchdog: monitors health of ProxyHub components."""
from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from ..config import get_settings
from ..db import get_session_factory
from ..models import ProxyNode
from .health_v2 import health_engine

log = logging.getLogger("proxyhub.watchdog")


@dataclass
class ComponentHealth:
    name: str
    healthy: bool
    last_check: datetime
    last_success: Optional[datetime] = None
    consecutive_failures: int = 0
    details: str = ""


@dataclass
class WatchdogConfig:
    check_interval: int = 60
    fastapi_timeout: int = 5
    xray_timeout: int = 5
    max_consecutive_failures: int = 3
    disk_warning_percent: float = 85.0
    memory_warning_percent: float = 85.0
    stale_health_cycle_seconds: int = 180  # 3 minutes
    stale_probe_seconds: int = 300  # 5 minutes


class Watchdog:
    """Independent watchdog for ProxyHub components."""

    def __init__(self, config: Optional[WatchdogConfig] = None):
        self.config = config or WatchdogConfig()
        self.settings = get_settings()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.components: Dict[str, ComponentHealth] = {}
        self.alerts: List[Dict] = []
        self._start_time = datetime.now(timezone.utc)

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        log.info("Watchdog started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Watchdog stopped")

    async def _monitor_loop(self):
        # Delay first check to allow API server to start
        await asyncio.sleep(10)
        while self._running:
            try:
                await self._check_all_components()
                self._evaluate_alerts()
            except Exception as exc:
                log.warning("watchdog monitor error: %s", exc)
            await asyncio.sleep(self.config.check_interval)

    async def _check_all_components(self):
        await asyncio.gather(
            self._check_fastapi(),
            self._check_xray(),
            self._check_health_engine(),
            self._check_database(),
            self._check_disk_space(),
            self._check_memory(),
            self._check_proxy_listeners(),
            return_exceptions=True,
        )

    async def _check_fastapi(self):
        """Check FastAPI health by inspecting internal state."""
        # Check if health engine is running (indicates main loop is alive)
        engine_healthy = health_engine._running
        
        # Check if lifespan completed (server is up)
        # We can't easily check this without HTTP, so rely on engine state
        healthy = engine_healthy
        
        if healthy:
            self._update_component("fastapi", True, "health engine running")
        else:
            self._update_component("fastapi", False, "health engine stopped")

    async def _check_xray(self):
        """Check proxyhub-xray process and listeners."""
        try:
            # Check systemd service
            proc = subprocess.run(
                ["systemctl", "is-active", "proxyhub-xray"],
                capture_output=True, text=True, timeout=self.config.xray_timeout
            )
            service_active = proc.stdout.strip() == "active"

            # Check listeners
            proc = subprocess.run(
                ["ss", "-tlnp"],
                capture_output=True, text=True, timeout=self.config.xray_timeout
            )
            has_socks = ":2081" in proc.stdout
            has_http = ":1081" in proc.stdout

            healthy = service_active and has_socks and has_http
            details = f"service={proc.stdout.strip()}, socks={has_socks}, http={has_http}"
            self._update_component("xray", healthy, details)

        except Exception as exc:
            self._update_component("xray", False, f"error: {exc}")

    async def _check_health_engine(self):
        """Check if health engine is running and cycles are fresh."""
        try:
            # Check if health engine tasks are alive
            engine_healthy = health_engine._running

            # Check last health cycle freshness
            db = get_session_factory()()
            try:
                active = db.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).first()
                if active and active.last_checked:
                    last_checked = active.last_checked
                    if last_checked.tzinfo is None:
                        last_checked = last_checked.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - last_checked).total_seconds()
                    fresh = age < self.config.stale_health_cycle_seconds
                    details = f"engine_running={engine_healthy}, last_probe_age={age:.0f}s"
                else:
                    fresh = False
                    details = f"engine_running={engine_healthy}, no active node"
            finally:
                db.close()

            healthy = engine_healthy and fresh
            self._update_component("health_engine", healthy, details)

        except Exception as exc:
            self._update_component("health_engine", False, f"error: {exc}")

    async def _check_database(self):
        """Check database connectivity and basic integrity."""
        try:
            db = get_session_factory()()
            try:
                # Simple query
                count = db.query(ProxyNode).count()
                healthy = True
                details = f"nodes={count}"
            finally:
                db.close()
            self._update_component("database", healthy, details)
        except Exception as exc:
            self._update_component("database", False, f"error: {exc}")

    async def _check_disk_space(self):
        """Check available disk space."""
        try:
            import shutil
            total, used, free = shutil.disk_usage("/")
            used_percent = (used / total) * 100
            healthy = used_percent < self.config.disk_warning_percent
            details = f"used_percent={used_percent:.1f}%"
            self._update_component("disk", healthy, details)
        except Exception as exc:
            self._update_component("disk", False, f"error: {exc}")

    async def _check_memory(self):
        """Check memory usage."""
        try:
            import psutil
            mem = psutil.virtual_memory()
            healthy = mem.percent < self.config.memory_warning_percent
            details = f"used_percent={mem.percent:.1f}%"
            self._update_component("memory", healthy, details)
        except Exception as exc:
            self._update_component("memory", False, f"error: {exc}")

    async def _check_proxy_listeners(self):
        """Check that proxy listeners are accepting connections via socket check."""
        import socket
        try:
            # Check HTTP proxy listener (1081)
            http_ok = False
            socks_ok = False
            
            # Check HTTP proxy port 1081
            try:
                with socket.create_connection(("127.0.0.1", 1081), timeout=2.0):
                    http_ok = True
            except Exception:
                pass
            
            # Check SOCKS proxy port 2081
            try:
                with socket.create_connection(("127.0.0.1", 2081), timeout=2.0):
                    socks_ok = True
            except Exception:
                pass
            
            healthy = http_ok and socks_ok
            details = f"http_proxy={http_ok}, socks_proxy={socks_ok}"
            self._update_component("proxy_listeners", healthy, details)
        except Exception as exc:
            self._update_component("proxy_listeners", False, f"error: {exc}")

    def _update_component(self, name: str, healthy: bool, details: str):
        now = datetime.now(timezone.utc)
        if name not in self.components:
            self.components[name] = ComponentHealth(name=name, healthy=healthy, last_check=now, details=details)
        else:
            comp = self.components[name]
            comp.healthy = healthy
            comp.last_check = now
            comp.details = details
            if healthy:
                comp.consecutive_failures = 0
                comp.last_success = now
            else:
                comp.consecutive_failures += 1

    def _evaluate_alerts(self):
        """Evaluate component states and generate alerts."""
        now = datetime.now(timezone.utc)
        for name, comp in self.components.items():
            if comp.consecutive_failures >= self.config.max_consecutive_failures:
                self._add_alert(
                    "critical",
                    f"Component {name} unhealthy for {comp.consecutive_failures} consecutive checks",
                    {"component": name, "details": comp.details, "failures": comp.consecutive_failures}
                )

    def _add_alert(self, level: str, message: str, details: dict):
        alert = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "message": message,
            "details": details,
        }
        # Avoid duplicate alerts for the same issue
        for existing in self.alerts[-10:]:
            if existing["message"] == message and existing["level"] == level:
                return
        self.alerts.append(alert)
        # Keep only last 100 alerts
        if len(self.alerts) > 100:
            self.alerts = self.alerts[-100:]
        log.warning("Watchdog alert [%s]: %s", level, message)

    def get_status(self) -> dict:
        """Get current watchdog status."""
        return {
            "uptime_seconds": (datetime.now(timezone.utc) - self._start_time).total_seconds(),
            "components": {
                name: {
                    "healthy": comp.healthy,
                    "last_check": comp.last_check.isoformat(),
                    "last_success": comp.last_success.isoformat() if comp.last_success else None,
                    "consecutive_failures": comp.consecutive_failures,
                    "details": comp.details,
                }
                for name, comp in self.components.items()
            },
            "recent_alerts": self.alerts[-10:],
            "all_healthy": all(comp.healthy for comp in self.components.values()),
        }


watchdog = Watchdog()


async def get_watchdog() -> Watchdog:
    return watchdog