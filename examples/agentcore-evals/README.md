# Import Amazon Bedrock AgentCore traces and evaluations

`import_agentcore.py` copies an AgentCore agent's spans and its
[AgentCore Evaluations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluations.html)
results from Amazon CloudWatch Logs into the Observability Stack over OTLP/HTTP. You can then explore
AgentCore traces and evaluation scores in OpenSearch Dashboards next to your other agents.

| Source (CloudWatch Logs) | Sent as | Lands in |
|---|---|---|
| Agent spans: the `spans` stream of `/aws/bedrock-agentcore/runtimes/<runtime-id>-<endpoint>`, or the shared `aws/spans` log group | OTLP spans to `/v1/traces` | `otel-v1-apm-span-*` |
| Online evaluation results: `/aws/bedrock-agentcore/evaluations/results/<online-eval-config-id>` | OTLP LogRecords with `eventName: gen_ai.evaluation.result` to `/v1/logs` | `otel-v1-genai-evaluation-*` |

The importer keeps `traceId`, `spanId`, timestamps, every attribute and the resource `service.name`.
Evaluation results therefore join the spans they scored. Data Prepper adds the
`attributes.derived.*` fields and sets `attributes.derived.evaluation.source` to `agentcore`.

## Prerequisites

- The Observability Stack is running (`docker compose up -d` from the repository root). The OTLP
  HTTP receiver listens on `http://localhost:4318`.
- AWS credentials that can call `logs:FilterLogEvents` on the two log groups. They also need
  `bedrock-agentcore:GetOnlineEvaluationConfig` if you use `--online-eval-config-id`.
- Python 3.9+ with `boto3` and `requests`.

## Usage

The simplest way is to pass the online evaluation config ID. The importer reads the span log
groups, service names and results log group from the config:

```bash
uv run --with boto3 --with requests python import_agentcore.py \
  --region us-east-1 \
  --online-eval-config-id EvalsPoc_travel_online-YPFn1EAH0s \
  --start 6h
```

You can also name the log groups explicitly:

```bash
python import_agentcore.py --region us-east-1 \
  --span-log-group /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT \
  --service-name <runtime-name>.DEFAULT \
  --eval-log-group /aws/bedrock-agentcore/evaluations/results/<online-eval-config-id> \
  --start 2026-10-26T08:30:00Z --end now
```

Useful options:

| Option | Default | Description |
|---|---|---|
| `--start`, `--end` | `24h`, `now` | Time window. Accepts ISO 8601, `now`, or a relative value such as `90m`, `6h` or `2d` |
| `--span-source` | `auto` | `unified` reads the `spans` stream of the agent log group. `shared` reads `aws/spans`. `auto` tries `unified` first and falls back to `aws/spans` |
| `--endpoint` | `$OTEL_EXPORTER_OTLP_ENDPOINT` or `http://localhost:4318` | OTLP/HTTP base URL |
| `--no-spans`, `--no-evals` | off | Import only one of the two signals |
| `--dry-run`, `--dump-dir DIR` | off | Read and convert without sending, and/or write the OTLP JSON payloads to `DIR` |

The importer prints how many results it found at each level (Session, Trace, Span). It also prints
how many results point at spans or traces outside the window. Widen the window if that number is
not 0.

## Verify

```bash
# Evaluation results (one document per result)
curl -sk -u 'admin:My_password_123!@#' \
  'https://localhost:9200/otel-v1-genai-evaluation-*/_search?q=attributes.derived.evaluation.source:agentcore&size=3'

# Spans
curl -sk -u 'admin:My_password_123!@#' \
  'https://localhost:9200/otel-v1-apm-span-*/_search?q=serviceName:EvalsPoc*&size=3'
```

Spans can take a few minutes to appear. Data Prepper's trace processor holds spans until it can
fill in the trace group.

## Where AgentCore spans live

AgentCore runtimes created after the unified span destination launch write spans to the `spans`
stream of the agent's own log group, not to `aws/spans`. In that case `aws/spans` stays empty even
with Transaction Search enabled. That is expected. Agents created earlier, or agents with
`UNIFIED_TRACES_DESTINATION_ENABLED=false`, still write to `aws/spans`. See
[Span destination for agents hosted in AgentCore runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html#observability-configure-unified-traces).
Either way the span records are OTLP-shaped JSON and include the prompt and completion events.
The importer only converts span kinds and status codes to their OTLP enum values.

## How evaluation records are mapped

AgentCore writes each result as an OTel-style JSON event with an Embedded Metric Format (EMF) block:

| AgentCore field | OTLP LogRecord |
|---|---|
| `name` (`gen_ai.evaluation.result`) | `eventName` |
| `timeUnixNano`, `observedTimeUnixNano`, `severityNumber` | same fields |
| `traceId` (Trace and Span levels), `spanId` (Span level) | same fields |
| `resource.attributes` (`service.name`, ...) | resource attributes |
| `attributes` (`gen_ai.evaluation.*`, `session.id`, `aws.bedrock_agentcore.*`) | attributes, unchanged. `gen_ai.evaluation.score.value` is sent as a double |
| `onlineEvaluationConfigId` | attribute `aws.bedrock_agentcore.online_evaluation_config.id` |
| `_aws` (EMF), `label`, `service.name`, `<evaluator name>: <score>` | dropped, because they duplicate the attributes above |

Result levels:

- **Session**: no `traceId` or `spanId`. The session is in `session.id`, and the evaluated range is
  in `aws.bedrock_agentcore.evaluation_start_trace_id` and `evaluation_end_trace_id`.
- **Trace**: `traceId` only.
- **Span**: `traceId` and `spanId` of the evaluated tool span. Tool-level evaluators such as
  `ToolSelectionAccuracy` and `ToolParameterAccuracy` produce these.

Custom categorical evaluators (pass/fail) have `gen_ai.evaluation.score.label` but no
`score.value`. AgentCore does not publish CloudWatch metrics for them.

## Limitations

- On-demand evaluations (`agentcore run eval`) return their results only to the caller, so the
  importer cannot see them. Batch evaluations (`agentcore run batch-evaluation`) write to
  `/aws/bedrock-agentcore/evaluations/batch-evaluations/results/default`. Add `--include-batch` to
  import them too. They have `aws.bedrock_agentcore.evaluation_job.*` attributes instead of the
  online config attributes.
- Re-running the importer over the same window is safe for spans, because the span index uses the
  span ID as the document ID. It creates duplicate evaluation documents. Use `--no-evals`, or
  choose a window that does not overlap the earlier import.
- The importer keeps AgentCore's own timestamps. An evaluation's `timeUnixNano` is set by AgentCore
  and can be a few seconds later than the span it scored. Join on `traceId`/`spanId`, not on time.
- The importer reads span records from CloudWatch Logs only. It does not read X-Ray segment
  documents.
