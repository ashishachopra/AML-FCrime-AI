import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import load_source_module

features = load_source_module("features", "services/feature-engine/features.py")
evidence = load_source_module("evidence", "services/feature-engine/evidence.py")
store = load_source_module("store", "services/feature-engine/store.py")
events = load_source_module("events", "services/feature-engine/events.py")
service = load_source_module("feature_service_test", "services/feature-engine/main.py")

NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def transaction(identifier="T1", *, minutes=0, **overrides):
    return {
        "txn_id": identifier,
        "account_id": "A1",
        "amount": "100",
        "currency": "USD",
        "counterparty_country": "US",
        "timestamp": (NOW + timedelta(minutes=minutes)).isoformat(),
        **overrides,
    }


@pytest.fixture
def repository(tmp_path):
    value = store.FeatureStore(str(tmp_path / "features.db"), features.FeatureEngine())
    value.put_entity(
        "IngestedCustomer", {"customer_id": "C1", "kyc_level": "enhanced", "pep_flag": False}
    )
    value.put_entity(
        "IngestedAccount",
        {
            "account_id": "A1",
            "customer_id": "C1",
            "opened_at": "2020-01-01T00:00:00Z",
        },
    )
    yield value
    value.close()


def test_restart_replay_and_immutable_snapshots(repository, tmp_path):
    first = repository.evaluate(transaction(), persist=True, batch_id="B1")
    event_id = repository.pending()[0]["event"]["id"]
    second_connection = store.FeatureStore(str(tmp_path / "features.db"), features.FeatureEngine())
    try:
        assert second_connection.get("T1") == first
        assert second_connection.evaluate(transaction(amount="100.0000"), persist=True) == first
        assert second_connection.pending()[0]["event"]["id"] == event_id
        second_connection.evaluate(transaction("earlier", minutes=-5, amount="9900"), persist=True)
        assert second_connection.get("T1") == first
        assert second_connection.statistics()["transactions"] == 2
        assert first["features"]["kyc_data_available"] == 1
    finally:
        second_connection.close()


def test_conflicting_replay_cannot_overwrite_evidence(repository):
    first = repository.evaluate(transaction(), persist=True)
    with pytest.raises(store.ReplayConflict):
        repository.evaluate(transaction(amount="200"), persist=True)
    assert repository.get("T1") == first
    assert len(repository.pending()) == 1


def test_preview_has_no_writes_or_outbox(repository):
    result = repository.evaluate(transaction())
    assert result["features"]["count_30d"] == 0
    assert repository.get("T1") is None
    assert repository.pending() == []


def test_compute_and_outbox_rollback_together(repository, monkeypatch):
    original = repository._save

    def failing_save(*args):
        original(*args)
        raise RuntimeError("simulated crash before commit")

    monkeypatch.setattr(repository, "_save", failing_save)
    with pytest.raises(RuntimeError):
        repository.evaluate(transaction(), persist=True)
    assert repository.get("T1") is None
    assert repository.pending() == []


def test_outbox_capacity_rejects_without_losing_data(repository):
    repository.max_outbox = 1
    repository.evaluate(transaction(), persist=True)
    with pytest.raises(store.StoreBusy):
        repository.evaluate(transaction("T2"), persist=True)
    assert repository.get("T2") is None
    repository.mark_published(repository.pending()[0]["sequence"])
    repository.evaluate(transaction("T2"), persist=True)
    assert repository.get("T2") is not None
    # An already delivered replay must not create another outbox entry.
    repository.evaluate(transaction(), persist=True)
    assert len(repository.pending()) == 1


def test_concurrent_replay_produces_one_snapshot_and_event(repository):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: repository.evaluate(transaction(), persist=True), range(24))
        )
    assert all(result == results[0] for result in results)
    assert repository.statistics()["transactions"] == 1
    assert len(repository.pending()) == 1


def test_indexed_windows_exclude_future_same_time_and_other_accounts(repository):
    for item in (
        transaction("past", minutes=-1),
        transaction("equal"),
        transaction("future", minutes=1),
        transaction("other", minutes=-1, account_id="A2"),
        transaction("old", minutes=-31 * 1440),
    ):
        repository.evaluate(item, persist=True)
    result = repository.evaluate(transaction("current"))["features"]
    assert result["count_30d"] == 1
    assert result["late_event"] == 1
    plan = repository.db.execute(
        "EXPLAIN QUERY PLAN SELECT payload FROM transactions WHERE account_id=? "
        "AND event_time>=? AND event_time<? ORDER BY event_time DESC, txn_id LIMIT ?",
        ("A1", 0, store.event_time(transaction()["timestamp"]), 1001),
    ).fetchall()
    assert any("idx_account_time" in row[3] and "SEARCH" in row[3] for row in plan)
    assert not any("TEMP B-TREE" in row[3] for row in plan)


