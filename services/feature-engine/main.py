import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aio_pika
from aio_pika import ExchangeType
from events import consume_events, publish_event
from fastapi import FastAPI, HTTPException, Query
from features import FeatureEngine
from pydantic import BaseModel, ConfigDict, Field

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

connection: aio_pika.RobustConnection | None = None
channel: aio_pika.abc.AbstractRobustChannel | None = None
exchange: aio_pika.abc.AbstractExchange | None = None
consumer_task: asyncio.Task[None] | None = None
feature_engine: FeatureEngine | None = None
transaction_store: dict[str, dict[str, Any]] = {}
customer_store: dict[str, dict[str, Any]] = {}
account_store: dict[str, dict[str, Any]] = {}


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


@asynccontextmanager
async def lifespan(_: FastAPI):
    global connection, channel, exchange, consumer_task, feature_engine
    feature_engine = FeatureEngine()
    connection = await aio_pika.connect_robust(_rabbitmq_url())
    channel = await connection.channel(publisher_confirms=True)
    await channel.set_qos(prefetch_count=int(os.getenv("EVENT_PREFETCH", "20")))
    exchange = await channel.declare_exchange("aml.events", ExchangeType.FANOUT, durable=True)
    consumer_task = asyncio.create_task(
        consume_events(channel, exchange, process_ingested_event),
        name="feature-engine-consumer",
    )
    logger.info("Feature engine started")
    try:
        yield
    finally:
        if consumer_task:
            consumer_task.cancel()
            with suppress(asyncio.CancelledError):
                await consumer_task
        if connection and not connection.is_closed:
            await connection.close()


app = FastAPI(
    title="AML Feature Engineering API",
    version="2.0.0",
    description="Computes deterministic, policy-versioned AML risk features",
    lifespan=lifespan,
)


class TransactionFeatures(BaseModel):
    txn_id: str
    features: dict[str, float]
    computed_at: datetime


class ComputeFeaturesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    txn_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(min_length=1, max_length=128)
    timestamp: datetime
    amount: float = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    counterparty_country: str = Field(pattern=r"^[A-Z]{2}$")
    base_currency_amount: float | None = Field(default=None, gt=0)
    base_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")


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


class HealthResponse(BaseModel):
    status: str
    timestamp: datetime
    dependencies: dict[str, str] | None = None


async def process_ingested_event(event_data: dict[str, Any]) -> None:
    if not feature_engine or not exchange:
        raise RuntimeError("feature engine is not ready")
    event_type = event_data.get("type")
    data = event_data.get("data")
    if not isinstance(data, dict):
        raise ValueError("event data must be an object")

    if event_type == "IngestedCustomer":
        customer_store[data["customer_id"]] = data
        return
    if event_type == "IngestedAccount":
        account_store[data["account_id"]] = data
        return
    if event_type != "IngestedTransaction":
        return

    txn_id = data["txn_id"]
    transaction_store[txn_id] = data
    features = await feature_engine.compute_features(
        data, transaction_store, customer_store, account_store
    )
    account = account_store.get(data["account_id"], {})
    transaction_context = {
        key: data.get(key)
        for key in (
            "txn_id",
            "account_id",
            "timestamp",
            "amount",
            "currency",
            "counterparty_country",
        )
    }
    transaction_context["customer_id"] = account.get("customer_id")
    await publish_event(
        exchange,
        "FeaturesReady",
        {"txn_id": txn_id, "features": features, "transaction": transaction_context},
        event_data.get("batchid"),
    )


def _engine() -> FeatureEngine:
    if not feature_engine:
        raise HTTPException(status_code=503, detail="service is not ready")
    return feature_engine


@app.get("/features/{txn_id}", response_model=TransactionFeatures)
async def get_features(txn_id: str) -> TransactionFeatures:
    transaction = transaction_store.get(txn_id)
    if not transaction:
        raise HTTPException(status_code=404, detail="transaction not found")
    try:
        values = await _engine().compute_features(
            transaction, transaction_store, customer_store, account_store
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return TransactionFeatures(
        txn_id=txn_id, features=values, computed_at=datetime.now(timezone.utc)
    )


@app.get("/transactions/{txn_id}", response_model=TransactionDetails)
async def get_transaction(txn_id: str) -> TransactionDetails:
    transaction = transaction_store.get(txn_id)
    if not transaction:
        raise HTTPException(status_code=404, detail="transaction not found")
    values = await _engine().compute_features(
        transaction, transaction_store, customer_store, account_store
    )
    account = account_store.get(transaction["account_id"], {})
    return TransactionDetails(
        txn_id=txn_id,
        account_id=transaction["account_id"],
        customer_id=account.get("customer_id"),
        timestamp=transaction["timestamp"],
        amount=float(transaction["amount"]),
        currency=transaction["currency"],
        counterparty_country=transaction["counterparty_country"],
        features=values,
    )


@app.post("/compute", response_model=TransactionFeatures)
async def compute_features(request: ComputeFeaturesRequest) -> TransactionFeatures:
    data = request.model_dump(mode="json", exclude_none=True)
    try:
        values = await _engine().compute_features(
            data, transaction_store, customer_store, account_store
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return TransactionFeatures(
        txn_id=request.txn_id,
        features=values,
        computed_at=datetime.now(timezone.utc),
    )


@app.get("/features", response_model=FeaturesListResponse)
async def get_all_features(
    limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)
) -> FeaturesListResponse:
    results = []
    items = list(transaction_store.items())[offset : offset + limit]
    for txn_id, transaction in items:
        values = await _engine().compute_features(
            transaction, transaction_store, customer_store, account_store
        )
        results.append(
            TransactionFeatures(
                txn_id=txn_id,
                features=values,
                computed_at=datetime.now(timezone.utc),
            )
        )
    return FeaturesListResponse(features=results, total=len(transaction_store))


@app.get("/metadata")
async def metadata() -> dict[str, Any]:
    return _engine().metadata()


@app.get("/health/live", response_model=HealthResponse)
async def liveness() -> HealthResponse:
    return HealthResponse(status="healthy", timestamp=datetime.now(timezone.utc))


@app.get("/health/ready", response_model=HealthResponse)
@app.get("/health", response_model=HealthResponse)
async def readiness() -> HealthResponse:
    broker_ready = bool(connection and not connection.is_closed and exchange)
    consumer_ready = bool(consumer_task and not consumer_task.done())
    if not broker_ready or not consumer_ready or not feature_engine:
        raise HTTPException(status_code=503, detail="service dependencies are not ready")
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(timezone.utc),
        dependencies={"rabbitmq": "ready", "consumer": "ready"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8002)
