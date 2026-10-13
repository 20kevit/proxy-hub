"""Telemetry, connections and operational report endpoints.

All endpoints require authentication. Inputs are validated and bounded.
No secrets, credentials or request bodies are ever returned.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func

from ..models import AuditLog, ProbeTelemetry, ProxyNode
from .deps import current_user, db_session

router = APIRouter()

ALLOWED_PROBE_TYPES = {"fast", "active", "deep"}
MAX_LIMIT = 500
MAX_HOURS = 168  # 7 days


def _parse_window(hours: int) -> tuple[datetime, datetime]:
    if hours < 1 or hours > MAX_HOURS:
        raise HTTPException(400, "hours must be 1..168")
    now = datetime.now(timezone.utc)
    return now - timedelta(hours=hours), now


def _percentile(sorted_vals: list[float], pct: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return round(sorted_vals[0], 1)
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    f, c = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return round(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f), 1)


@router.get("/api/telemetry/summary")
def telemetry_summary(hours: int = 24, node_id: int = 0, probe_type: str = "",
                      user: str = Depends(current_user)):
    start, now = _parse_window(hours)
    if probe_type and probe_type not in ALLOWED_PROBE_TYPES:
        raise HTTPException(400, "invalid probe_type")
    db = db_session()
    try:
        q = db.query(ProbeTelemetry).filter(ProbeTelemetry.created_at >= start)
        if node_id:
            q = q.filter(ProbeTelemetry.node_id == node_id)
        if probe_type:
            q = q.filter(ProbeTelemetry.probe_type == probe_type)
        rows = q.order_by(ProbeTelemetry.id.desc()).limit(20000).all()
        total = len(rows)
        ok_rows = [r for r in rows if r.ok]
        ok_count = len(ok_rows)
        lats = sorted([r.latency_ms for r in ok_rows if r.latency_ms is not None])
        by_type: dict = {}
        for r in rows:
            b = by_type.setdefault(r.probe_type, {"total": 0, "ok": 0, "lat": []})
            b["total"] += 1
            if r.ok:
                b["ok"] += 1
                if r.latency_ms is not None:
                    b["lat"].append(r.latency_ms)
        by_type_out = {}
        for t, b in by_type.items():
            sl = sorted(b["lat"])
            by_type_out[t] = {
                "total": b["total"], "ok": b["ok"],
                "success_rate": round(b["ok"] / b["total"], 4) if b["total"] else None,
                "p50": _percentile(sl, 50), "p95": _percentile(sl, 95),
                "p99": _percentile(sl, 99), "samples": len(sl),
            }
        return {
            "window_hours": hours,
            "from": start.isoformat(), "to": now.isoformat(),
            "total": total, "ok": ok_count,
            "success_rate": round(ok_count / total, 4) if total else None,
            "p50": _percentile(lats, 50), "p95": _percentile(lats, 95),
            "p99": _percentile(lats, 99), "latency_samples": len(lats),
            "by_type": by_type_out,
            "sufficient_for_percentiles": len(lats) >= 20,
            "note": "percentiles require >=20 latency samples; App-level (deep) probes measure end-to-end HTTPS egress",
        }
    finally:
        db.close()


@router.get("/api/telemetry/series")
def telemetry_series(hours: int = 24, node_id: int = 0, probe_type: str = "",
                     bucket_minutes: int = 60, limit: int = 200,
                     user: str = Depends(current_user)):
    start, now = _parse_window(hours)
    if probe_type and probe_type not in ALLOWED_PROBE_TYPES:
        raise HTTPException(400, "invalid probe_type")
    if bucket_minutes not in (5, 15, 30, 60, 180, 360, 720, 1440):
        raise HTTPException(400, "invalid bucket_minutes")
    limit = max(1, min(limit, MAX_LIMIT))
    db = db_session()
    try:
        q = db.query(ProbeTelemetry).filter(ProbeTelemetry.created_at >= start)
        if node_id:
            q = q.filter(ProbeTelemetry.node_id == node_id)
        if probe_type:
            q = q.filter(ProbeTelemetry.probe_type == probe_type)
        rows = q.order_by(ProbeTelemetry.created_at.asc()).limit(20000).all()
        buckets: dict[str, dict] = {}
        for r in rows:
            ts = r.created_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            bkey = ts.replace(minute=(ts.minute // bucket_minutes) * bucket_minutes,
                              second=0, microsecond=0).isoformat()
            b = buckets.setdefault(bkey, {"total": 0, "ok": 0, "lat": []})
            b["total"] += 1
            if r.ok:
                b["ok"] += 1
                if r.latency_ms is not None:
                    b["lat"].append(r.latency_ms)
        series = []
        for bkey in sorted(buckets):
            b = buckets[bkey]
            sl = sorted(b["lat"])
            series.append({
                "bucket": bkey, "total": b["total"], "ok": b["ok"],
                "success_rate": round(b["ok"] / b["total"], 4) if b["total"] else None,
                "p50": _percentile(sl, 50), "p95": _percentile(sl, 95), "samples": len(sl),
            })
        # Paginated raw rows (newest first)
        q2 = db.query(ProbeTelemetry).filter(ProbeTelemetry.created_at >= start)
        if node_id:
            q2 = q2.filter(ProbeTelemetry.node_id == node_id)
        if probe_type:
            q2 = q2.filter(ProbeTelemetry.probe_type == probe_type)
        raw = q2.order_by(ProbeTelemetry.id.desc()).limit(limit).all()
        id2name = {n.id: n.name for n in db.query(ProxyNode).all()}
        return {
            "window_hours": hours, "bucket_minutes": bucket_minutes,
            "from": start.isoformat(), "to": now.isoformat(),
            "series": series,
            "rows": [
                {"id": r.id, "node_id": r.node_id,
                 "node_name": id2name.get(r.node_id, f"#{r.node_id}"),
                 "probe_type": r.probe_type, "ok": bool(r.ok),
                 "latency_ms": r.latency_ms, "error_type": r.error_type or "",
                 "http_status": r.http_status,
                 "at": r.created_at.isoformat() if r.created_at else None}
                for r in raw
            ],
        }
    finally:
        db.close()


@router.get("/api/connections")
def connections(limit: int = 100, user: str = Depends(current_user)):
    """Honest connection visibility.

    Sources: proxyhub-xray journal tail (accepted lines) + ss listener snapshot
    + active outbound node. No process-level attribution is claimed.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    out: dict = {"source": "proxyhub-xray journal + ss snapshot",
                 "limitation": "Xray access entries show client IP:port, destination host:port, inbound/outbound tags. They do NOT identify OS processes or applications.",
                 "listeners": {}, "recent": [], "active_outbound": None}
    try:
        proc = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=8)
        for want, label in ((":1081", "http"), (":2081", "socks"),
                            (":8389", "gw-http"), (":8388", "gw-socks")):
            out["listeners"][label] = want in (proc.stdout or "")
    except Exception:
        out["listeners"] = {}
    try:
        db = db_session()
        try:
            active = db.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).first()
            if active:
                out["active_outbound"] = {"id": active.id, "name": active.name,
                                         "protocol": active.protocol,
                                         "quality_score": active.quality_score}
        finally:
            db.close()
    except Exception:
        pass
    try:
        proc = subprocess.run(
            ["journalctl", "-u", "proxyhub-xray", "-n", str(limit * 2),
             "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=10)
        import re
        pat = re.compile(
            r"(\S+) from (tcp:|udp:)?(\S+?) accepted (tcp:|udp:)?(\S+?):(\d+) \[([^\]]+)\]")
        for line in (proc.stdout or "").splitlines()[-limit * 2:]:
            m = pat.search(line)
            if not m:
                continue
            ts, _, client, _, dest, port, tags = m.groups()
            inb, _, outb = tags.partition(" -> ")
            out["recent"].append({
                "at": ts, "client": client, "dest": dest, "port": int(port),
                "inbound": inb.strip(), "outbound": outb.strip(),
            })
            if len(out["recent"]) >= limit:
                break
        out["recent"] = out["recent"][-limit:]
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}"
    return out


@router.get("/api/report/summary")
def report_summary(hours: int = 48, user: str = Depends(current_user)):
    """Operational report over an explicit window with sample counts."""
    start, now = _parse_window(hours)
    db = db_session()
    try:
        import psutil
        total = db.query(func.count(ProbeTelemetry.id)).filter(
            ProbeTelemetry.created_at >= start).scalar() or 0
        okc = db.query(func.count(ProbeTelemetry.id)).filter(
            ProbeTelemetry.created_at >= start, ProbeTelemetry.ok.is_(True)).scalar() or 0
        deep_total = db.query(func.count(ProbeTelemetry.id)).filter(
            ProbeTelemetry.created_at >= start,
            ProbeTelemetry.probe_type == "deep").scalar() or 0
        deep_ok = db.query(func.count(ProbeTelemetry.id)).filter(
            ProbeTelemetry.created_at >= start, ProbeTelemetry.probe_type == "deep",
            ProbeTelemetry.ok.is_(True)).scalar() or 0
        lats = [r[0] for r in db.query(ProbeTelemetry.latency_ms).filter(
            ProbeTelemetry.created_at >= start, ProbeTelemetry.probe_type == "deep",
            ProbeTelemetry.ok.is_(True), ProbeTelemetry.latency_ms.isnot(None)
        ).order_by(ProbeTelemetry.id.desc()).limit(5000).all()]
        sl = sorted(lats)
        fails = db.query(AuditLog).filter(
            AuditLog.created_at >= start,
            AuditLog.action == "node.activate").order_by(AuditLog.id.desc()).limit(100).all()
        failovers = [e for e in fails if e.actor == "failover"]
        rotations = [e for e in fails if e.actor == "rotation"]
        first_tel = db.query(func.min(ProbeTelemetry.created_at)).scalar()
        last_tel = db.query(func.max(ProbeTelemetry.created_at)).scalar()
        du = psutil.disk_usage("/")
        mem = psutil.virtual_memory()
        return {
            "window_hours": hours,
            "from": start.isoformat(), "to": now.isoformat(),
            "telemetry": {
                "total": total, "ok": okc,
                "success_rate": round(okc / total, 4) if total else None,
                "deep_total": deep_total, "deep_ok": deep_ok,
                "deep_success_rate": round(deep_ok / deep_total, 4) if deep_total else None,
                "deep_p50": _percentile(sl, 50), "deep_p95": _percentile(sl, 95),
                "deep_p99": _percentile(sl, 99), "deep_samples": len(sl),
                "sufficient_for_percentiles": len(sl) >= 20,
                "first_record": first_tel.isoformat() if first_tel else None,
                "last_record": last_tel.isoformat() if last_tel else None,
            },
            "transitions": {
                "failovers": len(failovers),
                "rotations": len(rotations),
                "recent": [{"at": e.created_at.isoformat() if e.created_at else None,
                            "actor": e.actor, "detail": e.detail} for e in fails[:20]],
            },
            "resources": {"disk_pct": du.percent, "mem_pct": mem.percent},
            "caveats": [
                "Interruption duration is not instrumented; failover counts reflect config transitions, not measured downtime.",
                "Monitoring gaps are inferred from telemetry absence, not from an explicit gap log.",
                "Percentiles computed only from successful deep (HTTPS egress) probes in window.",
            ],
        }
    finally:
        db.close()
