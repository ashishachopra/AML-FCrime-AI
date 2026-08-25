# Alert manager

Creates transaction-deduplicated alerts, persists them in SQLite, records append-only audit events, and produces fact-grounded narrative drafts for high-risk alerts. Drafts use the OpenAI Responses API when configured and deterministic templates otherwise.

Every narrative is marked for human review. Approval records a review event; it does not file or decide whether to file a SAR/STR.

Endpoints: `GET /alerts`, `GET/PATCH /alerts/{id}`, `GET /alerts/{id}/audit`, `GET /alerts/statistics`, and health routes.

