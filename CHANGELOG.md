# Changelog

## 3.1.0 — 2026-09-07

- Persist template alerts before optional AI work; add atomic per-transaction attempt reservations, durable UTC daily/concurrency limits, zero SDK retries, total timeouts and a circuit breaker. Paid drafting is now opt-in.
- Build model input from typed facts and allowlisted indicators; reject incomplete or out-of-contract responses and preserve human-reviewed drafts when AI finishes late.
- Require verified human identity with fresh MFA for narrative reviews and all case-status changes, including reopening. Bound agent token lifetimes to 15 minutes and reject malformed/missing identity claims.
- Require `expected_revision` for alert updates; record review revisions and narrative hashes in the audit. Replace whole-history Python decoding with SQL pagination and aggregation.
- Enforce actual streamed-byte limits, bounded active requests, read/processing deadlines and per-subject quotas at the gateway. Expose authenticated aggregate AI usage/circuit statistics.
- Add adversarial tests and update the README flowchart, deployment defaults, security/cost guide and generated alert-manager/gateway contracts. Feature and scoring policy versions remain 3.0.0.

### Migration notes

Clients must submit the current alert revision with every edit. Trusted issuers must supply human/MFA claims for status changes and reviews; missing identity type defaults to service. Optional AI now requires explicit `SAR_GENERATION_ENABLED=true` plus a key. Preserve the alert database across restarts and reconcile budgets when restoring backups. See [settings and migration](docs/AI_SECURITY_AND_COST.md).

## 3.0.0 — 2026-09-07

- Add robust amount baselines and temporal fan-in, fan-out, pass-through, and reciprocal-transfer features to the reference policy.
- Replace service-wide history scans with bounded SQLite indexes, persist immutable feature snapshots, and add a transactional feature outbox with stable retry identity.
- Fix mixed-currency historical amounts and use decimal threshold comparisons; disclose cold baselines, capped history, and observed late events.
- Add authenticated `/v1/evaluate` with a total deadline and read-only feature/scorer previews. Route incomplete evidence to a separate human data-quality review without raising the risk score.
- Add replay, recovery, network, preview, integration and overload tests; reproducible latency benchmarks; generated OpenAPI contracts; Windows/Linux CI; and an isolated RabbitMQ smoke stack.
- Expand the README with a rendered-and-checked Mermaid flowchart, document exact methods and scaling boundaries, and adopt Apache-2.0 with contributor guidance.

### Migration notes

Scores change under `hybrid-reference-policy-3.0.0`; revalidate policy thresholds. Feature reads now return the original stored snapshot. The feature service requires a persistent writable database and one authoritative worker. Existing JSON records remain accepted; network features require explicit canonical outbound transfer identifiers. See [the migration guide](docs/HYBRID_MONITORING.md#migration).
