"""API tests with TestClient (no network, temp SQLite)."""
from fastapi.testclient import TestClient

from app.db import get_session_factory
from app.main import app
from app.models import User
from app.services.auth import hash_password


def _client_with_admin():
    from app.db import init_db, reset_engine
    from app.config import refresh_settings

    reset_engine()
    refresh_settings()
    init_db()
    db = get_session_factory()()
    try:
        if not db.query(User).filter(User.username == "admin").first():
            db.add(User(username="admin", password_hash=hash_password("pw123456")))
            db.commit()
    finally:
        db.close()
    return TestClient(app)


def _login(c: TestClient):
    r = c.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
    assert r.status_code == 200, r.text
    csrf = r.json()["csrf"]
    c.headers.update({"x-csrf-token": csrf})
    return c


def test_login_and_me():
    c = _login(_client_with_admin())
    r = c.get("/api/auth/me")
    assert r.json()["username"] == "admin"


def test_login_rejects_bad_password():
    c = _client_with_admin()
    r = c.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 401


def test_csrf_enforced():
    c = _client_with_admin()
    c.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
    c.headers.pop("x-csrf-token", None)
    r = c.post("/api/subscription/update")
    assert r.status_code == 403


def test_nodes_subscription_never_leaks_url():
    c = _login(_client_with_admin())
    body = c.get("/api/subscription").text
    assert "FAKE-SECRET-99" not in body  # secret fragment must never be echoed
    assert "url_masked" in body
    for n in c.get("/api/nodes").json():
        assert "secret" not in n


def test_status_shape():
    c = _login(_client_with_admin())
    r = c.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    for key in ("running", "nodes", "subscription", "resources", "system_xray"):
        assert key in data


def test_activate_unknown_node_404():
    c = _login(_client_with_admin())
    assert c.post("/api/nodes/424242/activate").status_code == 404


def test_activate_requires_csrf():
    c = _client_with_admin()
    c.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
    c.headers.pop("x-csrf-token", None)
    assert c.post("/api/nodes/1/activate").status_code == 403


def test_activate_ineligible_node_400():
    from app.models import ProxyNode
    c = _login(_client_with_admin())
    db = get_session_factory()()
    try:
        db.add(ProxyNode(fingerprint="fp-off", name="Off", protocol="vless",
                         server="x.ir", port=443, enabled=False))
        db.commit()
        nid = db.query(ProxyNode).filter(ProxyNode.fingerprint == "fp-off").first().id
    finally:
        db.close()
    assert c.post(f"/api/nodes/{nid}/activate").status_code == 400


def test_public_health_minimal():
    from fastapi.testclient import TestClient as TC
    c = _client_with_admin()
    r = c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) <= {"ok", "ts"}
    assert "secret" not in r.text and "password" not in r.text
