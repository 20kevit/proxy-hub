"""Gateway client tests: lifecycle, crypto, gating, config compile (no network)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import ProxyClient, ProxyNode
from app.services.clients import (
    client_usable,
    generate_password,
    generate_username,
    make_password_hash,
    open_password,
    seal_password,
    validate_username,
    verify_client_password,
)
from app.services.cores import XrayAdapter
from app.services.failover import session_set


def _client(db, username="laptop_x1", enabled=True, expired=False):
    c = ProxyClient(
        name="Laptop", username=username,
        password_hash=make_password_hash("SeCuRe-Pass-12345"),
        password_enc=seal_password("SeCuRe-Pass-12345"),
        enabled=enabled,
        expires_at=(datetime.now(timezone.utc) - timedelta(days=1)) if expired else None,
    )
    db.add(c)
    db.commit()
    return c


def test_password_crypto_roundtrip():
    pw = generate_password()
    assert len(pw) == 20
    h = make_password_hash(pw)
    sealed = seal_password(pw)
    assert sealed != pw  # not plaintext
    assert open_password(sealed) == pw
    assert verify_client_password(type("C", (), {"password_hash": h})(), pw)
    assert not verify_client_password(type("C", (), {"password_hash": h})(), "wrong-pass-xyz")
    with pytest.raises(ValueError):
        make_password_hash("short")
    with pytest.raises(ValueError):
        validate_username("Bad Name!")


def test_client_gating(db):
    assert client_usable(_client(db))[0] is True
    ok, why = client_usable(_client(db, username="off_x1", enabled=False))
    assert ok is False and why == "disabled"
    ok, why = client_usable(_client(db, username="exp_x1", expired=True))
    assert ok is False and why == "expired"
    assert client_usable(None) == (False, "unknown user")


def test_gateway_context_compiles_clients_and_rules(db):
    _client(db)
    session_set(db, "gateway_enabled", "true")
    session_set(db, "gateway_bind", "127.0.0.1")
    session_set(db, "public_socks_port", "8388")
    session_set(db, "public_http_port", "8389")
    session_set(db, "default_route", "proxy")
    ctx = XrayAdapter().gateway_context(db)
    assert ctx["bind"] == "127.0.0.1"
    assert ("laptop_x1", "SeCuRe-Pass-12345") in ctx["accounts"]
    assert any("*.ir" in r.get("domain", [""])[0] or "ir" in str(r) for r in ctx["route_rules"])
    assert ctx["default_route"] == "proxy"


def test_gateway_disabled_returns_none(db):
    session_set(db, "gateway_enabled", "false")
    assert XrayAdapter().gateway_context(db) is None


def test_gateway_rejects_bad_bind_and_reserved_ports(db):
    _client(db)
    session_set(db, "gateway_enabled", "true")
    session_set(db, "gateway_bind", "0.0.0.0.evil")
    with pytest.raises(Exception):
        XrayAdapter().gateway_context(db)
    session_set(db, "gateway_bind", "127.0.0.1")
    session_set(db, "public_socks_port", "2080")  # reserved
    with pytest.raises(Exception):
        XrayAdapter().gateway_context(db)


def test_generate_includes_auth_inbounds_and_rules(db):
    _client(db)
    _client(db, username="phone_x1")
    session_set(db, "gateway_enabled", "true")
    ctx = XrayAdapter().gateway_context(db)
    node = ProxyNode(fingerprint="gw-test", name="T", protocol="vless",
                     server="x.ir", port=443, secret="u", params={})
    cfg = XrayAdapter().generate(node, gateway=ctx)
    tags = {i["tag"] for i in cfg["inbounds"]}
    assert {"socks-in", "http-in", "gw-socks", "gw-http"} <= tags
    gw = next(i for i in cfg["inbounds"] if i["tag"] == "gw-socks")
    assert gw["settings"]["auth"] == "password"
    users = {a["user"] for a in gw["settings"]["accounts"]}
    assert {"laptop_x1", "phone_x1"} <= users
    # private ranges direct, catch-all = default route
    last = cfg["routing"]["rules"][-1]
    assert last["outboundTag"] == "proxy" and last["port"] == "0-65535"


def test_hy2_blocked_when_gateway_enabled(db):
    from app.services.cores import CoreError

    session_set(db, "gateway_enabled", "true")
    node = ProxyNode(fingerprint="hy-test", name="H", protocol="hysteria2",
                     server="x.ir", port=443, secret="p", params={})
    with pytest.raises(CoreError):
        XrayAdapter().apply(node, session=db)


def test_generate_without_gateway_unchanged(db):
    node = ProxyNode(fingerprint="plain", name="T", protocol="vless",
                     server="x.ir", port=443, secret="u", params={})
    cfg = XrayAdapter().generate(node)
    assert {i["tag"] for i in cfg["inbounds"]} == {"socks-in", "http-in"}
    assert cfg["routing"]["rules"][-1]["outboundTag"] == "proxy"


def test_username_generation_unique():
    a, b = generate_username("My Laptop!"), generate_username("My Laptop!")
    assert a != b and validate_username(a) == a
