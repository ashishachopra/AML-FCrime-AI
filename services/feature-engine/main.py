import asyncio
import logging
import os
import sqlite3
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aio_pika
import anyio
from aio_pika import ExchangeType
from events import consume_events, publish_envelope
from evidence import ComputeFeaturesRequest
from fastapi import FastAPI, HTTPException, Query
from features import FeatureEngine
from pydantic import BaseModel
from store import FeatureStore, ReplayConflict

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

connection = None
channel = None
exchange = None
consumer_task = None
publisher_task = None
feature_engine: FeatureEngine | None = None
feature_store: FeatureStore | None = None
work_slots: asyncio.Semaphore | None = None


def _rabbitmq_url() -> str:
    configured = os.getenv("RABBITMQ_URL")
    if configured:
        return configured
    password_file = os.getenv("RABBITMQ_PASSWORD_FILE")
    password = (
        Path(password_file).read_text(encoding="utf-8").strip()
        if password_file
        else os.getenv("RABBITMQ_PASSWORD", "guest")
    )
    user = quote(os.getenv("RABBITMQ_USER", "guest"), safe="")
    host = os.getenv("RABBITMQ_HOST", "rabbitmq")
    port = int(os.getenv("RABBITMQ_PORT", "5672"))
    vhost = quote(os.getenv("RABBITMQ_VHOST", ""), safe="")
    return f"amqp://{user}:{quote(password, safe='')}@{host}:{port}/{vhost}"


async def run_store(function, *args, **kwargs):
    """Bound admitted work; do not block the event loop on SQLite or CPU work."""
    if work_slots is None:
        raise HTTPException(status_code=503, detail="service is not ready")
    try:
        await asyncio.wait_for(work_slots.acquire(), timeout=0.1)
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="feature service busy; retry later") from exc
    try:
        return await anyio.to_thread.run_sync(partial(function, *args, **kwargs))
    except sqlite3.OperationalError as exc:
        raise HTTPException(
            status_code=503, detail="feature store unavailable; retry later"
        ) from exc
    finally:
        work_slots.release()


async def flush_outbox() -> int:
    """Confirms precede deletion; an uncertain delivery retains the same event ID."""
    if not exchange:
        raise RuntimeError("event exchange is not ready")
    repository = _store()
    rows = await run_store(repository.pending)
    for row in rows:
        await publish_envelope(exchange, row["event"])
        await run_store(repository.mark_published, row["sequence"])
    return len(rows)


async def publish_outbox_forever() -> None:
    delay = 0.25
    while True:
        try:
            count = await flush_outbox()
            delay = 0.25
            if not count:
                await asyncio.sleep(0.1)
        except Exception:
            logger.exception("Feature outbox delivery failed; retained for retry")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global connection, channel, exchange, consumer_task, publisher_task
    global feature_engine, feature_store, work_slots
    feature_engine = FeatureEngine()
    slots = int(os.getenv("FEATURE_MAX_INFLIGHT", "16"))
    if slots < 1:
        raise ValueError("FEATURE_MAX_INFLIGHT must be positive")
    work_slots = asyncio.Semaphore(slots)
    feature_store = FeatureStore(
        os.getenv("FEATURE_DB_PATH", "/data/features.db"),
        feature_engine,
        max_history=int(os.getenv("FEATURE_MAX_HISTORY_ROWS", "1000")),
        max_network=int(os.getenv("FEATURE_MAX_NETWORK_ROWS", "1000")),
        max_outbox=int(os.getenv("FEATURE_MAX_OUTBOX", "10000")),
    )
    try:
        connection = await aio_pika.connect_robust(_rabbitmq_url())
        channel = await connection.channel(publisher_confirms=True)
        await channel.set_qos(prefetch_count=int(os.getenv("EVENT_PREFETCH", "20")))
        exchange = await channel.declare_exchange("aml.events", ExchangeType.FANOUT, durable=True)
        consumer_task = asyncio.create_task(
            consume_events(channel, exchange, process_ingested_event), name="feature-consumer"
        )
        publisher_task = asyncio.create_task(publish_outbox_forever(), name="feature-outbox")
        yield
    finally:
        for task in (consumer_task, publisher_task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        if connection and not connection.is_closed:
            await connection.close()
        await anyio.to_thread.run_sync(feature_store.close)
        feature_store = None
        work_slots = None


app = FastAPI(
    title="AML Feature Engineering API",
    version="3.0.0",
    description="Indexed hybrid AML features with immutable snapshots and a durable outbox",
    lifespan=lifespan,
)


class TransactionFeatures(BaseModel):
    txn_id: str
    features: dict[str, float]
    computed_at: datetime
    feature_version: str


class FeaturesListResponse(BaseModel):
    features: list[TransactionFeatures]
    total: int


class TransactionDetails(BaseModel):
    txn_id: str
    account_id: str
    customer_id: str | None = None
    timestamp: datetime
    amount: float
    currency: str
    counterparty_country: str
    features: dict[str, float]


def _store() -> FeatureStore:
    if not feature_store:
        raise HTTPException(status_code=503, detail="service is not ready")
    return feature_store


async def process_ingested_event(event_data: dict[str, Any]) -> None:
    kind = event_data.get("type")
    data = event_data.get("data")
    if not isinstance(data, dict):
        raise ValueError("event data must be an object")
    repository = _store()
    if kind in {"IngestedCustomer", "IngestedAccount"}:
        await run_store(repository.put_entity, kind, data)
    elif kind == "IngestedTransaction":
        await run_store(repository.evaluate, data, persist=True, batch_id=event_data.get("batchid"))


@app.get("/features/{txn_id}", response_model=TransactionFeatures)
async def get_features(txn_id: str):
    value = await run_store(_store().get, txn_id)
    if not value:
        raise HTTPException(status_code=404, detail="transaction not found")
    return value


@app.get("/transactions/{txn_id}", response_model=TransactionDetails)
async def get_transaction(txn_id: str):
    value = await get_features(txn_id)
    return dict(value["transaction"], features=value["features"])


@app.post("/compute", response_model=TransactionFeatures)
async def compute_features(request: ComputeFeaturesRequest):
    try:
        return await run_store(_store().evaluate, request.canonical())
    except ReplayConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/features", response_model=FeaturesListResponse)
async def get_all_features(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
    return await run_store(_store().list, limit, offset)


@app.get("/metadata")
async def metadata():
    return dict(_store().engine.metadata(), storage=await run_store(_store().statistics))


@app.get("/health/live")
async def liveness():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc)}


@app.get("/health/ready")
@app.get("/health")
async def readiness():
    if not (
        connection
        and not connection.is_closed
        and exchange
        and consumer_task
        and not consumer_task.done()
        and publisher_task
        and not publisher_task.done()
    ):
        raise HTTPException(status_code=503, detail="service dependencies are not ready")
    statistics = await run_store(_store().statistics)
    if statistics["outbox_pending"] >= statistics["max_outbox"]:
        raise HTTPException(status_code=503, detail="outbox capacity exhausted")
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc),
        "dependencies": {"rabbitmq": "ready", "store": "ready", "outbox": "ready"},
        "outbox_pending": statistics["outbox_pending"],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8002)
