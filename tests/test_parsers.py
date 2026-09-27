"""Parser tests — fixtures only, no network, no real subscription."""
import base64

import pytest

from app.services.parsers import (
    NodeParseError,
    looks_like_base64,
    parse_uri,
    split_subscription_text,
)

VLESS = (
    "vless://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee@example.ir:2474"
    "?encryption=none&flow=xtls-rprx-vision&security=reality&sni=example.com"
    "&fp=chrome&pbk=PUBKEY123&sid=abc123&type=tcp#Node-DE"
)
VMESS = "vmess://" + base64.b64encode(
    b'{"add":"vm.example.com","port":"443","id":"uuid-1","aid":"0","net":"ws",'
    b'"tls":"tls","host":"cdn.example.com","path":"/x","ps":"VM-Test"}'
).decode()
TROJAN = "trojan://secretpw@tj.example.com:443?sni=tj.example.com#TJ-1"
SS = "ss://YWVzLTI1Ni1nY206cGFzc3dvcmQxMjM=@ss.example.com:8388#SS-1"
HY2 = ("hysteria2://pass123@hy.example.com:8444/?obfs=salamander"
       "&obfs-password=OBFSPW&sni=hy.example.com#HY-1")


def test_vless_reality():
    n = parse_uri(VLESS)
    assert n.protocol == "vless"
    assert n.server == "example.ir" and n.port == 2474
    assert n.security == "reality"
    assert n.params["pbk"] == "PUBKEY123"
    assert n.secret == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert n.fingerprint() == parse_uri(VLESS).fingerprint()


def test_vmess():
    n = parse_uri(VMESS)
    assert n.protocol == "vmess" and n.transport == "ws"
    assert n.security == "tls" and n.name == "VM-Test"


def test_trojan():
    n = parse_uri(TROJAN)
    assert n.protocol == "trojan" and n.secret == "secretpw"


def test_ss_encoded_userinfo():
    n = parse_uri(SS)
    assert n.protocol == "ss" and n.params["method"] == "aes-256-gcm"
    assert n.server == "ss.example.com" and n.port == 8388


def test_hysteria2():
    n = parse_uri(HY2)
    assert n.protocol == "hysteria2" and n.port == 8444
    assert n.params["obfs"] == "salamander"


def test_unsupported_scheme():
    with pytest.raises(NodeParseError):
        parse_uri("wireguard://abc@x:1")


def test_malformed_does_not_crash_pipeline():
    from app.services.subscription import parse_subscription_text

    text = "\n".join([VLESS, "not-a-uri", "vless://bad", TROJAN, VLESS, "ss://%%%", HY2])
    nodes, stats = parse_subscription_text(text)
    assert stats["parsed"] == 3  # vless, trojan, hy2 (dup vless removed)
    assert stats["duplicates"] == 1
    assert stats["errors"] >= 1


def test_base64_subscription_blob():
    blob = base64.b64encode("\n".join([VLESS, TROJAN]).encode()).decode()
    lines = split_subscription_text(blob)
    assert len(lines) == 2
    assert parse_uri(lines[0]).protocol == "vless"


def test_plain_subscription_text():
    lines = split_subscription_text(f"{VLESS}\n{HY2}\n")
    assert len(lines) == 2


def test_looks_like_base64():
    blob = base64.b64encode(b"vless://x@y:1").decode()
    assert looks_like_base64(blob)
    assert not looks_like_base64(VLESS)
