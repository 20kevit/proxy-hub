"""Health & Quality Engine v2: Multi-level monitoring, quality scoring, automatic ranking."""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

from ..config import get_settings
from ..db import get_session_factory
from ..models import ProxyNode

log = logging.getLogger("proxyhub.health_v2")

# Thread pool for blocking I/O operations
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="health-probe")


@dataclass
class ProbeResult:
    ok: bool
    tcp_latency_ms: Optional[float] = None
    handshake_latency_ms: Optional[float] = None
    app_latency_ms: Optional[float] = None
    error: str = ""
    error_type: str = ""


@dataclass
class QualityWeights:
    availability: float = 0.30
    latency: float = 0.25
    stability: float = 0.20
    recency: float = 0.15
    recovery: float = 0.10


QUALITY_WEIGHTS = QualityWeights()


def tcp_probe(host: str, port: int, timeout: float = 5.0) -> ProbeResult:
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return ProbeResult(ok=True, tcp_latency_ms=round((time.monotonic() - started) * 1000, 1))
    except (socket.timeout, TimeoutError):
        return ProbeResult(ok=False, error="tcp_timeout", error_type="timeout")
    except ConnectionRefusedError:
        return ProbeResult(ok=False, error="connection_refused", error_type="refused")
    except OSError as exc:
        return ProbeResult(ok=False, error=f"{type(exc).__name__}", error_type="os_error")


def tls_handshake_probe(host: str, port: int, timeout: float = 8.0) -> ProbeResult:
    started = time.monotonic()
    try:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                ssock.do_handshake()
                return ProbeResult(
                    ok=True,
                    tcp_latency_ms=round((time.monotonic() - started) * 1000, 1),
                    handshake_latency_ms=round((time.monotonic() - started) * 1000, 1)
                )
    except (socket.timeout, TimeoutError):
        return ProbeResult(ok=False, error="tls_timeout", error_type="timeout")
    except ssl.SSLError as exc:
        return ProbeResult(ok=False, error=f"ssl:{exc}", error_type="tls_error")
    except OSError as exc:
        return ProbeResult(ok=False, error=f"{type(exc).__name__}", error_type="os_error")


def http_probe_via_proxy(proxy_url: str, test_url: str, timeout: int = 15) -> ProbeResult:
    started = time.monotonic()
    tcp_started = time.monotonic()
    tcp_latency = None
    handshake_latency = None
    
    try:
        with httpx.Client(proxy=proxy_url, timeout=timeout) as client:
            resp = client.get(test_url)
            resp.raise_for_status()
            total_ms = round((time.monotonic() - started) * 1000, 1)
            return ProbeResult(
                ok=True,
                app_latency_ms=total_ms,
                tcp_latency_ms=tcp_latency,
                handshake_latency_ms=handshake_latency
            )
    except httpx.TimeoutException:
        return ProbeResult(ok=False, error="http_timeout", error_type="timeout")
    except httpx.ConnectError as exc:
        if "SSL" in str(exc) or "TLS" in str(exc):
            return ProbeResult(ok=False, error=f"tls:{exc}", error_type="tls_error")
        if "reset" in str(exc).lower():
            return ProbeResult(ok=False, error=f"reset:{exc}", error_type="reset")
        return ProbeResult(ok=False, error=f"connect:{exc}", error_type="connect_error")
    except httpx.HTTPStatusError as exc:
        return ProbeResult(ok=False, error=f"http_{exc.response.status_code}", error_type="http_error")
    except httpx.RequestError as exc:
        return ProbeResult(ok=False, error=f"{type(exc).__name__}:{exc}", error_type="request_error")


