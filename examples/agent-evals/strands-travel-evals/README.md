# Strands travel agent: offline experiments and online evaluations

`travel_evals.py` runs a [Strands](https://strandsagents.com) travel agent on Amazon Bedrock and
evaluates it with the OpenSearch GenAI Observability SDK. Every result is an OpenTelemetry
`gen_ai.evaluation.result` log event. Data Prepper routes the events to `otel-v1-genai-evaluation-*`,
where Agent Traces > Evaluations shows them next to the agent's traces.

| Mode | What it produces |
|---|---|
| `experiment --version v1\|v2` | One evaluation run (`test.suite.run.id`) over 8 golden test cases: `tool_selection` (code), `answer_quality` (LLM judge, 1 to 5, sees the tool results) and `latency_budget` (code, lower is better). Run v1 and v2 and compare them in Evaluation runs. Each test case is the agent's own trace (its root span carries `test.suite.run.id` and `test.case.id`), plus one `opensearch.test_case.result` log event per case and one `opensearch.test_suite_run.result` event per run. No harness spans. |
| `online --sessions N` | Multi-turn sessions with a per-turn `answer_quality` judge and a `handles_missing_data` check parented to each agent run, and one `session_goal_success` result per session. |

## Run

Requires the stack (`docker compose up -d`), AWS credentials with `bedrock:InvokeModel` /
`bedrock:Converse` for Claude Haiku 4.5 in us-east-1, and the SDK with evaluation events
(branch `feat/eval-results-as-events` of genai-observability-sdk-py until it is released).

```bash
uv venv && uv pip install strands-agents boto3 opensearch-genai-observability-sdk-py
uv run python travel_evals.py experiment --version v1
uv run python travel_evals.py experiment --version v2
uv run python travel_evals.py online --sessions 5
```

The script sends OTLP to `http://localhost:4318`.
