import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi import Path as ApiPath
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, ConfigDict, Field

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


def _service_url(name: str, default: str) -> str:
    value = os.getenv(name, default).rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{name} must be an absolute HTTP(S) URL")
    return value


INGESTION_SERVICE_URL = _service_url("INGESTION_SERVICE_URL", "http://ingestion:8001")
FEATURE_ENGINE_URL = _service_url("FEATURE_ENGINE_URL", "http://feature-engine:8002")
RISK_SCORER_URL = _service_url("RISK_SCORER_URL", "http://risk-scorer:8003")
ALERT_MANAGER_URL = _service_url("ALERT_MANAGER_URL", "http://alert-manager:8005")

http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global http_client
    timeout = httpx.Timeout(
        connect=float(os.getenv("UPSTREAM_CONNECT_TIMEOUT_SECONDS", "3")),
        read=float(os.getenv("UPSTREAM_READ_TIMEOUT_SECONDS", "30")),
        write=float(os.getenv("UPSTREAM_WRITE_TIMEOUT_SECONDS", "30")),
        pool=3.0,
    )
    http_client = httpx.AsyncClient(
        timeout=timeout,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        follow_redirects=False,
    )
    try:
        yield
    finally:
        await http_client.aclose()
        http_client = None


app = FastAPI(
    title="AML Gateway API",
    version="2.0.0",
    description="Authenticated external gateway for the AML reference pipeline",
    lifespan=lifespan,
)
security = HTTPBearer(auto_error=False)


class BatchResponse(BaseModel):
    message: str
    batch_id: str
    records_processed: int


class Alert(BaseModel):
    alert_id: str
    txn_id: str
    customer_id: str | None = None
    risk_score: float
    status: str
    alert_type: str
    created_at: datetime
    updated_at: datetime
    sar_review_status: str


class AlertDetail(Alert):
    decision_basis: str
    scorer_version: str | None = None
    evidence: dict[str, Any]
    sar_narrative: str | None = None
    sar_generated_by: Literal["openai", "template"] | None = None
    sar_model: str | None = None
    sar_reviewed_by: str | None = None
    sar_reviewed_at: datetime | None = None
    investigation_notes: str | None = None
    assigned_to: str | None = None


class AlertUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["open", "investigating", "closed", "false_positive"] | None = None
    investigation_notes: str | None = Field(default=None, max_length=10_000)
    assigned_to: str | None = Field(default=None, max_length=200)
    sar_review_status: Literal["approved", "rejected"] | None = None


class AlertsResponse(BaseModel):
    alerts: list[Alert]
    total: int
    limit: int
    offset: int


class TransactionDetails(BaseModel):
    txn_id: str
    account_id: str
    customer_id: str | None = None
    amount: float
    currency: str
    timestamp: datetime
    counterparty_country: str
    risk_score: float | None = None
    risk_category: str | None = None
    scorer_version: str | None = None
    features: dict[str, float]
    alerts: list[Alert]


class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded"]
    timestamp: datetime
    dependencies: dict[str, str] | None = None


class Principal(BaseModel):
    subject: str
    roles: set[str]


def _read_secret(name: str) -> str | None:
    file_name = os.getenv(f"{name}_FILE")
    if file_name:
        return Path(file_name).read_text(encoding="utf-8").strip()
    value = os.getenv(name)
    return value.strip() if value else None


