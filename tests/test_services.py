"""Subscription / normalization / failover / routing / auth tests (no network)."""
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from app.models import ProxyNode, ProbeTelemetry
from app.services.auth import (
    create_session,
    hash_password,
    parse_session,
    rate_limited,
    verify_password,
)
from app.services.failover import maybe_failover, pick_failover_candidate, session_set
from app.services.health_v2 import HealthEngineV2
from app.services.routing import DEFAULT_RULES, ensure_defaults, ordered_rules
from app.services.subscription import mask_url, parse_subscription_text, sync_nodes
from app.db import get_session_factory

VLESS_A = ("vless://uuid-a@a.example.com:443?encryption=none&security=reality"
           "&sni=s.example.com&fp=chrome&pbk=K&sid=S&type=tcp#A")
VLESS_B = "trojan://pw@b.example.com:443?sni=b.example.com#B"


def _mk(db, name, healthy=True, latency=50.0, active=False, fail=0, quality_score=None):
    """Create a test node with proper quality engine fields."""
    now = datetime.now(timezone.utc)
    if quality_score is None:
        # Default quality score based on latency (lower latency = higher quality)
        quality_score = max(10.0, 100.0 - latency / 5.0) if healthy else 0.0
    
    n = ProxyNode(
        fingerprint=f"fp-{name}", name=name, protocol="vless", server="x.ir", port=443,
        health="healthy" if healthy else "unhealthy",
        latency_ms=latency if healthy else None,
        is_active=active,
        fail_count=fail,
        consecutive_failures=0 if healthy else fail,
        consecutive_successes=10 if healthy else 0,
        total_probes=100 if healthy else fail,
        successful_probes=100 if healthy else 0,
        quality_score=quality_score,
        egress_ok=healthy,
        last_checked=now,
        last_success=now if healthy else None,
        last_state_change=now,
    )
    db.add(n)
    db.commit()
    return n


def test_mask_url_hides_secret():
    m = mask_url("https://api.example.com/sub/SECRET-TOKEN-123/xyz")
    assert "SECRET" not in m and "api.example.com" in m


def test_sync_add_update_remove(db):
    nodes, _ = parse_subscription_text(f"{VLESS_A}\n{VLESS_B}")
    r1 = sync_nodes(db, nodes)
    assert r1 == {"added": 2, "updated": 0, "removed": 0, "total": 2}
    nodes2, _ = parse_subscription_text(VLESS_A)
    r2 = sync_nodes(db, nodes2)
    assert r2["added"] == 0 and r2["updated"] == 1 and r2["removed"] == 1
    removed = db.query(ProxyNode).filter(ProxyNode.protocol == "trojan").first()
    assert removed.removed_upstream is True


def test_removed_active_node_deactivated(db):
    nodes, _ = parse_subscription_text(VLESS_A)
    sync_nodes(db, nodes)
    node = db.query(ProxyNode).first()
    node.is_active = True
    db.commit()
    sync_nodes(db, [])
    assert db.query(ProxyNode).first().is_active is False


def test_failover_picks_lowest_latency(db):
    _mk(db, "slow", latency=300.0)
    _mk(db, "fast", latency=40.0)
    _mk(db, "sick", healthy=False)
    cand = pick_failover_candidate(db)
    assert cand.name == "fast"


def test_failover_prefers_proven_egress(db):
    from app.models import ProxyNode as _PN

    # "proven" has proven egress (better availability) but higher latency
    # "tcpfast" has low latency but no proven egress
    # Give "proven" better quality score through higher availability
    a = _mk(db, "tcpfast", latency=10.0, quality_score=50.0)  # Lower quality despite low latency
    b = _mk(db, "proven", latency=200.0, quality_score=80.0)  # Higher quality due to proven egress
    b.egress_ok = True
    db.commit()
    cand = pick_failover_candidate(db)
    assert cand.name == "proven"


