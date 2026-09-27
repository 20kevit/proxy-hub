"""Dashboard / status / system endpoints."""
from __future__ import annotations

import shutil
import time
from datetime import datetime, timezone

import psutil
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func

from ..config import get_settings
from ..models import AuditLog, ProxyNode, SubscriptionState
from ..services.cores import SingBoxAdapter, XrayAdapter, managed_http_url, managed_socks_url, system_xray_status
from ..services.failover import session_get
from ..services.health import egress_ip_via
from .deps import check_csrf, current_user, db_session

router = APIRouter()
_started = time.monotonic()


@router.get("/api/health")
def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


@router.get("/api/status")
def status(user: str = Depends(current_user)):
    db = db_session()
    try:
        total = db.query(func.count(ProxyNode.id)).scalar() or 0
        healthy = db.query(func.count(ProxyNode.id)).filter(ProxyNode.health == "healthy").scalar() or 0
        unhealthy = db.query(func.count(ProxyNode.id)).filter(ProxyNode.health == "unhealthy").scalar() or 0
        active = db.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).first()
        sub = db.query(SubscriptionState).first()
        try:
            managed = XrayAdapter().status()
        except Exception:
            managed = {"active": False, "state": "unknown"}
        sing = SingBoxAdapter()
        try:
            exit_ip, latency = current_exit_ip(db)
        except Exception:
            exit_ip, latency = "", None
        gw_enabled = session_get(db, "gateway_enabled",
                                 str(get_settings().gateway_enabled)).lower() in ("1", "true", "yes", "on")
        return {
            "running": True,
            "uptime_s": int(time.monotonic() - _started),
            "system_xray": system_xray_status(),
            "managed_xray": managed,
            "singbox": {"installed": sing.installed(), "enabled": get_settings().singbox_enabled},
            "active_node": node_public(active) if active else None,
            "active_core": session_get(db, "active_core"),
            "exit_ip": exit_ip,
            "exit_latency_ms": latency,
            "nodes": {"total": total, "healthy": healthy, "unhealthy": unhealthy},
            "subscription": {
                "last_ok_at": sub.last_ok_at.isoformat() if sub and sub.last_ok_at else None,
                "last_error": sub.last_error if sub else "",
                "node_count": sub.node_count if sub else 0,
            },
            "resources": {
                "cpu_pct": psutil.cpu_percent(interval=0.1),
                "mem_pct": psutil.virtual_memory().percent,
                "disk_pct": psutil.disk_usage("/").percent,
            },
            "failover": {
                "mode": session_get(db, "failover_mode", get_settings().failover_mode),
                "last_reason": session_get(db, "last_failover_reason"),
                "last_at": session_get(db, "last_failover_at"),
                "previous_node": session_get(db, "last_prev_node"),
            },
            "gateway": {
                "enabled": gw_enabled,
                "bind": session_get(db, "gateway_bind", get_settings().gateway_bind),
                "socks_port": int(session_get(db, "public_socks_port",
                                              str(get_settings().public_socks_port))),
                "http_port": int(session_get(db, "public_http_port",
                                             str(get_settings().public_http_port))),
                "default_route": session_get(db, "default_route",
                                             get_settings().default_route),
            },
        }
    finally:
        db.close()


def node_public(n: ProxyNode) -> dict:
    return {
        "id": n.id, "name": n.name, "protocol": n.protocol, "server": n.server,
        "port": n.port, "transport": n.transport or "", "security": n.security or "",
        "source": n.source, "tags": n.tags or "", "enabled": n.enabled,
        "removed_upstream": n.removed_upstream, "health": n.health,
        "health_state": n.display_health(),
        "latency_ms": n.latency_ms, "egress_ok": bool(n.egress_ok),
        "fail_count": n.fail_count or 0, "success_count": n.success_count or 0,
        "last_success": n.last_success.isoformat() if n.last_success else None, "last_error": n.last_error or "",
        "last_checked": n.last_checked.isoformat() if n.last_checked else None,
        "is_active": n.is_active,
    }


def current_exit_ip(db) -> tuple[str, float | None]:
    active = db.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).first()
    settings = get_settings()
    if active is None:
        return "", None
    ok, info, ms = egress_ip_via(managed_socks_url(), settings.health_url, settings.health_timeout)
    return (info, ms) if ok else ("", None)


@router.get("/api/system")
def system(user: str = Depends(current_user)):
    db = db_session()
    try:
        s = get_settings()
        logs = (
            db.query(AuditLog).order_by(AuditLog.id.desc()).limit(50).all()
        )
        return {
            "services": {
                "system_xray": system_xray_status(),
                "managed_xray": XrayAdapter().status(),
            },
            "paths": {
                "db": str(s.db_path),
                "xray_config": str(s.xray_config),
                "singbox_config": str(s.singbox_config),
            },
            "listeners": {
                "api": f"{s.host}:{s.port}",
                "managed_socks": managed_socks_url(),
                "managed_http": managed_http_url(),
                "system_socks": "socks5h://127.0.0.1:2080",
                "system_http": "http://127.0.0.1:1080",
            },
            "cores": {
                "xray": {"bin": s.xray_bin, "present": shutil.which("xray") is not None},
                "singbox": {"bin": s.singbox_bin, "installed": SingBoxAdapter().installed()},
            },
            "recent_events": [
                {"actor": e.actor, "action": e.action, "detail": e.detail,
                 "at": e.created_at.isoformat() if e.created_at else None}
                for e in logs
            ],
        }
    finally:
        db.close()
