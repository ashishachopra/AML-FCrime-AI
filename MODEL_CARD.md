# Reference scorer model card

## Identity and intended use

`hybrid-reference-policy-3.0.0` is a deterministic weighted policy implemented in `services/risk-scorer/scorer.py`. Its companion `hybrid-features-3.0.0` combines reference rules, a robust nonparametric amount baseline fitted to observed account history, and one-hour network motifs. There is no trained classifier or validated ML artifact. It demonstrates traceable feature-to-alert data flow and provides a testable baseline for institution-specific development.

The output may prioritize transactions for human review in demonstrations. It must not decide whether activity is illegal, whether a customer should be exited, whether a transaction should be blocked, or whether a SAR/STR should be filed.

## Inputs and outputs

Inputs are numeric transaction, velocity, jurisdiction, KYC, PEP, account-age, temporal, and independently supplied screening features. Outputs include a risk score, configured category, data-quality score, version, sorted policy contributions, and triggered rule identifiers.

New inputs include log-amount median/MAD anomaly scores, fan-out, pass-through, reciprocal flow, baseline readiness, evidence-cap flags, and observed late-event flags. The reference overlays prioritize pass-through, reciprocal transfers, and combined amount/fan-out anomalies for review. `review_recommended` also routes incomplete data for review without claiming criminal risk. Low-score evidence gaps create a data-quality alert. Missing graph IDs and cold baselines are disclosed; they are not independently treated as suspicious behavior.

Feature snapshots are immutable and carry their computation time/version. Historical windows exclude future and same-time records. Current transactions do not train their own baseline, and amounts from different currencies are not pooled. See [exact equations, tolerances, and evidence limitations](docs/HYBRID_MONITORING.md). These choices change score distributions from version 2 and require new validation; old and new scores are not automatically comparable.

`feature_contributions` are direct normalized-value × policy-weight products. They are not SHAP values, causal explanations, probabilities, or calibrated effect sizes.

## Validation status

`not_validated_for_production`

This repository contains no representative labeled development set, holdout set, sampling design, trained artifact, calibration study, back-test, threshold analysis, false-positive/false-negative cost analysis, stability study, subgroup analysis, independent validation, change approval, or production monitoring evidence. Performance metrics are therefore intentionally `null`.

Automated tests establish implementation behavior on synthetic examples, not effectiveness against actual laundering. Latency benchmarks measure local feature computation only. Median/MAD baselines can be contaminated or drift; sparse histories, seasonality, business-account patterns, missing cross-account records, legitimate refunds, and delayed data can change outcomes. Bounded queries explicitly disclose truncation; the policy's quality score is an illustrative coverage measure, not statistical confidence.

The 3.1 service update leaves scoring policy and feature versions unchanged. Its [AI and agent safeguards](docs/AI_SECURITY_AND_COST.md) bound optional drafting attempts, constrain model input, and bind human review to the current narrative revision. They neither validate generated facts nor establish detection effectiveness. External model output remains an untrusted draft; local fallback remains available before inference begins.

## Required work before use

- Document the risk assessment, intended use, users, prohibited uses, and decision consequences.
- Establish data lineage, quality controls, population representativeness, labels, time-based holdouts, and leakage tests.
- Validate conceptual soundness, implementation, outcomes, calibration, thresholds, overrides, stability, bias, and limitations independently of development.
- Benchmark against existing controls and measure incremental value, not just aggregate accuracy.
- Define monitoring thresholds, drift detection, alert-quality review, periodic validation, change control, rollback, incident response, and accountable owners.
- Keep jurisdiction and sanctions data current, separately governed, and traceable to authoritative sources.
- Preserve human authority over investigations, customer actions, and regulatory reporting.
