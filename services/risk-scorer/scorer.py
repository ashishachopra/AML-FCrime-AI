import math
import os
from datetime import datetime, timezone
from typing import Any, Dict


class RiskScorer:
    """A deterministic reference policy scorer, not a trained or validated ML model."""

    VERSION = "hybrid-reference-policy-3.0.0"
    FEATURE_WEIGHTS = {
        "structuring_score": 0.22,
        "amount_near_reporting_threshold": 0.08,
        "near_threshold_count": 0.05,
        "velocity_score": 0.09,
        "velocity_acceleration": 0.08,
        "amount_deviation": 0.07,
        "fatf_call_for_action": 0.14,
        "fatf_increased_monitoring": 0.05,
        "country_risk": 0.07,
        "pep_exposure": 0.08,
        "kyc_gap_score": 0.08,
        "new_account": 0.05,
        "is_off_hours": 0.02,
        "is_weekend": 0.02,
        "behavior_anomaly_score": 0.12,
        "network_fan_out_score": 0.08,
        "rapid_pass_through_score": 0.14,
        "reciprocal_flow_score": 0.10,
        # This may only be supplied by an independent entity-screening integration.
        "sanctions_match": 0.35,
    }
    QUALITY_FEATURES = {
        "structuring_score",
        "velocity_score",
        "velocity_acceleration",
        "amount_deviation",
        "fatf_call_for_action",
        "fatf_increased_monitoring",
        "country_risk",
        "kyc_gap_score",
        "pep_exposure",
        "new_account",
    }

    def __init__(self) -> None:
        self.risk_threshold_alert = float(os.getenv("RISK_THRESHOLD_ALERT", "0.7"))
        self.risk_threshold_high = float(os.getenv("RISK_THRESHOLD_HIGH", "0.8"))
        self.risk_threshold_critical = float(os.getenv("RISK_THRESHOLD_CRITICAL", "0.9"))
        if not (
            0
            <= self.risk_threshold_alert
            <= self.risk_threshold_high
            <= self.risk_threshold_critical
            <= 1
        ):
            raise ValueError("risk thresholds must be monotonic values between zero and one")

    async def score_transaction(
        self,
        txn_id: str,
        features: Dict[str, float],
        transaction: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        if not txn_id or len(txn_id) > 128:
            raise ValueError("txn_id must contain between 1 and 128 characters")
        normalized = self._normalize_features(features)
        recognized = sorted(set(normalized) & set(self.FEATURE_WEIGHTS))
        if not recognized:
            raise ValueError("no recognized risk features were supplied")

        weighted = {name: normalized[name] * self.FEATURE_WEIGHTS[name] for name in recognized}
        available_weight = sum(self.FEATURE_WEIGHTS[name] for name in recognized)
        raw_ratio = sum(weighted.values()) / available_weight
        score = 1.0 - math.exp(-2.25 * raw_ratio)
        score, rules = self._apply_policy_overlays(score, normalized)
        score = round(min(max(score, 0.0), 1.0), 6)

        quality = len(set(features) & self.QUALITY_FEATURES) / len(self.QUALITY_FEATURES)
        if features.get("kyc_data_available") == 0:
            quality *= 0.75
            rules.append("customer_due_diligence_data_missing")
        if features.get("base_currency_conversion_available") == 0:
            rules.append("base_currency_conversion_missing")
        for flag in ("history_truncated", "network_history_truncated"):
            if features.get(flag) == 1:
                quality *= 0.5
                rules.append(flag)
        if features.get("behavior_baseline_ready") == 0:
            rules.append("behavior_baseline_warming_up")
        if features.get("network_data_available") == 0:
            rules.append("network_evidence_unavailable")
        if features.get("late_event") == 1:
            rules.append("late_event_observed_history_only")
        evidence_incomplete = (
            any(
                features.get(flag) == 1
                for flag in ("history_truncated", "network_history_truncated", "late_event")
            )
            or features.get("kyc_data_available") == 0
        )

        ordered_contributions = dict(
            sorted(weighted.items(), key=lambda item: (-abs(item[1]), item[0]))
        )
        return {
            "txn_id": txn_id,
            "risk_score": score,
            "risk_category": self._risk_category(score),
            "data_quality_score": round(quality, 6),
            "decision_basis": "deterministic_reference_policy",
            "scorer_version": self.VERSION,
            "review_recommended": evidence_incomplete or score >= self.risk_threshold_alert,
            "feature_contributions": ordered_contributions,
            "triggered_rules": sorted(set(rules)),
            "transaction": transaction or {},
            "scored_at": datetime.now(timezone.utc),
        }

    @staticmethod
    def _normalize_features(features: Dict[str, float]) -> Dict[str, float]:
        normalized: Dict[str, float] = {}
        for name, raw_value in features.items():
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"feature {name!r} must be numeric") from exc
            if not math.isfinite(value):
                raise ValueError(f"feature {name!r} must be finite")
            if name == "amount_deviation":
                value /= 5.0
            elif name == "near_threshold_count":
                value /= 4.0
            elif name == "hour_of_day":
                value /= 23.0
            normalized[name] = min(max(value, 0.0), 1.0)
        return normalized

    @staticmethod
    def _apply_policy_overlays(score: float, features: Dict[str, float]) -> tuple[float, list[str]]:
        rules: list[str] = []
        if features.get("sanctions_match", 0.0) >= 0.8:
            score = max(score, 0.98)
            rules.append("independent_sanctions_screening_match")
        if features.get("structuring_score", 0.0) >= 0.75:
            score = max(score, 0.85)
            rules.append("potential_structuring_pattern")
        if features.get("fatf_call_for_action", 0.0) >= 0.5:
            rules.append("fatf_call_for_action_jurisdiction")
        if features.get("fatf_increased_monitoring", 0.0) >= 0.5:
            rules.append("fatf_increased_monitoring_jurisdiction")
        if features.get("pep_exposure", 0.0) >= 0.5 and features.get("kyc_gap_score", 0.0) >= 0.5:
            score = max(score, 0.75)
            rules.append("pep_with_due_diligence_gap")
        if features.get("behavior_anomaly_score", 0) >= 0.8:
            rules.append("unusual_amount_for_account")
        if features.get("rapid_pass_through_score", 0) >= 0.8:
            score = max(score, 0.82)
            rules.append("rapid_fan_in_pass_through")
        if features.get("reciprocal_flow_score", 0) >= 0.8:
            score = max(score, 0.75)
            rules.append("rapid_reciprocal_transfer")
        if features.get("network_fan_out_score", 0) >= 0.8:
            rules.append("rapid_fan_out")
            if features.get("behavior_anomaly_score", 0) >= 0.8:
                score = max(score, 0.8)
                rules.append("unusual_amount_with_rapid_fan_out")
        return score, rules

    def _risk_category(self, risk_score: float) -> str:
        if risk_score >= self.risk_threshold_critical:
            return "critical"
        if risk_score >= self.risk_threshold_high:
            return "high"
        if risk_score >= self.risk_threshold_alert:
            return "medium"
        return "low"

    def get_scorer_metadata(self) -> Dict[str, Any]:
        return {
            "scorer_version": self.VERSION,
            "scorer_type": "deterministic_reference_policy",
            "hybrid_methods": ["reference_rules", "robust_behavioral_baseline", "temporal_network"],
            "validation_status": "not_validated_for_production",
            "performance_metrics": None,
            "thresholds": {
                "alert": self.risk_threshold_alert,
                "high": self.risk_threshold_high,
                "critical": self.risk_threshold_critical,
            },
            "limitations": [
                "No trained model artifact or representative labeled validation dataset is included.",
                "Feature contributions are policy-weight contributions, not SHAP values.",
                "Jurisdiction indicators must not be used as automatic sanctions or de-risking decisions.",
                "Thresholds and weights require independent validation for each deployment context.",
            ],
        }

    # Backward-compatible method name for callers of the old endpoint.
    def get_model_metrics(self) -> Dict[str, Any]:
        return self.get_scorer_metadata()
