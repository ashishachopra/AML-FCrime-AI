# AML Reference Pipeline

This repository is a security-conscious reference implementation of an anti-money-laundering event pipeline. It validates related customer, account, and transaction records; computes deterministic risk features; applies a transparent reference policy; stores deduplicated alerts and their audit history; and optionally drafts investigator narratives.

It is not a production AML system, a sanctions-screening service, a validated machine-learning model, or a regulatory filing tool. A real deployment requires institution-specific risk assessment, independent model/policy validation, current screening data, durable operational data stores, legal review, monitoring, and human investigators.

## Architecture

```text
JWT client
    |
    v
Gateway :8000 ---> Ingestion :8001 ---> RabbitMQ
                                         |
                                         v
                               Feature engine :8002
                                         |
                                         v
                              Reference scorer :8003
                                         |
                                         v
                               Alert manager :8005
                               (SQLite + audit log)

Graph analysis :8004 is a deterministic, opt-in API for entity-network exploration.
```

The event consumers use durable queues, persistent messages, publisher confirms, manual acknowledgements, bounded prefetch, and dead-letter queues. Services expose separate liveness and readiness checks where they have dependencies.

## What is implemented

- Strict Pydantic validation, timezone-aware timestamps, decimal monetary input, duplicate detection, referential-integrity checks, record limits, and bounded uploads.
- Signed JWT validation with expiry, issuer, audience, algorithm allow-listing, and role checks. Authentication can only be bypassed with the explicit `AUTH_DISABLED=true` development setting.
- Currency-aware threshold features. Threshold logic is disabled when the base-currency amount is unavailable instead of comparing unrelated currencies.
- A versioned FATF jurisdiction snapshot. FATF monitored-jurisdiction flags are risk inputs only; they are not sanctions matches or automatic customer decisions.
- Deterministic reference-policy scoring. The repository publishes no accuracy, precision, recall, AUC, confidence, or SHAP claims because it includes no trained artifact and no representative labeled validation set.
- Persistent, transaction-deduplicated alerts in SQLite with append-only audit events for alert creation, investigation changes, assignment, and narrative review.
- OpenAI Responses API narrative drafting with `store=false`, bounded output, pseudonymous safety identifiers, evidence-only instructions, no tools, deterministic template fallback, and mandatory human-review state.
- A deterministic NetworkX graph analyzer with real centrality/community calculations rather than random simulated outputs.
- Non-root, read-only application containers, dropped Linux capabilities, loopback-bound development ports, health-gated startup, persistent alert data, and Compose-mounted broker/JWT secrets.

## Quick start

Prerequisites: Docker with Compose and Python 3.12 for local development.

1. Copy the configuration template and generate untracked local secret files:

   ```bash
   cp example.env.txt .env
   python scripts/create_local_secrets.py
   ```

2. Validate and start the stack:

   ```bash
   docker compose config --quiet
   docker compose up --build --wait
   ```

3. Create a short-lived local token using the generated JWT secret:

   ```bash
   export JWT_SECRET_KEY="$(cat secrets/jwt_secret)"
   export JWT_ISSUER='aml-reference'
   export JWT_AUDIENCE='aml-api'
   python scripts/create_dev_token.py
   ```

4. Use the returned token with the gateway:

   ```bash
   curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/v1/alerts
   ```

Swagger UI is available at `http://127.0.0.1:8000/docs`. Internal service ports are published only on loopback for local inspection; expose only the gateway in a deployed environment.

### Optional OpenAI drafting

Leave `OPENAI_API_KEY` empty to use fact-grounded deterministic templates. When a key is configured, the alert manager uses the pinned model snapshot in `OPENAI_MODEL`. Generated content is always marked `DRAFT - HUMAN REVIEW REQUIRED`; no code path files or submits a SAR/STR.

Do not send production customer data to an external model until your organization has approved the data classification, retention, residency, contractual, access-control, and incident-response requirements. The implementation omits customer, account, and transaction identifiers from model input by default.

## Development

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/ruff check services tests
.venv/bin/ruff format --check services tests
```

On Windows, use `.venv\Scripts\python.exe` and `.venv\Scripts\ruff.exe`.

The automated suite covers validation, relationship integrity, currency handling, temporal leakage, deterministic scoring, honest scorer metadata, narrative grounding, alert audit/review, graph determinism, and JWT enforcement.

## API summary

| Service | Main endpoints |
|---|---|
| Gateway | `POST /v1/batch`, `GET /v1/alerts`, `GET /v1/transactions/{txn_id}` |
| Ingestion | `POST /batch`, `/health/live`, `/health/ready` |
| Feature engine | `POST /compute`, `GET /features/{txn_id}`, `GET /metadata` |
| Risk scorer | `POST /score`, `GET /scores/{txn_id}`, `GET /scorer/metadata` |
| Alert manager | `GET/PATCH /alerts/{id}`, `GET /alerts/{id}/audit`, `GET /alerts/statistics` |
| Graph analysis | `POST /graph/transactions`, `GET /graph/risk/{party_id}`, `GET /metadata` |

## Important limitations

- Feature, score, and graph state remain in process memory. Only alerts and their audit events are persisted.
- SQLite is appropriate for the reference stack, not a horizontally scaled case-management system.
- Batch event publication does not implement a transactional outbox; a broker failure partway through a batch can produce a partial batch. Event IDs, batch IDs, alert deduplication, and dead letters limit the impact, but production ingestion needs an outbox/inbox design.
- Country codes do not identify sanctioned persons or prohibited transactions. Integrate an authoritative, current entity-screening system and pass a reviewed `sanctions_match` signal separately.
- The FATF snapshot must be refreshed and independently approved after each relevant publication. Increased monitoring does not itself call for enhanced due diligence or de-risking.
- Risk weights and thresholds are illustrative. Validate conceptual soundness, data suitability, outcomes, calibration, drift, bias, overrides, and operating controls before use.
- Narrative approval records human review of a draft; it does not constitute a regulatory filing decision or submission.
- TLS termination, distributed rate limiting, centralized authorization, immutable enterprise audit storage, observability, backups, disaster recovery, data retention, and key rotation belong in the deployment platform.

## Guidance used for this upgrade

- [FATF Recommendations and risk-based approach](https://www.fatf-gafi.org/en/publications/Fatfrecommendations/Fatf-recommendations.html)
- [FATF high-risk and monitored jurisdictions](https://www.fatf-gafi.org/en/countries/black-and-grey-lists.html)
- [OFAC Sanctions List Service](https://ofac.treasury.gov/sanctions-list-service)
- [FinCEN SAR narrative guidance](https://www.fincen.gov/resources/statutes-regulations/guidance/sar-narrative-guidance-package)
- [Federal Reserve revised model-risk guidance (SR 26-2)](https://www.federalreserve.gov/supervisionreg/srletters/SR2602.htm)
- [NIST AI Risk Management Framework](https://www.nist.gov/itl/ai-risk-management-framework)
- [OWASP API Security Top 10](https://owasp.org/API-Security/)
- [RabbitMQ reliability guidance](https://www.rabbitmq.com/docs/reliability)
- [Docker Compose startup-order guidance](https://docs.docker.com/compose/how-tos/startup-order/)
- [OpenAI Responses API reference](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)

See [MODEL_CARD.md](MODEL_CARD.md) and [SECURITY.md](SECURITY.md) before extending or deploying the system.
