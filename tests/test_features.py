import pytest
from conftest import load_source_module

features_module = load_source_module("feature_engine_test", "services/feature-engine/features.py")


@pytest.mark.asyncio
async def test_velocity_excludes_future_transactions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASE_CURRENCY", "USD")
    engine = features_module.FeatureEngine()
    current = {
        "txn_id": "T2",
        "account_id": "A1",
        "timestamp": "2026-08-10T12:00:00Z",
        "amount": "1000",
        "currency": "USD",
        "counterparty_country": "US",
    }
    transaction_store = {
        "T1": {**current, "txn_id": "T1", "timestamp": "2026-08-09T12:00:00Z"},
        "T2": current,
        "T3": {**current, "txn_id": "T3", "timestamp": "2026-08-11T12:00:00Z"},
    }
    values = await engine.compute_features(current, transaction_store, {}, {})
    assert values["count_30d"] == 1.0


@pytest.mark.asyncio
async def test_currency_and_fatf_features_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASE_CURRENCY", "USD")
    engine = features_module.FeatureEngine()
    transaction = {
        "txn_id": "T1",
        "account_id": "A1",
        "timestamp": "2026-08-10T12:00:00Z",
        "amount": "9900",
        "currency": "SAR",
        "counterparty_country": "IR",
    }
    values = await engine.compute_features(transaction, {"T1": transaction}, {}, {})
    assert values["base_currency_conversion_available"] == 0.0
    assert values["amount_near_reporting_threshold"] == 0.0
    assert values["fatf_call_for_action"] == 1.0
    assert "sanctions_country" not in values
    assert engine.metadata()["jurisdiction_data_as_of"] == "2026-06-19"
