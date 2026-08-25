import hashlib
import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import networkx as nx


class GraphAnalyzer:
    """Deterministic entity-network analysis backed by NetworkX."""

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
        self.centrality_threshold = float(os.getenv("CENTRALITY_THRESHOLD", "0.7"))
        self.community_risk_threshold = float(os.getenv("COMMUNITY_RISK_THRESHOLD", "0.6"))
        self.max_connections = int(os.getenv("MAX_GRAPH_CONNECTIONS", "1000"))
        if not (
            0 <= self.centrality_threshold <= 1
            and 0 <= self.community_risk_threshold <= 1
            and self.max_connections > 0
        ):
            raise ValueError("graph thresholds and limits are invalid")
        self.entity_graph = nx.Graph()
        self.transaction_graph = nx.MultiDiGraph()
        self.fatf_call_for_action = self._country_set(
            "FATF_CALL_FOR_ACTION_COUNTRIES", self.DEFAULT_CALL_FOR_ACTION
        )
        self.fatf_increased_monitoring = self._country_set(
            "FATF_INCREASED_MONITORING_COUNTRIES", self.DEFAULT_INCREASED_MONITORING
        )
        self.jurisdiction_data_as_of = os.getenv("JURISDICTION_DATA_AS_OF", "2026-06-19")

    @staticmethod
    def _country_set(name: str, default: Iterable[str]) -> set[str]:
        raw = os.getenv(name)
        values = default if raw is None else raw.split(",")
        return {str(value).strip().upper() for value in values if str(value).strip()}

    @staticmethod
    def _parse_timestamp(value: str | datetime) -> datetime:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("transaction timestamp must include a UTC offset")
        return parsed.astimezone(timezone.utc)

    def _country_risk(self, country: str) -> float:
        if country in self.fatf_call_for_action:
            return 0.9
        if country in self.fatf_increased_monitoring:
            return 0.6
        return 0.2

    @staticmethod
    def _customer_risk(customer_data: Dict[str, Any] | None) -> float:
        if not customer_data:
            return 0.5
        risk = {"basic": 0.5, "standard": 0.25, "enhanced": 0.1}.get(
            customer_data.get("kyc_level"), 0.5
        )
        if customer_data.get("pep_flag"):
            risk += 0.25
        return min(risk, 1.0)

    def add_transaction_to_graph(
        self,
        transaction_data: Dict[str, Any],
        customer_data: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        required = {"txn_id", "account_id", "counterparty_id", "amount", "currency", "timestamp"}
        missing = sorted(required - transaction_data.keys())
        if missing:
            raise ValueError(f"graph transaction is missing fields: {missing}")
        amount = float(transaction_data["amount"])
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("amount must be a positive finite number")
        timestamp = self._parse_timestamp(transaction_data["timestamp"])
        country = str(transaction_data.get("counterparty_country", "ZZ")).upper()
        customer_id = str(
            transaction_data.get("customer_id")
            or (customer_data or {}).get("customer_id")
            or f"UNKNOWN_CUSTOMER:{transaction_data['account_id']}"
        )
        account_id = str(transaction_data["account_id"])
        counterparty_id = str(transaction_data["counterparty_id"])
        txn_id = str(transaction_data["txn_id"])

        new_nodes = {customer_id, account_id, counterparty_id} - set(self.entity_graph)
        if self.entity_graph.number_of_nodes() + len(new_nodes) > self.max_connections:
            raise ValueError("graph node limit reached")

        self.entity_graph.add_node(
            customer_id,
            node_type="customer",
            risk_score=self._customer_risk(customer_data),
        )
        self.entity_graph.add_node(account_id, node_type="account", risk_score=0.1)
        self.entity_graph.add_node(
            counterparty_id,
            node_type="counterparty",
            risk_score=self._country_risk(country),
            country=country,
        )
        self.entity_graph.add_edge(
            customer_id,
            account_id,
            relationship="owns",
            transaction_count=0,
            total_amount=0.0,
        )
        edge = self.entity_graph.get_edge_data(account_id, counterparty_id, default={})
        self.entity_graph.add_edge(
            account_id,
            counterparty_id,
            relationship="transacts_with",
            transaction_count=int(edge.get("transaction_count", 0)) + 1,
            total_amount=float(edge.get("total_amount", 0.0)) + amount,
            last_transaction=timestamp,
        )
        self.transaction_graph.add_edge(
            account_id,
            counterparty_id,
            key=txn_id,
            txn_id=txn_id,
            amount=amount,
            currency=str(transaction_data["currency"]).upper(),
            timestamp=timestamp,
        )
        return {
            "txn_id": txn_id,
            "nodes_added": sorted(new_nodes),
            "total_nodes": self.entity_graph.number_of_nodes(),
            "total_transactions": self.transaction_graph.number_of_edges(),
        }

    async def analyze_party_risk(self, party_id: str) -> Optional[Dict[str, Any]]:
        if party_id not in self.entity_graph:
            return None
        centrality = nx.betweenness_centrality(self.entity_graph, normalized=True)
        centrality_score = round(float(centrality.get(party_id, 0.0)), 6)
        community = self._community_for(party_id)
        community_risk = round(
            sum(float(self.entity_graph.nodes[node].get("risk_score", 0.5)) for node in community)
            / len(community),
            6,
        )
        cluster_id = (
            "cluster-"
            + hashlib.sha256("|".join(sorted(community)).encode("utf-8")).hexdigest()[:12]
        )
        connected = self._connected_parties(party_id)
        return {
            "party_id": party_id,
            "cluster_id": cluster_id,
            "centrality_score": centrality_score,
            "community_risk": community_risk,
            "connected_parties": connected,
            "graph_alerts": self._alerts(party_id, centrality_score, community_risk),
            "analyzed_at": datetime.now(timezone.utc),
        }

    def _community_for(self, party_id: str) -> set[str]:
        if self.entity_graph.number_of_nodes() < 2 or self.entity_graph.number_of_edges() == 0:
            return {party_id}
        communities = nx.community.greedy_modularity_communities(self.entity_graph)
        return next((set(group) for group in communities if party_id in group), {party_id})

    def _connected_parties(self, party_id: str) -> List[Dict[str, Any]]:
        values = []
        for neighbor in self.entity_graph.neighbors(party_id):
            edge = self.entity_graph[party_id][neighbor]
            count = int(edge.get("transaction_count", 0))
            strength = 1.0 if edge.get("relationship") == "owns" else min(count / 5.0, 1.0)
            risk = float(self.entity_graph.nodes[neighbor].get("risk_score", 0.5))
            values.append(
                {
                    "party_id": neighbor,
                    "relationship_strength": round(strength, 6),
                    "risk_contribution": round(strength * risk, 6),
                }
            )
        return sorted(values, key=lambda item: (-item["risk_contribution"], item["party_id"]))[:20]

    def _alerts(
        self, party_id: str, centrality_score: float, community_risk: float
    ) -> List[Dict[str, Any]]:
        alerts = []
        if centrality_score >= self.centrality_threshold:
            alerts.append(
                {
                    "alert_type": "high_centrality",
                    "severity": "high",
                    "description": f"Party {party_id} exceeds the configured centrality threshold.",
                    "confidence": centrality_score,
                }
            )
        if community_risk >= self.community_risk_threshold:
            alerts.append(
                {
                    "alert_type": "suspicious_cluster",
                    "severity": "high",
                    "description": f"Party {party_id} belongs to a community above the risk threshold.",
                    "confidence": community_risk,
                }
            )
        return alerts

    def get_graph_statistics(self) -> Dict[str, Any]:
        return {
            "total_nodes": self.entity_graph.number_of_nodes(),
            "total_relationships": self.entity_graph.number_of_edges(),
            "total_transactions": self.transaction_graph.number_of_edges(),
            "graph_density": nx.density(self.entity_graph),
        }

    def metadata(self) -> Dict[str, Any]:
        return {
            "algorithm": "NetworkX betweenness centrality and greedy modularity communities",
            "deterministic": True,
            "jurisdiction_data_as_of": self.jurisdiction_data_as_of,
            "limitations": [
                "The in-memory graph is a reference implementation and is not horizontally shared.",
                "Community risk is a prioritization signal, not evidence of suspicious activity.",
            ],
        }
