import asyncio
import logging
import math
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Callable, Literal
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi import Path as ApiPath
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, ConfigDict, Field
from resource_limits import PrincipalLimiter, ResourceGuard

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
principal_limiter = PrincipalLimiter(
    per_minute=int(os.getenv("GATEWAY_REQUESTS_PER_MINUTE", "120")),
    burst=int(os.getenv("GATEWAY_REQUEST_BURST", "30")),
    max_principals=int(os.getenv("GATEWAY_MAX_PRINCIPALS", "4096")),
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global http_client
    evaluation_deadline()
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
    version="3.1.0",
    description="Authenticated external gateway for the AML reference pipeline",
    lifespan=lifespan,
)
security = HTTPBearer(auto_error=False)


class BatchResponse(BaseModel):
    message: str
    batch_id: str
    records_processed: int


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction: dict[
        Annotated[str, Field(max_length=32)], Annotated[str, Field(max_length=128)]
    ] = Field(min_length=6, max_length=10)


class EvaluationResponse(BaseModel):
    mode: Literal["preview"] = "preview"
    txn_id: str
    features: dict[str, float]
    feature_version: str
    feature_computed_at: datetime
    risk_score: float
    risk_category: str
    data_quality_score: float
    review_recommended: bool
    triggered_rules: list[str]
    feature_contributions: dict[str, float]
    scorer_version: str
    decision_basis: str


def evaluation_deadline() -> float:
    value = float(os.getenv("EVALUATION_TIMEOUT_SECONDS", "1.0"))
    if not math.isfinite(value) or not 0 < value <= 30:
        raise ValueError("EVALUATION_TIMEOUT_SECONDS must be in (0, 30]")
    return value


class Alert(BaseModel):
    revision: int = 1
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
    sar_generation_reason: str | None = None
    sar_reviewed_by: str | None = None
    sar_reviewed_at: datetime | None = None
    investigation_notes: str | None = None
    assigned_to: str | None = None


class AlertUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)

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
    subject: str = Field(min_length=1, max_length=200)
    roles: set[str]
    principal_type: Literal["human", "service", "agent"] = "service"
    amr: set[str] = Field(default_factory=set)
    auth_time: float | None = None


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
        return Principal(
            subject="local-development",
            roles={"admin", "analyst", "ingestor"},
            principal_type="human",
            amr={"mfa"},
            auth_time=datetime.now(timezone.utc).timestamp(),
        )
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if len(credentials.credentials) > 8192:
        raise HTTPException(status_code=401, detail="access token exceeds size limit")

    secret = _read_secret("JWT_SECRET_KEY")
    if not secret or len(secret) < 32 or secret == "your_jwt_secret_key_here":
        raise HTTPException(status_code=503, detail="authentication is not configured")
    algorithm = os.getenv("JWT_ALGORITHM", "HS256")
    if algorithm not in {"HS256", "HS384", "HS512"}:
        raise HTTPException(status_code=503, detail="unsupported JWT algorithm configuration")

    audience = os.getenv("JWT_AUDIENCE")
    issuer = os.getenv("JWT_ISSUER")
    if not audience or not issuer:
        raise HTTPException(status_code=503, detail="JWT issuer and audience must be configured")
    try:
        claims = jwt.decode(
            credentials.credentials,
            secret,
            algorithms=[algorithm],
            audience=audience,
            issuer=issuer,
            options={
                "require_aud": True,
                "require_iss": True,
                "require_sub": True,
                "require_exp": True,
            },
        )
    except (JWTError, TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(
            status_code=401,
            detail="invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    raw_roles = claims.get("roles", claims.get("role", []))
    roles = [raw_roles] if isinstance(raw_roles, str) else raw_roles
    amr = claims.get("amr", [])
    kind = claims.get("principal_type", "service")
    auth_time = claims.get("auth_time")
    now = datetime.now(timezone.utc).timestamp()
    if any(
        type(claims[key]) not in {int, float} or not math.isfinite(claims[key])
        for key in ("exp", "iat", "nbf")
        if key in claims
    ):
        raise HTTPException(status_code=401, detail="invalid token time claims")
    valid_lists = all(
        isinstance(value, list)
        and len(value) <= 20
        and all(isinstance(item, str) and 0 < len(item) <= 64 for item in value)
        for value in (roles, amr)
    )
    if not (
        valid_lists
        and isinstance(kind, str)
        and kind in {"human", "service", "agent"}
        and isinstance(claims["sub"], str)
        and 0 < len(claims["sub"]) <= 200
        and (
            auth_time is None
            or type(auth_time) in {int, float}
            and math.isfinite(auth_time)
            and auth_time <= now + 30
        )
    ):
        raise HTTPException(status_code=401, detail="invalid identity claims")
    if kind == "agent":
        issued, expires = claims.get("iat"), claims.get("exp")
        if not (
            type(issued) in {int, float}
            and type(expires) in {int, float}
            and math.isfinite(issued)
            and math.isfinite(expires)
            and issued <= now + 30
            and 0 < expires - issued <= 900
        ):
            raise HTTPException(
                status_code=401, detail="agent tokens require iat and lifetime at most 15 minutes"
            )
    return Principal(
        subject=claims["sub"],
        roles=set(roles),
        principal_type=kind,
        amr=set(amr),
        auth_time=auth_time,
    )


def require_roles(*allowed: str) -> Callable[..., Any]:
    async def dependency(principal: Principal = Depends(verify_token)) -> Principal:
        if "admin" not in principal.roles and principal.roles.isdisjoint(allowed):
            raise HTTPException(status_code=403, detail="insufficient role")
        retry_after = principal_limiter.admit(principal.subject)
        if retry_after:
            raise HTTPException(
                status_code=429,
                detail="principal request budget exhausted",
                headers={"Retry-After": str(retry_after)},
            )
        return principal

    return dependency


def require_human_review(principal: Principal) -> None:
    now = datetime.now(timezone.utc).timestamp()
    if not (
        principal.principal_type == "human"
        and "mfa" in principal.amr
        and principal.auth_time is not None
        and 0 <= now - principal.auth_time <= 300
    ):
        raise HTTPException(
            status_code=403,
            detail="final review requires a human identity with MFA from the last five minutes",
        )


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


# Outer ASGI boundary counts streamed bytes before FastAPI parses JSON/multipart.
app.add_middleware(ResourceGuard)


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


@app.post("/v1/evaluate", response_model=EvaluationResponse)
async def evaluate_transaction(
    request: EvaluationRequest,
    principal: Principal = Depends(require_roles("ingestor", "analyst", "compliance_officer")),
) -> EvaluationResponse:
    """Score a preview against observed history under a single total deadline."""
    try:
        async with asyncio.timeout(evaluation_deadline()):
            feature_response = await _client().post(
                f"{FEATURE_ENGINE_URL}/compute", json=request.transaction
            )
            if feature_response.status_code != 200:
                status_code = feature_response.status_code
                raise HTTPException(
                    status_code=status_code if status_code in {409, 422, 503} else 503,
                    detail="feature evaluation unavailable or invalid transaction evidence",
                )
            evidence = feature_response.json()
            score_response = await _client().post(
                f"{RISK_SCORER_URL}/evaluate",
                json={
                    "txn_id": evidence["txn_id"],
                    "features": evidence["features"],
                    "feature_version": evidence["feature_version"],
                },
            )
            if score_response.status_code != 200:
                raise HTTPException(status_code=503, detail="risk evaluation unavailable")
            score = score_response.json()
            return EvaluationResponse(
                **{
                    key: value
                    for key, value in score.items()
                    if key in EvaluationResponse.model_fields
                    and key not in {"feature_version", "feature_computed_at"}
                },
                features=evidence["features"],
                feature_version=evidence["feature_version"],
                feature_computed_at=evidence["computed_at"],
            )
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise HTTPException(
            status_code=504, detail="evaluation deadline exceeded; no decision available"
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="evaluation service unavailable") from exc


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


@app.get("/v1/alerts/statistics")
async def alert_statistics(
    principal: Principal = Depends(require_roles("compliance_officer")),
) -> dict[str, Any]:
    try:
        response = await _client().get(f"{ALERT_MANAGER_URL}/alerts/statistics")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="alert service unavailable") from exc
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail="could not retrieve alert statistics")
    return response.json()


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
    # Protect every status transition so automation cannot undo a human closure
    # by reopening the case. Notes and assignment remain available to assistants.
    if update.sar_review_status is not None or update.status is not None:
        require_human_review(principal)
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
            client.get(
                f"{ALERT_MANAGER_URL}/alerts", params={"txn_id": txn_id, "limit": 1, "offset": 0}
            ),
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
        "version": app.version,
        "authentication": "JWT bearer token required; role-based authorization enabled",
        "docs": "/docs",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