async def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> Principal:
    if os.getenv("AUTH_DISABLED", "false").lower() == "true":
        return Principal(subject="local-development", roles={"admin", "analyst", "ingestor"})
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    secret = _read_secret("JWT_SECRET_KEY")
    if not secret or len(secret) < 32 or secret == "your_jwt_secret_key_here":
        raise HTTPException(status_code=503, detail="authentication is not configured")
    algorithm = os.getenv("JWT_ALGORITHM", "HS256")
    if algorithm not in {"HS256", "HS384", "HS512"}:
        raise HTTPException(status_code=503, detail="unsupported JWT algorithm configuration")

    audience = os.getenv("JWT_AUDIENCE")
    issuer = os.getenv("JWT_ISSUER")
    try:
        claims = jwt.decode(
            credentials.credentials,
            secret,
            algorithms=[algorithm],
            audience=audience,
            issuer=issuer,
            options={"verify_aud": bool(audience), "require_sub": True, "require_exp": True},
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=401,
            detail="invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    raw_roles = claims.get("roles", claims.get("role", []))
    roles = {raw_roles} if isinstance(raw_roles, str) else set(raw_roles or [])
    return Principal(subject=str(claims["sub"]), roles=roles)


def require_roles(*allowed: str) -> Callable[..., Any]:
    async def dependency(principal: Principal = Depends(verify_token)) -> Principal:
        if "admin" not in principal.roles and principal.roles.isdisjoint(allowed):
            raise HTTPException(status_code=403, detail="insufficient role")
        return principal

    return dependency


def _client() -> httpx.AsyncClient:
    if not http_client:
        raise HTTPException(status_code=503, detail="gateway is not ready")
    return http_client


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    supplied = request.headers.get("x-request-id", "")
    request_id = supplied if 0 < len(supplied) <= 128 else str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


async def _read_upload(upload: UploadFile, max_bytes: int) -> bytes:
    content_type = (upload.content_type or "").partition(";")[0].lower()
    if content_type not in {"application/json", "text/json"}:
        raise HTTPException(status_code=415, detail="all batch files must use application/json")
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="batch file exceeds configured size limit")
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/v1/batch", response_model=BatchResponse, status_code=status.HTTP_201_CREATED)
async def upload_batch(
    accounts: UploadFile = File(...),
    customers: UploadFile = File(...),
    transactions: UploadFile = File(...),
    principal: Principal = Depends(require_roles("ingestor")),
) -> BatchResponse:
    max_bytes = int(os.getenv("MAX_UPLOAD_FILE_BYTES", str(5 * 1024 * 1024)))
    files = {
        "accounts": (
            accounts.filename or "accounts.json",
            await _read_upload(accounts, max_bytes),
            "application/json",
        ),
        "customers": (
            customers.filename or "customers.json",
            await _read_upload(customers, max_bytes),
            "application/json",
        ),
        "transactions": (
            transactions.filename or "transactions.json",
            await _read_upload(transactions, max_bytes),
            "application/json",
        ),
    }
    try:
        response = await _client().post(f"{INGESTION_SERVICE_URL}/batch", files=files)
    except httpx.RequestError as exc:
        logger.warning("Ingestion upstream unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="ingestion service unavailable") from exc
    if response.status_code != 201:
        detail = response.json().get("detail", "batch validation failed")
        raise HTTPException(status_code=response.status_code, detail=detail)
    logger.info("User %s published batch %s", principal.subject, response.json().get("batch_id"))
    return BatchResponse(**response.json())


@app.get("/v1/alerts", response_model=AlertsResponse)
async def get_alerts(
    status: str | None = Query(None, pattern=r"^(open|investigating|closed|false_positive)$"),
    risk_threshold: float | None = Query(None, ge=0, le=1),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(require_roles("analyst", "compliance_officer")),
) -> AlertsResponse:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if status is not None:
        params["status"] = status
    if risk_threshold is not None:
        params["risk_threshold"] = risk_threshold
    try:
        response = await _client().get(f"{ALERT_MANAGER_URL}/alerts", params=params)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="alert service unavailable") from exc
    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail="could not retrieve alerts")
    logger.info("User %s retrieved alerts", principal.subject)
    return AlertsResponse(**response.json())


@app.get("/v1/alerts/{alert_id}", response_model=AlertDetail)
async def get_alert(
    alert_id: str = ApiPath(min_length=1, max_length=128),
    principal: Principal = Depends(require_roles("analyst", "compliance_officer")),
) -> AlertDetail:
    try:
        response = await _client().get(f"{ALERT_MANAGER_URL}/alerts/{alert_id}")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="alert service unavailable") from exc
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="alert not found")
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail="could not retrieve alert")
    logger.info("User %s retrieved alert %s", principal.subject, alert_id)
    return AlertDetail(**response.json())


