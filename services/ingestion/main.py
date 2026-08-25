import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aio_pika
from aio_pika import ExchangeType
from data_processor import DataProcessor
from events import publish_event
from fastapi import FastAPI, File, HTTPException, UploadFile, status
from models import Account, Customer, Transaction
from pydantic import BaseModel, ValidationError

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

connection: aio_pika.RobustConnection | None = None
channel: aio_pika.abc.AbstractRobustChannel | None = None
exchange: aio_pika.abc.AbstractExchange | None = None
data_processor: DataProcessor | None = None


def _read_secret(name: str, default: str = "") -> str:
    file_name = os.getenv(f"{name}_FILE")
    if file_name:
        return Path(file_name).read_text(encoding="utf-8").strip()
    return os.getenv(name, default)


def _rabbitmq_url() -> str:
    configured_url = os.getenv("RABBITMQ_URL")
    if configured_url:
        return configured_url
    user = quote(os.getenv("RABBITMQ_USER", "guest"), safe="")
    password = quote(_read_secret("RABBITMQ_PASSWORD", "guest"), safe="")
    host = os.getenv("RABBITMQ_HOST", "rabbitmq")
    port = int(os.getenv("RABBITMQ_PORT", "5672"))
    vhost = quote(os.getenv("RABBITMQ_VHOST", ""), safe="")
    return f"amqp://{user}:{password}@{host}:{port}/{vhost}"


@asynccontextmanager
async def lifespan(_: FastAPI):
    global connection, channel, exchange, data_processor
    data_processor = DataProcessor(int(os.getenv("MAX_BATCH_RECORDS", "10000")))
    connection = await aio_pika.connect_robust(_rabbitmq_url())
    channel = await connection.channel(publisher_confirms=True)
    exchange = await channel.declare_exchange("aml.events", ExchangeType.FANOUT, durable=True)
    logger.info("Ingestion service connected to RabbitMQ")
    try:
        yield
    finally:
        if connection and not connection.is_closed:
            await connection.close()


app = FastAPI(
    title="AML Ingestion API",
    version="2.0.0",
    description="Validates related AML batch records and publishes durable events",
    lifespan=lifespan,
)


class BatchResponse(BaseModel):
    message: str
    batch_id: str
    records_processed: int


class HealthResponse(BaseModel):
    status: str
    timestamp: datetime
    dependencies: dict[str, str] | None = None


async def _read_upload(upload: UploadFile, max_bytes: int) -> bytes:
    content_type = (upload.content_type or "").partition(";")[0].lower()
    if content_type not in {"application/json", "text/json"}:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"{upload.filename or 'upload'} must use application/json",
        )

    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"{upload.filename or 'upload'} exceeds {max_bytes} bytes",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _validation_detail(label: str, exc: ValidationError) -> dict[str, Any]:
    return {"dataset": label, "errors": exc.errors(include_url=False)}


@app.post("/batch", response_model=BatchResponse, status_code=status.HTTP_201_CREATED)
async def upload_batch(
    accounts: UploadFile = File(...),
    customers: UploadFile = File(...),
    transactions: UploadFile = File(...),
):
    if not data_processor or not exchange:
        raise HTTPException(status_code=503, detail="service is not ready")

    max_bytes = int(os.getenv("MAX_UPLOAD_FILE_BYTES", str(5 * 1024 * 1024)))
    accounts_content = await _read_upload(accounts, max_bytes)
    customers_content = await _read_upload(customers, max_bytes)
    transactions_content = await _read_upload(transactions, max_bytes)

    try:
        raw_accounts, raw_customers, raw_transactions = await data_processor.process_batch_files(
            accounts_content, customers_content, transactions_content
        )
        validated_accounts = [Account.model_validate(record) for record in raw_accounts]
        validated_customers = [Customer.model_validate(record) for record in raw_customers]
        validated_transactions = [Transaction.model_validate(record) for record in raw_transactions]
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_validation_detail("batch", exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    batch_id = str(uuid.uuid4())
    records = (
        [("IngestedAccount", item) for item in validated_accounts]
        + [("IngestedCustomer", item) for item in validated_customers]
        + [("IngestedTransaction", item) for item in validated_transactions]
    )

    try:
        for event_type, record in records:
            await publish_event(
                exchange,
                event_type,
                record.model_dump(mode="json"),
                batch_id,
            )
    except Exception as exc:
        logger.exception("Batch %s could not be fully published", batch_id)
        raise HTTPException(
            status_code=503, detail="event broker did not confirm the batch"
        ) from exc

    logger.info("Published batch %s with %d records", batch_id, len(records))
    return BatchResponse(
        message="Batch validated and published",
        batch_id=batch_id,
        records_processed=len(records),
    )


@app.get("/health/live", response_model=HealthResponse)
async def liveness() -> HealthResponse:
    return HealthResponse(status="healthy", timestamp=datetime.now(timezone.utc))


@app.get("/health/ready", response_model=HealthResponse)
@app.get("/health", response_model=HealthResponse)
async def readiness() -> HealthResponse:
    broker_ready = bool(connection and not connection.is_closed and exchange)
    if not broker_ready:
        raise HTTPException(status_code=503, detail="RabbitMQ connection is not ready")
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(timezone.utc),
        dependencies={"rabbitmq": "ready"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