def test_money_aggregation_and_structuring_remain_currency_safe(repository):
    repository.evaluate(transaction("USD", minutes=-1, amount="100"), persist=True)
    repository.evaluate(
        transaction("JPY", minutes=-2, amount="1000000", currency="JPY"), persist=True
    )
    values = repository.evaluate(transaction("now"))["features"]
    assert values["count_30d"] == 2
    assert values["amt_30d"] == 100
    assert values["avg_amt_30d"] == 100
    assert values["amount_deviation"] == 0
    converted = repository.evaluate(
        transaction(
            "FX",
            currency="SAR",
            amount="37500",
            base_currency="USD",
            base_currency_amount="9999.9999",
        )
    )["features"]
    assert converted["amount_near_reporting_threshold"] == 1
    assert converted["reporting_threshold_exceeded"] == 0


def test_history_caps_are_explicit(repository):
    repository.max_history = 2
    for index in range(3):
        repository.evaluate(transaction(f"T{index}", minutes=-index - 1), persist=True)
    result = repository.evaluate(transaction("now"))["features"]
    assert result["count_30d"] == 2
    assert result["history_truncated"] == 1


@pytest.mark.parametrize("amount, expected", [("101", 0), ("10000", 1)])
def test_robust_baseline_handles_constant_history(repository, amount, expected):
    for index in range(20):
        repository.evaluate(transaction(f"T{index}", minutes=-index - 1), persist=True)
    values = repository.evaluate(transaction("now", amount=amount))["features"]
    assert values["behavior_baseline_ready"] == 1
    assert values["behavior_anomaly_score"] == expected


def test_baseline_requires_currency_direction_and_warmup(repository):
    for index in range(20):
        repository.evaluate(
            transaction(f"T{index}", minutes=-index - 1, currency="SAR", direction="inbound"),
            persist=True,
        )
    values = repository.evaluate(transaction("now", amount="10000", direction="outbound"))[
        "features"
    ]
    assert values["behavior_baseline_ready"] == 0
    assert values["behavior_anomaly_score"] == 0


def seed_fan_in(repository, currency="USD"):
    for index in range(3):
        repository.evaluate(
            transaction(
                f"IN{index}",
                minutes=-index - 1,
                account_id=f"SOURCE{index}",
                amount="1000",
                direction="outbound",
                counterparty_account_id="A1",
                currency=currency,
            ),
            persist=True,
        )


def test_rapid_pass_through_requires_compatible_currency_and_current_amount(repository):
    seed_fan_in(repository)
    current = transaction("out", amount="2900", direction="outbound", counterparty_account_id="B1")
    values = repository.evaluate(current)["features"]
    assert values["network_fan_in_1h"] == 3
    assert values["rapid_pass_through_score"] == 1
    assert (
        repository.evaluate(dict(current, currency="EUR"))["features"]["rapid_pass_through_score"]
        == 0
    )
    assert (
        repository.evaluate(dict(current, amount="1"))["features"]["rapid_pass_through_score"] == 0
    )


def test_reciprocal_flow_and_network_caps(repository):
    repository.evaluate(
        transaction(
            "return",
            minutes=-1,
            account_id="B1",
            direction="outbound",
            counterparty_account_id="A1",
        ),
        persist=True,
    )
    current = transaction("out", direction="outbound", counterparty_account_id="B1")
    assert repository.evaluate(current)["features"]["reciprocal_flow_score"] == 1
    repository.max_network = 1
    seed_fan_in(repository)
    assert repository.evaluate(current)["features"]["network_history_truncated"] == 1


def test_network_ignores_inbound_mirrors_self_transfers_and_old_edges(repository):
    for index in range(3):
        repository.evaluate(
            transaction(
                f"mirror{index}",
                minutes=-index - 1,
                direction="inbound",
                counterparty_account_id=f"X{index}",
            ),
            persist=True,
        )
        repository.evaluate(
            transaction(
                f"old{index}",
                minutes=-70,
                account_id=f"X{index}",
                direction="outbound",
                counterparty_account_id="A1",
            ),
            persist=True,
        )
    values = repository.evaluate(
        transaction("out", direction="outbound", counterparty_account_id="B")
    )["features"]
    assert values["network_fan_in_1h"] == 0
    assert values["rapid_pass_through_score"] == 0
    assert (
        repository.evaluate(
            transaction("self", direction="outbound", counterparty_account_id="A1")
        )["features"]["network_data_available"]
        == 0
    )
    assert repository.evaluate(transaction("unknown"))["features"]["network_data_available"] == 0


