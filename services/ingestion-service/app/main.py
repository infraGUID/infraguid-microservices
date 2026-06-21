import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from infraguid_common.config.settings import get_settings
from infraguid_common.database.postgres_client import dispose_engine, init_db
from infraguid_common.observability.logger import configure_logging, get_logger

from app.api.routes import router
from app.worker import consume_forever

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ingestion-service owns the schema: ensure tables + pgvector extension exist.
    try:
        await init_db()
    except Exception as exc:
        logger.warning("database_initialization_error", error=str(exc))

    # Start the SQS consumer alongside the API when a queue is configured.
    settings = get_settings()
    worker_task: asyncio.Task | None = None
    stop_event = asyncio.Event()
    if settings.ingestion_worker_enabled and settings.sqs_ingestion_enabled:
        worker_task = asyncio.create_task(consume_forever(stop_event))
    else:
        logger.info("ingestion_worker_disabled", queue_configured=settings.sqs_ingestion_enabled)

    yield

    if worker_task is not None:
        stop_event.set()
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
    await dispose_engine()


app = FastAPI(
    title="InfraGuidAI Ingestion Service",
    description="Document load, chunk, embed (LangChain AWS), and pgvector upsert.",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/")
async def root() -> dict:
    return {"service": "ingestion-service", "status": "running"}
