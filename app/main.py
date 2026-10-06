from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import db, scheduler
from .config import settings
from .dashboard.routes import router as dashboard_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("reply_bot.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(settings.db_path, settings.database_url)
    log.info(
        "BOT_MODE=%s, db=%s",
        settings.bot_mode,
        "Postgres (Supabase)" if settings.database_url else settings.db_path,
    )

    scheduler.runtime.start(settings)

    yield

    await scheduler.runtime.stop()
    db.close_db()


app = FastAPI(title="Community Reply Bot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/dashboard/static"), name="static")
app.include_router(dashboard_router)


@app.get("/health")
def health():
    return {"ok": True, "mode": settings.bot_mode, "runtime": scheduler.runtime.status()}