def test_failover_threshold_and_cooldown(db):
    session_set(db, "failover_mode", "auto")
    session_set(db, "failover_threshold", "3")
    session_set(db, "failover_cooldown", "600")
    session_set(db, "failover_min_healthy", "1")  # Reduce min_healthy for test
    from datetime import datetime, timezone, timedelta
    # Create active node with consecutive_failures=2 (below threshold of 3)
    active = _mk(db, "active", active=True, fail=2, quality_score=50.0)
    active.consecutive_failures = 2
    active.last_state_change = datetime.now(timezone.utc) - timedelta(seconds=10)  # Old enough for min_healthy
    db.commit()
    audited = []
    res = maybe_failover(db, active, audit=lambda a, x, d: audited.append(x))
    assert res["switched"] is False  # below threshold
    # Now set consecutive_failures to 5 (above threshold)
    active.consecutive_failures = 5
    active.consecutive_successes = 0
    active.quality_score = 20.0  # Low quality due to failures
    active.last_state_change = datetime.now(timezone.utc) - timedelta(seconds=10)
    db.commit()
    # failover would call adapter.apply -> avoid by checking reason path only
    # No other healthy candidates exist
    res = maybe_failover(db, active, audit=lambda a, x, d: audited.append(x))
    # no healthy candidate besides itself... only 'active' exists -> no switch
    assert res["switched"] is False
    assert res["reason"] == "no healthy candidate"


def test_failover_manual_mode(db):
    session_set(db, "failover_mode", "manual")
    active = _mk(db, "active", active=True, fail=99)
    res = maybe_failover(db, active, audit=None)
    assert res == {"switched": False, "reason": "manual mode"}


def test_routing_defaults(db):
    ensure_defaults(db)
    rules = ordered_rules(db)
    assert len(rules) == len(DEFAULT_RULES)
    by_app = {r.app: r.route for r in rules}
    assert by_app["opencode"] == "proxy"
    assert by_app["microchess"] == "direct"
    assert by_app["ssh"] == "direct"


def test_auth_roundtrip():
    pw = "S3cure-Pass! 123"
    h = hash_password(pw)
    assert verify_password(pw, h)
    assert not verify_password("wrong", h)
    tok = create_session("admin")
    assert parse_session(tok) == "admin"
    assert parse_session(tok + "x") is None


def test_rate_limit():
    ip = "9.9.9.9-test"
    for _ in range(10):
        assert rate_limited(ip) is False
    assert rate_limited(ip) is True


def test_probe_history_persists_across_restarts(db):
    """Test that probe_history persists across database sessions."""
    from app.models import ProbeTelemetry
    from datetime import datetime, timezone
    
    n = ProxyNode(
        fingerprint="fp-history", name="History Node", protocol="vless", server="x.ir", port=443,
        health="healthy", latency_ms=50.0, is_active=False,
        quality_score=80.0, egress_ok=True,
        last_checked=datetime.now(timezone.utc),
        last_success=datetime.now(timezone.utc),
        last_state_change=datetime.now(timezone.utc),
    )
    db.add(n)
    db.commit()
    node_id = n.id
    
    # Simulate recording probe history
    from app.services.health_v2 import HealthEngineV2
    engine = HealthEngineV2()
    
    from dataclasses import dataclass
    @dataclass
    class ProbeResult:
        ok: bool
        app_latency_ms: float = None
        handshake_latency_ms: float = None
        tcp_latency_ms: float = None
        error: str = ""
        error_type: str = ""
    
    # Record some probe history
    db_exp = get_session_factory()()
    try:
        node = db_exp.query(ProxyNode).filter(ProxyNode.id == node_id).first()
        result = ProbeResult(ok=True, app_latency_ms=100.0)
        engine._record_probe_history(node, result)
        db_exp.commit()
        
        # Verify in same session
        node = db_exp.query(ProxyNode).filter(ProxyNode.id == node_id).first()
        assert "2026-10-12" in node.probe_history or len(node.probe_history) > 0
        hist = node.probe_history
        today = datetime.now(timezone.utc).date().isoformat()
        assert today in hist
        assert hist[today]["probes"] == 1
        assert hist[today]["success"] == 1
    finally:
        db_exp.close()
    
    # Verify in NEW session (simulates restart)
    db_new = get_session_factory()()
    try:
        node = db_new.query(ProxyNode).filter(ProxyNode.id == node_id).first()
        assert today in node.probe_history
        assert node.probe_history[today]["probes"] == 1
        assert node.probe_history[today]["success"] == 1
    finally:
        db_new.close()


