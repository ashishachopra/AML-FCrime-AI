# Graph analysis

A deterministic in-memory NetworkX reference service. Transactions are added with explicit customer, account, and counterparty identifiers. Analysis uses betweenness centrality and greedy modularity communities; no random scores or relationships are generated.

Endpoints: `POST /graph/transactions`, `GET /graph/risk/{party_id}`, `GET /graph/statistics`, `GET /metadata`, and `GET /health`.

The graph is process-local and must be replaced with shared, durable graph storage for horizontal or production deployment.

