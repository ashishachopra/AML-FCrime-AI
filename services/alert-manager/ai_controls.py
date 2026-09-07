"""Deterministic, local controls around optional paid inference.

Reservations count attempts, including uncertain/failed requests. They are never
refunded automatically and cannot be repeated for the same transaction.
"""

import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class AIPolicy:
    daily_calls: int = 100
    concurrent_calls: int = 2
    input_bytes: int = 8192
    output_tokens: int = 700
    timeout_seconds: float = 8.0
    failure_threshold: int = 3
    cooldown_seconds: int = 300

    @classmethod
    def from_env(cls):
        value = cls(
            daily_calls=int(os.getenv("AI_DAILY_CALL_LIMIT", "100")),
            concurrent_calls=int(os.getenv("AI_MAX_CONCURRENT", "2")),
            input_bytes=int(os.getenv("AI_MAX_INPUT_BYTES", "8192")),
            output_tokens=int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "700")),
            timeout_seconds=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "8")),
            failure_threshold=int(os.getenv("AI_FAILURE_THRESHOLD", "3")),
            cooldown_seconds=int(os.getenv("AI_COOLDOWN_SECONDS", "300")),
        )
        if not (
            0 <= value.daily_calls <= 1_000_000
            and 1 <= value.concurrent_calls <= 32
            and 512 <= value.input_bytes <= 65536
            and 64 <= value.output_tokens <= 2000
            and math.isfinite(value.timeout_seconds)
            and 0 < value.timeout_seconds <= 60
            and 1 <= value.failure_threshold <= 100
            and 1 <= value.cooldown_seconds <= 86400
        ):
            raise ValueError("invalid AI resource limits")
        return value


class NarrativeBudget:
    def __init__(self, connection, lock):
        self.db = connection
        self.lock = lock
        with self.lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS ai_budget_days (
                    day TEXT PRIMARY KEY, calls INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ai_attempts (
                    txn_id TEXT PRIMARY KEY, state TEXT NOT NULL,
                    expires_at REAL NOT NULL, outcome TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_ai_active ON ai_attempts(state, expires_at);
                CREATE TABLE IF NOT EXISTS ai_circuit (
                    id INTEGER PRIMARY KEY CHECK(id=1), failures INTEGER NOT NULL,
                    open_until REAL NOT NULL
                );
                INSERT OR IGNORE INTO ai_circuit VALUES (1, 0, 0);
            """)

    def reserve(self, txn_id: str, policy: AIPolicy, now: float) -> str:
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if self.db.execute(
                    "SELECT 1 FROM ai_attempts WHERE txn_id=?", (txn_id,)
                ).fetchone():
                    return "already_attempted"
                if (
                    self.db.execute("SELECT open_until FROM ai_circuit WHERE id=1").fetchone()[0]
                    > now
                ):
                    return "circuit_open"
                row = self.db.execute(
                    "SELECT calls FROM ai_budget_days WHERE day=?", (day,)
                ).fetchone()
                if (row[0] if row else 0) >= policy.daily_calls:
                    return "daily_budget_exhausted"
                active = self.db.execute(
                    "SELECT count(*) FROM ai_attempts WHERE state='reserved' AND expires_at>?",
                    (now,),
                ).fetchone()[0]
                if active >= policy.concurrent_calls:
                    return "concurrency_limit"
                self.db.execute(
                    "INSERT INTO ai_budget_days VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET calls=calls+1",
                    (day,),
                )
                self.db.execute(
                    "INSERT INTO ai_attempts VALUES (?, 'reserved', ?, NULL)",
                    (txn_id, now + policy.timeout_seconds + 5),
                )
                self.db.commit()
                return "reserved"
            finally:
                # Also rolls back early-return denials, or partial writes on error.
                if self.db.in_transaction:
                    self.db.rollback()

    def finish(self, txn_id: str, *, success: bool, outcome: str, policy: AIPolicy, now: float):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                changed = self.db.execute(
                    "UPDATE ai_attempts SET state='finished', outcome=? WHERE txn_id=? AND state='reserved'",
                    (outcome, txn_id),
                ).rowcount
                if changed:
                    failures, opened = self.db.execute(
                        "SELECT failures, open_until FROM ai_circuit WHERE id=1"
                    ).fetchone()
                    # A success from an older concurrent request must not close an
                    # already-open breaker. Only a later successful probe can.
                    if success and opened <= now:
                        failures, opened = 0, 0
                    elif not success:
                        failures += 1
                        if failures >= policy.failure_threshold:
                            opened = max(opened, now + policy.cooldown_seconds)
                    self.db.execute(
                        "UPDATE ai_circuit SET failures=?, open_until=? WHERE id=1",
                        (failures, opened),
                    )
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def statistics(self, policy: AIPolicy, now: float) -> dict:
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        with self.lock:
            row = self.db.execute("SELECT calls FROM ai_budget_days WHERE day=?", (day,)).fetchone()
            failures, opened = self.db.execute(
                "SELECT failures, open_until FROM ai_circuit WHERE id=1"
            ).fetchone()
            active = self.db.execute(
                "SELECT count(*) FROM ai_attempts WHERE state='reserved' AND expires_at>?", (now,)
            ).fetchone()[0]
        return {
            "utc_day": day,
            "reserved_calls": row[0] if row else 0,
            "daily_call_limit": policy.daily_calls,
            "active_reservations": active,
            "concurrency_limit": policy.concurrent_calls,
            "consecutive_failures": failures,
            "circuit_open": opened > now,
            "circuit_open_until": opened,
        }
