#!/usr/bin/env python3
"""Import Amazon Bedrock AgentCore spans and evaluation results into an OTLP endpoint.

Reads, for a time window:
  * agent spans from CloudWatch Logs. AgentCore runtimes created after the
    "unified span destination" launch write spans to the `spans` stream of the
    agent's own log group (/aws/bedrock-agentcore/runtimes/<id>-<endpoint>).
    Older agents write to the shared `aws/spans` log group. Both are supported.
  * online evaluation results (`gen_ai.evaluation.result` events) from the
    evaluation results log group
    (/aws/bedrock-agentcore/evaluations/results/<online-eval-config-id>).

and sends them as OTLP/HTTP JSON:
  * spans          -> POST <endpoint>/v1/traces
  * eval results   -> POST <endpoint>/v1/logs  (LogRecord, eventName
                      gen_ai.evaluation.result)

Trace IDs, span IDs, timestamps, attributes and resource service.name are kept
as-is, so evaluation records join their spans downstream.

Dependencies: boto3, requests.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from collections import OrderedDict
from typing import Any, Iterable, Iterator

import boto3
import requests

EVAL_EVENT_NAME = "gen_ai.evaluation.result"
EVAL_SCOPE = "aws.bedrock_agentcore.evaluations"
SHARED_SPANS_LOG_GROUP = "aws/spans"
UNIFIED_SPANS_STREAM = "spans"

SPAN_KIND = {
    "UNSPECIFIED": 0, "INTERNAL": 1, "SERVER": 2, "CLIENT": 3, "PRODUCER": 4, "CONSUMER": 5,
}
STATUS_CODE = {"UNSET": 0, "OK": 1, "ERROR": 2}

# Top-level keys in an AgentCore evaluation log entry that are not part of the
# OTel LogRecord. They duplicate attributes or carry the EMF metric payload.
EVAL_NON_OTEL_KEYS = {"_aws", "label", "service.name", "onlineEvaluationConfigId", "evaluationJobId", "runId"}
# Batch evaluation results (agentcore run batch-evaluation / StartBatchEvaluation) default log group.
BATCH_RESULTS_LOG_GROUP = "/aws/bedrock-agentcore/evaluations/batch-evaluations/results/default"


# --------------------------------------------------------------------------- #
# Time window parsing
# --------------------------------------------------------------------------- #

_REL = re.compile(r"^(\d+)([smhd])$")


def parse_time(value: str, now: dt.datetime) -> dt.datetime:
    """Accept 'now', a relative duration like '6h' (meaning now minus 6h), or ISO 8601."""
    if value == "now":
        return now
    m = _REL.match(value)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"s": dt.timedelta(seconds=n), "m": dt.timedelta(minutes=n),
                 "h": dt.timedelta(hours=n), "d": dt.timedelta(days=n)}[unit]
        return now - delta
    t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t


def to_ms(t: dt.datetime) -> int:
    return int(t.timestamp() * 1000)


# --------------------------------------------------------------------------- #
# CloudWatch Logs reading
# --------------------------------------------------------------------------- #

def read_log_events(logs, log_group: str, start_ms: int, end_ms: int,
                    stream_names: list[str] | None = None) -> Iterator[dict]:
    """Yield parsed JSON messages from a log group (optionally limited to streams)."""
    kwargs: dict[str, Any] = {"logGroupName": log_group, "startTime": start_ms, "endTime": end_ms}
    if stream_names:
        kwargs["logStreamNames"] = stream_names
    paginator = logs.get_paginator("filter_log_events")
    try:
        for page in paginator.paginate(**kwargs):
            for ev in page.get("events", []):
                try:
                    yield json.loads(ev["message"])
                except (ValueError, KeyError):
                    continue
    except logs.exceptions.ResourceNotFoundException:
        print(f"  log group or stream not found: {log_group} {stream_names or ''}", file=sys.stderr)


def resolve_from_online_eval_config(control, config_id: str) -> dict:
    cfg = control.get_online_evaluation_config(onlineEvaluationConfigId=config_id)
    cwl = cfg.get("dataSourceConfig", {}).get("cloudWatchLogs", {})
    out = cfg.get("outputConfig", {}).get("cloudWatchConfig", {})
    return {
        "span_log_groups": cwl.get("logGroupNames", []),
        "service_names": cwl.get("serviceNames", []),
        "eval_log_group": out.get("logGroupName")
        or f"/aws/bedrock-agentcore/evaluations/results/{config_id}",
    }


def read_spans(logs, log_groups: list[str], service_names: set[str], start_ms: int, end_ms: int,
               source: str) -> list[dict]:
    """Read span records. source: auto | unified | shared."""
    spans: "OrderedDict[tuple[str, str], dict]" = OrderedDict()

    def add(records: Iterable[dict]) -> int:
        n = 0
        for rec in records:
            if "spanId" not in rec or "traceId" not in rec:
                continue
            svc = rec.get("resource", {}).get("attributes", {}).get("service.name")
            if service_names and svc not in service_names:
                continue
            spans[(rec["traceId"], rec["spanId"])] = rec  # dedupe
            n += 1
        return n

    if source in ("auto", "unified"):
        for lg in log_groups:
            if lg == SHARED_SPANS_LOG_GROUP:
                continue
            n = add(read_log_events(logs, lg, start_ms, end_ms, [UNIFIED_SPANS_STREAM]))
            print(f"  {lg} [{UNIFIED_SPANS_STREAM}]: {n} spans")
    if source == "shared" or (source == "auto" and not spans):
        n = add(read_log_events(logs, SHARED_SPANS_LOG_GROUP, start_ms, end_ms))
        print(f"  {SHARED_SPANS_LOG_GROUP}: {n} spans")
    return list(spans.values())


# --------------------------------------------------------------------------- #
# OTLP JSON conversion
# --------------------------------------------------------------------------- #

def any_value(v: Any) -> dict:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [any_value(x) for x in v]}}
    if isinstance(v, dict):
        return {"kvlistValue": {"values": key_values(v)}}
    if v is None:
        return {}
    return {"stringValue": str(v)}


def key_values(attrs: dict | None) -> list[dict]:
    return [{"key": k, "value": any_value(v)} for k, v in (attrs or {}).items()]


def _nano(v: Any) -> str | None:
    return None if v is None else str(int(v))


def span_to_otlp(rec: dict) -> dict:
    kind = rec.get("kind", "INTERNAL")
    span: dict[str, Any] = {
        "traceId": rec["traceId"],
        "spanId": rec["spanId"],
        "name": rec.get("name", ""),
        "kind": SPAN_KIND.get(str(kind).upper().replace("SPAN_KIND_", ""), 0) if isinstance(kind, str) else int(kind),
        "startTimeUnixNano": _nano(rec.get("startTimeUnixNano")),
        "endTimeUnixNano": _nano(rec.get("endTimeUnixNano")),
        "attributes": key_values(rec.get("attributes")),
    }
    if rec.get("parentSpanId"):
        span["parentSpanId"] = rec["parentSpanId"]
    if rec.get("traceState"):
        span["traceState"] = rec["traceState"]
    if rec.get("flags") is not None:
        span["flags"] = int(rec["flags"])
    status = rec.get("status") or {}
    code = status.get("code", "UNSET")
    span["status"] = {"code": STATUS_CODE.get(str(code).upper().replace("STATUS_CODE_", ""), 0)
                      if isinstance(code, str) else int(code)}
    if status.get("message"):
        span["status"]["message"] = status["message"]
    if rec.get("events"):
        span["events"] = [{"timeUnixNano": _nano(e.get("timeUnixNano")), "name": e.get("name", ""),
                           "attributes": key_values(e.get("attributes"))} for e in rec["events"]]
    if rec.get("links"):
        span["links"] = [{"traceId": l.get("traceId"), "spanId": l.get("spanId"),
                          "attributes": key_values(l.get("attributes"))} for l in rec["links"]]
    return span


def eval_to_otlp(rec: dict) -> tuple[dict, dict]:
    """Return (resource_attributes, LogRecord) for one AgentCore evaluation entry."""
    resource = dict(rec.get("resource", {}).get("attributes", {}))
    if "service.name" not in resource and rec.get("service.name"):
        resource["service.name"] = rec["service.name"]

    attrs = dict(rec.get("attributes", {}))
    if rec.get("onlineEvaluationConfigId"):
        attrs.setdefault("aws.bedrock_agentcore.online_evaluation_config.id", rec["onlineEvaluationConfigId"])
    metric_name = attrs.get("gen_ai.evaluation.name")
    # Scores are always numeric doubles in OTel; JSON may carry 1 instead of 1.0.
    score = attrs.get("gen_ai.evaluation.score.value")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        attrs["gen_ai.evaluation.score.value"] = float(score)

    log: dict[str, Any] = {
        "timeUnixNano": _nano(rec.get("timeUnixNano")),
        "observedTimeUnixNano": _nano(rec.get("observedTimeUnixNano") or rec.get("timeUnixNano")),
        "severityNumber": int(rec.get("severityNumber", 9)),
        "severityText": rec.get("severityText", "INFO"),
        "eventName": rec.get("name") or rec.get("eventName") or EVAL_EVENT_NAME,
        "attributes": key_values(attrs),
    }
    if rec.get("traceId"):
        log["traceId"] = rec["traceId"]
    if rec.get("spanId"):
        log["spanId"] = rec["spanId"]
    if rec.get("body") is not None:
        log["body"] = any_value(rec["body"])
    # Keep any other non-EMF top-level fields as attributes, so nothing is lost.
    for k, v in rec.items():
        if k in EVAL_NON_OTEL_KEYS or k == metric_name or k in log or k in (
                "resource", "attributes", "name", "eventName", "severityText"):
            continue
        log["attributes"].append({"key": f"aws.bedrock_agentcore.record.{k}", "value": any_value(v)})
    return resource, log


def _resource_key(attrs: dict) -> str:
    return json.dumps(attrs, sort_keys=True)


def build_trace_payloads(spans: list[dict], batch: int) -> Iterator[dict]:
    for i in range(0, len(spans), batch):
        groups: "OrderedDict[str, dict]" = OrderedDict()
        for rec in spans[i:i + batch]:
            res = rec.get("resource", {}).get("attributes", {})
            scope = rec.get("scope", {})
            rk = _resource_key(res)
            g = groups.setdefault(rk, {"resource": {"attributes": key_values(res)}, "scopes": OrderedDict()})
            sk = f"{scope.get('name', '')}|{scope.get('version', '')}"
            s = g["scopes"].setdefault(sk, {"scope": {k: v for k, v in (("name", scope.get("name", "")),
                                                                         ("version", scope.get("version")))
                                                      if v is not None}, "spans": []})
            s["spans"].append(span_to_otlp(rec))
        yield {"resourceSpans": [{"resource": g["resource"], "scopeSpans": list(g["scopes"].values())}
                                 for g in groups.values()]}


def build_log_payloads(evals: list[dict], batch: int) -> Iterator[dict]:
    for i in range(0, len(evals), batch):
        groups: "OrderedDict[str, dict]" = OrderedDict()
        for rec in evals[i:i + batch]:
            res, log = eval_to_otlp(rec)
            g = groups.setdefault(_resource_key(res), {"resource": {"attributes": key_values(res)}, "logs": []})
            g["logs"].append(log)
        yield {"resourceLogs": [{"resource": g["resource"],
                                 "scopeLogs": [{"scope": {"name": EVAL_SCOPE}, "logRecords": g["logs"]}]}
                                for g in groups.values()]}


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #

def post(session: requests.Session, url: str, payload: dict, retries: int = 5) -> None:
    body = json.dumps(payload)
    for attempt in range(retries):
        try:
            r = session.post(url, data=body, headers={"Content-Type": "application/json"}, timeout=30)
            if r.status_code < 300:
                return
            msg = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code < 500 and r.status_code != 429:
                raise RuntimeError(msg)
        except requests.RequestException as e:
            msg = repr(e)
        time.sleep(min(2 ** attempt, 15))
    raise RuntimeError(f"POST {url} failed after {retries} attempts: {msg}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--online-eval-config-id",
                   help="Online evaluation config ID. Resolves the span log group(s), service name(s) "
                        "and the results log group from the config.")
    p.add_argument("--span-log-group", action="append", default=[],
                   help="Log group with agent spans (repeatable), e.g. "
                        "/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT")
    p.add_argument("--eval-log-group", action="append", default=[],
                   help="Evaluation results log group (repeatable)")
    p.add_argument("--include-batch", action="store_true",
                   help=f"Also import batch evaluation results from {BATCH_RESULTS_LOG_GROUP}")
    p.add_argument("--service-name", action="append", default=[],
                   help="Only import spans whose resource service.name matches (repeatable)")
    p.add_argument("--span-source", choices=["auto", "unified", "shared"], default="auto",
                   help="unified: 'spans' stream of the agent log group; shared: aws/spans; "
                        "auto: unified, falling back to aws/spans when nothing is found")
    p.add_argument("--start", default="24h", help="Window start: ISO 8601, 'now', or relative like 6h / 2d")
    p.add_argument("--end", default="now", help="Window end: ISO 8601, 'now', or relative")
    p.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    p.add_argument("--profile", default=None, help="AWS profile name")
    p.add_argument("--endpoint", default=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"),
                   help="OTLP/HTTP base endpoint")
    p.add_argument("--batch-size", type=int, default=200)
    p.add_argument("--no-spans", action="store_true", help="Skip spans")
    p.add_argument("--no-evals", action="store_true", help="Skip evaluation results")
    p.add_argument("--dump-dir", help="Also write the OTLP JSON payloads to this directory")
    p.add_argument("--dry-run", action="store_true", help="Read and convert, but do not send")
    args = p.parse_args(argv)

    now = dt.datetime.now(dt.timezone.utc)
    start, end = parse_time(args.start, now), parse_time(args.end, now)
    start_ms, end_ms = to_ms(start), to_ms(end)

    sess = boto3.Session(profile_name=args.profile, region_name=args.region)
    logs = sess.client("logs")

    span_groups = list(args.span_log_group)
    services = set(args.service_name)
    eval_groups = list(args.eval_log_group)
    if args.online_eval_config_id:
        r = resolve_from_online_eval_config(sess.client("bedrock-agentcore-control"), args.online_eval_config_id)
        span_groups = span_groups or r["span_log_groups"]
        services = services or set(r["service_names"])
        eval_groups = eval_groups or [r["eval_log_group"]]
    if args.include_batch and BATCH_RESULTS_LOG_GROUP not in eval_groups:
        eval_groups.append(BATCH_RESULTS_LOG_GROUP)
    if not args.no_spans and not span_groups and args.span_source != "shared":
        p.error("give --online-eval-config-id or --span-log-group (or --span-source shared)")
    if not args.no_evals and not eval_groups:
        p.error("give --online-eval-config-id, --eval-log-group or --include-batch")

    print(f"window {start.isoformat()} .. {end.isoformat()}")
    spans: list[dict] = []
    evals: list[dict] = []
    if not args.no_spans:
        print("reading spans")
        spans = read_spans(logs, span_groups, services, start_ms, end_ms, args.span_source)
    if not args.no_evals:
        print("reading evaluation results")
        for g in eval_groups:
            found = [r for r in read_log_events(logs, g, start_ms, end_ms)
                     if (r.get("name") or r.get("eventName")) == EVAL_EVENT_NAME]
            print(f"  {g}: {len(found)} evaluation results")
            evals.extend(found)

    # Join report: how many span/trace-level results point at spans/traces we have.
    span_ids = {(s["traceId"], s["spanId"]) for s in spans}
    trace_ids = {s["traceId"] for s in spans}
    by_level: dict[str, int] = {}
    unjoined = 0
    for e in evals:
        lvl = e.get("attributes", {}).get("aws.bedrock_agentcore.evaluation_level", "?")
        by_level[lvl] = by_level.get(lvl, 0) + 1
        if spans and e.get("spanId") and (e.get("traceId"), e["spanId"]) not in span_ids:
            unjoined += 1
        elif spans and e.get("traceId") and not e.get("spanId") and e["traceId"] not in trace_ids:
            unjoined += 1
    if evals:
        print(f"  results by level: {by_level}; results whose span/trace is not in this window: {unjoined}")

    session = requests.Session()
    base = args.endpoint.rstrip("/")
    sent_spans = sent_logs = 0
    for kind, payloads, url, key in (
            ("traces", build_trace_payloads(spans, args.batch_size), f"{base}/v1/traces", "resourceSpans"),
            ("logs", build_log_payloads(evals, args.batch_size), f"{base}/v1/logs", "resourceLogs")):
        for i, payload in enumerate(payloads):
            if args.dump_dir:
                os.makedirs(args.dump_dir, exist_ok=True)
                with open(os.path.join(args.dump_dir, f"{kind}-{i:04d}.json"), "w") as f:
                    json.dump(payload, f)
            if kind == "traces":
                n = sum(len(ss["spans"]) for rs in payload[key] for ss in rs["scopeSpans"])
            else:
                n = sum(len(sl["logRecords"]) for rl in payload[key] for sl in rl["scopeLogs"])
            if not args.dry_run:
                post(session, url, payload)
            if kind == "traces":
                sent_spans += n
            else:
                sent_logs += n
    verb = "converted (dry run)" if args.dry_run else f"sent to {base}"
    print(f"{verb}: {sent_spans} spans, {sent_logs} evaluation results")
    return 0


if __name__ == "__main__":
    sys.exit(main())
