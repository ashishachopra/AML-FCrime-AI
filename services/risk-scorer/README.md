# Reference risk scorer

Applies `reference-policy-2.0.0`, a deterministic weighted policy with explicit overlays. It is not a trained or validated ML model. The service returns policy contributions and triggered rule IDs; it does not return SHAP values, probabilities, or fabricated performance metrics.

Endpoints: `POST /score`, `GET /scores/{txn_id}`, `GET /scores`, `GET /scorer/metadata`, and health routes.

Read the repository `MODEL_CARD.md` before changing weights or thresholds.

