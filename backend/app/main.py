"""ProxyHub control plane. Management only — proxy traffic is handled by Xray/sing-box."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import auth as auth_api
from .api import clients as clients_api
from .api import logs as logs_api
from .api import manage as manage_api
from .api import nodes as nodes_api
from .api import rules as rules_api
from .api import subscription as sub_api
from .api import system as system_api
from .api import telemetry as telemetry_api
from .api.deps import current_user
from .config import get_settings
from .db import get_session_factory, init_db
from .models import Setting, User
from .services.auth import hash_password
from .services.health_v2 import health_engine
from .services.lifecycle import lifecycle_manager
from .services.routing import ensure_defaults
from .services.watchdog import watchdog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("proxyhub")

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


def ensure_admin() -> None:
    settings = get_settings()
    db = get_session_factory()()
    try:
        if db.query(User).count() == 0:
            password = settings.admin_password
            if not password:
                log.warning("no users and PROXYHUB_ADMIN_PASSWORD unset — run scripts/set-admin.sh")
                return
            db.add(User(username=settings.admin_user, password_hash=hash_password(password)))
            db.commit()
            log.info("bootstrap admin user '%s' created", settings.admin_user)
    finally:
        db.close()


def _setting(db, key: str, default: str) -> str:
    row = db.query(Setting).filter(Setting.key == key).first()
    return row.value if row else default


async def subscription_scheduler() -> None:
    from .services.failover import session_get, session_set
    from .services.subscription import update_subscription

    await asyncio.sleep(30)
    while True:
        settings = get_settings()
        db = get_session_factory()()
        try:
            try:
                interval = int(session_get(db, "sub_update_interval", str(settings.sub_update_interval)))
                last_ok = session_get(db, "last_sub_run", "")
                import time as _t
                now = _t.time()
                due = not last_ok or (now - float(last_ok or 0)) >= interval
                if due and settings.subscription_url:
                    try:
                        update_subscription(db)
                    except Exception as exc:
                        log.warning("scheduled subscription update failed: %s", type(exc).__name__)
                    db2 = get_session_factory()()
                    try:
                        session_set(db2, "last_sub_run", str(now))
                    finally:
                        db2.close()
            except Exception as exc:
                log.warning("scheduler subscription pass failed: %s", exc)
            await asyncio.sleep(300)
        finally:
            db.close()


async def failover_monitor() -> None:
    from .services.failover import maybe_failover, session_get
    from .models import AuditLog, ProxyNode

    await asyncio.sleep(10)
    while True:
        db = get_session_factory()()
        try:
            active = db.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).first()
            should_failover = False
            reason = ""
            if active:
                if active.health == "unhealthy" and not active.circuit_open:
                    should_failover = True
                    reason = "health unhealthy"
                elif not active.egress_ok and active.consecutive_failures >= 3:
                    should_failover = True
                    reason = f"egress failures ({active.consecutive_failures})"
                elif active.consecutive_failures >= 5:
                    should_failover = True
                    reason = f"consecutive failures ({active.consecutive_failures})"
            
            if should_failover:
                def _audit(actor, action, detail):
                    db.add(AuditLog(actor=actor, action=action, detail=detail))
                    db.commit()
                res = maybe_failover(db, active, audit=_audit)
                if res.get("switched"):
                    log.info("failover: %s (%s)", res.get("reason"), reason)
            await asyncio.sleep(5)
        except Exception as exc:
            log.warning("failover monitor error: %s", exc)
            await asyncio.sleep(10)
        finally:
            db.close()


async def lifecycle_monitor() -> None:
    """Monitor and execute scheduled node rotations."""
    from .services.lifecycle import lifecycle_manager

    await asyncio.sleep(60)  # Start after health engine stabilizes
    while True:
        try:
            await lifecycle_manager._check_and_rotate()
        except Exception as exc:
            log.warning("lifecycle monitor error: %s", exc)
        await asyncio.sleep(300)  # Check every 5 minutes


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = get_session_factory()()
    try:
        ensure_defaults(db)
        from .services.routing_engine import ensure_seed_rules
        ensure_seed_rules(db)
    finally:
        db.close()
    ensure_admin()

    await health_engine.start()
    await lifecycle_manager.start()
    await watchdog.start()

    sub_task = asyncio.create_task(subscription_scheduler())
    fo_task = asyncio.create_task(failover_monitor())
    lc_task = asyncio.create_task(lifecycle_monitor())

    log.info("proxyhub control plane starting with HealthEngineV2, LifecycleManager, and Watchdog")
    yield

    await health_engine.stop()
    await lifecycle_manager.stop()
    await watchdog.stop()
    sub_task.cancel()
    fo_task.cancel()
    lc_task.cancel()
    await asyncio.gather(sub_task, fo_task, lc_task, return_exceptions=True)


app = FastAPI(title="ProxyHub", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    if (
        request.method in ("POST", "PUT", "PATCH", "DELETE")
        and request.url.path.startswith("/api")
        and request.url.path != "/api/auth/login"
    ):
        header = request.headers.get("x-csrf-token", "")
        cookie = request.cookies.get("ph_csrf", "")
        if not header or not cookie or header != cookie:
            return JSONResponse(status_code=403, content={"detail": "bad csrf token"})
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "no-referrer-when-downgrade"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'"
    )
    return resp


app.include_router(auth_api.router)
app.include_router(system_api.router)
app.include_router(nodes_api.router)
app.include_router(sub_api.router)
app.include_router(manage_api.router)
app.include_router(clients_api.router)
app.include_router(rules_api.router)
app.include_router(logs_api.router)
app.include_router(telemetry_api.router)


@app.get("/api/logs")
def logs_tail(lines: int = 100, user: str = Depends(current_user)):
    return {"note": "use journalctl -u proxy-hub -n 100 (see System page / OPERATIONS.md)"}


if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR)), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(str(FRONTEND_DIR / "index.html"))