# Ingestion service

Validates three related JSON arrays (customers, accounts, transactions), enforces size/count limits, rejects duplicate IDs and broken references, validates timezone-aware timestamps and decimal monetary values, then publishes persistent CloudEvents-style messages with publisher confirms.

Endpoints: `POST /batch`, `GET /health/live`, and `GET /health/ready`.

This reference publisher can produce a partial batch if the broker fails mid-loop. A production implementation should use a transactional outbox and durable batch status.

