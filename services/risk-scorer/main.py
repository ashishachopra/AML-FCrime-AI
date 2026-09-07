import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import aio_pika
from aio_pika import ExchangeType
from events import consume_events, publish_event
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from scorer import RiskScorer

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

connection: aio_pika.RobustConnection | None = None
channel: aio_pika.abc.AbstractRobustChannel | None = None
exchange: aio_pika.abc.AbstractExchange | None = None
consumer_task: asyncio.Task[None] | None = None
risk_scorer: RiskScorer | None = None
scored_transactions: dict[str, dict[str, Any]] = {}


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
    global connection, channel, exchange, consumer_task, risk_scorer
    risk_scorer = RiskScorer()
    connection = await aio_pika.connect_robust(_rabbitmq_url())
    channel = await connection.channel(publisher_confirms=True)
    await channel.set_qos(prefetch_count=int(os.getenv("EVENT_PREFETCH", "20")))
    exchange = await channel.declare_exchange("aml.events", ExchangeType.FANOUT, durable=True)
    consumer_task = asyncio.create_task(
        consume_events(channel, exchange, process_features_ready_event),
        name="risk-scorer-consumer",
    )
    logger.info("Risk scorer started")
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
    title="AML Risk Scoring API",
    version="3.0.0",
    description="Deterministic reference risk policy with explicit validation limitations",
    lifespan=lifespan,
)


class ScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    txn_id: str = Field(min_length=1, max_length=128)
    features: dict[str, float] = Field(min_length=1, max_length=100)
    transaction: dict[str, Any] | None = None
    feature_version: str | None = Field(default=None, max_length=100)


class ScoreResponse(BaseModel):
    txn_id: str
    risk_score: float = Field(ge=0, le=1)
    risk_category: Literal["low", "medium", "high", "critical"]
    data_quality_score: float = Field(ge=0, le=1)
    decision_basis: str
    scorer_version: str
    feature_version: str | None = None
    review_recommended: bool
    feature_contributions: dict[str, float]
    triggered_rules: list[str]
    transaction: dict[str, Any]
    scored_at: datetime


class ScoresListResponse(BaseModel):
    scores: list[ScoreResponse]
    total: int
    limit: int
    offset: int


class HealthResponse(BaseModel):
    status: str
    timestamp: datetime
    dependencies: dict[str, str] | None = None


def _scorer() -> RiskScorer:
    if not risk_scorer:
        raise HTTPException(status_code=503, detail="service is not ready")
    return risk_scorer


async def process_features_ready_event(event_data: dict[str, Any]) -> None:
    if not exchange:
        raise RuntimeError("event exchange is not ready")
    if event_data.get("type") != "FeaturesReady":
        return
    data = event_data.get("data")
    if not isinstance(data, dict):
        raise ValueError("event data must be an object")
    result = await _scorer().score_transaction(
        data["txn_id"], data["features"], data.get("transaction")
    )
    result["feature_version"] = data.get("feature_version")
    scored_transactions[data["txn_id"]] = result
    await publish_event(exchange, "Scored", result, event_data.get("batchid"))


@app.post("/score", response_model=ScoreResponse)
async def score_transaction(request: ScoreRequest) -> ScoreResponse:
    try:
        result = await _scorer().score_transaction(
            request.txn_id, request.features, request.transaction
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    scored_transactions[request.txn_id] = result
    result["feature_version"] = request.feature_version
    return ScoreResponse(**result)


@app.post("/evaluate", response_model=ScoreResponse)
async def evaluate_transaction(request: ScoreRequest) -> ScoreResponse:
    """Read-only preview: does not overwrite stored scores or emit events."""
    try:
        result = await _scorer().score_transaction(
            request.txn_id, request.features, request.transaction
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result["feature_version"] = request.feature_version
    return ScoreResponse(**result)


@app.get("/scorer/metadata")
@app.get("/model/metrics", deprecated=True)
async def get_scorer_metadata() -> dict[str, Any]:
    return _scorer().get_scorer_metadata()


@app.get("/scores", response_model=ScoresListResponse)
async def get_all_scores(
    limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)
) -> ScoresListResponse:
    values = list(scored_transactions.values())
    page = [ScoreResponse(**value) for value in values[offset : offset + limit]]
    return ScoresListResponse(
        scores=page,
        total=len(values),
        limit=limit,
        offset=offset,
    )


@app.get("/scores/{txn_id}", response_model=ScoreResponse)
async def get_score(txn_id: str) -> ScoreResponse:
    value = scored_transactions.get(txn_id)
    if not value:
        raise HTTPException(status_code=404, detail="score not found")
    return ScoreResponse(**value)


@app.get("/health/live", response_model=HealthResponse)
async def liveness() -> HealthResponse:
    return HealthResponse(status="healthy", timestamp=datetime.now(timezone.utc))


@app.get("/health/ready", response_model=HealthResponse)
@app.get("/health", response_model=HealthResponse)
async def readiness() -> HealthResponse:
    broker_ready = bool(connection and not connection.is_closed and exchange)
    consumer_ready = bool(consumer_task and not consumer_task.done())
    if not broker_ready or not consumer_ready or not risk_scorer:
        raise HTTPException(status_code=503, detail="service dependencies are not ready")
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(timezone.utc),
        dependencies={"rabbitmq": "ready", "consumer": "ready"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8003)
