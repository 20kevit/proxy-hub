"""Node management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..models import AuditLog, ProxyNode
from ..schemas import NodeUpdate
from ..services.cores import managed_socks_url
from ..services.failover import activate_node
from ..services.health_v2 import health_engine
from .deps import check_csrf, current_user, db_session
from .system import node_public

router = APIRouter()


def _audit(db, actor: str, action: str, detail: str = "") -> None:
    db.add(AuditLog(actor=actor, action=action, detail=detail))
    db.commit()


def node_public_v2(n: ProxyNode) -> dict:
    base = node_public(n)
    base.update({
        "quality_score": n.quality_score,
        "tcp_latency_ms": n.tcp_latency_ms,
        "handshake_latency_ms": n.handshake_latency_ms,
        "app_latency_ms": n.app_latency_ms,
        "latency_variance": n.latency_variance,
        "consecutive_failures": n.consecutive_failures,
        "consecutive_successes": n.consecutive_successes,
        "total_probes": n.total_probes,
        "successful_probes": n.successful_probes,
        "timeout_count": n.timeout_count,
        "connection_reset_count": n.connection_reset_count,
        "tls_failure_count": n.tls_failure_count,
        "circuit_open": n.circuit_open,
        "circuit_open_until": n.circuit_open_until.isoformat() if n.circuit_open_until else None,
        "last_state_change": n.last_state_change.isoformat() if n.last_state_change else None,
        "rank": n.rank,
    })
    return base


@router.get("/api/nodes")
def list_nodes(user: str = Depends(current_user)):
    db = db_session()
    try:
        nodes = db.query(ProxyNode).order_by(ProxyNode.rank, ProxyNode.id).all()
        return [node_public_v2(n) for n in nodes]
    finally:
        db.close()


@router.get("/api/nodes/ranking")
def get_ranking(user: str = Depends(current_user)):
    db = db_session()
    try:
        nodes = db.query(ProxyNode).filter(
            ProxyNode.enabled.is_(True),
            ProxyNode.removed_upstream.is_(False)
        ).order_by(ProxyNode.quality_score.desc()).all()
        return [
            {
                "rank": n.rank,
                "id": n.id,
                "name": n.name,
                "protocol": n.protocol,
                "server": n.server,
                "port": n.port,
                "quality_score": n.quality_score,
                "health": n.health,
                "latency_ms": n.latency_ms,
                "tcp_latency_ms": n.tcp_latency_ms,
                "handshake_latency_ms": n.handshake_latency_ms,
                "app_latency_ms": n.app_latency_ms,
                "latency_variance": n.latency_variance,
                "consecutive_failures": n.consecutive_failures,
                "consecutive_successes": n.consecutive_successes,
                "egress_ok": n.egress_ok,
                "circuit_open": n.circuit_open,
                "is_active": n.is_active,
            }
            for n in nodes
        ]
    finally:
        db.close()


@router.patch("/api/nodes/{node_id}")
def update_node(node_id: int, payload: NodeUpdate, user: str = Depends(current_user)):
    db = db_session()
    try:
        node = db.query(ProxyNode).filter(ProxyNode.id == node_id).first()
        if node is None:
            raise HTTPException(404, "node not found")
        data = payload.model_dump(exclude_unset=True)
        if "name" in data and data["name"] is not None:
            node.name = data["name"][:256]
        if "tags" in data and data["tags"] is not None:
            node.tags = data["tags"][:512]
        if "enabled" in data and data["enabled"] is not None:
            node.enabled = bool(data["enabled"])
            if not node.enabled and node.is_active:
                node.is_active = False
        db.commit()
        _audit(db, user, "node.update", f"#{node.id} {node.name}")
        return node_public_v2(node)
    finally:
        db.close()


@router.post("/api/nodes/{node_id}/activate")
def activate(node_id: int, user: str = Depends(current_user)):
    db = db_session()
    try:
        node = db.query(ProxyNode).filter(ProxyNode.id == node_id).first()
        if node is None:
            raise HTTPException(404, "node not found")
        try:
            activate_node(db, node, actor=user, audit=lambda a, x, d: _audit(db, a, x, d))
        except (ValueError, Exception) as exc:
            raise HTTPException(400, str(exc)[:300])
        return {"ok": True, "node": node_public_v2(node)}
    finally:
        db.close()


@router.post("/api/nodes/{node_id}/test")
async def test_node(node_id: int, user: str = Depends(current_user)):
    result = await health_engine.force_check_node(node_id)
    if "error" in result:
        raise HTTPException(400, result["error"])
    db = db_session()
    try:
        node = db.query(ProxyNode).filter(ProxyNode.id == node_id).first()
        return {"ok": result["ok"], "node": node_public_v2(node)}
    finally:
        db.close()


@router.post("/api/nodes/{node_id}/restore")
def restore(node_id: int, user: str = Depends(current_user)):
    db = db_session()
    try:
        node = db.query(ProxyNode).filter(ProxyNode.id == node_id).first()
        if node is None:
            raise HTTPException(404, "node not found")
        node.enabled = True
        node.removed_upstream = False
        db.commit()
        _audit(db, user, "node.restore", f"#{node.id} {node.name}")
        return {"ok": True, "node": node_public_v2(node)}
    finally:
        db.close()


@router.delete("/api/nodes/{node_id}")
def delete_local(node_id: int, user: str = Depends(current_user)):
    db = db_session()
    try:
        node = db.query(ProxyNode).filter(ProxyNode.id == node_id).first()
        if node is None:
            raise HTTPException(404, "node not found")
        if node.is_active:
            raise HTTPException(400, "cannot delete the active node")
        _audit(db, user, "node.delete", f"#{node.id} {node.name}")
        db.delete(node)
        db.commit()
        return {"ok": True}
    finally:
        db.close()