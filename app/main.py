import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from app.api.v1.auth import router as auth_router
from app.api.v1.error_handlers import register_error_handlers
from app.api.v1.health import router as health_router
from app.api.v1.live_sessions import router as live_sessions_router
from app.api.v1.meetings import router as meetings_router
from app.api.v1.participants import router as participants_router
from app.api.v1.transcripts import router as transcripts_router
from app.api.v1.users import router as users_router
from app.config import get_settings
from app.database import SessionLocal
from app.services.meeting_lifecycle_service import MeetingLifecycleService

logger = logging.getLogger(__name__)


async def _zombie_session_reaper_loop():
    while True:
        try:
            await asyncio.sleep(60)

            def _reap_sync():
                with SessionLocal() as db:
                    svc = MeetingLifecycleService(db)
                    reaped_sessions = svc.reap_timed_out_sessions(ttl_seconds=120)
                    reaped_meetings = svc.reap_orphaned_meetings(session_ttl_seconds=120, min_age_seconds=180)
                    stale_ai = svc.reap_stale_ai_processing(timeout_minutes=5)
                    if reaped_sessions or reaped_meetings or stale_ai:
                        logger.info(
                            "Lifecycle Reaper: %d sessions timed out, %d meetings completed, %d stale AI reset",
                            reaped_sessions,
                            reaped_meetings,
                            stale_ai,
                        )

            await run_in_threadpool(_reap_sync)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Error in zombie session reaper loop: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    reaper_task = asyncio.create_task(_zombie_session_reaper_loop())
    yield
    reaper_task.cancel()
    try:
        await reaper_task
    except asyncio.CancelledError:
        pass


def create_app() -> FastAPI:
    app = FastAPI(title="DevMeeting AI Backend", lifespan=lifespan)
    register_error_handlers(app)

    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")
    app.include_router(meetings_router, prefix="/api/v1")
    app.include_router(live_sessions_router, prefix="/api/v1")
    app.include_router(participants_router, prefix="/api/v1")
    app.include_router(transcripts_router, prefix="/api/v1")

    return app


app = create_app()