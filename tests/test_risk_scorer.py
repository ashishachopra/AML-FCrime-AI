import pytest
from conftest import load_source_module

scorer_module = load_source_module("risk_scorer_test", "services/risk-scorer/scorer.py")


@pytest.mark.asyncio
async def test_scoring_is_deterministic_and_explanations_are_honest() -> None:
    scorer = scorer_module.RiskScorer()
    features = {
        "structuring_score": 0.8,
        "velocity_score": 0.7,
        "velocity_acceleration": 0.4,
        "amount_deviation": 2.0,
        "fatf_call_for_action": 0.0,
        "fatf_increased_monitoring": 0.0,
        "country_risk": 0.2,
        "kyc_gap_score": 0.3,
        "pep_exposure": 0.0,
        "new_account": 0.0,
        "kyc_data_available": 1.0,
        "base_currency_conversion_available": 1.0,
    }
    first = await scorer.score_transaction("T1", features)
    second = await scorer.score_transaction("T1", features)
    assert first["risk_score"] == second["risk_score"]
    assert first["risk_score"] >= 0.85
    assert first["feature_contributions"] == second["feature_contributions"]
    assert "potential_structuring_pattern" in first["triggered_rules"]
    assert "shap_values" not in first

    metadata = scorer.get_scorer_metadata()
    assert metadata["validation_status"] == "not_validated_for_production"
    assert metadata["performance_metrics"] is None


@pytest.mark.asyncio
async def test_scoring_rejects_unknown_or_non_finite_features() -> None:
    scorer = scorer_module.RiskScorer()
    with pytest.raises(ValueError, match="no recognized"):
        await scorer.score_transaction("T1", {"unknown": 1.0})
    with pytest.raises(ValueError, match="finite"):
        await scorer.score_transaction("T1", {"structuring_score": float("nan")})
