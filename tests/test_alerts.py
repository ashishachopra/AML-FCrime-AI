import pytest
from conftest import load_source_module

alerts_module = load_source_module("alerts_test", "services/alert-manager/alerts.py")


@pytest.mark.asyncio
async def test_template_draft_uses_only_evidence_and_requires_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAR_GENERATION_ENABLED", "false")
    manager = alerts_module.AlertManager(alerts_module.AlertRepository(":memory:"))
    scored = {
        "txn_id": "T-EVIDENCE",
        "risk_score": 0.9,
        "risk_category": "critical",
        "data_quality_score": 1.0,
        "decision_basis": "deterministic_reference_policy",
        "scorer_version": "reference-policy-2.0.0",
        "feature_contributions": {"structuring_score": 0.2},
        "triggered_rules": ["potential_structuring_pattern"],
        "transaction": {
            "txn_id": "T-EVIDENCE",
            "account_id": "A1",
            "customer_id": "C1",
            "timestamp": "2026-08-25T10:00:00Z",
            "amount": "1234.50",
            "currency": "SAR",
            "counterparty_country": "SA",
        },
    }
    alert = await manager.process_scored_transaction(scored)
    assert alert is not None
    assert alert["customer_id"] == "C1"
    assert alert["sar_review_status"] == "draft_pending_review"
    assert alert["sar_generated_by"] == "template"
    assert alert["sar_narrative"].startswith("DRAFT - HUMAN REVIEW REQUIRED")
    assert "1234.50 SAR" in alert["sar_narrative"]
    assert "50000" not in alert["sar_narrative"]

    reviewed = await manager.update_alert(
        alert["alert_id"], {"sar_review_status": "approved", "actor": "analyst@example"}
    )
    assert reviewed["sar_reviewed_by"] == "analyst@example"
    audit = await manager.get_alert_audit(alert["alert_id"])
    assert [entry["action"] for entry in audit] == ["alert_created", "alert_updated"]

    duplicate = await manager.process_scored_transaction(scored)
    assert duplicate["alert_id"] == alert["alert_id"]
