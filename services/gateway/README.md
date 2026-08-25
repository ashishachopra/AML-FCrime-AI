# Gateway

External API boundary with signed JWT validation, expiry/issuer/audience checks, algorithm allow-listing, role authorization, request IDs, upload limits, upstream timeouts, aggregated transaction lookup, and compliance-only alert review/audit access. Review actors come from the verified JWT, never the request body.

Endpoints: `POST /v1/batch` (ingestor), `GET /v1/alerts` and alert detail (analyst/compliance officer), `PATCH /v1/alerts/{alert_id}` and alert audit (compliance officer), `GET /v1/transactions/{txn_id}`, and health routes.

`AUTH_DISABLED=true` is an explicit isolated-development bypass and must not be used in deployed environments.
