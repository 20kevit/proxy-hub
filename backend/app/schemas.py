"""Pydantic schemas — public API contracts (no secrets anywhere)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str
    password: str


class NodeOut(BaseModel):
    id: int
    name: str
    protocol: str
    server: str
    port: int
    transport: str = ""
    security: str = ""
    source: str = ""
    tags: str = ""
    enabled: bool
    removed_upstream: bool = False
    health: str
    latency_ms: float | None = None
    last_error: str = ""
    last_checked: str | None = None
    is_active: bool


class NodeUpdate(BaseModel):
    name: str | None = None
    tags: str | None = None
    enabled: bool | None = None


class RoutingRuleIn(BaseModel):
    app: str = Field(min_length=1, max_length=128)
    route: str = Field(pattern="^(direct|proxy)$")
    position: int = 0
    note: str = ""


class SettingsIn(BaseModel):
    sub_update_interval: int | None = None
    health_interval: int | None = None
    health_url: str | None = None
    failover_mode: str | None = None
    failover_threshold: int | None = None
    failover_cooldown: int | None = None
    gateway_enabled: bool | None = None
    gateway_bind: str | None = None
    public_socks_port: int | None = None
    public_http_port: int | None = None
    default_route: str | None = None


class RouteRuleIn(BaseModel):
    priority: int = 100
    match_type: str
    pattern: str
    port: str = ""
    network: str = "any"
    action: str
    client_scope: str = "all"
    enabled: bool = True
    note: str = ""


class SimulateIn(BaseModel):
    domain: str = ""
    ip: str = ""
    port: int | None = None
    network: str = "any"
    client: str = "all"


class ClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    username: str | None = None
    password: str | None = None
    expires_at: str | None = None
    dedicated_port: int | None = None
    max_connections: int = 10
    notes: str = ""


class ClientUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    expires_at: str | None = None
    dedicated_port: int | None = None
    max_connections: int | None = None
    notes: str | None = None


class SubscriptionIn(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    update_interval: int | None = None
