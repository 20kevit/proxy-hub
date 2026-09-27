"""Routing engine tests: matching, validation, priority, client scope, Xray compile."""
import pytest

from app.models import RouteRule
from app.services.routing_engine import (
    RuleError,
    ensure_seed_rules,
    ordered_rules,
    rule_matches,
    simulate,
    to_xray_rule,
    validate_rule,
)


def _rule(**kw):
    base = dict(priority=100, match_type="domain_exact", pattern="telegram.org",
                port="", network="any", action="proxy", client_scope="all", enabled=True)
    base.update(kw)
    return RouteRule(**base)


def test_exact_domain():
    r = _rule()
    assert rule_matches(r, domain="telegram.org")
    assert rule_matches(r, domain="Telegram.ORG.")  # normalized
    assert not rule_matches(r, domain="sub.telegram.org")
    assert not rule_matches(r, domain="telegram.org.evil.com")


def test_suffix_domain():
    r = _rule(match_type="domain_suffix", pattern="*.telegram.org")
    assert rule_matches(r, domain="telegram.org")
    assert rule_matches(r, domain="a.b.telegram.org")
    assert not rule_matches(r, domain="telegram.org.evil.com")
    assert not rule_matches(r, domain="faketelegram.org")


def test_wildcard_domain():
    r = _rule(match_type="domain_wildcard", pattern="*.google.*")
    assert rule_matches(r, domain="x.google.com")
    assert rule_matches(r, domain="x.google.co.uk")
    assert not rule_matches(r, domain="google.com")
    assert not rule_matches(r, domain="xgoogle.com")


def test_ip_and_cidr():
    assert rule_matches(_rule(match_type="ip", pattern="1.2.3.4"), ip="1.2.3.4")
    assert not rule_matches(_rule(match_type="ip", pattern="1.2.3.4"), ip="1.2.3.5")
    assert rule_matches(_rule(match_type="cidr", pattern="10.0.0.0/8"), ip="10.9.9.9")
    assert not rule_matches(_rule(match_type="cidr", pattern="10.0.0.0/8"), ip="11.0.0.1")


def test_port_and_network_constraints():
    assert rule_matches(_rule(match_type="port", pattern="443"), port=443)
    assert rule_matches(_rule(match_type="port", pattern="80-443"), port=80)
    assert not rule_matches(_rule(match_type="port", pattern="443"), port=80)
    assert not rule_matches(_rule(match_type="port", pattern="443"), port=None)
    r = _rule(match_type="domain_exact", pattern="x.com", port="22", network="tcp")
    assert rule_matches(r, domain="x.com", port=22, network="tcp")
    assert not rule_matches(r, domain="x.com", port=22, network="udp")


def test_disabled_and_client_scope():
    assert not rule_matches(_rule(enabled=False), domain="telegram.org")
    r = _rule(client_scope="laptop_x1")
    assert rule_matches(r, domain="telegram.org", client="laptop_x1")
    assert not rule_matches(r, domain="telegram.org", client="phone_x1")
    assert not rule_matches(r, domain="telegram.org", client="all")


def test_validation_rejects_bad_input():
    for mt, pat in [("cidr", "10.0.0.0/33"), ("ip", "999.1.1.1"),
                    ("port", "0"), ("port", "22-21"), ("port", "abc"),
                    ("domain_exact", "*.x.com"), ("domain_exact", "nonsense"),
                    ("domain_suffix", "x.com"), ("domain_wildcard", "plain.com")]:
        with pytest.raises(RuleError):
            validate_rule(mt, pat, action="proxy")
    with pytest.raises(RuleError):
        validate_rule("domain_exact", "x.com", action="route-to-moon")


def test_simulate_priority_and_default(db):
    ensure_seed_rules(db)
    res = simulate(db, domain="sub.example.ir", default_route="proxy")
    assert res["decision"] == "direct"  # *.ir seed
    assert res["is_default"] is False
    res = simulate(db, domain="telegram.org", default_route="block")
    assert res["decision"] == "block" and res["is_default"] is True
    # higher-priority (lower number) proxy rule wins over *.ir
    from app.models import RouteRule as _RR

    db.add(_RR(priority=5, match_type="domain_exact", pattern="sub.example.ir",
               action="proxy", client_scope="all"))
    db.commit()
    res = simulate(db, domain="sub.example.ir", default_route="proxy")
    assert res["decision"] == "proxy" and res["matched_rule_id"] is not None


def test_xray_compile():
    xr = to_xray_rule(_rule(match_type="domain_exact", pattern="Telegram.ORG", action="proxy"))
    assert xr["domain"] == ["full:Telegram.ORG"] and xr["outboundTag"] == "proxy"
    xr = to_xray_rule(_rule(match_type="domain_suffix", pattern="*.telegram.org", action="proxy"))
    assert xr["domain"] == ["domain:telegram.org"]
    xr = to_xray_rule(_rule(match_type="domain_wildcard", pattern="*.google.*", action="block"))
    assert xr["domain"] == ["regexp:^.*\\.google\\..*$"] and xr["outboundTag"] == "block"
    xr = to_xray_rule(_rule(match_type="cidr", pattern="10.0.0.0/8", action="direct"))
    assert xr["ip"] == ["10.0.0.0/8"] and xr["outboundTag"] == "direct"
    xr = to_xray_rule(_rule(match_type="port", pattern="443", action="proxy"))
    assert xr["port"] == "443"
    xr = to_xray_rule(_rule(client_scope="phone_x1"))
    assert xr["inboundTag"] == ["gw-phone_x1"]
    xr = to_xray_rule(_rule(client_scope="all"))
    assert "inboundTag" not in xr


def test_seed_rules_present(db):
    ensure_seed_rules(db)
    rules = ordered_rules(db)
    assert len(rules) >= 6
    assert rules[0].priority == 1
    # seeds are idempotent
    ensure_seed_rules(db)
    assert len(ordered_rules(db)) == len(rules)
