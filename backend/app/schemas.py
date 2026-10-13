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
    active_probe_interval: int | None = None
    deep_probe_interval: int | None = None
    health_url: str | None = None
    health_timeout: int | None = None
    failover_mode: str | None = None
    failover_threshold: int | None = None
    failover_cooldown: int | None = None
    failover_min_healthy: int | None = None
    rotation_enabled: bool | None = None
    rotation_max_duration_hours: int | None = None
    rotation_exclusion_hours: int | None = None
    rotation_dry_run: bool | None = None
    rotation_check_interval: int | None = None
    gateway_enabled: bool | None = None
    gateway_bind: str | None = None
    public_socks_port: int | None = None
    public_http_port: int | None = None
    default_route: str | None = None

    from pydantic import field_validator as _fv2

    @_fv2("health_url")
    @classmethod
    def _health_url_must_be_safe(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validated_http_url(v, field="health_url")


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


def _validated_http_url(value: str, field: str = "url") -> str:
    """Validate an absolute http(s) URL; reject credentials, loopback and
    cloud-metadata targets (SSRF defense for admin-supplied URLs)."""
    from urllib.parse import urlparse

    v = (value or "").strip()
    if len(v) < 8 or len(v) > 2048:
        raise ValueError(f"{field} must be 8..2048 characters")
    try:
        u = urlparse(v)
    except Exception as exc:
        raise ValueError(f"{field} is not a valid URL: {exc}") from exc
    if u.scheme not in ("http", "https"):
        raise ValueError(f"{field} scheme must be http or https")
    host = (u.hostname or "").lower()
    if not host:
        raise ValueError(f"{field} must include a host")
    if u.username or u.password:
        raise ValueError(f"{field} must not embed credentials")
    if host in ("localhost", "metadata.google.internal") or host.endswith(".localhost"):
        raise ValueError(f"{field} host is not allowed")
    try:
        import ipaddress as _ip

        ip = _ip.ip_address(host.strip("[]"))
        if ip.is_loopback or ip.is_link_local or str(ip) == "169.254.169.254":
            raise ValueError(f"{field} host is not allowed")
    except ValueError as exc:
        if "not allowed" in str(exc):
            raise
    return v


class SubscriptionIn(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    update_interval: int | None = Field(default=None, ge=300, le=2592000)

    from pydantic import field_validator as _fv

    @_fv("url")
    @classmethod
    def _url_must_be_safe(cls, v: str) -> str:
        return _validated_http_url(v)


class FailoverConfigIn(BaseModel):
    mode: str | None = None
    threshold: int | None = Field(default=None, ge=1, le=100)
    cooldown: int | None = Field(default=None, ge=0, le=86400)
    min_healthy: int | None = Field(default=None, ge=0, le=86400)
