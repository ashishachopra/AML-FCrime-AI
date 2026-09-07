import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import load_source_module
from test_feature_store import features, repository, service, transaction

scorer = load_source_module("scorer", "services/risk-scorer/scorer.py")
load_source_module("events", "services/risk-scorer/events.py")
risk_service = load_source_module("hybrid_risk_service_test", "services/risk-scorer/main.py")
gateway = load_source_module("hybrid_gateway_test", "services/gateway/main.py")
alerts = load_source_module("hybrid_alerts_test", "services/alert-manager/alerts.py")

# Import the shared fixture explicitly for pytest discovery.
__all__ = ["repository"]


@pytest.mark.asyncio
async def test_gateway_feature_score_preview_integration(repository, monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setattr(service, "feature_store", repository)
    monkeypatch.setattr(service, "work_slots", asyncio.Semaphore(4))
    monkeypatch.setattr(risk_service, "risk_scorer", scorer.RiskScorer())
    monkeypatch.setattr(risk_service, "scored_transactions", {})
    applications = {"feature-engine": service.app, "risk-scorer": risk_service.app}

    async def route(request):
        transport = httpx.ASGITransport(app=applications[request.url.host])
        return await transport.handle_async_request(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as upstream:
        monkeypatch.setattr(gateway, "http_client", upstream)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway"
        ) as client:
            response = await client.post("/v1/evaluate", json={"transaction": transaction()})
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["mode"] == "preview"
            assert result["feature_version"] == features.FeatureEngine.VERSION
            assert result["scorer_version"] == scorer.RiskScorer.VERSION
            assert "behavior_baseline_warming_up" in result["triggered_rules"]
            assert "feature_computed_at" in result
            assert risk_service.scored_transactions == {}
            assert repository.statistics()["transactions"] == 0
            invalid = await client.post(
                "/v1/evaluate", json={"transaction": transaction(amount="nan")}
            )
            assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_preview_timeout_never_returns_a_risk_score(monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("EVALUATION_TIMEOUT_SECONDS", "0.01")

    async def slow(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as upstream:
        monkeypatch.setattr(gateway, "http_client", upstream)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway"
        ) as client:
            response = await client.post("/v1/evaluate", json={"transaction": transaction()})
    assert response.status_code == 504
    assert "risk_score" not in response.json()


@pytest.mark.asyncio
async def test_preview_requires_authentication_and_role(monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "false")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway"
    ) as client:
        response = await client.post("/v1/evaluate", json={"transaction": transaction()})
        assert response.status_code == 401
        gateway.app.dependency_overrides[gateway.verify_token] = lambda: gateway.Principal(
            subject="reader", roles={"viewer"}
        )
        try:
            response = await client.post("/v1/evaluate", json={"transaction": transaction()})
            assert response.status_code == 403
        finally:
            gateway.app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_hybrid_network_signals_reach_alert_evidence(repository, monkeypatch, tmp_path):
    monkeypatch.setenv("SAR_GENERATION_ENABLED", "false")
    monkeypatch.setenv("ALERT_DB_PATH", str(tmp_path / "alerts.db"))
    for index in range(3):
        await service_event(
            repository,
            transaction(
                f"IN{index}",
                minutes=-index - 1,
                account_id=f"SOURCE{index}",
                amount="1000",
                direction="outbound",
                counterparty_account_id="A1",
            ),
        )
    snapshot = repository.evaluate(
        transaction("OUT", amount="2900", direction="outbound", counterparty_account_id="B1"),
        persist=True,
    )
    monkeypatch.setattr(risk_service, "risk_scorer", scorer.RiskScorer())
    monkeypatch.setattr(risk_service, "exchange", object())
    publisher = AsyncMock()
    monkeypatch.setattr(risk_service, "publish_event", publisher)
    await risk_service.process_features_ready_event({"type": "FeaturesReady", "data": snapshot})
    scored = publisher.call_args.args[2]
    manager = alerts.AlertManager()
    alert = await manager.process_scored_transaction(scored)
    assert alert["alert_type"] == "network_flow_review"
    assert "rapid_fan_in_pass_through" in alert["evidence"]["triggered_rules"]
    assert alert["evidence"]["feature_version"] == features.FeatureEngine.VERSION
    assert alert["sar_review_status"] == "draft_pending_review"
    assert alert["sar_generated_by"] == "template"
    assert (await manager.process_scored_transaction(scored))["alert_id"] == alert["alert_id"]


async def service_event(repository, data):
    # Mirrors the store operation used by the asynchronous event handler.
    return repository.evaluate(data, persist=True)


@pytest.mark.asyncio
async def test_incomplete_history_routes_to_review_without_inflating_score(monkeypatch, tmp_path):
    monkeypatch.setenv("SAR_GENERATION_ENABLED", "false")
    monkeypatch.setenv("ALERT_DB_PATH", str(tmp_path / "alerts.db"))
    result = await scorer.RiskScorer().score_transaction(
        "gap", {"country_risk": 0, "history_truncated": 1, "kyc_data_available": 1}
    )
    assert result["risk_score"] == 0
    assert result["review_recommended"] is True
    alert = await alerts.AlertManager().process_scored_transaction(result)
    assert alert["alert_type"] == "data_quality_review"
    assert alert["sar_narrative"] is None


@pytest.mark.asyncio
async def test_scoring_is_independent_of_feature_key_order():
    value = {name: (index % 3) / 3 for index, name in enumerate(scorer.RiskScorer.FEATURE_WEIGHTS)}
    first = await scorer.RiskScorer().score_transaction("T1", value)
    second = await scorer.RiskScorer().score_transaction("T1", dict(reversed(list(value.items()))))
    first.pop("scored_at")
    second.pop("scored_at")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


@pytest.mark.asyncio
async def test_unusual_amount_combines_with_rapid_fan_out(repository):
    for index in range(20):
        repository.evaluate(
            transaction(
                f"T{index}",
                minutes=-index - 1,
                direction="outbound",
                counterparty_account_id=f"B{index}",
            ),
            persist=True,
        )
    snapshot = repository.evaluate(
        transaction("outlier", amount="5000", direction="outbound", counterparty_account_id="NEW")
    )
    values = snapshot["features"]
    assert values["network_fan_out_1h"] == 21
    assert values["behavior_baseline_ready"] == 1
    result = await scorer.RiskScorer().score_transaction("outlier", values)
    assert "unusual_amount_with_rapid_fan_out" in result["triggered_rules"]
    assert result["review_recommended"] is True
    assert result["risk_score"] >= 0.8
