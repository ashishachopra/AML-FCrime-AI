# Alert manager

Creates transaction-deduplicated alerts, persists them in SQLite, records append-only audit events, and produces narrative drafts for high-risk alerts. A deterministic template is committed before optional AI work. Paid drafting requires explicit enablement plus a key; it is disabled by default.

SQLite also stores per-transaction attempt identity, daily call allowances, active reservations and circuit-breaker state. Defaults allow 100 attempts per UTC day, two active reservations, an eight-second deadline, 8 KiB of typed input and 700 output tokens. SDK retries are always zero. Unavailable budget, outage, cancellation or malformed output leaves the template usable; replay never buys another draft. [Configuration and recovery semantics](../../docs/AI_SECURITY_AND_COST.md#paid-drafting-lifecycle).

Every narrative is marked for human review. Approval records a review event; it does not file or decide whether to file a SAR/STR.

Every update requires `expected_revision`; stale updates return `409`. Narrative review audits include the reviewed text's hash. Late AI cannot replace approved/rejected text or text in a closed case. The gateway enforces human/MFA authorization for review and status changes; this internal API trusts the private service network and must not be publicly exposed.

Use a persistent writable `ALERT_DB_PATH` (direct default `data/alerts.db`; Compose `/data/alerts.db`). Startup adds indexes and budget tables without deleting existing alerts. Listing and counts support an exact `txn_id` filter and execute in SQL before decoding the requested page. Aggregate statistics include AI attempt/circuit counters; aggregate queries still scan history.

Endpoints: `GET /alerts`, `GET/PATCH /alerts/{id}`, `GET /alerts/{id}/audit`, `GET /alerts/statistics`, and health routes.