class QualityCalculator:
    @staticmethod
    def calculate(node: ProxyNode, weights: QualityWeights = QUALITY_WEIGHTS) -> float:
        if not node.enabled or node.removed_upstream:
            return 0.0
        
        if node.total_probes == 0:
            return 50.0
        
        availability = (node.successful_probes / node.total_probes) * 100 if node.total_probes > 0 else 0
        
        latencies = []
        if node.tcp_latency_ms:
            latencies.append(node.tcp_latency_ms)
        if node.handshake_latency_ms:
            latencies.append(node.handshake_latency_ms)
        if node.app_latency_ms:
            latencies.append(node.app_latency_ms)
        
        avg_latency = sum(latencies) / len(latencies) if latencies else 1000
        latency_score = max(0, 100 - (avg_latency / 10))
        
        stability = 100 - min(100, node.latency_variance * 2)
        stability = max(0, stability)
        
        now = datetime.now(timezone.utc)
        recency_score = 100
        if node.last_checked:
            last_checked = node.last_checked
            if last_checked.tzinfo is None:
                last_checked = last_checked.replace(tzinfo=timezone.utc)
            hours_since = (now - last_checked).total_seconds() / 3600
            recency_score = max(0, 100 - hours_since * 5)
        
        recovery_score = 0
        if node.consecutive_failures > 0 and node.consecutive_successes > 0:
            recovery_score = min(100, node.consecutive_successes * 20)
        
        score = (
            weights.availability * availability +
            weights.latency * latency_score +
            weights.stability * stability +
            weights.recency * recency_score +
            weights.recovery * recovery_score
        )
        
        if node.circuit_open:
            score *= 0.1
        
        return round(max(0.0, min(100.0, score)), 1)


