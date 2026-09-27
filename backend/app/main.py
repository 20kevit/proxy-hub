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
from .api.deps import current_user
from .config import get_settings
from .db import get_session_factory, init_db
from .models import Setting, User
from .services.auth import hash_password
from .services.routing import ensure_defaults

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
                # First-run bootstrap: create locked admin; operator sets password via set-admin.
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


async def scheduler_loop() -> None:
    """In-process timers (single VPS, lightweight): subscription refresh + health sweeps."""
    from .services.failover import maybe_failover, session_get
    from .services.health import check_all
    from .services.subscription import update_subscription

    # Stagger first runs so startup stays fast.
    await asyncio.sleep(20)
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
                        from .services.failover import session_set as _set
                        _set(db2, "last_sub_run", str(now))
                    finally:
                        db2.close()
            except Exception as exc:
                log.warning("scheduler subscription pass failed: %s", exc)
            try:
                h_interval = int(session_get(db, "health_interval", str(settings.health_interval)))
                from .models import ProxyNode
                active = db.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).first()
                check_all(db, egress_for_active=True)
                if active is not None:
                    fresh = db.query(ProxyNode).filter(ProxyNode.id == active.id).first()
                    if fresh is not None and fresh.health == "unhealthy":
                        from .models import AuditLog as _Audit

                        def _audit(a, x, d):
                            db.add(_Audit(actor=a, action=x, detail=d))
                            db.commit()

                        res = maybe_failover(db, fresh, audit=_audit)
                        if res.get("switched"):
                            log.info("failover: %s", res.get("reason"))
                await asyncio.sleep(max(h_interval, 60))
            except Exception as exc:
                log.warning("scheduler health pass failed: %s", exc)
                await asyncio.sleep(120)
        finally:
            db.close()


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
    task = asyncio.create_task(scheduler_loop())
    log.info("proxyhub control plane starting")
    yield
    task.cancel()


app = FastAPI(title="ProxyHub", docs_url=None, redoc_url=None, openapi_url=None)


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
    return await call_next(request)


app.include_router(auth_api.router)
app.include_router(system_api.router)
app.include_router(nodes_api.router)
app.include_router(sub_api.router)
app.include_router(manage_api.router)
app.include_router(clients_api.router)
app.include_router(rules_api.router)
app.include_router(logs_api.router)


@app.get("/api/logs")
def logs_tail(lines: int = 100, user: str = Depends(current_user)):
    return {"note": "use journalctl -u proxy-hub -n 100 (see System page / OPERATIONS.md)"}


if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR)), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(str(FRONTEND_DIR / "index.html"))
