"""Database models. Sensitive node fields are stored but never serialized to the API."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ProxyNode(Base):
    __tablename__ = "proxy_nodes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # dedupe key
    name: Mapped[str] = mapped_column(String(256), default="")
    protocol: Mapped[str] = mapped_column(String(32), index=True)  # vless/vmess/trojan/ss/hysteria2
    server: Mapped[str] = mapped_column(String(256))
    port: Mapped[int] = mapped_column(Integer)
    transport: Mapped[str] = mapped_column(String(32), default="")
    security: Mapped[str] = mapped_column(String(32), default="")
    # Sensitive payload (uuid/password/keys) — never exposed via API
    secret: Mapped[str] = mapped_column(Text, default="")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(32), default="subscription")
    tags: Mapped[str] = mapped_column(String(512), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    removed_upstream: Mapped[bool] = mapped_column(Boolean, default=False)
    health: Mapped[str] = mapped_column(String(16), default="unknown", index=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    egress_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str] = mapped_column(String(512), default="")
    last_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)

    def display_health(self) -> str:
        """UNKNOWN / HEALTHY / DEGRADED / UNHEALTHY / DISABLED (derived, not stored)."""
        if not self.enabled or self.removed_upstream:
            return "DISABLED"
        if self.last_checked is None:
            return "UNKNOWN"
        if self.health == "healthy":
            return "HEALTHY"
        if (self.fail_count or 0) <= 0:
            return "UNKNOWN"
        return "UNHEALTHY" if (self.fail_count or 0) >= 3 else "DEGRADED"


class RouteRule(Base):
    """Client-side traffic rule. Evaluated by priority ASC, id ASC; first match wins.

    match_type: domain_exact | domain_suffix | domain_wildcard | ip | cidr | port
    action: direct | proxy | block
    client_scope: 'all' or a ProxyClient username (enforced via dedicated inbound).
    port: optional extra constraint (single '443' or range '80-443').
    network: any | tcp | udp — extra constraint.
    """

    __tablename__ = "route_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    match_type: Mapped[str] = mapped_column(String(24))
    pattern: Mapped[str] = mapped_column(String(512))
    port: Mapped[str] = mapped_column(String(32), default="")
    network: Mapped[str] = mapped_column(String(8), default="any")
    action: Mapped[str] = mapped_column(String(16))
    client_scope: Mapped[str] = mapped_column(String(64), default="all")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ProxyClient(Base):
    """Gateway client credentials. Password shown ONLY at creation/rotation."""

    __tablename__ = "proxy_clients"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    password_enc: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dedicated_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_connections: Mapped[int] = mapped_column(Integer, default=10)
    notes: Mapped[str] = mapped_column(String(512), default="")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SubscriptionState(Base):
    __tablename__ = "subscription_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url_hash: Mapped[str] = mapped_column(String(64), default="")
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(String(512), default="")
    node_count: Mapped[int] = mapped_column(Integer, default=0)
    update_interval: Mapped[int] = mapped_column(Integer, default=21600)


class RoutingRule(Base):
    __tablename__ = "routing_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app: Mapped[str] = mapped_column(String(128), unique=True)
    route: Mapped[str] = mapped_column(String(16), default="direct")  # direct | proxy
    position: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(String(512), default="")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    action: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
