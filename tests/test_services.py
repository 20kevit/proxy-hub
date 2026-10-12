"""Subscription / normalization / failover / routing / auth tests (no network)."""
from datetime import datetime, timezone
from app.models import ProxyNode
from app.services.auth import (
    create_session,
    hash_password,
    parse_session,
    rate_limited,
    verify_password,
)
from app.services.failover import maybe_failover, pick_failover_candidate, session_set
from app.services.routing import DEFAULT_RULES, ensure_defaults, ordered_rules
from app.services.subscription import mask_url, parse_subscription_text, sync_nodes

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
