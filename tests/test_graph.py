import pytest
from conftest import load_source_module

graph_module = load_source_module("graph_analysis_test", "services/graph-analysis/graph.py")


@pytest.mark.asyncio
async def test_graph_analysis_is_deterministic() -> None:
    analyzer = graph_module.GraphAnalyzer()
    transaction = {
        "txn_id": "T1",
        "account_id": "A1",
        "customer_id": "C1",
        "counterparty_id": "CP1",
        "timestamp": "2026-08-25T10:00:00Z",
        "amount": 1000,
        "currency": "USD",
        "counterparty_country": "IR",
    }
    analyzer.add_transaction_to_graph(
        transaction, {"customer_id": "C1", "kyc_level": "standard", "pep_flag": False}
    )
    first = await analyzer.analyze_party_risk("A1")
    second = await analyzer.analyze_party_risk("A1")
    assert first["centrality_score"] == second["centrality_score"]
    assert first["community_risk"] == second["community_risk"]
    assert first["connected_parties"] == second["connected_parties"]
    assert analyzer.metadata()["deterministic"] is True
