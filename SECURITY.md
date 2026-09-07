# Security and responsible-use notes

## Reporting a vulnerability

Do not include customer data, credentials, tokens, or live suspicious-activity details in an issue. Share a minimal reproduction through the repository owner's private security channel.

## Secure deployment checklist

- Keep `AUTH_DISABLED=false`; rotate a random JWT key of at least 32 bytes and broker credentials through a managed secret store.
- Terminate TLS at a trusted gateway, expose only port 8000, enforce centralized identity and fine-grained authorization, and add distributed rate limits and request-size limits at the edge.
- Remove loopback debug port publications from the production Compose override.
- Run behind network policies that restrict service-to-service and outbound traffic. Permit the alert manager to reach OpenAI only when approved.
- Replace SQLite and in-memory stores with access-controlled, encrypted, backed-up operational databases and immutable audit retention appropriate to the governing jurisdiction.
- Add malware scanning and stronger content controls if ingestion expands beyond JSON.
- Centralize structured logs and traces while redacting tokens, PII, account data, narratives, and secrets. Alert on authentication failures, dead letters, partial batches, review backlogs, and policy-data staleness.
- Pin container images by digest, generate an SBOM, scan dependencies/images, sign artifacts, and enforce reviewed build provenance.
- Test restore, broker failure, poison messages, partial publication, duplicate delivery, OpenAI outage, key rotation, and incident response.

## AML-specific boundaries

- Never infer a sanctions match from a country code. Screen relevant names, identifiers, ownership/control, vessels, addresses, and other required attributes against authoritative, current lists under reviewed policy.
- Never interpret a FATF monitored jurisdiction as proof of suspicious activity or as an automatic reason to reject a customer or transaction.
- Treat PEP status as a risk factor requiring proportionate controls, not evidence of wrongdoing.
- Require documented human review for alert disposition and all reporting decisions.
- Do not describe generated narratives as regulator-ready. Verify every fact against the case record and applicable filing instructions.

## OpenAI data boundary

Optional drafting is disabled by default. The implementation uses the Responses API with `store=false`, no tools, bounded typed input/output, no SDK retries, persistent attempt allowances and a circuit breaker. Alerts/templates are committed before model calls. Customer/account/transaction identifiers and free-form source strings are omitted from model input. Those technical controls do not replace organizational approval of data processing, residency, retention, access, contracts, privacy, model risk, and regulatory obligations.

Read [AI_SECURITY_AND_COST.md](docs/AI_SECURITY_AND_COST.md) for tested abuse cases, configuration and recovery limits. Paid attempt counts are not dollar guarantees; a local timeout cannot prove provider work stopped. Protect the authoritative alert database and provider credentials. Separate databases multiply allowances; per-process gateway quotas require a shared edge replacement when scaling.

## Agent and review boundary

The gateway requires short-lived typed agent identities and fresh issuer-asserted human MFA for narrative reviews and every case-status change. All edits compare the alert revision atomically; review audit entries identify the narrative hash. Automation may assist with authorized notes and assignment, but cannot use an admin role to finalize or reopen cases. The local token helper's simulated MFA and `AUTH_DISABLED=true` are development bypasses.

Internal APIs trust the service network; direct access bypasses gateway identity enforcement. Claims cannot distinguish an agent borrowing a real human token. The reference is single-organization and has no tenant-level authorization. Treat all narratives and investigator notes as untrusted text in downstream UIs/agents; do not execute them, fetch embedded links, or treat them as instructions.
