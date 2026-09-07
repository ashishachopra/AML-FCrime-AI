# Gateway

`POST /v1/evaluate` provides an authenticated read-only feature/scorer preview for ingestors, analysts, and compliance officers. It applies one total `EVALUATION_TIMEOUT_SECONDS` deadline (default 1 second) and returns `504` without a score on timeout. It does not ingest the candidate, create an alert, call RabbitMQ, or invoke a language model. See the [root README](../../README.md#fast-transaction-preview) for a complete request and [generated contract](../../contracts/openapi/gateway-api.json).

External API boundary with signed JWT validation, expiry/issuer/audience checks, algorithm allow-listing, role authorization, request IDs, upload limits, upstream timeouts, aggregated transaction lookup, and compliance-only alert review/audit access. Review actors come from the verified JWT, never the request body.

The ASGI boundary checks actual streamed bytes before parsing (64 KiB JSON, 16 MiB batch), limits active requests (16), and applies body/processing deadlines. Authenticated subjects receive a local token bucket (120 requests/minute, burst 30); new tokens for the same subject share that bucket. See [resource settings and distributed limits](../../docs/AI_SECURITY_AND_COST.md#gateway-resource-limits).

Agent identities require issue/expiry times and a lifetime of at most 15 minutes. Narrative review and every case-status change require a verified human with MFA from the last five minutes, even for admins. Missing identity type defaults to `service`. Every alert edit requires `expected_revision`; conflicts return `409`. Assistants with the appropriate role can propose notes and assignment. [Identity and client migration](../../docs/AI_SECURITY_AND_COST.md#agent-credentials-and-human-review).

Endpoints: `POST /v1/batch` (ingestor), `GET /v1/alerts` and alert detail (analyst/compliance officer), `PATCH /v1/alerts/{alert_id}` and alert audit (compliance officer), `GET /v1/transactions/{txn_id}`, and health routes.

`GET /v1/alerts/statistics` (compliance officer/admin) exposes aggregate alert and AI attempt/circuit counters. It contains no raw evidence or narratives.

`AUTH_DISABLED=true` is an explicit isolated-development bypass and must not be used in deployed environments.
