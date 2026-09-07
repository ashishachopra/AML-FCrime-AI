# Feature engine

Consumes validated records and computes deterministic amount, currency-availability, velocity, structuring, FATF-jurisdiction, KYC/PEP-data-quality, account-age, and temporal features.

Version 3 adds robust account amount baselines and bounded temporal network signals, backed by indexed SQLite evidence and immutable snapshots. Transaction, snapshot, and outgoing event commit atomically. The broker publisher retries a persisted event ID after failure; conflicting transaction replays are rejected. HTTP `/compute` is read-only and does not modify history or create events.

Threshold features apply only to the configured base currency or to an upstream converted amount. FATF flags are versioned risk inputs, never sanctions matches or automatic decisions.

Endpoints: `POST /compute`, `GET /features/{txn_id}`, `GET /transactions/{txn_id}`, `GET /metadata`, and health routes.

Run one process with a writable `FEATURE_DB_PATH` (Compose uses `/data/features.db` on `feature_data`). Query work is capped through `FEATURE_MAX_HISTORY_ROWS` and `FEATURE_MAX_NETWORK_ROWS`; truncation is explicit. Store work runs outside the event loop with bounded admission. Metadata exposes queue/storage counts and active feature settings. Feature reads return original snapshots, including their original computation time.

Network evidence needs explicit outbound transfers and stable `counterparty_account_id` values. Do not add competing workers with separate databases. See [the operational and scaling guide](../../docs/HYBRID_MONITORING.md) and [generated API](../../contracts/openapi/feature-engine-api.json).
