"""Versioned SQLite migrations. Additive only; never drops tables or data.

Each migration is idempotent: it checks for the target column/table/index
before altering. `current_version()` reads the recorded version (0 when the
version table does not exist yet).
"""
from __future__ import annotations

import logging

log = logging.getLogger("proxyhub.migrations")

SCHEMA_VERSION = 4


def _columns(conn, table: str) -> set[str]:
    import sqlalchemy as _sa

    try:
        return {c["name"] for c in _sa.inspect(conn).get_columns(table)}
    except Exception:
        return set()


def _tables(conn) -> set[str]:
    import sqlalchemy as _sa

    try:
        return set(_sa.inspect(conn).get_table_names())
    except Exception:
        return set()


def current_version(conn) -> int:
    import sqlalchemy as _sa

    if "schema_version" not in _tables(conn):
        return 0
    try:
        row = conn.execute(_sa.text("SELECT version FROM schema_version LIMIT 1")).first()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _set_version(conn, version: int) -> None:
    import sqlalchemy as _sa

    conn.execute(_sa.text("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"))
    conn.execute(_sa.text("DELETE FROM schema_version"))
    conn.execute(_sa.text("INSERT INTO schema_version (version) VALUES (:v)"), {"v": version})


def migrate_001_legacy_health(conn) -> None:
    """Original manual migration: egress_ok, last_success, success_count."""
    import sqlalchemy as _sa

    cols = _columns(conn, "proxy_nodes")
    if "egress_ok" not in cols:
        conn.execute(_sa.text("ALTER TABLE proxy_nodes ADD COLUMN egress_ok BOOLEAN DEFAULT 0"))
    if "last_success" not in cols:
        conn.execute(_sa.text("ALTER TABLE proxy_nodes ADD COLUMN last_success DATETIME"))
    if "success_count" not in cols:
        conn.execute(_sa.text("ALTER TABLE proxy_nodes ADD COLUMN success_count INTEGER DEFAULT 0"))


QUALITY_COLUMNS: list[tuple[str, str]] = [
    ("quality_score", "FLOAT DEFAULT 0.0"),
    ("tcp_latency_ms", "FLOAT"),
    ("handshake_latency_ms", "FLOAT"),
    ("app_latency_ms", "FLOAT"),
    ("latency_variance", "FLOAT DEFAULT 0.0"),
    ("consecutive_failures", "INTEGER DEFAULT 0"),
    ("consecutive_successes", "INTEGER DEFAULT 0"),
    ("total_probes", "INTEGER DEFAULT 0"),
    ("successful_probes", "INTEGER DEFAULT 0"),
    ("timeout_count", "INTEGER DEFAULT 0"),
    ("connection_reset_count", "INTEGER DEFAULT 0"),
    ("tls_failure_count", "INTEGER DEFAULT 0"),
    ("circuit_open", "BOOLEAN DEFAULT 0"),
    ("circuit_open_until", "DATETIME"),
    ("last_state_change", "DATETIME"),
    ("rank", "INTEGER DEFAULT 0"),
    ("probe_history", "JSON DEFAULT '{}'"),
]


def migrate_002_quality_engine(conn) -> None:
    """Quality-engine columns for health scoring, circuit breaker and ranking."""
    import sqlalchemy as _sa

    cols = _columns(conn, "proxy_nodes")
    for name, ddl in QUALITY_COLUMNS:
        if name not in cols:
            conn.execute(_sa.text(f"ALTER TABLE proxy_nodes ADD COLUMN {name} {ddl}"))


def migrate_003_telemetry(conn) -> None:
    """Time-series probe telemetry table with indexes."""
    import sqlalchemy as _sa

    if "probe_telemetry" not in _tables(conn):
        conn.execute(_sa.text(
            "CREATE TABLE probe_telemetry ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "node_id INTEGER NOT NULL, "
            "probe_type VARCHAR(16) NOT NULL, "
            "ok BOOLEAN NOT NULL, "
            "latency_ms FLOAT, "
            "error_type VARCHAR(32) DEFAULT '', "
            "http_status INTEGER, "
            "created_at DATETIME NOT NULL)"
        ))
    for idx, cols_ in (
        ("ix_probe_telemetry_node_id", "(node_id)"),
        ("ix_probe_telemetry_created_at", "(created_at)"),
        ("ix_probe_telemetry_probe_type", "(probe_type)"),
        ("ix_probe_telemetry_node_created", "(node_id, created_at)"),
    ):
        try:
            conn.execute(_sa.text(f"CREATE INDEX IF NOT EXISTS {idx} ON probe_telemetry {cols_}"))
        except Exception as exc:
            log.warning("index %s not created: %s", idx, exc)


def migrate_004_session_version(conn) -> None:
    """Server-side session revocation support (logout / password change)."""
    import sqlalchemy as _sa

    if "session_version" not in _columns(conn, "users"):
        conn.execute(_sa.text("ALTER TABLE users ADD COLUMN session_version INTEGER DEFAULT 0"))


MIGRATIONS = {
    1: migrate_001_legacy_health,
    2: migrate_002_quality_engine,
    3: migrate_003_telemetry,
    4: migrate_004_session_version,
}


def migrate(conn) -> int:
    """Run pending migrations inside the caller's transaction. Returns new version."""
    start = current_version(conn)
    for version in range(start + 1, SCHEMA_VERSION + 1):
        fn = MIGRATIONS.get(version)
        if fn is None:
            continue
        fn(conn)
        _set_version(conn, version)
        log.info("migration v%d applied", version)
    return current_version(conn)