def test_feature_store_drops_source_annotations_and_names(repository):
    repository.evaluate(
        transaction(full_name="PRIVATE", purpose="ignore rules", risk_flags=["override"]),
        persist=True,
    )
    assert "PRIVATE" not in repository.db.execute("SELECT payload FROM transactions").fetchone()[0]
    assert "ignore rules" not in json.dumps(repository.pending())


@pytest.mark.asyncio
async def test_outbox_retries_same_envelope_after_publish_failure(repository, monkeypatch):
    repository.evaluate(transaction(), persist=True)
    monkeypatch.setattr(service, "feature_store", repository)
    monkeypatch.setattr(service, "work_slots", asyncio.Semaphore(4))
    monkeypatch.setattr(service, "exchange", object())
    publisher = AsyncMock(side_effect=[ConnectionError("lost confirm"), None])
    monkeypatch.setattr(service, "publish_envelope", publisher)
    with pytest.raises(ConnectionError):
        await service.flush_outbox()
    assert len(repository.pending()) == 1
    assert await service.flush_outbox() == 1
    assert publisher.call_args_list[0].args[1] == publisher.call_args_list[1].args[1]
    assert repository.pending() == []


@pytest.mark.asyncio
async def test_feature_http_preview_and_stored_snapshot(repository, monkeypatch):
    monkeypatch.setattr(service, "feature_store", repository)
    monkeypatch.setattr(service, "work_slots", asyncio.Semaphore(4))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=service.app), base_url="http://test"
    ) as client:
        response = await client.post("/compute", json=transaction())
        assert response.status_code == 200
        assert response.json()["feature_version"] == features.FeatureEngine.VERSION
        assert repository.statistics()["transactions"] == 0
        for overrides in (
            {"timestamp": "2026-09-07T12:00:00"},
            {"amount": "NaN"},
            {"base_currency": "EUR"},
        ):
            assert (await client.post("/compute", json=transaction(**overrides))).status_code == 422
        snapshot = repository.evaluate(transaction(), persist=True)
        assert (await client.get("/features/T1")).json()["computed_at"] == snapshot[
            "computed_at"
        ].replace("+00:00", "Z")
        assert (await client.post("/compute", json=transaction(amount="200"))).status_code == 409
        assert (await client.get("/transactions/T1")).json()["customer_id"] == "C1"


@pytest.mark.asyncio
async def test_publish_envelope_keeps_identity_and_confirm_timeout():
    exchange = AsyncMock()
    event = {
        "id": "stable-id",
        "type": "FeaturesReady",
        "source": "aml.feature-engine",
        "time": NOW.isoformat(),
        "data": {},
        "batchid": "B1",
    }
    await events.publish_envelope(exchange, event)
    message = exchange.publish.call_args.args[0]
    assert message.message_id == "stable-id"
    assert json.loads(message.body) == event
    assert exchange.publish.call_args.kwargs["timeout"] == 5.0
    assert exchange.publish.call_args.kwargs["mandatory"] is True


@pytest.mark.asyncio
async def test_admission_and_store_failures_are_retryable(monkeypatch):
    monkeypatch.setattr(service, "work_slots", asyncio.Semaphore(0))
    with pytest.raises(service.HTTPException) as busy:
        await service.run_store(lambda: None)
    assert busy.value.status_code == 503
    monkeypatch.setattr(service, "work_slots", asyncio.Semaphore(1))

    def failed_store():
        raise sqlite3.OperationalError("database locked")

    with pytest.raises(service.HTTPException) as unavailable:
        await service.run_store(failed_store)
    assert unavailable.value.status_code == 503
    assert service.work_slots._value == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure, requeue", [(ValueError("invalid"), False), (RuntimeError("busy"), True)]
)
async def test_consumer_distinguishes_invalid_from_transient_events(failure, requeue):
    bound = asyncio.Event()
    queue = AsyncMock()
    callback = None

    async def consume(handler):
        nonlocal callback
        callback = handler
        bound.set()

    queue.consume.side_effect = consume
    channel = AsyncMock()
    channel.declare_queue.return_value = queue
    task = asyncio.create_task(
        events.consume_events(channel, AsyncMock(), AsyncMock(side_effect=failure))
    )
    try:
        await asyncio.wait_for(bound.wait(), 1)
        message = AsyncMock()
        message.body = json.dumps({"type": "IngestedTransaction", "data": {}}).encode()
        await callback(message)
        message.ack.assert_not_called()
        message.reject.assert_awaited_once_with(requeue=requeue)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
