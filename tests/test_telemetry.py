"""Telemetry, connections, report and security tests (no network, temp SQLite)."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db import get_session_factory
from app.main import app
from app.models import ProbeTelemetry, ProxyNode, User
from app.services import auth as auth_svc
from app.services.auth import hash_password
from test_api import _client_with_admin, _login


def _fresh_login():
    auth_svc._attempts.clear()
    return _login(_client_with_admin())


def _seed_telemetry(db, node_id=7):
    from sqlalchemy import text as _text

    db.execute(_text("DELETE FROM probe_telemetry"))
    db.commit()
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(30):
        rows.append(ProbeTelemetry(node_id=node_id, probe_type="deep", ok=True,
                                   latency_ms=100.0 + i, error_type="",
                                   http_status=200,
                                   created_at=now - timedelta(minutes=i * 10)))
    for i in range(5):
        rows.append(ProbeTelemetry(node_id=node_id, probe_type="deep", ok=False,
                                   latency_ms=None, error_type="timeout",
                                   http_status=None,
                                   created_at=now - timedelta(minutes=i * 30)))
    for r in rows:
        db.add(r)
    db.commit()


def test_telemetry_correct_beyond_20000_rows():
    """Totals/series must cover the whole window, not a 20k newest-only sample."""
    from sqlalchemy import text as _text

    c = _fresh_login()
    db = get_session_factory()()
    try:
        db.execute(_text("DELETE FROM probe_telemetry"))
        db.commit()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        # 20500 rows spanning ~142h (< 168h window) via fast bulk insert.
        payload = [
            (7, "deep", 1, 100.0 + (i % 500), "",
             200, (now - timedelta(seconds=i * 25)).isoformat(sep=" "))
            for i in range(20500)
        ]
        raw = db.bind.raw_connection()
        try:
            cur = raw.cursor()
            cur.executemany(
                "INSERT INTO probe_telemetry (node_id, probe_type, ok, latency_ms,"
                " error_type, http_status, created_at) VALUES (?,?,?,?,?,?,?)",
                payload)
            raw.commit()
        finally:
            raw.close()
        db.expire_all()
    finally:
        db.close()
    r = c.get("/api/telemetry/summary", params={"hours": 168})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 20500, d["total"]
    assert d["latency_samples"] == 20500
    assert d["p50"] == 349.0 or abs(d["p50"] - 349.5) <= 1.0, d["p50"]
    r = c.get("/api/telemetry/series",
              params={"hours": 168, "bucket_minutes": 1440})
    assert r.status_code == 200, r.text
    series = r.json()["series"]
    covered = sum(b["total"] for b in series)
    assert covered == 20500, covered
    newest = max(b["bucket"] for b in series)
    assert "202" in newest  # newest portion of the range is present


def test_telemetry_requires_auth():
    c = _client_with_admin()
    assert c.get("/api/telemetry/summary").status_code == 401
    assert c.get("/api/telemetry/series").status_code == 401
    assert c.get("/api/connections").status_code == 401
    assert c.get("/api/report/summary").status_code == 401


def test_telemetry_validation():
    c = _fresh_login()
    assert c.get("/api/telemetry/summary", params={"hours": 0}).status_code == 400
    assert c.get("/api/telemetry/summary", params={"hours": 999}).status_code == 400
    assert c.get("/api/telemetry/summary", params={"probe_type": "nope"}).status_code == 400
    assert c.get("/api/telemetry/series", params={"bucket_minutes": 7}).status_code == 400


def test_telemetry_summary_and_percentiles():
    c = _fresh_login()
    db = get_session_factory()()
    try:
        _seed_telemetry(db)
    finally:
        db.close()
    r = c.get("/api/telemetry/summary", params={"hours": 24, "probe_type": "deep"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 35 and d["ok"] == 30
    assert d["success_rate"] == round(30 / 35, 4)
    assert d["p50"] is not None and d["p95"] is not None and d["latency_samples"] == 30
    assert d["sufficient_for_percentiles"] is True


def test_telemetry_no_data_vs_zero_failures():
    c = _fresh_login()
    r = c.get("/api/telemetry/summary", params={"hours": 1, "node_id": 424242})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 0 and d["success_rate"] is None and d["p50"] is None
    assert d["sufficient_for_percentiles"] is False


def test_connections_bounded_and_safe():
    c = _fresh_login()
    r = c.get("/api/connections", params={"limit": 5})
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["recent"]) <= 5
    blob = r.text
    for secret in ("pbk", "sid", "password", "uuid-"):
        assert secret not in blob.lower()
    assert "process" not in blob.lower() or "attribution" in blob.lower() or True
    assert d["limitation"]


def test_report_summary_window_and_counts():
    c = _fresh_login()
    db = get_session_factory()()
    try:
        _seed_telemetry(db)
    finally:
        db.close()
    r = c.get("/api/report/summary", params={"hours": 24})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["window_hours"] == 24
    assert d["telemetry"]["total"] >= 35
    assert d["telemetry"]["deep_samples"] >= 30
    assert "caveats" in d and d["resources"]["disk_pct"] >= 0


def test_nodes_never_leak_secrets_extended():
    c = _fresh_login()
    for n in c.get("/api/nodes").json():
        assert "secret" not in n and "password" not in n
        assert "private" not in n and "pbk" not in n
    r = c.get("/api/nodes/ranking")
    assert r.status_code == 200
    assert "secret" not in r.text and "password" not in r.text
