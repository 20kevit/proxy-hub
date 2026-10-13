"""Telemetry, connections and operational report endpoints.

All endpoints require authentication. Inputs are validated and bounded.
No secrets, credentials or request bodies are ever returned.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Integer, case, func

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


def _base_query(db, start, node_id: int = 0, probe_type: str = ""):
    q = db.query(ProbeTelemetry).filter(ProbeTelemetry.created_at >= start.replace(tzinfo=None))
    if node_id:
        q = q.filter(ProbeTelemetry.node_id == node_id)
    if probe_type:
        q = q.filter(ProbeTelemetry.probe_type == probe_type)
    return q


def _latency_at_offset(db, start, pct: float, node_id: int = 0,
                       probe_type: str = "") -> tuple[float | None, int]:
    """Exact-ish percentile via ORDER BY + OFFSET (3 bounded queries, no full fetch)."""
    base = _base_query(db, start, node_id, probe_type).filter(
        ProbeTelemetry.ok.is_(True), ProbeTelemetry.latency_ms.isnot(None))
    count = base.with_entities(func.count(ProbeTelemetry.id)).scalar() or 0
    if not count:
        return None, 0
    offset = min(count - 1, int((count - 1) * (pct / 100.0)))
    row = (base.with_entities(ProbeTelemetry.latency_ms)
           .order_by(ProbeTelemetry.latency_ms.asc())
           .offset(offset).limit(1).first())
    return (round(row[0], 1) if row and row[0] is not None else None), count


@router.get("/api/telemetry/summary")
def telemetry_summary(hours: int = 24, node_id: int = 0, probe_type: str = "",
                      user: str = Depends(current_user)):
    start, now = _parse_window(hours)
    if probe_type and probe_type not in ALLOWED_PROBE_TYPES:
        raise HTTPException(400, "invalid probe_type")
    db = db_session()
    try:
        # Exact totals via aggregate COUNT (correct at any dataset size).
        total = _base_query(db, start, node_id, probe_type).with_entities(
            func.count(ProbeTelemetry.id)).scalar() or 0
        ok_count = _base_query(db, start, node_id, probe_type).filter(
            ProbeTelemetry.ok.is_(True)).with_entities(
            func.count(ProbeTelemetry.id)).scalar() or 0
        p50, n_lat = _latency_at_offset(db, start, 50, node_id, probe_type)
        p95, _ = _latency_at_offset(db, start, 95, node_id, probe_type)
        p99, _ = _latency_at_offset(db, start, 99, node_id, probe_type)
        by_type_out = {}
        for t, in db.query(ProbeTelemetry.probe_type).filter(
                ProbeTelemetry.created_at >= start.replace(tzinfo=None)).distinct().all():
            if probe_type and t != probe_type:
                continue
            bt = _base_query(db, start, node_id, t).with_entities(
                func.count(ProbeTelemetry.id)).scalar() or 0
            bo = _base_query(db, start, node_id, t).filter(
                ProbeTelemetry.ok.is_(True)).with_entities(
                func.count(ProbeTelemetry.id)).scalar() or 0
            tp50, tn = _latency_at_offset(db, start, 50, node_id, t)
            tp95, _ = _latency_at_offset(db, start, 95, node_id, t)
            tp99, _ = _latency_at_offset(db, start, 99, node_id, t)
            by_type_out[t] = {
                "total": bt, "ok": bo,
                "success_rate": round(bo / bt, 4) if bt else None,
                "p50": tp50, "p95": tp95, "p99": tp99, "samples": tn,
            }
        return {
            "window_hours": hours,
            "from": start.isoformat(), "to": now.isoformat(),
            "total": total, "ok": ok_count,
            "success_rate": round(ok_count / total, 4) if total else None,
            "p50": p50, "p95": p95, "p99": p99, "latency_samples": n_lat,
            "by_type": by_type_out,
            "sufficient_for_percentiles": n_lat >= 20,
            "note": "totals are exact DB counts; percentiles via ordered offsets; "
                    "deep probes measure end-to-end HTTPS egress",
        }
    finally:
        db.close()


@router.get("/api/telemetry/series")
def telemetry_series(hours: int = 24, node_id: int = 0, probe_type: str = "",
                     bucket_minutes: int = 60, limit: int = 200,
                     before_id: int = 0, user: str = Depends(current_user)):
    start, now = _parse_window(hours)
    if probe_type and probe_type not in ALLOWED_PROBE_TYPES:
        raise HTTPException(400, "invalid probe_type")
    if bucket_minutes not in (5, 15, 30, 60, 180, 360, 720, 1440):
        raise HTTPException(400, "invalid bucket_minutes")
    limit = max(1, min(limit, MAX_LIMIT))
    db = db_session()
    try:
        # Database-side bucketing: one GROUP BY query regardless of dataset size.
        # Buckets with zero rows are absent (missing data is never fabricated).
        secs = bucket_minutes * 60
        ok_latency = case((ProbeTelemetry.ok.is_(True), ProbeTelemetry.latency_ms),
                          else_=None)
        bucket = func.datetime(
            (func.strftime("%s", ProbeTelemetry.created_at) / secs).cast(Integer) * secs,
            "unixepoch")
        bq = db.query(
            bucket.label("bucket"),
            func.count(ProbeTelemetry.id).label("total"),
            func.sum(func.cast(ProbeTelemetry.ok, Integer)).label("ok"),
            func.avg(ok_latency).label("avg_latency"),
            func.max(ok_latency).label("max_latency"),
            func.count(case((ok_latency.isnot(None), 1), else_=None)).label("samples"),
        ).filter(ProbeTelemetry.created_at >= start.replace(tzinfo=None))
        if node_id:
            bq = bq.filter(ProbeTelemetry.node_id == node_id)
        if probe_type:
            bq = bq.filter(ProbeTelemetry.probe_type == probe_type)
        series = []
        for bkey, total, okc, avg_lat, max_lat, samples in bq.group_by(bucket).order_by(bucket).all():
            okc = int(okc or 0)
            # SQLite returns "YYYY-MM-DD HH:MM:SS"; emit strict ISO-8601 UTC.
            iso_bucket = bkey.replace(" ", "T") + "+00:00" if bkey else bkey
            series.append({
                "bucket": iso_bucket,
                "total": total, "ok": okc,
                "success_rate": round(okc / total, 4) if total else None,
                "avg_latency": round(avg_lat, 1) if avg_lat is not None else None,
                "max_latency": round(max_lat, 1) if max_lat is not None else None,
                "samples": samples,
            })
        # Keyset-paginated raw rows (newest first; before_id for stable paging).
        q2 = _base_query(db, start, node_id, probe_type)
        if before_id:
            q2 = q2.filter(ProbeTelemetry.id < before_id)
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
        deep_p50, deep_n = _latency_at_offset(db, start, 50, 0, "deep")
        deep_p95, _ = _latency_at_offset(db, start, 95, 0, "deep")
        deep_p99, _ = _latency_at_offset(db, start, 99, 0, "deep")
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
                "deep_p50": deep_p50, "deep_p95": deep_p95,
                "deep_p99": deep_p99, "deep_samples": deep_n,
                "sufficient_for_percentiles": deep_n >= 20,
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
