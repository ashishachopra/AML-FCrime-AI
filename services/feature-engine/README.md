# Feature engine

Consumes validated records and computes deterministic amount, currency-availability, velocity, structuring, FATF-jurisdiction, KYC/PEP-data-quality, account-age, and temporal features.

Threshold features apply only to the configured base currency or to an upstream converted amount. FATF flags are versioned risk inputs, never sanctions matches or automatic decisions.

Endpoints: `POST /compute`, `GET /features/{txn_id}`, `GET /transactions/{txn_id}`, `GET /metadata`, and health routes.

