# Contributing

Contributions are licensed under [Apache-2.0](LICENSE). Submit only code and data you have the right to contribute; retain third-party notices and license terms. The license includes an express patent grant subject to its conditions. No contributor license agreement or sign-off bot is currently configured.

Use Python 3.12 or 3.13 and an isolated virtual environment:

```bash
python -m venv .venv
python -m pip install -r requirements-dev.txt
python -m pytest
python -m ruff check services tests scripts
python -m ruff format --check services tests scripts
python scripts/export_openapi.py --check
docker compose --env-file example.env.txt config --quiet
```

Activate the environment first, or prefix Python/Ruff paths with `.venv/bin/` (Windows: `.venv\Scripts\`). Generate local secrets with `python scripts/create_local_secrets.py` before validating Compose. Unit and ASGI integration tests need no broker or API key. Use synthetic fixtures; never commit customer data, secrets, databases, or private investigation material.

For a real RabbitMQ/HTTP integration check, run the isolated smoke stack. It publishes no host ports, uses synthetic test credentials and evidence, and disables external AI calls:

```bash
docker compose -f docker/compose.smoke.yml -p aml-hybrid-smoke up --build --abort-on-container-exit --exit-code-from smoke
docker compose -f docker/compose.smoke.yml -p aml-hybrid-smoke down --volumes --remove-orphans
```

The smoke test covers batch validation, broker consumers, network-feature computation, scoring, persisted alerts, and gateway preview isolation. Cleanup affects only this test project's containers and volumes. To run the unit suite in the built Python 3.12 image, use `docker compose -f docker/compose.smoke.yml -p aml-hybrid-smoke run --rm --no-deps smoke python -m pytest -q`.

Keep pull requests focused on a concrete behavior and include relevant verification. Changes to feature/scoring semantics need version updates, time/currency/data-quality regression tests, explicit evidence requirements, and a model-card update. Do not add accuracy, confidence, probability, compliance, or latency claims without reproducible supporting evidence. Avoid loading pickled or executable model artifacts from untrusted sources. Optional model integrations must preserve deterministic operation and review controls when unavailable.

Run `python scripts/export_openapi.py` after changing the ingestion, feature, scorer, alert-manager, or gateway API. It generates complete JSON contracts and YAML path-reference entry points without starting services. CI rejects drift. Test response serialization and failure behavior as well as calculation logic. Performance changes should include a reproducible benchmark with the measured scope and environment; do not make shared-runner unit tests depend on tight wall-clock assertions.

Security changes must preserve [AI cost and identity boundaries](docs/AI_SECURITY_AND_COST.md). Test malformed signed claims, agent authority, revisions, replay, cancellation, resource exhaustion and provider failures using synthetic identities and mocked model calls. Do not add paid calls to default tests or CI; an API key must not be needed to contribute.

The reference deployment deliberately uses a single authoritative feature worker and bounded indexed queries. Read [the scaling and event semantics](docs/HYBRID_MONITORING.md) before adding workers, stores, retention, backfills, or distributed consumers. Report security issues according to [SECURITY.md](SECURITY.md).
