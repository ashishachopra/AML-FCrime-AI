"""Reproducible synthetic feature-lookup benchmark, not AML model validation.

Bulk fixture loading bypasses scoring/outbox and is excluded from timings. Both
paths use exactly the same feature policy and history. No live customer data,
broker, external model, or network service is used.
"""

import argparse
import asyncio
import hashlib
import json
import math
import platform
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "feature-engine"))
from features import FeatureEngine  # noqa: E402
from store import FeatureStore, encode, event_time  # noqa: E402


def percentiles(values):
    ordered = sorted(values)
    return {
        f"p{percentile}_ms": round(ordered[math.ceil(len(ordered) * percentile / 100) - 1], 4)
        for percentile in (50, 95, 99)
    }


async def benchmark(args):
    engine = FeatureEngine()
    now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    history = {}
    for index in range(args.transactions):
        data = {
            "txn_id": f"T{index}",
            "account_id": f"A{index % args.accounts}",
            "timestamp": (now - timedelta(minutes=1 + index // args.accounts)).isoformat(),
            "amount": str(100 + index % 13),
            "currency": "USD",
            "counterparty_country": "US",
        }
        history[data["txn_id"]] = data
    candidate = {
        "txn_id": "preview",
        "account_id": "A0",
        "timestamp": now.isoformat(),
        "amount": "120",
        "currency": "USD",
        "counterparty_country": "US",
    }
    with tempfile.TemporaryDirectory(prefix="aml-benchmark-") as directory:
        repository = FeatureStore(str(Path(directory) / "benchmark.db"), engine)
        try:
            # Seed only the history table so large benchmark fixtures need no
            # artificial per-record fsync or irrelevant scoring/outbox work.
            with repository.db:
                repository.db.executemany(
                    "INSERT INTO transactions VALUES (?, ?, ?, NULL, NULL, ?, ?, '{}')",
                    (
                        (
                            data["txn_id"],
                            data["account_id"],
                            event_time(data["timestamp"]),
                            hashlib.sha256(encode(data).encode()).hexdigest(),
                            encode(data),
                        )
                        for data in history.values()
                    ),
                )
            reference = await engine.compute_features(candidate, history, {}, {})
            indexed = repository.evaluate(candidate)["features"]
            if indexed["history_truncated"]:
                raise ValueError("benchmark target history exceeds cap; increase --accounts")
            assert all(indexed[key] == value for key, value in reference.items()), (
                "feature parity failed"
            )
            timings = {"global_scan_reference": [], "indexed_sqlite_preview": []}
            for index in range(args.samples + 5):
                # Alternate execution order to reduce systematic warm-cache bias.
                names = list(timings) if index % 2 else list(reversed(timings))
                for name in names:
                    start = perf_counter()
                    if name == "global_scan_reference":
                        await engine.compute_features(candidate, history, {}, {})
                    else:
                        repository.evaluate(candidate)
                    elapsed = (perf_counter() - start) * 1000
                    if index >= 5:
                        timings[name].append(elapsed)
            return {
                "benchmark": "synthetic_feature_preview",
                "fixture_as_of": now.isoformat(),
                "python": platform.python_version(),
                "platform": platform.platform(),
                "sqlite": sqlite3.sqlite_version,
                "transactions": args.transactions,
                "accounts": args.accounts,
                "target_history_rows": int(indexed["count_30d"]),
                "samples": args.samples,
                "warmup_samples": 5,
                "feature_parity": True,
                "results": {name: percentiles(values) for name, values in timings.items()},
                "excludes": ["fixture_loading", "HTTP", "RabbitMQ", "durable_ingestion", "LLM"],
                "limitations": "Synthetic warm local reads; not a production SLO or detection-quality estimate.",
            }
        finally:
            repository.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transactions", type=int, default=100000)
    parser.add_argument("--accounts", type=int, default=1000)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.transactions, args.accounts, args.samples) < 1:
        parser.error("transactions, accounts, and samples must be positive")
    result = asyncio.run(benchmark(args))
    content = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    print(content, end="")


if __name__ == "__main__":
    main()
