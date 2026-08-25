# Reference scorer model card

## Identity and intended use

`reference-policy-2.0.0` is a deterministic weighted policy implemented in `services/risk-scorer/scorer.py`. Despite the historical service name, it is not a trained statistical or machine-learning model. It exists to demonstrate traceable feature-to-alert data flow and to provide a testable baseline for institution-specific development.

The output may prioritize transactions for human review in demonstrations. It must not decide whether activity is illegal, whether a customer should be exited, whether a transaction should be blocked, or whether a SAR/STR should be filed.

## Inputs and outputs

Inputs are numeric transaction, velocity, jurisdiction, KYC, PEP, account-age, temporal, and independently supplied screening features. Outputs include a risk score, configured category, data-quality score, version, sorted policy contributions, and triggered rule identifiers.

`feature_contributions` are direct normalized-value × policy-weight products. They are not SHAP values, causal explanations, probabilities, or calibrated effect sizes.

## Validation status

`not_validated_for_production`

This repository contains no representative labeled development set, holdout set, sampling design, trained artifact, calibration study, back-test, threshold analysis, false-positive/false-negative cost analysis, stability study, subgroup analysis, independent validation, change approval, or production monitoring evidence. Performance metrics are therefore intentionally `null`.

## Required work before use

- Document the risk assessment, intended use, users, prohibited uses, and decision consequences.
- Establish data lineage, quality controls, population representativeness, labels, time-based holdouts, and leakage tests.
- Validate conceptual soundness, implementation, outcomes, calibration, thresholds, overrides, stability, bias, and limitations independently of development.
- Benchmark against existing controls and measure incremental value, not just aggregate accuracy.
- Define monitoring thresholds, drift detection, alert-quality review, periodic validation, change control, rollback, incident response, and accountable owners.
- Keep jurisdiction and sanctions data current, separately governed, and traceable to authoritative sources.
- Preserve human authority over investigations, customer actions, and regulatory reporting.
