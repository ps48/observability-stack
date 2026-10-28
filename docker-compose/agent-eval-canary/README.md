# Agent Eval Canary (Deterministic)

Periodically scores un-evaluated agent traces with deterministic signals. No LLM, no AWS credentials, zero cost.

Included by default with the example services (`docker-compose.examples.yml`).

## Evaluation Signals

Emits **4 `gen_ai.evaluation.result` events** per trace. Each declares its verdict rule (`opensearch.evaluation.assessment`, or `opensearch.evaluation.score.threshold` + `.direction` with the computed assessment), so OpenSearch Dashboards shows pass/fail rather than a neutral "Scored":

| Eval Name | What it measures | Value | Pass rule |
|---|---|---|---|
| `span_coverage` | Instrumentation health — input, output, and tool spans present | 0.0 / 0.33 / 0.67 / 1.0 | `>= 0.67` |
| `error_free` | No span in the trace has `status.code=ERROR` | 0 or 1 | explicit assessment |
| `tool_call_success_rate` | Fraction of tool calls that returned non-empty results | 0.0–1.0 | `>= 0.5` |
| `tool_diversity` | Unique tools / total tool calls (catches looping) | 0.0–1.0 | `>= 0.34` |

## Configuration

| Env Var | Default | Description |
|---|---|---|
| `EVAL_CANARY_INTERVAL` | `120` | Seconds between poll cycles |
| `EVAL_CANARY_LOOKBACK_MINUTES` | `15` | How far back to search for traces |

## Running with the LLM Canary

Both canaries can run simultaneously without duplicating scores. Each deduplicates by its own evaluation name — this canary checks for existing `span_coverage` scores, the LLM canary checks for `helpfulness`. Neither blocks the other.

A trace scored by both gets **5 evaluation results**: `span_coverage`, `error_free`, `tool_call_success_rate`, `tool_diversity` (deterministic) and `helpfulness` (LLM-as-judge).

## OTel Compliance

Each result is an OTel log event `gen_ai.evaluation.result` ([OTel GenAI events](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-events/)) parented to the evaluated agent span (its `traceId` / `spanId`), with `gen_ai.evaluation.name`, `.score.value`, `.score.label`, `.explanation`, plus `opensearch.evaluation.*` extensions (level, evaluator type, scale, direction, threshold, assessment). Data Prepper routes the events to `otel-v1-genai-evaluation-*`.