class HealthEngineV2:
    def __init__(self):
        self.settings = get_settings()
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._lock = asyncio.Lock()
    
    async def start(self):
        self._running = True
        self._tasks = [
            asyncio.create_task(self._fast_loop()),
            asyncio.create_task(self._active_loop()),
            asyncio.create_task(self._deep_loop()),
            asyncio.create_task(self._ranking_loop()),
            asyncio.create_task(self._cleanup_loop()),
        ]
        log.info("HealthEngineV2 started")
    
    async def stop(self):
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        log.info("HealthEngineV2 stopped")
    
    async def _fast_loop(self):
        while self._running:
            try:
                await self._fast_check_all()
            except Exception as exc:
                log.warning("fast_loop error: %s", exc)
            await asyncio.sleep(30)
    
    async def _active_loop(self):
        interval = getattr(self.settings, 'active_probe_interval', 60)
        while self._running:
            try:
                await self._active_check_all()
            except Exception as exc:
                log.warning("active_loop error: %s", exc)
            await asyncio.sleep(interval)
    
    async def _deep_loop(self):
        interval = getattr(self.settings, 'deep_probe_interval', 60)
        while self._running:
            try:
                await self._deep_check_all()
            except Exception as exc:
                log.warning("deep_loop error: %s", exc)
            await asyncio.sleep(interval)
    
    async def _ranking_loop(self):
        while self._running:
            try:
                await self._update_rankings()
            except Exception as exc:
                log.warning("ranking_loop error: %s", exc)
            await asyncio.sleep(60)
    
    async def _cleanup_loop(self):
        while self._running:
            try:
                await self._cleanup_old_data()
            except Exception as exc:
                log.warning("cleanup_loop error: %s", exc)
            await asyncio.sleep(3600)
    
    async def _fast_check_all(self):
        # Run blocking I/O in thread pool to avoid blocking event loop
        await asyncio.to_thread(self._fast_check_all_sync)
    
    def _fast_check_all_sync(self):
        db = get_session_factory()()
        try:
            nodes = db.query(ProxyNode).filter(
                ProxyNode.enabled.is_(True),
                ProxyNode.removed_upstream.is_(False)
            ).all()
            
            now_utc = datetime.now(timezone.utc)
            for node in nodes:
                if node.circuit_open and node.circuit_open_until:
                    circuit_until = node.circuit_open_until
                    if circuit_until.tzinfo is None:
                        circuit_until = circuit_until.replace(tzinfo=timezone.utc)
                    if now_utc < circuit_until:
                        continue
                    else:
                        node.circuit_open = False
                        node.circuit_open_until = None
                        db.commit()
                
                result = tcp_probe(node.server, node.port, timeout=2.0)
                self._update_fast_metrics(db, node, result)
            
            db.commit()
        finally:
            db.close()
    
    def _update_fast_metrics(self, db, node: ProxyNode, result: ProbeResult):
        now = datetime.now(timezone.utc)
        node.last_seen = now
        
        if result.ok:
            node.tcp_latency_ms = result.tcp_latency_ms
            # Only reset consecutive_failures if egress is OK (don't mask egress failures)
            if node.egress_ok:
                node.consecutive_failures = 0
            node.consecutive_successes += 1
            node.total_probes += 1
            node.successful_probes += 1
            if node.health != "healthy":
                node.health = "healthy"
                node.last_state_change = now
        else:
            node.consecutive_failures += 1
            node.consecutive_successes = 0
            node.total_probes += 1
            node.last_error = result.error[:500]
            if result.error_type == "timeout":
                node.timeout_count += 1
            elif result.error_type == "reset":
                node.connection_reset_count += 1
            
            if node.consecutive_failures >= 3:
                if node.health != "unhealthy":
                    node.health = "unhealthy"
                    node.last_state_change = now
        
        node.quality_score = QualityCalculator.calculate(node)
    
    async def _active_check_all(self):
        # Run blocking I/O in thread pool to avoid blocking event loop
        await asyncio.to_thread(self._active_check_all_sync)
    
    def _active_check_all_sync(self):
        db = get_session_factory()()
        try:
            socks_url = self._get_socks_url(db)
            if not socks_url:
                return
            
            active_node = db.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).first()
            candidates = db.query(ProxyNode).filter(
                ProxyNode.enabled.is_(True),
                ProxyNode.removed_upstream.is_(False),
                ProxyNode.circuit_open.is_(False)
            ).all()
            
            for node in candidates:
                if node.id == (active_node.id if active_node else -1):
                    result = http_probe_via_proxy(socks_url, self.settings.health_url, self.settings.health_timeout)
                    self._update_active_metrics(db, node, result)
                else:
                    result = tls_handshake_probe(node.server, node.port, timeout=5.0)
                    self._update_active_metrics(db, node, result)
            
            db.commit()
        finally:
            db.close()
    
    def _update_active_metrics(self, db, node: ProxyNode, result: ProbeResult):
        now = datetime.now(timezone.utc)
        node.last_checked = now
        
        if result.ok:
            if result.handshake_latency_ms:
                node.handshake_latency_ms = result.handshake_latency_ms
            if result.app_latency_ms:
                node.app_latency_ms = result.app_latency_ms
                node.latency_ms = result.app_latency_ms
                node.egress_ok = True
            node.consecutive_failures = 0
            node.consecutive_successes += 1
            node.total_probes += 1
            node.successful_probes += 1
            node.last_success = now
            if node.health != "healthy":
                node.health = "healthy"
                node.last_state_change = now
        else:
            node.consecutive_failures += 1
            node.consecutive_successes = 0
            node.total_probes += 1
            node.last_error = result.error[:500]
            node.egress_ok = False
            if result.error_type == "timeout":
                node.timeout_count += 1
            elif result.error_type == "reset":
                node.connection_reset_count += 1
            elif result.error_type == "tls_error":
                node.tls_failure_count += 1
            
            if node.consecutive_failures >= 3:
                if node.health != "unhealthy":
                    node.health = "unhealthy"
                    node.last_state_change = now
        
        self._update_latency_variance(node, result)
        node.quality_score = QualityCalculator.calculate(node)
    
    def _update_latency_variance(self, node: ProxyNode, result: ProbeResult):
        latencies = []
        if node.tcp_latency_ms:
            latencies.append(node.tcp_latency_ms)
        if node.handshake_latency_ms:
            latencies.append(node.handshake_latency_ms)
        if result.app_latency_ms:
            latencies.append(result.app_latency_ms)
        elif node.app_latency_ms:
            latencies.append(node.app_latency_ms)
        
        if len(latencies) >= 2:
            mean = sum(latencies) / len(latencies)
            variance = sum((x - mean) ** 2 for x in latencies) / len(latencies)
            node.latency_variance = round(variance, 1)
    
    async def _deep_check_all(self):
        # Run blocking I/O in thread pool to avoid blocking event loop
        await asyncio.to_thread(self._deep_check_all_sync)
    
    def _deep_check_all_sync(self):
        db = get_session_factory()()
        try:
            socks_url = self._get_socks_url(db)
            if not socks_url:
                return
            
            nodes = db.query(ProxyNode).filter(
                ProxyNode.enabled.is_(True),
                ProxyNode.removed_upstream.is_(False),
                ProxyNode.circuit_open.is_(False)
            ).all()
            
            for node in nodes:
                result = http_probe_via_proxy(socks_url, self.settings.health_url, self.settings.health_timeout)
                self._update_deep_metrics(db, node, result)
            
            db.commit()
        finally:
            db.close()
    
    def _update_deep_metrics(self, db, node: ProxyNode, result: ProbeResult):
        now = datetime.now(timezone.utc)
        node.last_checked = now
        
        if result.ok:
            node.app_latency_ms = result.app_latency_ms
            node.latency_ms = result.app_latency_ms
            node.egress_ok = True
            node.consecutive_failures = 0
            node.consecutive_successes += 1
            node.total_probes += 1
            node.successful_probes += 1
            node.last_success = now
            if node.health != "healthy":
                node.health = "healthy"
                node.last_state_change = now
        else:
            node.consecutive_failures += 1
            node.consecutive_successes = 0
            node.total_probes += 1
            node.last_error = result.error[:500]
            node.egress_ok = False
            if result.error_type == "timeout":
                node.timeout_count += 1
            elif result.error_type == "reset":
                node.connection_reset_count += 1
            elif result.error_type == "tls_error":
                node.tls_failure_count += 1
            
            if node.consecutive_failures >= 5:
                node.circuit_open = True
                node.circuit_open_until = datetime.now(timezone.utc).replace(
                    second=0, microsecond=0
                )
                if node.health != "unhealthy":
                    node.health = "unhealthy"
                    node.last_state_change = now
        
        self._record_probe_history(node, result)
        node.quality_score = QualityCalculator.calculate(node)
    
    def _record_probe_history(self, node: ProxyNode, result: ProbeResult):
        history = node.probe_history or {}
        today = datetime.now(timezone.utc).date().isoformat()
        if today not in history:
            history[today] = {"probes": 0, "success": 0, "failures": {}}
        history[today]["probes"] += 1
        if result.ok:
            history[today]["success"] += 1
        else:
            etype = result.error_type
            history[today]["failures"][etype] = history[today]["failures"].get(etype, 0) + 1
        
        cutoff = datetime.now(timezone.utc).date().replace(day=1)
        history = {k: v for k, v in history.items() if k >= cutoff.isoformat()}
        node.probe_history = history
    
    def _get_socks_url(self, db) -> Optional[str]:
        try:
            from .cores import managed_socks_url
            return managed_socks_url()
        except Exception:
            return None
    
    async def _update_rankings(self):
        await asyncio.to_thread(self._update_rankings_sync)
    
    def _update_rankings_sync(self):
        db = get_session_factory()()
        try:
            nodes = db.query(ProxyNode).filter(
                ProxyNode.enabled.is_(True),
                ProxyNode.removed_upstream.is_(False)
            ).order_by(ProxyNode.quality_score.desc()).all()
            
            for rank, node in enumerate(nodes, 1):
                if node.rank != rank:
                    node.rank = rank
            
            db.commit()
        finally:
            db.close()
    
    async def _cleanup_old_data(self):
        await asyncio.to_thread(self._cleanup_old_data_sync)
    
    def _cleanup_old_data_sync(self):
        db = get_session_factory()()
        try:
            cutoff = datetime.now(timezone.utc).date().replace(day=1).isoformat()
            nodes = db.query(ProxyNode).filter(ProxyNode.probe_history.isnot(None)).all()
            for node in nodes:
                history = node.probe_history or {}
                history = {k: v for k, v in history.items() if k >= cutoff}
                node.probe_history = history
            db.commit()
        finally:
            db.close()
    
    async def force_check_node(self, node_id: int) -> dict:
        db = get_session_factory()()
        try:
            node = db.query(ProxyNode).filter(ProxyNode.id == node_id).first()
            if not node:
                return {"error": "node not found"}
            
            socks_url = self._get_socks_url(db)
            if not socks_url:
                return {"error": "no proxy available"}
            
            result = http_probe_via_proxy(socks_url, self.settings.health_url, self.settings.health_timeout)
            self._update_deep_metrics(db, node, result)
            db.commit()
            
            return {
                "node_id": node.id,
                "ok": result.ok,
                "latency_ms": result.app_latency_ms,
                "quality_score": node.quality_score,
                "rank": node.rank
            }
        finally:
            db.close()


health_engine = HealthEngineV2()


async def get_health_engine() -> HealthEngineV2:
    return health_engine