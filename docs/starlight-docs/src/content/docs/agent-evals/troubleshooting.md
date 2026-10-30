---
title: "Agent Evals Troubleshooting & Limits"
description: "Fix missing evaluation results, neutral Scored verdicts, and unlinked runs, and know the mapping, paging, and retention limits of agent evaluations"
---

## Traces arrive but no evaluation results

Evaluation results travel over OTLP **logs**, not traces. Go through these checks in order:

1. **Logs endpoint.** `register()` prints a warning and skips logs when it cannot resolve a logs URL, for example when the traces URL doesn't end in `/v1/traces` or traces use gRPC. Pass `logs_endpoint=` (TS: `logsEndpoint`) or set `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`. See [Endpoints](/docs/agent-evals/instrumenting/#endpoints-traces-and-logs).
2. **Point at the collector.** Without an endpoint, the SDKs default to `http://localhost:21890/opentelemetry/v1/...`. The stack's Data Prepper only accepts OTLP gRPC from the collector on that port and answers 404 there. Send to the OTel Collector instead: `endpoint="http://localhost:4318/v1/traces"`, or `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`.
3. **Flush before exit.** Short scripts must call `provider.force_flush()` (TS: `await provider.forceFlush()`), or the last batch is lost.
4. **Your own OTel setup.** If you didn't use `register()`, install a global `LoggerProvider` with an OTLP log exporter. Without one, `score()` has nowhere to send events.
5. **Collector and pipeline.** The collector's `logs` pipeline must export to Data Prepper, and Data Prepper must route the three event names to `genai-evaluation-pipeline`. Results land in `otel-v1-genai-evaluation-*`, not in `logs-otel-v1-*`.
6. **Give it a moment.** Data Prepper batches writes. Results normally appear within about 30 seconds.

To check the index directly:

```bash
curl -sk -u 'admin:My_password_123!@#' \
  "https://localhost:9200/otel-v1-genai-evaluation-*/_search?size=0" \
  -H 'Content-Type: application/json' \
  -d '{"aggs":{"events":{"terms":{"field":"eventName"}}}}'
```

## Results show "Scored" instead of pass/fail

"Scored" means no rule decides the verdict. A numeric value alone isn't a verdict, and neither is a label such as `pass`. Declare one of these:

- `assessment="pass"` / `"fail"` on `score()`, or return a `bool` or an `EvalScore(passed=...)` from a scorer
- `threshold=` (and `direction="minimize"` when lower is better) on `score()`, `@scorer(...)`, or `defineScorer(...)`

Results already stored keep their verdict. New results get the rule. Test case status follows the same rules: a case with only undeclared scores is `pass` unless something raised.

## A run shows no test cases, or no trace links

- **"The producer sent no run event"** - the run was uploaded without `opensearch.test_case.result` / `opensearch.test_suite_run.result` events (older SDKs or a third-party producer). The run still shows, with counts computed from its grades.
- **No trace on a test case** - the task started no span. Decorate the agent with `@observe(...)` and call `register()` first, so the root span is created on the global provider. In TypeScript the task must be synchronous. For `Benchmark`, pass `trace_id` and `span_id` to `log()`.
- **Runs don't line up in Compare** - keep the suite name and `case_id`s stable across runs. Compare matches test cases by id and warns when the runs have different cases or evaluators.

## The Evaluations page is empty

- Check **Settings**: an evaluations dataset must be selected. The stack's init job creates **Evaluation Dataset - Local Cluster** and the `AgentEvals-Config-<workspace>` app config. If you deleted the dataset, re-run the init job or create the dataset from Settings.
- Index patterns with spaces, quotes, or `|` are not supported for the evaluations dataset.
- Check the time range. Overview and Results use the global range (15 minutes by default). Evaluation runs use their own range (7 days by default).

## Limits

**Every field the UI reads must be mapped from the first index on.** The Evaluations pages run PPL queries. On OpenSearch 3.9, PPL drops the documents of indices that do not map a field an `eval` expression names. This affects `gen_ai.evaluation.*`, `opensearch.evaluation.*`, `derived.*`, `test.*`, and `error.type`. The stack's index template maps all of them. If you created evaluation indices before installing the template, or with your own template, add the mappings to those indices (`PUT <index>/_mapping` with the template's mappings). Otherwise their documents are missing from verdict aggregations. When no index maps a field at all, the UI retries the query without it.

**Results paging stops at 10,000.** The Results table pages in the query. On OpenSearch 3.9, a page that straddles offset 10,000 (`plugins.query.size_limit`) is cut at 10,000. The page sizes offered (25, 50, 100) never straddle it, and pages on either side are complete. Totals, KPIs, and charts come from aggregations and are exact at any volume. Distinct trace and session counts are exact below 3,000 per period and approximate above.

**"Traces where it fails" lists the first 500 traces.** The filter matches distinct target trace ids in the time range, up to 500.

**Text is truncated.** The Python SDK keeps explanations whole up to 8,192 characters. The TypeScript SDK truncates them at 500. Test case input, expected, and output (`record_io`) are truncated on the case event, and their `keyword` sub-fields index the first 256 characters.

**Retention.** The init script's ISM retention policies do not cover `otel-v1-genai-evaluation-*`. Delete old daily indices yourself, or add an ISM policy for the pattern.

**One shard, no replicas.** The index template sets `number_of_shards: 1` and `number_of_replicas: 0` for a single-node development stack. Raise them for production clusters.

**Whole numbers from TypeScript.** OTel JS encodes whole numbers as integers. The SDK's own log exporters re-encode score fields as doubles, and the index maps them as `double` either way.
