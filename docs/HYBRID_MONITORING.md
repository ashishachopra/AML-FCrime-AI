# Hybrid monitoring: September 2026

Research cutoff: **7 September 2026**. This is an engineering reference, not a claim that the software meets a jurisdiction's AML obligations.

## Problems and implemented responses

| Problem | Evidence and rationale | Implemented response |
|---|---|---|
| Fraud proceeds move quickly through account networks | FATF's February 2026 cyber-enabled fraud paper discusses technology-enabled fraud, rapidly sharing information, and tracing illicit proceeds. | One-hour fan-in/fan-out, pass-through, and reciprocal-transfer indicators from explicit outbound payment records. These indicators do not detect deepfakes or establish criminal intent. |
| Isolated transaction rules miss coordinated behavior | BIS Project Hertha (June 2025) examines analytics across real-time payment networks and emphasizes labeled data, feedback, and explainability. | Combine temporal network indicators, robust amount baselines, and reference rules, with named contributions and human-review routing. |
| Full-history scans become slower as unrelated accounts grow | The previous feature implementation iterated over every transaction for each candidate. | SQLite indexes on account/source/target and event time, bounded result sets, and thread-offloaded computation. SQLite's native C engine performs indexed lookup without adding a separate runtime or model server. |
| Retries and restarts can alter or lose evidence | RabbitMQ documents at-least-once delivery and duplicate deliveries around uncertain confirmations. | Atomically commit the normalized transaction, immutable feature result, and outgoing event. Retry the same persisted event ID; delete an outbox entry only after confirmation. |
| Fast scoring must not depend on an external generative model | Amount and transfer-pattern evidence can be computed locally; an LLM adds no necessary information to these calculations. | An authenticated preview path with one total deadline. Existing optional AI narrative drafting remains downstream of scoring and subject to human review. |

