# Agent identity, AI costs, and resource boundaries

Implemented in the September 7, 2026 update. The deterministic hybrid still performs all scoring locally. Optional generative AI assists narrative drafting only; it has no tools, payment authority, filing endpoint, or access to reviewer credentials.

The threat model includes malicious or faulty API clients, automated request loops, repeated broker deliveries, provider outages, prompt-bearing source fields, and investigators reviewing a draft while an AI response is still arriving. OWASP identifies excessive consumption and denial of wallet as operational risks; this implementation uses deterministic admission controls around paid work. [OWASP unbounded consumption](https://genai.owasp.org/llmrisk/llm102025-unbounded-consumption/).

## What changed and why

| Failure or abuse | Enforced behavior | Verification |
|---|---|---|
| Duplicate deliveries buy duplicate narratives | Commit the alert/template using unique transaction identity before inference; only the creator may attempt AI | Concurrent replay test; shared SQLite reservation race |
| A provider outage drains budget or stalls evidence persistence | Persist the template first; charge attempts before calls; disable retries; apply a deadline and circuit breaker | Failure, timeout, cancellation, restart and cooldown tests |
| Novel transactions bypass replay protection to exhaust spend | Durable UTC daily allowance and active-reservation cap | Daily cap across database connections; zero-budget test |
| Instructions arrive inside evidence | Only validated amount, currency, country, timestamp, score and allowlisted indicators enter model context | Prompt-bearing rules, versions and feature keys are excluded |
| A machine uses an administrative role to finalize a case | Final actions require issuer-asserted human identity and recent MFA as well as the review role | Agent/service/admin denial tests |
| The model changes a draft after the reviewer loads it | Every mutation compares the submitted revision inside a database write transaction | Stale revisions rejected; late AI preserves human decisions |
| Requests consume excessive parsing memory or upstream capacity | Count actual streamed bytes before parsing, cap active requests, limit body-read time and per-subject admissions | Lying/missing content length, slow body, cancellation and quota tests |
| Alert retrieval repeatedly materializes every narrative | SQL filtering, pagination, counts and aggregation; indexed transaction lookup | Requested page alone is decoded in Python |

These choices apply OWASP's guidance on least privilege, treating evidence as untrusted, and binding high-impact approvals to the reviewed action. They are implementation controls, not a claim that prompt injection or financial abuse has been eliminated. [OWASP AI Agent Security](https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html).

## Paid drafting lifecycle

1. Build and atomically persist a deduplicated alert and, when eligible, a deterministic narrative template. The stored template can be reviewed immediately.
2. If explicitly enabled and a model client is configured, construct the narrow input contract. Unknown rules, source annotations, arbitrary feature names, versions, notes, assignments and customer/account/transaction IDs do not enter the model input.
3. In the same alert database, reserve the transaction's one lifetime attempt, one UTC-day call, and an active slot. Concurrent database connections cannot exceed the shared allowance. A denial leaves the stored template and records its reason; it does not queue inference.
4. Call the Responses API once with an overall local deadline, no SDK retries, no tools, `tool_choice="none"`, `background=false`, `store=false`, `truncation="disabled"`, and bounded output tokens. These request controls are supported by the [Responses API reference](https://developers.openai.com/api/reference/python/resources/responses/methods/create).
5. Accept completed, nonempty text within the output-size and control-character bounds. Incomplete responses, unexpected tool output, timeouts and failures keep the template. Responses are never executed, fetched as URLs, or used to change scores or review decisions.
6. Under a write transaction, apply accepted text only to a still-pending draft in an open/investigating case, preserving investigator notes and assignment. Increment the revision and record the outcome.

Cancelled or uncertain calls consume the allowance. No automatic retry or refund exists. If the process crashes after saving the template but before finishing AI work, broker replay returns that template without another model attempt. An abandoned active reservation expires after the configured timeout plus five seconds; the transaction attempt and daily charge remain. A process crash can leave `sar_generation_reason=template_ready`; it is not evidence that a provider call never occurred.

| Setting | Default | Meaning |
|---|---:|---|
| `SAR_GENERATION_ENABLED` | `false` | Must be explicitly enabled, with a key, for optional paid drafting |
| `AI_DAILY_CALL_LIMIT` | `100` | Maximum reserved attempts per UTC day in this database; `0` prevents new calls |
| `AI_MAX_CONCURRENT` | `2` | Maximum nonexpired active reservations |
| `AI_MAX_INPUT_BYTES` | `8192` | UTF-8 bytes of fixed instructions plus serialized facts |
| `OPENAI_MAX_OUTPUT_TOKENS` | `700` | Provider output-token limit; configuration capped at 2000 |
| `OPENAI_TIMEOUT_SECONDS` | `8` | Total local time allowed for one model call; configuration capped at 60 seconds |
| `AI_FAILURE_THRESHOLD` | `3` | Consecutive completed failures that open the circuit |
| `AI_COOLDOWN_SECONDS` | `300` | Cooldown before new transactions may try the provider again |
| `ALERT_DB_PATH` | `data/alerts.db` directly; `/data/alerts.db` in Compose | Persist alerts, revisions, audit, daily budgets, circuit and attempts together |

The legacy `OPENAI_MAX_RETRIES` setting is ignored; retries are always zero. Settings are loaded at service startup; restart/recreate the alert service after changing them. Turning drafting off does not cancel calls already accepted by the provider.

`GET /v1/alerts/statistics` is available to compliance officers/admins and includes `ai.reserved_calls`, `daily_call_limit`, `active_reservations`, `concurrency_limit`, `consecutive_failures`, `circuit_open`, and `circuit_open_until`. Alert details expose `sar_generation_reason` for fallback investigation. Monitor denied calls and draft-review backlogs as well as paid attempts. Statistics return aggregates without raw customer data; SQL aggregates still scan stored rows and are not constant-time metrics.

This is an **attempt allowance, not a dollar cap**. Provider prices and accounting differ; local cancellation does not prove that remote processing stopped. Use a dedicated restricted provider project/key, provider-side spending controls where available, billing alerts, and outbound network restrictions. Separate alert databases each get their own allowance; replicas must share an authoritative transactional budget implementation. Deleting/restoring old database state can reset usage accounting, so reconcile provider usage during recovery. Do not use `:memory:` for paid deployments.

## Agent credentials and human review

The gateway requires signed JWTs containing `sub`, `iss`, `aud`, and `exp`, matching configured issuer/audience and an allowed HMAC algorithm. Role checks remain mandatory. It rejects oversized tokens and malformed role, identity, method and time claims. Identity classification must be assigned by the trusted issuer; a client-supplied body field cannot grant human authority.

| Claim | Contract |
|---|---|
| `principal_type` | `human`, `service`, or `agent`; omitted means `service` |
| `roles` | A bounded list of role strings; the legacy single-string role is also accepted |
| `iat`, `exp` | Agent tokens require numeric issue/expiry times and lifetime at most 900 seconds |
| `amr` | A bounded list of authentication methods; final human actions require `mfa` |
| `auth_time` | Numeric time of authentication; final human actions require age 0–300 seconds |

Automation can use permitted read, ingestion, preview, note and assignment operations. `sar_review_status=approved/rejected` and every `status` change additionally require `principal_type=human`, recent MFA, and the compliance-officer/admin role. Protecting transitions to `open` and `investigating` also prevents automation from undoing a human's closure. An admin role never bypasses this condition. These controls reflect the need for accountable agent identities and explicitly bounded authority discussed by [NIST on August 27, 2026](https://www.nist.gov/blogs/cybersecurity-insights/back-future-why-agentic-ai-needs-strong-identity-foundation).

The token issuer must map real MFA freshness into these claims. A token label alone cannot prove that a person is present or distinguish an agent holding a stolen human token. The reference HS256 issuer/key model is intended for local integration; production needs managed identity, short-lived scoped credentials, revocation, rotation and service authentication. Never give the signing key or human credentials to an agent.

Every `PATCH /v1/alerts/{id}` now requires an integer `expected_revision` obtained from the current alert detail. Example:

```json
{"expected_revision": 2, "sar_review_status": "approved", "investigation_notes": "Verified this draft against the case record."}
```

A stale revision returns `409`. Reload the alert and re-review the changed content before resubmitting; do not auto-retry approval with a fresh revision. The audit stores old/new revisions, the verified actor, and the SHA-256 hash of the reviewed narrative. An approved/rejected narrative cannot subsequently be replaced by AI. This is optimistic concurrency protection, not an immutable external audit ledger or proof of an individual's diligence.

For isolated development, the token helper defaults to a 15-minute service token with `analyst,ingestor` roles. To exercise human review locally after configuring the test signing environment:

```bash
python scripts/create_dev_token.py --subject local-reviewer --roles compliance_officer --principal-type human --simulate-mfa --minutes 5
```

`--simulate-mfa` does not perform real MFA. `AUTH_DISABLED=true` bypasses authentication and supplies a synthetic human identity with fresh MFA; it is strictly an isolated-development setting.

## Gateway resource limits

| Setting | Default | Boundary |
|---|---:|---|
| `GATEWAY_MAX_INFLIGHT` | `16` | Active HTTP requests per process; excess returns `503` immediately |
| `GATEWAY_MAX_JSON_BYTES` | `65536` | Actual body bytes outside `/v1/batch`; oversized requests return `413` |
| `GATEWAY_MAX_BATCH_BYTES` | `16777216` | Total multipart body bytes, including overhead; per-file limits still apply |
| `GATEWAY_BODY_TIMEOUT_SECONDS` | `5` | Total body-read deadline; slow requests return `408` |
| `GATEWAY_REQUEST_TIMEOUT_SECONDS` | `35` | Application deadline after body receipt; normally returns `504` on expiry |
| `GATEWAY_REQUESTS_PER_MINUTE` | `120` | Per verified subject refill rate |
| `GATEWAY_REQUEST_BURST` | `30` | Per verified subject initial/maximum token bucket capacity |
| `GATEWAY_MAX_PRINCIPALS` | `4096` | Maximum resident quota identities; depleted live buckets are not evicted to admit new subjects |

Byte checks work without `Content-Length` and when it understates the body size. Compressed request bodies are rejected with `415`. The gateway buffers only within these caps before FastAPI parses JSON or multipart data. A subject that exhausts its quota gets `429` plus `Retry-After`, even with a newly minted token for that same subject. The separate one-second `/v1/evaluate` deadline remains in effect.

Timeout cancellation is cooperative; it cannot preempt synchronous parsing or a CPU-bound call. Container CPU/memory limits and edge controls remain necessary. If a mutating request times out, its outcome can be unknown because an upstream action may already have committed. Reconcile state; do not blindly repeat actions. After response headers have started, a timeout can terminate the body instead of changing the status. Rate buckets reset on restart and multiply with gateway replicas; deploy shared quotas before scaling horizontally.

## Deployment boundaries

- Internal service APIs trust the private service network. The gateway enforces human identity; exposing the alert manager directly bypasses that enforcement. Remove development port publications and restrict internal access with service identities/network policy.
- Generated and template text remain untrusted content. Clients must render text safely and must never interpret narratives, notes or retrieved evidence as instructions to another agent. Typed input and no-tool inference reduce exposure without establishing factual accuracy.
- This is a single-organization reference. Roles are not tenant or per-customer authorization; add those boundaries before a multi-tenant deployment.
- The persistent database is part of the cost boundary. Protect its integrity and backups, monitor disk usage, and retain attempt/replay records under an approved policy. No automatic deletion is provided.
- Gateway controls reduce bounded application work but do not stop volumetric attacks, compromised issuers, malicious insiders, poisoned financial evidence, or abuse of other accessible services. Use shared edge quotas, restricted egress, container resource budgets, storage alerts, and centralized monitoring.
- Scoring effectiveness and regulatory suitability remain unvalidated. These tests exercise engineering failure/abuse cases; they do not demonstrate detection quality or certify an agent as safe.

## Upgrade and verification

Back up the alert database. Startup adds budget tables and indexes without deleting alerts; existing payloads default to revision `1` until updated. Retain a rollback copy before upgrading application versions. Existing API clients must send `expected_revision`. Legacy tokens without an identity type can still use authorized assistance operations but cannot finalize cases; the issuer must provide human/MFA claims for that workflow. Drafting now requires explicit enablement even when a key exists.

Run `python -m pytest`, `python -m ruff check services tests scripts`, and `python scripts/export_openapi.py --check`. The adversarial suites are `tests/test_ai_controls.py` and `tests/test_gateway_controls.py`; they use synthetic credentials and mocked provider responses, without paid calls. See [CONTRIBUTING.md](../CONTRIBUTING.md) for Linux/Windows CI and isolated RabbitMQ integration commands.
