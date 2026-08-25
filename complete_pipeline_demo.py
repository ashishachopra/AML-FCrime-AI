"""Upload the fixtures through the gateway and display resulting decisions."""

import json
import os
import time
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")


def main() -> None:
    token = os.getenv("AML_ACCESS_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=GATEWAY_URL, headers=headers, timeout=30) as client:
        health = client.get("/health/ready")
        health.raise_for_status()

        with (
            (ROOT / "fixtures/accounts.json").open("rb") as accounts,
            (ROOT / "fixtures/customers.json").open("rb") as customers,
            (ROOT / "fixtures/transactions.json").open("rb") as transactions,
        ):
            response = client.post(
                "/v1/batch",
                files={
                    "accounts": ("accounts.json", accounts, "application/json"),
                    "customers": ("customers.json", customers, "application/json"),
                    "transactions": ("transactions.json", transactions, "application/json"),
                },
            )
        if response.is_error:
            raise SystemExit(f"Batch upload failed ({response.status_code}): {response.text}")
        batch_result = response.json()

        transaction_ids = [
            item["txn_id"]
            for item in json.loads(
                (ROOT / "fixtures/transactions.json").read_text(encoding="utf-8")
            )
        ]
        deadline = time.monotonic() + 30
        pending = set(transaction_ids)
        results: list[dict[str, object]] = []
        while pending and time.monotonic() < deadline:
            for txn_id in list(pending):
                result = client.get(f"/v1/transactions/{txn_id}")
                if result.status_code == 200 and result.json().get("risk_score") is not None:
                    results.append(result.json())
                    pending.remove(txn_id)
            if pending:
                time.sleep(1)
        if pending:
            raise SystemExit(f"Timed out waiting for transactions: {sorted(pending)}")

        ordered = sorted(results, key=lambda item: float(item["risk_score"]), reverse=True)
        print(
            json.dumps(
                {
                    "batch": batch_result,
                    "transactions_scored": len(results),
                    "risk_categories": dict(
                        Counter(str(item["risk_category"]) for item in results)
                    ),
                    "alerts_created": sum(len(item["alerts"]) for item in results),
                    "highest_risk": [
                        {
                            "txn_id": item["txn_id"],
                            "risk_score": item["risk_score"],
                            "risk_category": item["risk_category"],
                        }
                        for item in ordered[:5]
                    ],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
