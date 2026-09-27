"""Explicit per-application routing. No transparent proxy, no global interception.

Each rule maps an application name -> direct | proxy. Consumers read the rules via
the API (or the generated env snippet) and configure ONLY themselves, e.g.:

    # /etc/opencode/proxy.env (example, opt-in)
    HTTP_PROXY=socks5h://127.0.0.1:2081
    HTTPS_PROXY=socks5h://127.0.0.1:2081
    NO_PROXY=127.0.0.1,localhost
"""
from __future__ import annotations

from ..models import RoutingRule
from .cores import managed_socks_url

DEFAULT_RULES: list[tuple[str, str, int, str]] = [
    ("opencode", "proxy", 10, "AI coding assistant -> managed proxy"),
    ("chrome", "proxy", 20, "Browser -> managed proxy"),
    ("telegram", "proxy", 30, "Telegram client -> managed proxy"),
    ("microchess", "direct", 40, "Must stay direct"),
    ("fide-service", "direct", 50, "Must stay direct"),
    ("ssh", "direct", 60, "Never proxy SSH"),
    ("nginx", "direct", 70, "Never proxy Nginx"),
    ("apt", "direct", 80, "System packages stay direct"),
    ("git", "direct", 90, "Direct unless explicitly configured"),
]


def ensure_defaults(session) -> None:
    if session.query(RoutingRule).count():
        return
    for app, route, pos, note in DEFAULT_RULES:
        session.add(RoutingRule(app=app, route=route, position=pos, note=note))
    session.commit()


def ordered_rules(session) -> list[RoutingRule]:
    return session.query(RoutingRule).order_by(RoutingRule.position, RoutingRule.id).all()


def proxy_env_snippet() -> str:
    socks = managed_socks_url()
    return (
        f"# ProxyHub managed proxy (opt-in per application — never global)\n"
        f"export HTTP_PROXY={socks}\n"
        f"export HTTPS_PROXY={socks}\n"
        f"export ALL_PROXY={socks}\n"
        f"export NO_PROXY=127.0.0.1,localhost\n"
    )
