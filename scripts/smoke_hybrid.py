"""Synthetic end-to-end test for docker/compose.smoke.yml only."""

import json
import time
from datetime import datetime, timedelta, timezone

import httpx


def main():
    now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    customers = [
        {
            "customer_id": "C1",
            "full_name": "Synthetic Person",
            "dob": "1990-01-01",
            "kyc_level": "enhanced",
            "pep_flag": False,
        }
    ]
    accounts = [
        {
            "account_id": account,
            "customer_id": "C1",
            "country": "US",
            "opened_at": "2020-01-01T00:00:00Z",
            "account_type": "current",
        }
        for account in ("A1", "S0", "S1", "S2")
    ]
    transactions = [
        {
            "txn_id": f"IN{index}",
            "account_id": f"S{index}",
            "timestamp": (now - timedelta(minutes=3 - index)).isoformat(),
            "amount": "1000",
            "currency": "USD",
            "counterparty_country": "US",
            "direction": "outbound",
            "counterparty_account_id": "A1",
        }
        for index in range(3)
    ]
    transactions.append(
        {
            "txn_id": "OUT",
            "account_id": "A1",
            "timestamp": now.isoformat(),
            "amount": "2900",
            "currency": "USD",
            "counterparty_country": "US",
            "direction": "outbound",
            "counterparty_account_id": "B1",
        }
    )
    with httpx.Client(timeout=5) as client:
        response = client.post(
            "http://gateway:8000/v1/batch",
            files={
                key: (f"{key}.json", json.dumps(value).encode(), "application/json")
                for key, value in {
                    "customers": customers,
                    "accounts": accounts,
                    "transactions": transactions,
                }.items()
            },
        )
        assert response.status_code == 201, response.text
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            response = client.get("http://gateway:8000/v1/alerts")
            response.raise_for_status()
            matches = [alert for alert in response.json()["alerts"] if alert["txn_id"] == "OUT"]
            if matches:
                break
            time.sleep(0.2)
        else:
            raise AssertionError("network alert did not arrive through the broker pipeline")
        assert matches[0]["alert_type"] == "network_flow_review", matches
        assert matches[0]["risk_score"] >= 0.82
        before = client.get("http://features:8002/features").json()["total"]
        preview = client.post(
            "http://gateway:8000/v1/evaluate",
            json={"transaction": dict(transactions[-1], txn_id="PREVIEW")},
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["mode"] == "preview"
        assert preview.json()["features"]["rapid_pass_through_score"] == 1
        assert client.get("http://features:8002/features").json()["total"] == before == 4
        # Batch replay is harmless to the immutable feature store and alert ID.
        again = client.post(
            "http://gateway:8000/v1/batch",
            files={
                key: (f"{key}.json", json.dumps(value).encode(), "application/json")
                for key, value in {
                    "customers": customers,
                    "accounts": accounts,
                    "transactions": transactions,
                }.items()
            },
        )
        assert again.status_code == 201
        # Review the persisted template through HTTP and exercise revision binding.
        alert_url = f"http://gateway:8000/v1/alerts/{matches[0]['alert_id']}"
        detail = client.get(alert_url).json()
        assert detail["sar_generated_by"] == "template"
        review = {"expected_revision": detail["revision"], "sar_review_status": "approved"}
        reviewed = client.patch(alert_url, json=review)
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["sar_review_status"] == "approved"
        assert client.patch(alert_url, json=review).status_code == 409
        audit = client.get(f"{alert_url}/audit").json()
        assert any("reviewed_narrative_sha256" in item["changes"] for item in audit)
        statistics = client.get("http://gateway:8000/v1/alerts/statistics")
        assert statistics.status_code == 200, statistics.text
        assert statistics.json()["ai"]["reserved_calls"] == 0
        print(
            json.dumps(
                {
                    "smoke": "passed",
                    "transactions": before,
                    "alert_type": matches[0]["alert_type"],
                    "risk_score": matches[0]["risk_score"],
                    "preview": preview.json()["mode"],
                    "revision_review": "passed",
                    "paid_calls": statistics.json()["ai"]["reserved_calls"],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
