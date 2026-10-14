"""Regression tests for the remediation findings (isolated temp DB, fakes only)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.db import get_session_factory
from app.models import ProxyNode
from test_api import _client_with_admin, _login


def _fresh_login():
    """Login with a cleared rate-limit bucket (in-memory limiter is shared)."""
    from app.services import auth as _auth

    _auth._attempts.clear()
    return _login(_client_with_admin())


def _mk(db, name, active=False, **kw):
    now = datetime.now(timezone.utc)
    base = dict(
        fingerprint=f"fp-{name}", name=name, protocol="vless", server="x.ir", port=443,
        health="healthy", latency_ms=50.0, egress_ok=True,
        last_checked=now, last_success=now, last_state_change=now,
        consecutive_failures=0, consecutive_successes=10,
        total_probes=100, successful_probes=100, quality_score=70.0,
        is_active=active,
    )
    base.update(kw)
    n = ProxyNode(**base)
    db.add(n)
    db.commit()
    return n


# --- 1. core status reporting -------------------------------------------

def test_xray_status_never_raises(monkeypatch):
    import subprocess as _sp

    from app.services.cores import SingBoxAdapter, XrayAdapter

    assert XrayAdapter().status() in (
        {"active": True, "state": "active"},
        {"active": False, "state": XrayAdapter().status()["state"]},
    )

    class P:
        returncode = 0
        stdout = "active\n"
        stderr = ""

    monkeypatch.setattr(_sp, "run", lambda *a, **k: P())
    assert XrayAdapter().status() == {"active": True, "state": "active"}

    class P2:
        returncode = 1
        stdout = "inactive\n"
        stderr = ""

    monkeypatch.setattr(_sp, "run", lambda *a, **k: P2())
    assert XrayAdapter().status() == {"active": False, "state": "inactive"}

    def boom(*a, **k):
        raise FileNotFoundError("no systemctl")

    monkeypatch.setattr(_sp, "run", boom)
    assert XrayAdapter().status() == {"active": False, "state": "missing"}

    def weird(*a, **k):
        class Q:
            returncode = 0
            stdout = "weird-state\n"
            stderr = ""
        return Q()

    monkeypatch.setattr(_sp, "run", weird)
    assert XrayAdapter().status() == {"active": False, "state": "unknown"}
    # sing-box consistency: not installed here -> explicit state, never raises
    assert SingBoxAdapter().status()["state"] in (
        "not-installed", "active", "inactive", "failed", "unknown", "missing")


# --- 3. circuit breaker timing ------------------------------------------

def test_circuit_breaker_opens_with_future_deadline(db, monkeypatch):
    from app.services import health_v2 as hv

    monkeypatch.setattr(hv, "verify_node_egress", None, raising=False)
    n = _mk(db, "cb-node")
    n.consecutive_failures = 4
    db.commit()
    eng = hv.HealthEngineV2()
    result = hv.ProbeResult(ok=False, error="http_timeout", error_type="timeout")
    eng._update_deep_metrics(db, n, result)
    db.commit()
    assert n.circuit_open is True
    until = n.circuit_open_until
    assert until is not None
    aware = until if until.tzinfo else until.replace(tzinfo=timezone.utc)
    delta = (aware - datetime.now(timezone.utc)).total_seconds()
    assert delta >= 55, f"breaker must stay open ~cooldown, got {delta}s"


def test_circuit_recovery_after_expiry(db, monkeypatch):
    from app.services import health_v2 as hv

    n = _mk(db, "rec-node")
    n.circuit_open = True
    n.circuit_open_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    n.consecutive_failures = 5
    db.commit()
    # Expired breaker reopens for probing (TCP probe faked: no network).
    monkeypatch.setattr(hv, "tcp_probe",
                        lambda host, port, timeout=2.0: hv.ProbeResult(ok=True, tcp_latency_ms=9.0))
    eng = hv.HealthEngineV2()
    eng._fast_check_all_sync()
    db.expire_all()
    row = db.query(ProxyNode).filter(ProxyNode.id == n.id).first()
    assert row.circuit_open is False


def test_circuit_stays_open_until_deadline(db, monkeypatch):
    from app.services import health_v2 as hv

    probed = []
    n = _mk(db, "held-node")
    n.circuit_open = True
    n.circuit_open_until = datetime.now(timezone.utc) + timedelta(seconds=600)
    db.commit()

    def spy(host, port, timeout=2.0):
        probed.append(host)
        return hv.ProbeResult(ok=True, tcp_latency_ms=9.0)

    monkeypatch.setattr(hv, "tcp_probe", spy)
    hv.HealthEngineV2()._fast_check_all_sync()
    assert probed == []  # open breaker: node skipped, no probe wasted
    db.expire_all()
    assert db.query(ProxyNode).filter(ProxyNode.id == n.id).first().circuit_open is True


# --- 2D/4. eligibility matrix --------------------------------------------

def test_eligibility_matrix(db):
    from app.services.failover import node_eligibility

    now = datetime.now(timezone.utc)
    fresh = {"freshness": 1800, "min_quality": 30.0}
    ok_node = _mk(db, "elig-ok")
    assert node_eligibility(ok_node, now, **fresh)[0] is True

    cases = [
        ({"enabled": False}, False),
        ({"removed_upstream": True}, False),
        ({"circuit_open": True}, False),
        ({"health": "unhealthy"}, False),
        ({"health": "unknown"}, False),
        ({"egress_ok": False}, False),
        ({"quality_score": 10.0}, False),
    ]
    for i, (kw, expected) in enumerate(cases):
        n = _mk(db, f"elig-{i}", **kw)
        got, reason = node_eligibility(n, now, **fresh)
        assert got is expected, (kw, reason)
    stale = _mk(db, "elig-stale")
    stale.last_success = now - timedelta(hours=2)
    db.commit()
    assert node_eligibility(stale, now, **fresh)[0] is False

    from app.services.failover import eligible_candidates
    cands, rejected = eligible_candidates(db, exclude_id=None)
    assert any(c.name == "elig-ok" for c in cands)
    # disabled/removed/circuit-open rows are pre-filtered in SQL; the five
    # policy rejections below must be reported, not silently skipped.
    assert sum(rejected.values()) == 5, rejected
    assert any("no verified egress" in r for r in rejected)
    assert any("stale egress" in r for r in rejected)


# --- 6. rotation concurrency ---------------------------------------------

def test_rotation_single_winner_under_concurrency(db, monkeypatch):
    """Concurrent transitions serialize; exactly one node ends active."""
    import threading

    import app.services.failover as fo

    calls = []

    def fake_apply(self, node, session=None, force_restart=False):
        import time as _t
        calls.append(node.id)
        _t.sleep(0.05)

    monkeypatch.setattr("app.services.cores.XrayAdapter.apply", fake_apply)
    a = _mk(db, "conc-a")
    b = _mk(db, "conc-b")
    db.commit()
    ids = [a.id, b.id]
    errors = []

    def worker(nid):
        try:
            s = get_session_factory()()
            try:
                node = s.query(ProxyNode).filter(ProxyNode.id == nid).first()
                fo.activate_node(s, node, actor="t")
            finally:
                s.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in ids for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    db.expire_all()
    assert db.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).count() == 1


# --- 5B. subscription active removal -------------------------------------

def test_subscription_removed_active_enters_degraded(db, monkeypatch):
    """Vanished active node with no replacement -> explicit degraded state."""
    from app.services.subscription import parse_subscription_text, sync_nodes

    nodes, _ = parse_subscription_text(
        "vless://uuid-a@a.example.com:443?encryption=none&security=reality"
        "&sni=s.example.com&fp=chrome&pbk=K&sid=S&type=tcp#A")
    sync_nodes(db, nodes)
    node = db.query(ProxyNode).first()
    node.is_active = True
    node.egress_ok = True
    node.health = "healthy"
    node.last_success = datetime.now(timezone.utc)
    node.quality_score = 80.0
    db.commit()
    # Upstream drops everything and no other node exists.
    res = sync_nodes(db, [])
    assert db.query(ProxyNode).first().is_active is False
    assert res["active_replacement"].startswith("none: degraded")
    from app.services.failover import session_get
    assert session_get(db, "degraded_no_active") == "true"


def test_subscription_replaces_active_with_verified(db, monkeypatch):
    """Vanished active node with a healthy replacement -> verified switch."""
    import app.services.failover as fo
    from app.services.subscription import parse_subscription_text, sync_nodes

    monkeypatch.setattr("app.services.cores.XrayAdapter.apply",
                        lambda self, node, session=None, force_restart=False: None)
    monkeypatch.setattr("app.services.cores.verify_node_egress",
                        lambda node, url, timeout=15: type("R", (), {
                            "ok": True, "app_latency_ms": 100.0,
                            "error": "", "error_type": ""})())
    nodes, _ = parse_subscription_text(
        "vless://uuid-a@a.example.com:443?encryption=none&security=reality"
        "&sni=s.example.com&fp=chrome&pbk=K&sid=S&type=tcp#A")
    sync_nodes(db, nodes)
    old = db.query(ProxyNode).first()
    old.is_active = True
    db.commit()
    # A previously verified standby exists (probed in an earlier cycle).
    # source="manual" so this sync (source="subscription") leaves it alone.
    standby = _mk(db, "standby-node", source="manual")
    new_nodes, _ = parse_subscription_text(
        "vless://uuid-b@b.example.com:443?encryption=none&security=reality"
        "&sni=s.example.com&fp=chrome&pbk=K&sid=S&type=tcp#B")
    res = sync_nodes(db, new_nodes)
    # Freshly synced node B is unverified, so the verified standby wins.
    assert res.get("active_replacement", "").startswith("subscription-replacement: ")
    assert "standby-node" in res["active_replacement"]
    assert db.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).count() == 1
    assert db.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).first().name == "standby-node"


# --- activation failure: no false active ----------------------------------

def test_failed_activation_keeps_previous_active(db, monkeypatch):
    import app.services.failover as fo

    def boom(self, node, session=None, force_restart=False):
        from app.services.cores import CoreError
        raise CoreError("simulated restart failure")

    monkeypatch.setattr("app.services.cores.XrayAdapter.apply", boom)
    active = _mk(db, "keep-active", active=True)
    cand = _mk(db, "bad-cand")
    db.commit()
    import pytest
    with pytest.raises(Exception):
        fo.activate_node(db, cand)
    db.expire_all()
    assert db.query(ProxyNode).filter(ProxyNode.id == active.id).first().is_active is True
    assert db.query(ProxyNode).filter(ProxyNode.id == cand.id).first().is_active is False


# --- 7. gateway compatibility ----------------------------------------------

def test_hysteria2_gateway_matrix(db):
    import app.services.failover as fo
    from app.models import ProxyClient
    from app.services.failover import session_set

    hy = ProxyNode(fingerprint="fp-hy", name="HY", protocol="hysteria2",
                   server="x.ir", port=443, secret="p", params={},
                   health="healthy", egress_ok=True,
                   last_checked=datetime.now(timezone.utc),
                   last_success=datetime.now(timezone.utc),
                   last_state_change=datetime.now(timezone.utc),
                   consecutive_successes=5, total_probes=10, successful_probes=10,
                   quality_score=70.0)
    db.add(hy)
    db.commit()
    # gateway disabled -> compatible (passes gate; may fail later on binary)
    session_set(db, "gateway_enabled", "false")
    fo.check_gateway_compatible(db, hy)  # must not raise
    # gateway enabled -> explicit rejection before any mutation
    session_set(db, "gateway_enabled", "true")
    import pytest
    with pytest.raises(ValueError, match="incompatible"):
        fo.check_gateway_compatible(db, hy)
    # xray protocols always pass
    x = _mk(db, "vx")
    fo.check_gateway_compatible(db, x)


# --- 8. routing inbound consistency ------------------------------------------

def test_client_scoped_rule_requires_dedicated_port(db):
    from app.models import ProxyClient, RouteRule
    from app.services.auth import hash_password
    from app.services.routing_engine import validate_client_scope
    from app.services.routing_engine import RuleError
    import pytest

    c = ProxyClient(username="nodedi", password_hash=hash_password("x" * 12),
                    password_enc="")
    db.add(c)
    db.commit()
    with pytest.raises(RuleError):
        validate_client_scope(db, "nodedi")
    c.dedicated_port = 42999
    db.commit()
    validate_client_scope(db, "nodedi")  # must not raise
    with pytest.raises(RuleError):
        validate_client_scope(db, "ghost")


# --- 10. protocol parameter preservation --------------------------------------

def test_protocol_params_preserved():
    from app.services.cores import (_ss_outbound, _trojan_outbound,
                                    _vless_outbound, _vmess_outbound)
    from app.models import ProxyNode

    def N(**kw):
        base = dict(protocol="vless", server="s.ir", port=443, secret="u",
                    security="", params={})
        base.update(kw)
        return ProxyNode(fingerprint="x", name="n", **base)

    vless = _vless_outbound(N(protocol="vless", security="tls",
                              params={"sni": "e.com", "fp": "chrome",
                                      "alpn": "h2,http/1.1", "type": "tcp"}))
    assert vless["streamSettings"]["tlsSettings"]["serverName"] == "e.com"
    assert vless["streamSettings"]["tlsSettings"]["alpn"] == ["h2", "http/1.1"]

    vmess = _vmess_outbound(N(protocol="vmess", security="tls",
                              params={"net": "ws", "tls": "tls", "sni": "e.com",
                                      "path": "/p", "host": "h.com", "aid": "0"}))
    assert vmess["streamSettings"]["tlsSettings"]["serverName"] == "e.com"
    assert vmess["streamSettings"]["wsSettings"]["path"] == "/p"

    trojan = _trojan_outbound(N(protocol="trojan", security="tls",
                                params={"sni": "e.com", "type": "tcp"}))
    assert trojan["streamSettings"]["tlsSettings"]["serverName"] == "e.com"

    ss = _ss_outbound(N(protocol="ss", params={"method": "2022-blake3-aes-128-gcm"}))
    assert ss["settings"]["servers"][0]["method"] == "2022-blake3-aes-128-gcm"


def test_hysteria2_unsupported_tls_modes_rejected():
    from app.services.cores import CoreError, SingBoxAdapter
    from app.models import ProxyNode
    import pytest

    def N(**params):
        return ProxyNode(fingerprint="x", name="n", protocol="hysteria2",
                         server="s.ir", port=443, secret="p", params=params)

    with pytest.raises(CoreError, match="insecure"):
        SingBoxAdapter().generate(N(insecure="1"))
    with pytest.raises(CoreError, match="pinSHA256"):
        SingBoxAdapter().generate(N(pinSHA256="abc"))
    ok = SingBoxAdapter().generate(N(sni="e.com", obfs="salamander",
                                     obfs_password="pw123"))
    assert ok["outbounds"][0]["obfs"] == {"type": "salamander", "password": "pw123"}


# --- 5A/11. API validation boundaries ------------------------------------------

def test_failover_config_schema_bounds():
    cl = _fresh_login()
    assert cl.put("/api/failover/config", json={"threshold": 0}).status_code == 422
    assert cl.put("/api/failover/config", json={"cooldown": -5}).status_code == 422
    assert cl.put("/api/failover/config", json={"mode": "sometimes"}).status_code == 400
    r = cl.put("/api/failover/config", json={"threshold": 5})
    assert r.status_code == 200 and r.json()["threshold"] == 5


def test_settings_reject_bad_values():
    cl = _fresh_login()
    assert cl.put("/api/settings", json={"failover_mode": "sometimes"}).status_code == 400
    assert cl.put("/api/settings", json={"gateway_bind": "8.8.8.8"}).status_code == 400
    assert cl.put("/api/settings", json={"public_socks_port": 80}).status_code == 400
    assert cl.put("/api/settings", json={"health_url": "not-a-url"}).status_code == 422
    assert cl.put("/api/settings", json={"health_url": "http://169.254.169.254/"}).status_code == 422


def test_subscription_url_validation():
    cl = _fresh_login()
    assert cl.put("/api/subscription", json={"url": "not-a-url"}).status_code == 422
    assert cl.put("/api/subscription", json={"url": "ftp://x/y"}).status_code == 422
    assert cl.put("/api/subscription", json={"url": "http://169.254.169.254/"}).status_code == 422
    assert cl.put("/api/subscription", json={"url": "http://127.0.0.1/x"}).status_code == 422


# --- 5C. session revocation -----------------------------------------------------

def test_logout_revokes_token():
    from app.services import auth as _auth

    _auth._attempts.clear()
    c = _client_with_admin()
    r = c.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
    assert r.status_code == 200
    csrf = r.json()["csrf"]
    c.headers.update({"x-csrf-token": csrf})
    assert c.get("/api/auth/me").status_code == 200
    assert c.post("/api/auth/logout").status_code == 200
    # Old token must now be rejected server-side (cookie still present).
    assert c.get("/api/auth/me").status_code == 401


def test_password_change_revokes_tokens(db):
    from app.models import User
    from app.services.auth import (bump_session_version, create_session,
                                   hash_password, parse_session_verified)

    db.add(User(username="u1", password_hash=hash_password("x" * 12)))
    db.commit()
    tok = create_session("u1", 0)
    assert parse_session_verified(tok, db) == "u1"
    bump_session_version(db, "u1")
    assert parse_session_verified(tok, db) is None
    tok2 = create_session("u1", 1)
    assert parse_session_verified(tok2, db) == "u1"
    # forged version field fails signature
    assert parse_session_verified(tok[:-1] + ("0" if tok[-1] != "0" else "1"), db) is None


def test_missing_secret_key_fails_closed(monkeypatch):
    import app.services.clients as cl

    monkeypatch.delenv("PROXYHUB_SECRET_KEY", raising=False)
    monkeypatch.setattr("app.services.clients._HAS_CRYPTO", True)
    import pytest
    with pytest.raises(RuntimeError, match="PROXYHUB_SECRET_KEY"):
        cl._fernet()


# --- 9. validate-before-replace + rollback -----------------------------------------

def _fake_settings(tmp_path):
    import types

    return types.SimpleNamespace(
        xray_config=tmp_path / "xray.json",
        xray_bin="/nonexistent-xray",
        socks_port=2081, http_port=1081,
    )


def _vless_node():
    from app.models import ProxyNode
    return ProxyNode(fingerprint="fp-v", name="V", protocol="vless",
                     server="s.ir", port=443, secret="u", params={})


def test_invalid_candidate_preserves_live_config(tmp_path, monkeypatch):
    from app.services import cores as C

    live = tmp_path / "xray.json"
    live.write_text('{"known": "good"}')
    monkeypatch.setattr(C, "get_settings", lambda: _fake_settings(tmp_path))
    monkeypatch.setattr(C.XrayAdapter, "_validate_path",
                        lambda self, path: (_ for _ in ()).throw(
                            C.CoreError("simulated invalid candidate")))
    import pytest
    with pytest.raises(C.CoreError):
        C.XrayAdapter().apply(_vless_node(), session=None)
    assert live.read_text() == '{"known": "good"}'
    assert list(tmp_path.glob("xray-*.json")) == []  # temp cleaned up


def test_restart_failure_restores_previous_config(tmp_path, monkeypatch):
    from app.services import cores as C

    live = tmp_path / "xray.json"
    live.write_text('{"known": "good"}')
    monkeypatch.setattr(C, "get_settings", lambda: _fake_settings(tmp_path))
    monkeypatch.setattr(C.XrayAdapter, "_validate_path", lambda self, path: None)
    monkeypatch.setattr(C.XrayAdapter, "_svc",
                        lambda self, action: (_ for _ in ()).throw(
                            C.CoreError(f"simulated {action} failure")))
    import pytest
    with pytest.raises(C.CoreError, match="previous config restored"):
        C.XrayAdapter().apply(_vless_node(), session=None, force_restart=True)
    assert live.read_text() == '{"known": "good"}'


# --- 8. migrations -------------------------------------------------------------------

def test_migrations_idempotent_and_versioned(tmp_path):
    import sqlalchemy as sa
    from app import migrations as mig

    url = f"sqlite:///{tmp_path}/old.db"
    eng = sa.create_engine(url)
    with eng.begin() as conn:
        # Simulate a pre-v1 database: users + bare proxy_nodes only.
        conn.execute(sa.text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64),"
            " password_hash VARCHAR(256), created_at DATETIME)"))
        conn.execute(sa.text(
            "CREATE TABLE proxy_nodes (id INTEGER PRIMARY KEY,"
            " fingerprint VARCHAR(64), name VARCHAR(256))"))
        conn.execute(sa.text(
            "INSERT INTO proxy_nodes (id, fingerprint, name) VALUES (1, 'fp', 'Old')"))
    with eng.begin() as conn:
        assert mig.current_version(conn) == 0
        assert mig.migrate(conn) == mig.SCHEMA_VERSION
        # Data preserved, new columns present.
        assert conn.execute(sa.text("SELECT name FROM proxy_nodes WHERE id=1")).first()[0] == "Old"
        cols = {c["name"] for c in sa.inspect(conn).get_columns("proxy_nodes")}
        assert {"egress_ok", "quality_score", "probe_history", "consecutive_failures"} <= cols
        assert "probe_telemetry" in {t for t in sa.inspect(conn).get_table_names()}
        assert "session_version" in {c["name"] for c in sa.inspect(conn).get_columns("users")}
        # Second run is a no-op.
        assert mig.migrate(conn) == mig.SCHEMA_VERSION
    eng.dispose()


# --- 5F + misc --------------------------------------------------------------------

def test_max_connections_is_metadata():
    c = _login_here()
    r = c.get("/api/clients")
    assert r.status_code == 200
    for cli in r.json():
        assert cli["max_connections_enforced"] is False


def _login_here():
    return _fresh_login()


# --- Hysteria2 egress verification (isolated ephemeral sing-box) ---------------

def _hy2_node(**kw):
    from app.models import ProxyNode
    base = dict(fingerprint="fp-hy2", name="HY2", protocol="hysteria2",
                server="hy.example.com", port=8444, secret="pw",
                transport="udp", security="tls",
                params={"sni": "hy.example.com"})
    base.update(kw)
    return ProxyNode(**base)


def _fake_settings(tmp_path):
    import types
    return types.SimpleNamespace(
        xray_config=tmp_path / "xray.json",
        singbox_config=tmp_path / "sing-box.json",
        xray_bin="/nonexistent-xray",
        singbox_bin="/nonexistent-singbox",
        socks_port=2081, http_port=1081,
    )


class _FakeProc:
    def __init__(self, exit_early=False):
        self._exit_early = exit_early
        self.terminated = False
        self.killed = False

    def poll(self):
        return 1 if self._exit_early else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class _FakeSock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeResp:
    def raise_for_status(self):
        return None


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        return _FakeResp()


def _patch_ephemeral_success(monkeypatch, tmp_path):
    import socket as _socket
    import httpx as _httpx
    import subprocess as _sp
    from app.services import cores as C

    monkeypatch.setattr(C, "get_settings", lambda: _fake_settings(tmp_path))
    monkeypatch.setattr(C.SingBoxAdapter, "installed", lambda self: True)

    class _Run:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _Run())
    procs = []
    orig_popen = _sp.Popen

    def fake_popen(*a, **k):
        p = _FakeProc()
        procs.append((a, p))
        return p

    monkeypatch.setattr(_sp, "Popen", fake_popen)
    monkeypatch.setattr(_socket, "create_connection", lambda *a, **k: _FakeSock())
    monkeypatch.setattr(_httpx, "Client", _FakeClient)
    return procs


def test_ephemeral_singbox_success_and_cleanup(monkeypatch, tmp_path):
    from app.services import cores as C

    procs = _patch_ephemeral_success(monkeypatch, tmp_path)
    with C.EphemeralSingBox(_hy2_node()) as proxy_url:
        assert proxy_url.startswith("socks5h://127.0.0.1:")
        port = int(proxy_url.rsplit(":", 1)[1])
        assert port > 0
    assert len(procs) == 1
    assert procs[0][1].terminated is True  # process stopped
    assert list(tmp_path.glob("verify-sb-*.json")) == []  # config removed


def test_ephemeral_singbox_invalid_config(monkeypatch, tmp_path):
    import subprocess as _sp
    from app.services import cores as C

    monkeypatch.setattr(C, "get_settings", lambda: _fake_settings(tmp_path))
    monkeypatch.setattr(C.SingBoxAdapter, "installed", lambda self: True)

    class _Bad:
        returncode = 1
        stdout = ""
        stderr = "bad outbound"

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _Bad())
    import pytest
    with pytest.raises(C.CoreError, match="verify config invalid"):
        with C.EphemeralSingBox(_hy2_node()):
            pass
    assert list(tmp_path.glob("verify-sb-*.json")) == []


def test_ephemeral_singbox_startup_failure(monkeypatch, tmp_path):
    import subprocess as _sp
    from app.services import cores as C

    monkeypatch.setattr(C, "get_settings", lambda: _fake_settings(tmp_path))
    monkeypatch.setattr(C.SingBoxAdapter, "installed", lambda self: True)

    class _Run:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _Run())
    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: _FakeProc(exit_early=True))
    import pytest
    with pytest.raises(C.CoreError, match="exited during startup"):
        with C.EphemeralSingBox(_hy2_node()):
            pass
    assert list(tmp_path.glob("verify-sb-*.json")) == []


def test_verify_hy2_http_and_tls_failures(monkeypatch, tmp_path):
    import httpx as _httpx
    from app.services import cores as C

    _patch_ephemeral_success(monkeypatch, tmp_path)

    class _TimeoutClient(_FakeClient):
        def get(self, url):
            raise _httpx.TimeoutException("timed out")

    class _TlsClient(_FakeClient):
        def get(self, url):
            raise _httpx.ConnectError("SSL: EOF occurred")

    monkeypatch.setattr(_httpx, "Client", _TimeoutClient)
    r = C.verify_node_egress(_hy2_node(), "https://x.test", timeout=1)
    assert r.ok is False and r.error_type == "timeout"
    assert list(tmp_path.glob("verify-sb-*.json")) == []  # cleaned on failure path

    monkeypatch.setattr(_httpx, "Client", _TlsClient)
    r = C.verify_node_egress(_hy2_node(), "https://x.test", timeout=1)
    assert r.ok is False and r.error_type == "tls_error"


def test_verify_dispatch_by_protocol(monkeypatch, tmp_path):
    import subprocess as _sp
    from app.services import cores as C

    seen = {}

    class _X:
        def __init__(self, node):
            seen["xray"] = node.protocol

        def __enter__(self):
            return "socks5h://127.0.0.1:1"

        def __exit__(self, *a):
            return False

    class _S:
        def __init__(self, node):
            seen["singbox"] = node.protocol

        def __enter__(self):
            return "socks5h://127.0.0.1:2"

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(C, "EphemeralXray", _X)
    monkeypatch.setattr(C, "EphemeralSingBox", _S)
    monkeypatch.setattr(C, "_probe_http",
                        lambda *a: C.EgressResult(ok=True, app_latency_ms=5.0))

    from app.models import ProxyNode
    vless = ProxyNode(fingerprint="a", name="v", protocol="vless",
                      server="s", port=1, secret="u", params={})
    assert C.verify_node_egress(vless, "https://x.test").ok is True
    assert seen == {"xray": "vless"}

    seen.clear()
    assert C.verify_node_egress(_hy2_node(), "https://x.test").ok is True
    assert seen == {"singbox": "hysteria2"}

    # Unknown protocol: honest unverified, no subprocess spawned.
    spawned = []
    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: spawned.append(a) or _FakeProc())
    weird = ProxyNode(fingerprint="w", name="w", protocol="wireguard",
                      server="s", port=1, secret="u", params={})
    r = C.verify_node_egress(weird, "https://x.test")
    assert r.ok is False and r.error_type == "unverified"
    assert spawned == []


def test_singbox_missing_binary_is_unverified(monkeypatch, tmp_path):
    from app.services import cores as C

    monkeypatch.setattr(C, "get_settings", lambda: _fake_settings(tmp_path))
    monkeypatch.setattr(C.SingBoxAdapter, "installed", lambda self: False)
    r = C.verify_node_egress(_hy2_node(), "https://x.test", timeout=1)
    assert r.ok is False and r.error_type == "unverified"


def test_deep_unverified_leaves_counters_untouched(db):
    from app.services import health_v2 as hv

    n = _mk(db, "unv-node")
    n.consecutive_failures = 2
    n.egress_ok = True
    n.health = "healthy"
    db.commit()
    eng = hv.HealthEngineV2()
    eng._update_deep_metrics(
        db, n, hv.ProbeResult(ok=False, error="sing-box is not installed",
                             error_type="unverified"))
    db.commit()
    db.expire_all()
    row = db.query(ProxyNode).filter(ProxyNode.id == n.id).first()
    assert row.consecutive_failures == 2  # not incremented
    assert row.egress_ok is True  # not cleared
    assert row.health == "healthy"  # not poisoned


def test_attempt_transition_skips_unverified_without_mutation(db, monkeypatch):
    import app.services.failover as fo

    monkeypatch.setattr("app.services.cores.verify_node_egress",
                        lambda node, url, timeout=15: type("R", (), {
                            "ok": False, "app_latency_ms": None,
                            "error": "sing-box is not installed",
                            "error_type": "unverified"})())
    applied = []
    monkeypatch.setattr("app.services.cores.XrayAdapter.apply",
                        lambda self, node, session=None, force_restart=False: applied.append(node.id))
    active = _mk(db, "t-active", active=True)
    cand = _mk(db, "t-hy2")
    cand.protocol = "hysteria2"
    cand.transport = "udp"
    db.commit()
    ok, reason = fo.attempt_transition(db, cand, active, actor="t",
                                       audit=lambda a, x, d: None,
                                       reason_kind="failover")
    assert ok is False and "unverified" in reason
    assert applied == []  # never attempted activation
    db.expire_all()
    row = db.query(ProxyNode).filter(ProxyNode.id == cand.id).first()
    assert row.consecutive_failures == 0  # untouched


def test_hy2_failover_end_to_end(db, monkeypatch):
    import app.services.failover as fo

    monkeypatch.setattr("app.services.cores.verify_node_egress",
                        lambda node, url, timeout=15: type("R", (), {
                            "ok": True, "app_latency_ms": 150.0,
                            "error": "", "error_type": ""})())
    monkeypatch.setattr("app.services.cores.SingBoxAdapter.apply",
                        lambda self, node, session=None, force_restart=False: None)
    monkeypatch.setattr("app.services.cores.SingBoxAdapter.installed",
                        lambda self: True)
    from app.services.failover import session_set
    session_set(db, "gateway_enabled", "false")
    session_set(db, "failover_mode", "auto")
    session_set(db, "failover_threshold", "3")
    session_set(db, "failover_cooldown", "0")
    session_set(db, "failover_min_healthy", "1")
    from datetime import timedelta
    active = _mk(db, "t-active2", active=True)
    active.consecutive_failures = 5
    active.last_state_change = datetime.now(timezone.utc) - timedelta(seconds=10)
    cand = _mk(db, "t-hy2-ok")
    cand.protocol = "hysteria2"
    cand.transport = "udp"
    cand.egress_ok = True
    db.commit()
    audited = []
    res = fo.maybe_failover(db, active, audit=lambda a, x, d: audited.append((a, x, d)))
    assert res["switched"] is True and res["node_id"] == cand.id
    assert any(a == "failover" and x == "node.activate" for a, x, d in audited)


def test_udp_transport_skipped_in_fast_loop(db, monkeypatch):
    from app.services import health_v2 as hv

    probed = []
    tcp_node = _mk(db, "tcp-node")
    hy_node = _mk(db, "hy-node")
    hy_node.protocol = "hysteria2"
    hy_node.transport = "udp"
    db.commit()

    def spy(host, port, timeout=2.0):
        probed.append(host)
        return hv.ProbeResult(ok=True, tcp_latency_ms=9.0)

    monkeypatch.setattr(hv, "tcp_probe", spy)
    hv.HealthEngineV2()._fast_check_all_sync()
    assert probed == ["x.ir"]  # exactly one TCP probe: the TCP node


# --- Ephemeral verifier failure-path cleanup (both cores) ----------------------

def _vless_node(**kw):
    from app.models import ProxyNode
    base = dict(fingerprint="fp-vx", name="VX", protocol="vless",
                server="s.ir", port=443, secret="u", params={})
    base.update(kw)
    return ProxyNode(**base)


def _patch_ephemeral_env(monkeypatch, tmp_path):
    import shutil as _shutil
    from app.services import cores as C

    monkeypatch.setattr(C, "get_settings", lambda: _fake_settings(tmp_path))
    monkeypatch.setattr(C.SingBoxAdapter, "installed", lambda self: True)
    monkeypatch.setattr(_shutil, "which", lambda *a, **k: "/fake/bin")

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Ok())
    return C


def _ephemeral_cases():
    from app.services import cores as C
    return [
        ("xray", C.EphemeralXray, _vless_node(), "verify-"),
        ("sing-box", C.EphemeralSingBox, _hy2_node(), "verify-sb-"),
    ]


def _glob_prefix(tmp_path, prefix):
    return list(tmp_path.glob(prefix + "*.json"))


def test_ephemeral_validator_timeout_cleans_up(monkeypatch, tmp_path):
    import subprocess as _sp
    from app.services import cores as C

    _patch_ephemeral_env(monkeypatch, tmp_path)
    monkeypatch.setattr(_sp, "run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _sp.TimeoutExpired(cmd=a[0], timeout=30)))
    import pytest
    for label, cls, node, prefix in _ephemeral_cases():
        with pytest.raises(C.CoreError, match="verify validation failed"):
            with cls(node):
                pass
        assert _glob_prefix(tmp_path, prefix) == [], label


def test_ephemeral_validator_oserror_cleans_up(monkeypatch, tmp_path):
    import subprocess as _sp
    from app.services import cores as C

    _patch_ephemeral_env(monkeypatch, tmp_path)
    monkeypatch.setattr(_sp, "run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError("disk gone")))
    import pytest
    for label, cls, node, prefix in _ephemeral_cases():
        with pytest.raises(C.CoreError, match="verify validation failed"):
            with cls(node):
                pass
        assert _glob_prefix(tmp_path, prefix) == [], label


def test_ephemeral_spawn_failure_cleans_up(monkeypatch, tmp_path):
    import subprocess as _sp
    from app.services import cores as C

    _patch_ephemeral_env(monkeypatch, tmp_path)
    monkeypatch.setattr(_sp, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(
                            FileNotFoundError("binary vanished")))
    import pytest
    for label, cls, node, prefix in _ephemeral_cases():
        with pytest.raises(C.CoreError, match="failed to start"):
            with cls(node):
                pass
        assert _glob_prefix(tmp_path, prefix) == [], label


def test_ephemeral_readiness_timeout_reaps_process(monkeypatch, tmp_path):
    import socket as _socket
    import subprocess as _sp
    import time as _time
    from app.services import cores as C

    _patch_ephemeral_env(monkeypatch, tmp_path)
    procs = []
    monkeypatch.setattr(_sp, "Popen",
                        lambda *a, **k: procs.append(_FakeProc()) or procs[-1])
    monkeypatch.setattr(_socket, "create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError("nothing listening")))
    # Fast-forward the readiness clock; keep real sleeps out of the test.
    tick = {"t": 1000.0}

    def _mono():
        tick["t"] += 10.0
        return tick["t"]

    monkeypatch.setattr(_time, "monotonic", _mono)
    monkeypatch.setattr(_time, "sleep", lambda s: None)
    import pytest
    for label, cls, node, prefix in _ephemeral_cases():
        procs.clear()
        with pytest.raises(C.CoreError, match="did not listen in time"):
            with cls(node):
                pass
        assert len(procs) == 1, label
        assert procs[0].terminated is True, label  # reaped, not orphaned
        assert _glob_prefix(tmp_path, prefix) == [], label


def test_hy2_poisoned_node_recovers_on_genuine_success(db, monkeypatch):
    """A hy2 node stuck unhealthy (incl. expired circuit) must recover once a
    genuine egress check succeeds — and must NOT recover on 'unverified'."""
    from datetime import timedelta
    from app.services import health_v2 as hv

    n = _mk(db, "hy2-poisoned")
    n.protocol = "hysteria2"
    n.transport = "udp"
    n.health = "unhealthy"
    n.egress_ok = False
    n.consecutive_failures = 12
    n.consecutive_successes = 0
    n.circuit_open = True
    n.circuit_open_until = datetime.now(timezone.utc) - timedelta(seconds=5)
    n.quality_score = 5.0
    db.commit()
    nid = n.id

    # 1) verifier unavailable -> state untouched, still ineligible.
    monkeypatch.setattr(
        "app.services.cores.verify_node_egress",
        lambda node, url, timeout=15: type("R", (), {
            "ok": False, "app_latency_ms": None,
            "error": "sing-box is not installed", "error_type": "unverified"})())
    hv.HealthEngineV2()._deep_check_all_sync()
    db.expire_all()
    row = db.query(ProxyNode).filter(ProxyNode.id == nid).first()
    assert row.health == "unhealthy" and row.egress_ok is False
    assert row.consecutive_failures == 12
    from app.services.failover import eligible_candidates
    cands, _ = eligible_candidates(db, exclude_id=None)
    assert all(c.id != nid for c in cands)

    # 2) verifier available, genuine success -> full recovery + eligible.
    # Realistic sequence: the fast loop clears the expired breaker first
    # (no TCP probe is wasted on the UDP transport), then the deep loop
    # verifies egress genuinely.
    monkeypatch.setattr(
        "app.services.cores.verify_node_egress",
        lambda node, url, timeout=15: type("R", (), {
            "ok": True, "app_latency_ms": 180.0,
            "error": "", "error_type": ""})())
    hv.HealthEngineV2()._fast_check_all_sync()
    db.expire_all()
    row = db.query(ProxyNode).filter(ProxyNode.id == nid).first()
    assert row.circuit_open is False  # expired breaker reopened for probing
    hv.HealthEngineV2()._deep_check_all_sync()
    db.expire_all()
    row = db.query(ProxyNode).filter(ProxyNode.id == nid).first()
    assert row.health == "healthy"
    assert row.egress_ok is True
    assert row.consecutive_failures == 0
    assert row.last_success is not None
    cands, _ = eligible_candidates(db, exclude_id=None)
    assert any(c.id == nid for c in cands)


# --- API boundaries: scoped rules, subscription URL, expiry --------------------

def test_api_rejects_scoped_rule_without_dedicated_port():
    from test_api import _client_with_admin as _cwa
    from app.services import auth as _auth

    _auth._attempts.clear()
    from test_api import _login
    c = _login(_cwa())
    body = {"priority": 50, "match_type": "domain_exact", "pattern": "x.example.com",
            "port": "", "network": "any", "action": "proxy", "client_scope": "ghost",
            "enabled": True, "note": ""}
    assert c.post("/api/rules", json=body).status_code == 400


def test_api_subscription_url_stored_masked():
    from test_api import _client_with_admin as _cwa
    from app.services import auth as _auth

    _auth._attempts.clear()
    from test_api import _login
    c = _login(_cwa())
    bad = c.put("/api/subscription", json={"url": "http://169.254.169.254/x"})
    assert bad.status_code == 422
    good_url = "https://provider.example.com/sub/0123456789abcdef-token"
    r = c.put("/api/subscription", json={"url": good_url})
    assert r.status_code == 200, r.text
    assert good_url not in r.text  # masked in response
    got = c.get("/api/subscription").json()
    assert good_url not in str(got)
    assert "url_masked" in got


def test_session_expiry_rejected():
    import time as _time
    from app.services import auth as _auth

    past = int(_time.time()) - 10
    import hashlib as _h
    import hmac as _m
    from app.config import get_settings

    payload = f"admin.{past}.nonce123.0"
    sig = _m.new(get_settings().secret_key.encode(), payload.encode(),
                 _h.sha256).hexdigest()
    assert _auth.parse_session(f"{payload}.{sig}") is None


def test_lifecycle_status_never_leaks_secrets(db):
    """Regression: /api/lifecycle/status must not serialize node secrets."""
    from test_api import _client_with_admin as _cwa
    from app.services import auth as _auth

    # Active node WITH a real secret: the old code serialized the whole row.
    _mk(db, "leak-probe-node", active=True)
    db.query(ProxyNode).filter(
        ProxyNode.fingerprint == "fp-leak-probe-node").update(
        {"secret": "SUPERSECRET-UUID-1234"})
    db.commit()
    _auth._attempts.clear()
    from test_api import _login
    c = _login(_cwa())
    r = c.get("/api/lifecycle/status")
    assert r.status_code == 200, r.text
    blob = r.text.lower()
    for forbidden in ('"secret"', '"password"', "private", "begin private",
                      "pbk", "sid", "password_enc", "password_hash"):
        assert forbidden not in blob, forbidden
    d = r.json()
    assert "active_node" in d
    if d["active_node"] is not None:
        assert set(d["active_node"]).issubset(
            {"id", "name", "protocol", "quality_score", "duration_hours",
             "max_duration_hours", "remaining_hours"})