Primary sources: [FATF cyber-enabled fraud, February 2026](https://www.fatf-gafi.org/en/publications/Methodsandtrends/cyber-enabled-fraud-digitalisation-ml-tf-pf-risks.html), [BIS Project Hertha, June 2025](https://www.bis.org/publications/project-hertha-identifying-financial-crime-patterns-real-time-retail-payment-systems), [RabbitMQ reliability guide](https://www.rabbitmq.com/docs/reliability). The implementation choices above are this project's interpretation of those engineering needs, not recommendations endorsed by those organizations.

## Methods and evidence semantics

The hybrid has three local parts. Existing amount, structuring, jurisdiction, and KYC rules remain transparent. A nonparametric behavioral baseline is fitted from observed account amounts. Small temporal network motifs contribute complementary signals. The scorer is still a deterministic, unvalidated reference policy; no trained classifier, externally hosted inference, accuracy claim, or fabricated probability has been added.

### Amount baseline

For the configured long window, select **strictly earlier** transactions in the same currency and with the same direction, including unspecified direction only when both records omit it. Compute `log1p(amount)`, its median, and median absolute deviation (MAD). The upward robust deviation is:

```text
z = max(0, (log1p(current_amount) - median) / max(1.4826 * MAD, 0.1))
anomaly_score = clip((z - 3) / 5, 0, 1)
```

The baseline needs 20 compatible historical observations by default (`ANOMALY_MIN_HISTORY`, minimum 5). Before warm-up, the anomaly score is zero and readiness is explicitly false. The scale floor prevents tiny deviations in a constant series from producing extreme scores. The current transaction never trains its own baseline. There is no claim that these illustrative cutoffs are calibrated to a bank's population. Poisoning, seasonality, legitimate business changes, low-value anomalies, and cohort differences remain validation concerns.

### Currency and time

Transaction counts span currencies; `amt_*d` and `avg_amt_*d` only aggregate the candidate's currency. Threshold comparisons use `Decimal`. Optional `base_currency` and `base_currency_amount` must occur together and must come from a governed upstream conversion process; this service supplies no exchange rates.

The historical interval is `[candidate_time - window, candidate_time)`. Equal timestamps are conservatively excluded. The service uses observed records, not records that will arrive in the future. A same-account event arriving behind an already observed timestamp sets `late_event=1` and requests review. Earlier data does not rewrite previously stored snapshots or alert evidence. This is not a watermark/retraction engine: delays on other accounts can also leave network evidence incomplete without being detectable locally. Customer/account context is the latest observed context at computation time, not a historical KYC timeline.

### Network signals

Set `direction: "outbound"` and `counterparty_account_id` on canonical transfers. `account_id` is the source and `counterparty_account_id` the destination. IDs must be pseudonymous, stable, and resolved into the same namespace. Free-text names and source risk annotations never become edges or instructions. Inbound ledger mirrors and self-transfers do not contribute edges. Upstream systems must deduplicate the same payment under different transaction IDs.

In the previous hour:

- Fan-in counts distinct sources paying the current source account. Fan-out counts distinct recipients of that account, including the current candidate.
- The fan-out score is `clip((distinct_recipients - 3) / 7, 0, 1)`.
- Pass-through indicates a current payment between 80% and 120% of observed incoming value from at least three distinct sources in the **same currency**. Prior outflows do not make a later tiny payment suspicious. This is a motif, not a balance or funds-attribution calculation.
- Reciprocal flow indicates an earlier payment from the candidate's recipient back to its source, in the same currency and within a 20% amount tolerance. Legitimate refunds can match it.

The policy routes pass-through and reciprocal signals for investigation. Strong amount anomaly combined with strong fan-out is a separate overlay. These bounded one-hop signals do not replace the standalone exploratory graph service or detect arbitrary-length cycles, cross-institution networks absent from the data, or sanctions matches.

## Storage, retries, and bounds

The feature database persists minimal customer/account fields, normalized transactions, feature results, and pending CloudEvents. It omits customer names, date of birth, purposes, and source annotations. Canonical decimal and UTC normalization make equivalent representations replay-equivalent. Reusing an ID with different feature-relevant evidence is a `409` for preview and a dead letter for an ingested event; corrections need a new governed identity/process.

One SQLite transaction creates both the snapshot and its outbox record. No database lock spans a broker await. The consumer acknowledges after the local commit. The publisher confirms persistent messages, waits at most five seconds per publication, and retries with backoff up to 30 seconds. A crash after publish but before deletion can duplicate a delivery; it retains the same event ID. This is at-least-once delivery with idempotent feature materialization, not end-to-end exactly-once processing.

Malformed events are dead-lettered; transient store failures are requeued. Default bounds:

| Setting | Default | Behavior |
|---|---:|---|
| `FEATURE_MAX_HISTORY_ROWS` | 1000 | Latest account observations in the required windows; set `history_truncated` if exceeded. |
| `FEATURE_MAX_NETWORK_ROWS` | 1000 | Per incoming/outgoing direction; set `network_history_truncated` if exceeded. |
| `FEATURE_MAX_OUTBOX` | 10000 | Reject new durable feature work until the publisher catches up; preserve broker messages for retry. |
| `FEATURE_MAX_INFLIGHT` | 16 | Bound admitted database/compute work; reject HTTP admission after 100 ms when saturated. |
| `EVALUATION_TIMEOUT_SECONDS` | 1.0 | Total gateway preview deadline across both HTTP calls; timeout returns `504` and no score. |

Capped evidence, late events, and missing KYC context set `review_recommended`. A low-score result requiring review creates a `data_quality_review` alert without raising its numeric risk score or drafting a SAR narrative. A lack of graph IDs or a warming amount baseline is disclosed separately; it alone does not imply suspicious activity. `review_recommended=false` is never a payment authorization or a declaration of innocence.

SQLite uses WAL with `synchronous=FULL` for committed writes and a serialized connection. WAL needs local storage and permits one writer at a time; it is not a distributed database. Use maintained SQLite/runtime patches and tested backups via the SQLite backup API, preserving active WAL state. See [SQLite WAL documentation](https://www.sqlite.org/wal.html). Disk growth is intentionally not hidden by automatic deletion; monitor capacity and establish evidence retention before deployment.

## Scaling and operations

Run **one feature worker per authoritative evidence database**. Do not increase Uvicorn workers or add consumers with separate local files to the same queue: that fragments account/network history. The outbox dispatcher is designed for one process, with no distributed lease. HTTP overload receives a retryable error rather than accumulating unbounded compute work. The ingress/web server must separately bound connection counts and request sizes.

The local bottleneck now scales with the bounded account neighborhood, rather than all transactions. A PostgreSQL implementation can preserve the same `evaluate`/`get`/`list`/outbox interfaces, using transactional snapshots, uniqueness constraints, and claimed outbox leases. A distributed deployment must materialize transfers for both source and destination accounts, order processing by a governed event-time policy, and provide shared context; hashing only by source loses fan-in evidence. Namespace tenant IDs and screen data access before introducing multi-tenancy. No PostgreSQL adapter, distributed router, or cross-bank data-sharing arrangement is included here.

Monitor `/metadata` storage counts, outbox backlog, `/health/ready`, disk/WAL growth, broker dead letters, feature-cap incidence, cold-start rates, late events, preview timeout rates, and reviewed alert outcomes. The stored snapshots preserve observed decisions; retrospective backfills should be isolated and versioned rather than silently overwriting live evidence. Ingestion still lacks a batch outbox, and scorer publication can still require DLQ recovery. Deployment-wide rate limiting, service authentication, broker topology, disaster recovery, and retention remain platform responsibilities.

## Migration

1. Back up existing alert data. Add the new Compose `feature_data` volume and rebuild the changed services.
2. Existing transaction JSON remains valid. Add direction and counterparty account IDs only where upstream systems can supply canonical transfers; add converted amount pairs only with governed currency conversion.
3. Reingest approved historical fixtures in event-time order to build the initially empty feature database. The old in-memory state has no automatic migration. Use controlled replay procedures: existing alerts deduplicate by transaction ID and will not automatically incorporate the new policy.
4. Expect changed score distributions: version `hybrid-reference-policy-3.0.0` adds weights/overlays and fixes currency mixing. Revalidate thresholds before operational use. Feature reads now return the original snapshot and timestamp, not a recalculation using newly arrived data.
5. Add `review_recommended` handling to consumers and regenerate client bindings from the runtime OpenAPI contracts. Old `scorer_version` values remain meaningful on historical alerts.

## Benchmarking

```bash
python scripts/benchmark_hybrid.py --transactions 100000 --accounts 1000 --samples 100
python scripts/benchmark_hybrid.py --transactions 1000000 --accounts 10000 --samples 100
```

The benchmark seeds synthetic historical rows outside the timing loop, verifies feature equality between the global-scan and indexed paths, alternates their measurement order, warms both paths, and prints p50/p95/p99 with Python/SQLite/platform details. It aborts if the target history would be truncated. It does not time transaction ingestion, fsync, broker delivery, HTTP, or model calls. Results are machine/workload-specific and unsuitable as a production SLO.

[Windows 100k result](benchmarks/windows-100k.json): 100 observations for the target account, 100 timed samples; p95 11.6534 ms for the global scan and 1.6796 ms for the indexed preview. This measures retrieval/feature computation, not AML accuracy. Production load tests must include skewed hot accounts, multiple currencies, concurrent admission, graph density, disk contention, recovery, queue lag, and representative labels.
