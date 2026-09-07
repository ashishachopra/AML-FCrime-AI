import json

import pytest
from conftest import load_source_module
from pydantic import ValidationError

models = load_source_module("ingestion_models_test", "services/ingestion/models.py")
processor_module = load_source_module(
    "ingestion_processor_test", "services/ingestion/data_processor.py"
)


def test_transaction_requires_timezone_and_normalizes_codes() -> None:
    transaction = models.Transaction.model_validate(
        {
            "txn_id": "T-1",
            "account_id": "A-1",
            "timestamp": "2026-08-25T10:00:00+03:00",
            "amount": "123.4500",
            "currency": "usd",
            "counterparty_country": "sa",
        }
    )
    assert transaction.currency == "USD"
    assert transaction.counterparty_country == "SA"
    assert transaction.timestamp.isoformat() == "2026-08-25T07:00:00+00:00"

    with pytest.raises(ValidationError, match="UTC offset"):
        models.Transaction.model_validate(
            {
                "txn_id": "T-2",
                "account_id": "A-1",
                "timestamp": "2026-08-25T10:00:00",
                "amount": "10.00",
                "currency": "USD",
                "counterparty_country": "SA",
            }
        )


@pytest.mark.asyncio
async def test_batch_processor_rejects_duplicates_and_orphans() -> None:
    processor = processor_module.DataProcessor(max_batch_records=10)
    customers = [{"customer_id": "C1"}]
    accounts = [
        {"account_id": "A1", "customer_id": "C1"},
        {"account_id": "A1", "customer_id": "C1"},
    ]
    transactions = [{"txn_id": "T1", "account_id": "missing"}]
    with pytest.raises(ValueError, match="duplicate account_id"):
        await processor.process_batch_files(
            json.dumps(accounts).encode(),
            json.dumps(customers).encode(),
            json.dumps(transactions).encode(),
        )

    accounts = [{"account_id": "A1", "customer_id": "C1"}]
    with pytest.raises(ValueError, match="unknown accounts"):
        await processor.process_batch_files(
            json.dumps(accounts).encode(),
            json.dumps(customers).encode(),
            json.dumps(transactions).encode(),
        )


def test_ingestion_accepts_network_evidence_and_requires_paired_conversion():
    value = {
        "txn_id": "T1",
        "account_id": "A1",
        "timestamp": "2026-09-07T12:00:00Z",
        "amount": "3750",
        "currency": "SAR",
        "counterparty_country": "US",
        "direction": "outbound",
        "counterparty_account_id": "B1",
        "base_currency": "USD",
        "base_currency_amount": "1000",
    }
    parsed = models.Transaction.model_validate(value)
    assert parsed.counterparty_account_id == "B1"
    assert str(parsed.base_currency_amount) == "1000"
    del value["base_currency_amount"]
    with pytest.raises(ValidationError, match="supplied together"):
        models.Transaction.model_validate(value)
