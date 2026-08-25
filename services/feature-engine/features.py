import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List


class FeatureEngine:
    """Deterministic AML feature computation with versioned policy inputs."""

    DEFAULT_CALL_FOR_ACTION = {"IR", "KP", "MM"}
    DEFAULT_INCREASED_MONITORING = {
        "AO",
        "BA",
        "BG",
        "BO",
        "CD",
        "CI",
        "CM",
        "HT",
        "IQ",
        "KE",
        "KW",
        "LA",
        "LB",
        "MC",
        "NP",
        "PG",
        "SS",
        "SY",
        "VE",
        "VG",
        "VN",
        "YE",
    }

    def __init__(self) -> None:
        self.velocity_window_days = int(os.getenv("VELOCITY_WINDOW_DAYS", "30"))
        self.velocity_short_window_days = int(os.getenv("VELOCITY_SHORT_WINDOW_DAYS", "7"))
        if not 0 < self.velocity_short_window_days <= self.velocity_window_days:
            raise ValueError("velocity windows must be positive and short <= long")

        self.base_currency = os.getenv("BASE_CURRENCY", "USD").strip().upper()
        self.reporting_thresholds = self._parse_thresholds(
            os.getenv("STRUCTURING_THRESHOLDS", "10000")
        )
        self.fatf_call_for_action = self._country_set(
            "FATF_CALL_FOR_ACTION_COUNTRIES", self.DEFAULT_CALL_FOR_ACTION
        )
        self.fatf_increased_monitoring = self._country_set(
            "FATF_INCREASED_MONITORING_COUNTRIES", self.DEFAULT_INCREASED_MONITORING
        )
        self.jurisdiction_data_as_of = os.getenv("JURISDICTION_DATA_AS_OF", "2026-06-19")
        self.kyc_scores = {"basic": 0.7, "standard": 0.3, "enhanced": 0.1}

    @staticmethod
    def _parse_thresholds(value: str) -> tuple[float, ...]:
        try:
            thresholds = tuple(
                sorted({float(item.strip()) for item in value.split(",") if item.strip()})
            )
        except ValueError as exc:
            raise ValueError("STRUCTURING_THRESHOLDS must contain positive numbers") from exc
        if not thresholds or any(item <= 0 for item in thresholds):
            raise ValueError("STRUCTURING_THRESHOLDS must contain positive numbers")
        return thresholds

    @staticmethod
    def _country_set(name: str, default: Iterable[str]) -> set[str]:
        raw = os.getenv(name)
        values = default if raw is None else raw.split(",")
        countries = {value.strip().upper() for value in values if value.strip()}
        if any(len(country) != 2 or not country.isalpha() for country in countries):
            raise ValueError(f"{name} must contain ISO alpha-2 country codes")
        return countries

    @staticmethod
    def parse_timestamp(value: str | datetime) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        else:
            normalized = value.strip().replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamps must include a UTC offset")
        return parsed.astimezone(timezone.utc)

    async def compute_features(
        self,
        transaction: Dict[str, Any],
        transaction_store: Dict[str, Any],
        customer_store: Dict[str, Any],
        account_store: Dict[str, Any],
    ) -> Dict[str, float]:
        required = {
            "txn_id",
            "account_id",
            "timestamp",
            "amount",
            "currency",
            "counterparty_country",
        }
        missing = sorted(required - transaction.keys())
        if missing:
            raise ValueError(f"transaction is missing required fields: {missing}")

        features: Dict[str, float] = {}
        features.update(self._compute_transaction_features(transaction))
        features.update(self._compute_velocity_features(transaction, transaction_store))
        features.update(self._compute_country_risk_features(transaction))
        features.update(self._compute_customer_features(transaction, customer_store, account_store))
        features.update(self._compute_time_features(transaction))
        return features

    def _base_amount(self, transaction: Dict[str, Any]) -> float | None:
        currency = str(transaction.get("currency", "")).upper()
        if currency == self.base_currency:
            return float(transaction["amount"])
        if str(transaction.get("base_currency", "")).upper() == self.base_currency:
            converted = transaction.get("base_currency_amount")
            if converted is not None:
                return float(converted)
        return None

    def _compute_transaction_features(self, transaction: Dict[str, Any]) -> Dict[str, float]:
        amount = float(transaction["amount"])
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("transaction amount must be a positive finite number")
        base_amount = self._base_amount(transaction)
        primary_threshold = self.reporting_thresholds[0]
        return {
            "amount": amount,
            "amount_log": math.log1p(amount),
            "amount_rounded": float(amount % 1000 == 0),
            "base_currency_conversion_available": float(base_amount is not None),
            "reporting_threshold_exceeded": float(
                base_amount is not None and base_amount >= primary_threshold
            ),
            "amount_near_reporting_threshold": float(
                base_amount is not None
                and primary_threshold * 0.8 <= base_amount < primary_threshold
            ),
        }

    def _historical_account_transactions(
        self,
        transaction: Dict[str, Any],
        transaction_store: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        current_time = self.parse_timestamp(transaction["timestamp"])
        account_id = transaction["account_id"]
        history = [
            candidate
            for candidate in transaction_store.values()
            if candidate.get("account_id") == account_id
            and candidate.get("txn_id") != transaction["txn_id"]
            and self.parse_timestamp(candidate["timestamp"]) <= current_time
        ]
        return sorted(history, key=lambda item: self.parse_timestamp(item["timestamp"]))

    def _compute_velocity_features(
        self,
        transaction: Dict[str, Any],
        transaction_store: Dict[str, Any],
    ) -> Dict[str, float]:
        current_time = self.parse_timestamp(transaction["timestamp"])
        history = self._historical_account_transactions(transaction, transaction_store)
        long_start = current_time - timedelta(days=self.velocity_window_days)
        short_start = current_time - timedelta(days=self.velocity_short_window_days)
        long_history = [
            item for item in history if self.parse_timestamp(item["timestamp"]) >= long_start
        ]
        short_history = [
            item for item in history if self.parse_timestamp(item["timestamp"]) >= short_start
        ]

        long_amounts = [float(item["amount"]) for item in long_history]
        short_amounts = [float(item["amount"]) for item in short_history]
        long_average = sum(long_amounts) / len(long_amounts) if long_amounts else 0.0
        current_amount = float(transaction["amount"])
        amount_deviation = (
            abs(current_amount - long_average) / long_average if long_average > 0 else 0.0
        )
        long_daily = len(long_history) / self.velocity_window_days
        short_daily = len(short_history) / self.velocity_short_window_days
        structuring = self._detect_structuring_patterns(transaction, history, current_time)

        return {
            f"amt_{self.velocity_window_days}d": sum(long_amounts),
            f"count_{self.velocity_window_days}d": float(len(long_history)),
            f"avg_amt_{self.velocity_window_days}d": long_average,
            f"amt_{self.velocity_short_window_days}d": sum(short_amounts),
            f"count_{self.velocity_short_window_days}d": float(len(short_history)),
            f"avg_amt_{self.velocity_short_window_days}d": (
                sum(short_amounts) / len(short_amounts) if short_amounts else 0.0
            ),
            "velocity_score": min(long_daily / 2.0, 1.0),
            "velocity_acceleration": min(max(short_daily - long_daily, 0.0) / 2.0, 1.0),
            "amount_deviation": min(amount_deviation, 5.0),
            "structuring_score": structuring["score"],
            "near_threshold_count": structuring["near_threshold_count"],
        }

    def _detect_structuring_patterns(
        self,
        transaction: Dict[str, Any],
        history: List[Dict[str, Any]],
        current_time: datetime,
    ) -> Dict[str, float]:
        seven_days_ago = current_time - timedelta(days=7)
        candidates = [transaction] + [
            item for item in history if self.parse_timestamp(item["timestamp"]) >= seven_days_ago
        ]
        base_amounts = [
            amount for item in candidates if (amount := self._base_amount(item)) is not None
        ]
        near_threshold_count = sum(
            1
            for amount in base_amounts
            if any(threshold * 0.8 <= amount < threshold for threshold in self.reporting_thresholds)
        )
        score = min(near_threshold_count / 4.0, 1.0)
        return {"score": score, "near_threshold_count": float(near_threshold_count)}

    def _compute_country_risk_features(self, transaction: Dict[str, Any]) -> Dict[str, float]:
        country = str(transaction["counterparty_country"]).upper()
        call_for_action = float(country in self.fatf_call_for_action)
        increased_monitoring = float(country in self.fatf_increased_monitoring)
        country_risk = 0.9 if call_for_action else 0.6 if increased_monitoring else 0.2
        return {
            "country_risk": country_risk,
            "fatf_call_for_action": call_for_action,
            "fatf_increased_monitoring": increased_monitoring,
            "high_risk_country": call_for_action,
        }

    def _compute_customer_features(
        self,
        transaction: Dict[str, Any],
        customer_store: Dict[str, Any],
        account_store: Dict[str, Any],
    ) -> Dict[str, float]:
        account = account_store.get(transaction["account_id"])
        customer = customer_store.get(account.get("customer_id")) if account else None
        if not account or not customer:
            return {
                "kyc_data_available": 0.0,
                "kyc_gap_score": 1.0,
                "pep_data_available": 0.0,
                "pep_exposure": 0.0,
                "account_age_score": 0.0,
                "new_account": 1.0,
            }

        transaction_time = self.parse_timestamp(transaction["timestamp"])
        opened_time = self.parse_timestamp(account["opened_at"])
        account_age_days = max((transaction_time - opened_time).days, 0)
        account_age_score = min(account_age_days / 365.0, 1.0)
        return {
            "kyc_data_available": 1.0,
            "kyc_gap_score": self.kyc_scores.get(customer.get("kyc_level"), 1.0),
            "pep_data_available": 1.0,
            "pep_exposure": float(bool(customer.get("pep_flag"))),
            "account_age_score": account_age_score,
            "new_account": float(account_age_days < 90),
        }

    def _compute_time_features(self, transaction: Dict[str, Any]) -> Dict[str, float]:
        timestamp = self.parse_timestamp(transaction["timestamp"])
        return {
            "hour_of_day": float(timestamp.hour),
            "is_weekend": float(timestamp.weekday() >= 5),
            "is_off_hours": float(timestamp.hour < 8 or timestamp.hour >= 18),
        }

    def metadata(self) -> Dict[str, Any]:
        return {
            "base_currency": self.base_currency,
            "reporting_thresholds": list(self.reporting_thresholds),
            "jurisdiction_source": "FATF public statements (configured snapshot)",
            "jurisdiction_data_as_of": self.jurisdiction_data_as_of,
            "limitations": [
                "Jurisdiction indicators are risk inputs, not sanctions matches or automatic decisions.",
                "Non-base-currency threshold features require an upstream converted amount.",
                "Production deployments must refresh and independently approve policy data.",
            ],
        }
