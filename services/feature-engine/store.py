"""Indexed evidence, immutable feature snapshots, and a transactional outbox.

One serialized connection per service process. No database transaction or lock is
held while awaiting the broker. Deploy one feature-engine process per data shard.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from evidence import FeatureAccount, FeatureCustomer, FeatureTransaction
from features import FeatureEngine


class ReplayConflict(ValueError):
    """A transaction identifier was reused for different normalized evidence."""


class StoreBusy(RuntimeError):
    """Retryable admission failure; evidence has not been accepted."""


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def event_time(value: str) -> int:
    delta = FeatureEngine.parse_timestamp(value) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


class FeatureStore:
    def __init__(
        self,
        path: str,
        engine: FeatureEngine,
        *,
        max_history: int = 1000,
        max_network: int = 1000,
        max_outbox: int = 10000,
    ) -> None:
        if min(max_history, max_network, max_outbox) < 1:
            raise ValueError("store limits must be positive")
        self.engine = engine
        self.max_history = max_history
        self.max_network = max_network
        self.max_outbox = max_outbox
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=1.0)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS customers (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS transactions (
                txn_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                event_time INTEGER NOT NULL, source TEXT, target TEXT,
                fingerprint TEXT NOT NULL, payload TEXT NOT NULL, result TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_account_time
                ON transactions(account_id, event_time DESC, txn_id);
            CREATE INDEX IF NOT EXISTS idx_source_time
                ON transactions(source, event_time DESC, txn_id);
            CREATE INDEX IF NOT EXISTS idx_target_time
                ON transactions(target, event_time DESC, txn_id);
            CREATE TABLE IF NOT EXISTS outbox (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                txn_id TEXT NOT NULL UNIQUE REFERENCES transactions(txn_id),
                event TEXT NOT NULL
            );
        """)

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def put_entity(self, kind: str, data: dict) -> None:
        if kind == "IngestedCustomer":
            value = FeatureCustomer.model_validate(data).model_dump(mode="json")
            table, key = "customers", "customer_id"
        elif kind == "IngestedAccount":
            value = FeatureAccount.model_validate(data).model_dump(mode="json")
            table, key = "accounts", "account_id"
        else:
            raise ValueError("unsupported entity type")
        with self._lock, self.db:
            self.db.execute(
                f"INSERT INTO {table}(id, payload) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (value[key], encode(value)),
            )

    def _entity(self, table: str, key: str) -> dict:
        row = self.db.execute(f"SELECT payload FROM {table} WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def _window(self, column: str, key: str, start: int, end: int, limit: int) -> tuple[list, bool]:
        # column is selected only by internal code, never from API input.
        rows = self.db.execute(
            f"SELECT payload FROM transactions WHERE {column}=? "
            "AND event_time>=? AND event_time<? ORDER BY event_time DESC, txn_id LIMIT ?",
            (key, start, end, limit + 1),
        ).fetchall()
        return [json.loads(row[0]) for row in rows[:limit]], len(rows) > limit

    def _compute(self, data: dict) -> dict:
        end = event_time(data["timestamp"])
        days = max(self.engine.velocity_window_days, 7)
        start = end - int(timedelta(days=days).total_seconds() * 1_000_000)
        history, truncated = self._window(
            "account_id", data["account_id"], start, end, self.max_history
        )
        network, network_truncated = [], False
        if data.get("direction") == "outbound" and data.get("counterparty_account_id"):
            for column in ("source", "target"):
                rows, capped = self._window(
                    column, data["account_id"], end - 3_600_000_000, end, self.max_network
                )
                network.extend(rows)
                network_truncated |= capped
        account = self._entity("accounts", data["account_id"])
        customer = self._entity("customers", account.get("customer_id", ""))
        features = self.engine.compute_from_history(
            data,
            history,
            {customer.get("customer_id"): customer} if customer else {},
            {data["account_id"]: account} if account else {},
            network,
        )
        features["history_truncated"] = float(truncated)
        features["network_history_truncated"] = float(network_truncated)
        newer = self.db.execute(
            "SELECT 1 FROM transactions WHERE account_id=? AND event_time>=? LIMIT 1",
            (data["account_id"], end),
        ).fetchone()
        features["late_event"] = float(newer is not None)
        context = dict(data, customer_id=account.get("customer_id"))
        return {
            "txn_id": data["txn_id"],
            "features": features,
            "transaction": context,
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "feature_version": self.engine.VERSION,
        }

    def evaluate(self, raw: dict, *, persist: bool = False, batch_id: str | None = None) -> dict:
        data = FeatureTransaction.model_validate(raw).canonical()
        payload = encode(data)
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        with self._lock:
            # This also provides a consistent read snapshot for previews and
            # serializes conflicting writers using separate SQLite connections.
            self.db.execute("BEGIN IMMEDIATE" if persist else "BEGIN")
            try:
                existing = self.db.execute(
                    "SELECT fingerprint, result FROM transactions WHERE txn_id=?", (data["txn_id"],)
                ).fetchone()
                if existing:
                    if fingerprint != existing["fingerprint"]:
                        raise ReplayConflict("txn_id already exists with different evidence")
                    result = json.loads(existing["result"])
                else:
                    if (
                        persist
                        and self.db.execute("SELECT count(*) FROM outbox").fetchone()[0]
                        >= self.max_outbox
                    ):
                        raise StoreBusy("feature outbox is full; retry after publisher recovery")
                    result = self._compute(data)
                    if persist:
                        self._save(data, payload, fingerprint, result, batch_id)
                self.db.commit()
                return result
            except BaseException:
                self.db.rollback()
                raise

    def _save(self, data, payload, fingerprint, result, batch_id):
        canonical_edge = data.get("direction") == "outbound" and data.get("counterparty_account_id")
        source = data["account_id"] if canonical_edge else None
        target = data["counterparty_account_id"] if canonical_edge else None
        self.db.execute(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data["txn_id"],
                data["account_id"],
                event_time(data["timestamp"]),
                source,
                target,
                fingerprint,
                payload,
                encode(result),
            ),
        )
        event = {
            "specversion": "1.0",
            "type": "FeaturesReady",
            "source": "aml.feature-engine",
            "id": str(uuid.uuid4()),
            "time": result["computed_at"],
            "datacontenttype": "application/json",
            "data": result,
        }
        if batch_id:
            event["batchid"] = batch_id
        self.db.execute(
            "INSERT INTO outbox(txn_id, event) VALUES (?, ?)", (data["txn_id"], encode(event))
        )

    def get(self, txn_id: str) -> dict | None:
        with self._lock:
            row = self.db.execute(
                "SELECT result FROM transactions WHERE txn_id=?", (txn_id,)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def list(self, limit: int, offset: int) -> dict:
        with self._lock:
            rows = self.db.execute(
                "SELECT result FROM transactions ORDER BY rowid LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
            total = self.db.execute("SELECT count(*) FROM transactions").fetchone()[0]
        return {"features": [json.loads(row[0]) for row in rows], "total": total}

    def pending(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT sequence, event FROM outbox ORDER BY sequence LIMIT ?", (limit,)
            ).fetchall()
            return [{"sequence": row[0], "event": json.loads(row[1])} for row in rows]

    def mark_published(self, sequence: int) -> None:
        with self._lock, self.db:
            self.db.execute("DELETE FROM outbox WHERE sequence=?", (sequence,))

    def statistics(self) -> dict:
        with self._lock:
            pending = self.db.execute("SELECT count(*) FROM outbox").fetchone()[0]
            transactions = self.db.execute("SELECT count(*) FROM transactions").fetchone()[0]
        return {
            "transactions": transactions,
            "outbox_pending": pending,
            "max_outbox": self.max_outbox,
            "max_history_rows": self.max_history,
            "max_network_rows_per_direction": self.max_network,
            "sqlite_version": sqlite3.sqlite_version,
        }