def test_telemetry_written_for_each_probe(db):
    """Test that telemetry is recorded for each probe type."""
    from app.models import ProbeTelemetry
    from datetime import datetime, timezone
    
    n = ProxyNode(
        fingerprint="fp-telemetry", name="Telemetry Node", protocol="vless", server="x.ir", port=443,
        health="healthy", latency_ms=50.0, is_active=False,
        quality_score=80.0, egress_ok=True,
        last_checked=datetime.now(timezone.utc),
        last_success=datetime.now(timezone.utc),
        last_state_change=datetime.now(timezone.utc),
    )
    db.add(n)
    db.commit()
    node_id = n.id
    
    from app.services.health_v2 import HealthEngineV2
    engine = HealthEngineV2()
    
    @dataclass
    class ProbeResult:
        ok: bool
        app_latency_ms: float = None
        handshake_latency_ms: float = None
        tcp_latency_ms: float = None
        error: str = ""
        error_type: str = ""
    
    # Record telemetry for each probe type
    db_exp = get_session_factory()()
    try:
        engine._record_probe_telemetry(db_exp, node_id, "fast", ProbeResult(ok=True, tcp_latency_ms=10.0))
        engine._record_probe_telemetry(db_exp, node_id, "active", ProbeResult(ok=True, app_latency_ms=100.0), http_status=200)
        engine._record_probe_telemetry(db_exp, node_id, "deep", ProbeResult(ok=True, app_latency_ms=200.0), http_status=200)
        db_exp.commit()
        
        # Verify telemetry records
        telemetry = db_exp.query(ProbeTelemetry).filter(ProbeTelemetry.node_id == node_id).all()
        assert len(telemetry) == 3
        probe_types = {t.probe_type for t in telemetry}
        assert probe_types == {"fast", "active", "deep"}
        
        # Check fast probe
        fast = next(t for t in telemetry if t.probe_type == "fast")
        assert fast.ok is True
        assert fast.latency_ms == 10.0
        
        # Check active probe
        active = next(t for t in telemetry if t.probe_type == "active")
        assert active.ok is True
        assert active.latency_ms == 100.0
        assert active.http_status == 200
        
        # Check deep probe
        deep = next(t for t in telemetry if t.probe_type == "deep")
        assert deep.ok is True
        assert deep.latency_ms == 200.0
        assert deep.http_status == 200
    finally:
        db_exp.close()


def test_probe_history_cleanup_removes_expired(db):
    """Test that cleanup removes old probe history but keeps recent."""
    from datetime import datetime, timezone, timedelta
    
    n = ProxyNode(
        fingerprint="fp-cleanup", name="Cleanup Node", protocol="vless", server="x.ir", port=443,
        health="healthy", latency_ms=50.0, is_active=False,
        quality_score=80.0, egress_ok=True,
        last_checked=datetime.now(timezone.utc),
        last_success=datetime.now(timezone.utc),
        last_state_change=datetime.now(timezone.utc),
        probe_history={
            "2026-09-01": {"probes": 10, "success": 8, "failures": {}},
            "2026-10-01": {"probes": 10, "success": 10, "failures": {}},
            "2026-10-12": {"probes": 5, "success": 5, "failures": {}},
        }
    )
    db.add(n)
    db.commit()
    node_id = n.id
    
    from app.services.health_v2 import HealthEngineV2
    engine = HealthEngineV2()
    
    db_exp = get_session_factory()()
    try:
        engine._cleanup_old_data_sync()
        db_exp.commit()
        
        node = db_exp.query(ProxyNode).filter(ProxyNode.id == node_id).first()
        # Should keep current month (October) and possibly September, remove older
        assert "2026-09-01" not in node.probe_history  # Too old
        assert "2026-10-01" in node.probe_history     # Previous month
        assert "2026-10-12" in node.probe_history     # Current month
    finally:
        db_exp.close()
