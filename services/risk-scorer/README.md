# Reference risk scorer

Applies `hybrid-reference-policy-3.0.0`, a deterministic weighted policy with explicit overlays combining transaction rules, robust amount baselines, and temporal network indicators. It is not a trained or validated classifier. The service returns policy contributions, triggered rule IDs, versions, and `review_recommended`; it does not return SHAP values, probabilities, or fabricated performance metrics. Incomplete evidence can request human review without increasing the numeric risk score.

Endpoints: `POST /evaluate` (read-only preview), `POST /score` (updates the process-local cache), `GET /scores/{txn_id}`, `GET /scores`, `GET /scorer/metadata`, and health routes. Preview neither overwrites an ingested score nor emits an event. Gateway clients use the authenticated `POST /v1/evaluate` composition.

Read the repository `MODEL_CARD.md` before changing weights or thresholds.
