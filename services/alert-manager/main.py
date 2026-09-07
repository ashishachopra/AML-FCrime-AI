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
from alerts import AlertManager
from events import consume_events
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

connection: aio_pika.RobustConnection | None = None
channel: aio_pika.abc.AbstractRobustChannel | None = None
exchange: aio_pika.abc.AbstractExchange | None = None
consumer_task: asyncio.Task[None] | None = None
alert_manager: AlertManager | None = None


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
    global connection, channel, exchange, consumer_task, alert_manager
    alert_manager = AlertManager()
    connection = await aio_pika.connect_robust(_rabbitmq_url())
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=int(os.getenv("EVENT_PREFETCH", "20")))
    exchange = await channel.declare_exchange("aml.events", ExchangeType.FANOUT, durable=True)
    consumer_task = asyncio.create_task(
        consume_events(channel, exchange, process_scored_event),
        name="alert-manager-consumer",
    )
    logger.info("Alert manager started")
    try:
        yield
    finally:
        if consumer_task:
            consumer_task.cancel()
            with suppress(asyncio.CancelledError):
                await consumer_task
        if connection and not connection.is_closed:
            await connection.close()
        if alert_manager:
            if alert_manager.openai_client:
                await alert_manager.openai_client.close()
            alert_manager.repository._connection.close()
            alert_manager = None


app = FastAPI(
    title="AML Alert Manager API",
    version="3.1.0",
    description="Persistent alerts, append-only audit events, and human-reviewed narrative drafts",
    lifespan=lifespan,
)

AlertStatus = Literal["open", "investigating", "closed", "false_positive"]
SarReviewStatus = Literal["not_generated", "draft_pending_review", "approved", "rejected"]


class Alert(BaseModel):
    revision: int = 1
    alert_id: str
    txn_id: str
    customer_id: str | None = None
    risk_score: float = Field(ge=0, le=1)
    status: AlertStatus
    alert_type: str
    created_at: datetime
    updated_at: datetime
    decision_basis: str
    scorer_version: str | None = None
    evidence: dict[str, Any]
    sar_narrative: str | None = None
    sar_review_status: SarReviewStatus
    sar_generated_by: Literal["openai", "template"] | None = None
    sar_model: str | None = None
    sar_generation_reason: str | None = None
    sar_reviewed_by: str | None = None
    sar_reviewed_at: datetime | None = None
    investigation_notes: str | None = None
    assigned_to: str | None = None


class AlertUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)

    status: AlertStatus | None = None
    investigation_notes: str | None = Field(default=None, max_length=10_000)
    assigned_to: str | None = Field(default=None, max_length=200)
    sar_review_status: Literal["approved", "rejected"] | None = None
    actor: str = Field(min_length=1, max_length=200)


class AlertsResponse(BaseModel):
    alerts: list[Alert]
    total: int
    limit: int
    offset: int


class HealthResponse(BaseModel):
    status: str
    timestamp: datetime
    dependencies: dict[str, str] | None = None


def _manager() -> AlertManager:
    if not alert_manager:
        raise HTTPException(status_code=503, detail="service is not ready")
    return alert_manager


async def process_scored_event(event_data: dict[str, Any]) -> None:
    if event_data.get("type") != "Scored":
        return
    data = event_data.get("data")
    if not isinstance(data, dict):
        raise ValueError("event data must be an object")
    alert = await _manager().process_scored_transaction(data)
    if alert:
        logger.info("Processed alert %s for transaction %s", alert["alert_id"], alert["txn_id"])


@app.get("/alerts", response_model=AlertsResponse)
async def get_alerts(
    status: AlertStatus | None = Query(None),
    risk_threshold: float | None = Query(None, ge=0, le=1),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    txn_id: str | None = Query(None, min_length=1, max_length=128),
) -> AlertsResponse:
    alerts = await _manager().get_alerts(status, risk_threshold, limit, offset, txn_id)
    total = await _manager().count_alerts(status, risk_threshold, txn_id)
    return AlertsResponse(
        alerts=[Alert(**item) for item in alerts],
        total=total,
        limit=limit,
        offset=offset,
    )


@app.get("/alerts/statistics")
async def get_statistics() -> dict[str, Any]:
    return _manager().get_alert_statistics()


@app.get("/alerts/{alert_id}", response_model=Alert)
async def get_alert(alert_id: str) -> Alert:
    alert = await _manager().get_alert_by_id(alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="alert not found")
    return Alert(**alert)


@app.get("/alerts/{alert_id}/audit")
async def get_alert_audit(alert_id: str) -> list[dict[str, Any]]:
    if not await _manager().get_alert_by_id(alert_id):
        raise HTTPException(status_code=404, detail="alert not found")
    return await _manager().get_alert_audit(alert_id)


@app.patch("/alerts/{alert_id}", response_model=Alert)
async def update_alert(alert_id: str, update: AlertUpdate) -> Alert:
    try:
        alert = await _manager().update_alert(alert_id, update.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not alert:
        raise HTTPException(status_code=404, detail="alert not found")
    return Alert(**alert)


@app.get("/health/live", response_model=HealthResponse)
async def liveness() -> HealthResponse:
    return HealthResponse(status="healthy", timestamp=datetime.now(timezone.utc))


@app.get("/health/ready", response_model=HealthResponse)
@app.get("/health", response_model=HealthResponse)
async def readiness() -> HealthResponse:
    broker_ready = bool(connection and not connection.is_closed and exchange)
    consumer_ready = bool(consumer_task and not consumer_task.done())
    if not broker_ready or not consumer_ready or not alert_manager:
        raise HTTPException(status_code=503, detail="service dependencies are not ready")
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(timezone.utc),
        dependencies={"rabbitmq": "ready", "consumer": "ready", "sqlite": "ready"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8005)