@app.get("/v1/alerts/{alert_id}/audit")
async def get_alert_audit(
    alert_id: str = ApiPath(min_length=1, max_length=128),
    principal: Principal = Depends(require_roles("compliance_officer")),
) -> list[dict[str, Any]]:
    try:
        response = await _client().get(f"{ALERT_MANAGER_URL}/alerts/{alert_id}/audit")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="alert service unavailable") from exc
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="alert not found")
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail="could not retrieve alert audit")
    logger.info("User %s retrieved audit for alert %s", principal.subject, alert_id)
    return response.json()


@app.patch("/v1/alerts/{alert_id}", response_model=AlertDetail)
async def update_alert(
    update: AlertUpdate,
    alert_id: str = ApiPath(min_length=1, max_length=128),
    principal: Principal = Depends(require_roles("compliance_officer")),
) -> AlertDetail:
    payload = update.model_dump(exclude_unset=True)
    payload["actor"] = principal.subject
    try:
        response = await _client().patch(f"{ALERT_MANAGER_URL}/alerts/{alert_id}", json=payload)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="alert service unavailable") from exc
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="alert not found")
    if response.status_code == 409:
        detail = response.json().get("detail", "invalid state change")
        raise HTTPException(status_code=409, detail=detail)
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail="could not update alert")
    logger.info("User %s updated alert %s", principal.subject, alert_id)
    return AlertDetail(**response.json())


@app.get("/v1/transactions/{txn_id}", response_model=TransactionDetails)
async def get_transaction(
    txn_id: str = ApiPath(min_length=1, max_length=128),
    principal: Principal = Depends(require_roles("analyst", "compliance_officer")),
) -> TransactionDetails:
    client = _client()
    try:
        transaction_response, score_response, alerts_response = await asyncio.gather(
            client.get(f"{FEATURE_ENGINE_URL}/transactions/{txn_id}"),
            client.get(f"{RISK_SCORER_URL}/scores/{txn_id}"),
            client.get(f"{ALERT_MANAGER_URL}/alerts", params={"limit": 1000, "offset": 0}),
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="transaction dependencies unavailable") from exc
    if transaction_response.status_code == 404:
        raise HTTPException(status_code=404, detail="transaction not found")
    if transaction_response.status_code != 200:
        raise HTTPException(status_code=503, detail="feature service unavailable")

    transaction = transaction_response.json()
    score = score_response.json() if score_response.status_code == 200 else {}
    all_alerts = (
        alerts_response.json().get("alerts", []) if alerts_response.status_code == 200 else []
    )
    transaction_alerts = [item for item in all_alerts if item.get("txn_id") == txn_id]
    logger.info("User %s retrieved transaction %s", principal.subject, txn_id)
    return TransactionDetails(
        **transaction,
        risk_score=score.get("risk_score"),
        risk_category=score.get("risk_category"),
        scorer_version=score.get("scorer_version"),
        alerts=[Alert(**item) for item in transaction_alerts],
    )


@app.get("/health/live", response_model=HealthResponse)
async def liveness() -> HealthResponse:
    return HealthResponse(status="healthy", timestamp=datetime.now(timezone.utc))


@app.get("/health/ready", response_model=HealthResponse)
@app.get("/health", response_model=HealthResponse)
async def readiness() -> HealthResponse:
    client = _client()
    dependencies = {
        "ingestion": f"{INGESTION_SERVICE_URL}/health/ready",
        "feature_engine": f"{FEATURE_ENGINE_URL}/health/ready",
        "risk_scorer": f"{RISK_SCORER_URL}/health/ready",
        "alert_manager": f"{ALERT_MANAGER_URL}/health/ready",
    }

    async def check(url: str) -> str:
        try:
            response = await client.get(url, timeout=3.0)
            return "ready" if response.status_code == 200 else "unavailable"
        except httpx.RequestError:
            return "unavailable"

    states = dict(
        zip(dependencies, await asyncio.gather(*(check(url) for url in dependencies.values())))
    )
    if any(value != "ready" for value in states.values()):
        raise HTTPException(status_code=503, detail={"dependencies": states})
    return HealthResponse(
        status="healthy", timestamp=datetime.now(timezone.utc), dependencies=states
    )


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": "AML Gateway API",
        "version": "2.0.0",
        "authentication": "JWT bearer token required; role-based authorization enabled",
        "docs": "/docs",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
