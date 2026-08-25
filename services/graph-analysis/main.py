import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from graph import GraphAnalyzer
from pydantic import BaseModel, ConfigDict, Field

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
graph_analyzer: GraphAnalyzer | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global graph_analyzer
    graph_analyzer = GraphAnalyzer()
    yield


app = FastAPI(
    title="AML Graph Analysis API",
    version="2.0.0",
    description="Deterministic entity-network risk indicators",
    lifespan=lifespan,
)


class GraphTransaction(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    txn_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(min_length=1, max_length=128)
    customer_id: str | None = Field(default=None, max_length=128)
    counterparty_id: str = Field(min_length=1, max_length=128)
    timestamp: datetime
    amount: float = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    counterparty_country: str = Field(pattern=r"^[A-Z]{2}$")
    customer_kyc_level: str | None = None
    customer_pep_flag: bool | None = None


class ConnectedParty(BaseModel):
    party_id: str
    relationship_strength: float
    risk_contribution: float


class GraphAlert(BaseModel):
    alert_type: str
    severity: str
    description: str
    confidence: float


class GraphRiskResponse(BaseModel):
    party_id: str
    cluster_id: str
    centrality_score: float
    community_risk: float
    connected_parties: list[ConnectedParty]
    graph_alerts: list[GraphAlert]
    analyzed_at: datetime


def _analyzer() -> GraphAnalyzer:
    if not graph_analyzer:
        raise HTTPException(status_code=503, detail="service is not ready")
    return graph_analyzer


@app.post("/graph/transactions", status_code=201)
async def add_transaction(transaction: GraphTransaction) -> dict[str, Any]:
    data = transaction.model_dump(mode="json", exclude_none=True)
    customer = None
    if transaction.customer_id:
        customer = {
            "customer_id": transaction.customer_id,
            "kyc_level": transaction.customer_kyc_level,
            "pep_flag": transaction.customer_pep_flag,
        }
    try:
        return _analyzer().add_transaction_to_graph(data, customer)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/graph/risk/{party_id}", response_model=GraphRiskResponse)
async def get_graph_risk(party_id: str) -> GraphRiskResponse:
    result = await _analyzer().analyze_party_risk(party_id)
    if not result:
        raise HTTPException(status_code=404, detail="party not found in graph")
    return GraphRiskResponse(**result)


@app.get("/graph/statistics")
async def graph_statistics() -> dict[str, Any]:
    return _analyzer().get_graph_statistics()


@app.get("/metadata")
async def metadata() -> dict[str, Any]:
    return _analyzer().metadata()


@app.get("/health")
async def health() -> dict[str, Any]:
    if not graph_analyzer:
        raise HTTPException(status_code=503, detail="service is not ready")
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8004)
