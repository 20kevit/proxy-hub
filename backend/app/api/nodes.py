"""Node management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..models import AuditLog, ProxyNode
from ..schemas import NodeUpdate
from ..services.cores import managed_socks_url
from ..services.failover import activate_node
from ..services.health import check_node
from .deps import check_csrf, current_user, db_session
from .system import node_public

router = APIRouter()


def _audit(db, actor: str, action: str, detail: str = "") -> None:
    db.add(AuditLog(actor=actor, action=action, detail=detail))
    db.commit()


@router.get("/api/nodes")
def list_nodes(user: str = Depends(current_user)):
    db = db_session()
    try:
        nodes = db.query(ProxyNode).order_by(ProxyNode.is_active.desc(), ProxyNode.id).all()
        return [node_public(n) for n in nodes]
    finally:
        db.close()


@router.patch("/api/nodes/{node_id}")
def update_node(node_id: int, payload: NodeUpdate, user: str = Depends(current_user)):
    from fastapi import Request  # noqa
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
        return node_public(node)
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
        return {"ok": True, "node": node_public(node)}
    finally:
        db.close()


@router.post("/api/nodes/{node_id}/test")
def test_node(node_id: int, user: str = Depends(current_user)):
    db = db_session()
    try:
        node = db.query(ProxyNode).filter(ProxyNode.id == node_id).first()
        if node is None:
            raise HTTPException(404, "node not found")
        full = node.is_active
        check_node(db, node, full_egress=full,
                   socks_proxy=managed_socks_url() if full else "")
        return {"ok": node.health == "healthy", "node": node_public(node)}
    finally:
        db.close()


@router.post("/api/nodes/{node_id}/restore")
def restore(node_id: int, user: str = Depends(current_user)):
    """Re-enable a locally deleted/disabled node (re-imported on next subscription sync)."""
    db = db_session()
    try:
        node = db.query(ProxyNode).filter(ProxyNode.id == node_id).first()
        if node is None:
            raise HTTPException(404, "node not found")
        node.enabled = True
        node.removed_upstream = False
        db.commit()
        _audit(db, user, "node.restore", f"#{node.id} {node.name}")
        return {"ok": True, "node": node_public(node)}
    finally:
        db.close()


@router.delete("/api/nodes/{node_id}")
def delete_local(node_id: int, user: str = Depends(current_user)):
    """Local-only delete. Subscription sync may re-import it if still upstream."""
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
