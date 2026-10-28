"""Agent evals POC data: a Strands travel agent on Bedrock, evaluated two ways.

  experiment --version v1|v2   offline run over a golden dataset (test_suite_run / test_case,
                               code checks + an LLM judge), for the Evaluation runs view.
  online --sessions N          multi-turn production-style sessions with inline LLM-judge
                               scores per turn and one session-level score.

Every result is a gen_ai.evaluation.result log event sent with the OpenSearch GenAI SDK;
Data Prepper routes the events to otel-v1-genai-evaluation-*.

Every check declares its verdict rule, so the UI shows pass/fail (not a neutral "Scored"):
tool_selection and handles_missing_data give an explicit assessment, answer_quality and
session_goal_success pass at 4 or more on a 1-5 scale, latency_budget passes at 12 s or less.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import time
import uuid

import boto3
from opentelemetry import trace
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from opensearch_genai_observability_sdk_py import (
    EvalScore,
    Op,
    enrich,
    evaluate,
    observe,
    register,
    score,
    scorer,
)

MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
JUDGE_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
QUALITY_THRESHOLD = 4  # 1-5 judge scale: pass at 4 or more
LATENCY_BUDGET_S = 12  # pass at 12 s or less

CITIES = {"paris": ("EUR", 18), "tokyo": ("JPY", 22), "london": ("GBP", 14),
          "new york": ("USD", 20), "sydney": ("AUD", 25), "seattle": ("USD", 12)}
RATES = {"EUR": 0.92, "JPY": 151.3, "GBP": 0.79, "USD": 1.0, "AUD": 1.52}
TOOL_LOG: list[str] = []
TOOL_RESULTS: list[str] = []


def _out(text: str) -> str:
    TOOL_RESULTS.append(text)
    return text


@tool
def get_weather(city: str) -> str:
    """Get the current weather forecast for a city."""
    TOOL_LOG.append("get_weather")
    info = CITIES.get(city.lower())
    return _out(f"No weather data for {city}." if not info else f"{city}: {info[1]} C, sunny.")


@tool
def search_flights(origin: str, destination: str, date: str) -> str:
    """Search flights between two cities on a date (YYYY-MM-DD). Returns price in USD."""
    TOOL_LOG.append("search_flights")
    if destination.lower() not in CITIES:
        return _out(f"No flights found to {destination}.")
    price = 200 + (len(origin) * 37 + len(destination) * 53) % 900
    return _out(f"Cheapest flight {origin} -> {destination} on {date}: ${price} USD, 1 stop.")


@tool
def search_hotels(city: str, nights: int) -> str:
    """Find hotels in a city for a number of nights. Returns total price in local currency."""
    TOOL_LOG.append("search_hotels")
    info = CITIES.get(city.lower())
    if not info:
        return _out(f"No hotels found in {city}.")
    nightly = round(140 * RATES[info[0]])
    return _out(f"Hotel Central, {city}: {nightly} {info[0]} per night, {nightly * nights} {info[0]} total.")


@tool
def convert_currency(amount: float, from_currency: str, to_currency: str) -> str:
    """Convert an amount between currencies (ISO codes such as USD, EUR, JPY)."""
    TOOL_LOG.append("convert_currency")
    src, dst = RATES.get(from_currency.upper()), RATES.get(to_currency.upper())
    if src is None or dst is None:
        return _out(f"Unknown currency {from_currency} or {to_currency}.")
    return _out(f"{amount} {from_currency.upper()} = {round(amount / src * dst, 2)} {to_currency.upper()}")


TOOLS = [get_weather, search_flights, search_hotels, convert_currency]
PROMPTS = {
    # v1: a vague prompt; the model often answers from memory instead of calling tools.
    "v1": "You are a friendly travel assistant. Answer quickly from what you know. Tools are optional.",
    # v2: tools first, never invent prices.
    "v2": ("You are a travel planning assistant. Always use the tools to look up weather, flights, "
           "hotels and currency rates before answering. Quote only prices returned by tools, and "
           "say clearly when a tool returned no result."),
}

DATASET = [
    {"case_id": "paris-trip", "case_name": "Paris flights and hotel",
     "input": "Flights from Seattle to Paris on 2026-12-10 and a hotel for 5 nights. Total in USD?",
     "expected": "Uses search_flights, search_hotels and convert_currency; total near $1,450 USD.",
     "tools": ["search_flights", "search_hotels", "convert_currency"]},
    {"case_id": "tokyo-weather", "case_name": "Tokyo weather",
     "input": "What's the weather in Tokyo right now?",
     "expected": "Calls get_weather for Tokyo: 22 C and sunny.", "tools": ["get_weather"]},
    {"case_id": "atlantis", "case_name": "Unknown destination",
     "input": "Book me a flight from London to Atlantis on 2026-12-01.",
     "expected": "Calls search_flights, reports no flights to Atlantis, does not invent a price.",
     "tools": ["search_flights"]},
    {"case_id": "eur-jpy", "case_name": "Currency conversion",
     "input": "How much is 500 EUR in JPY?", "expected": "Calls convert_currency: about 82,228 JPY.",
     "tools": ["convert_currency"]},
    {"case_id": "london-weekend", "case_name": "London weekend budget",
     "input": "Plan a weekend in London from New York on 2026-11-20, 2 hotel nights, total in USD.",
     "expected": "Flights plus 2 hotel nights converted to USD, prices from tools.",
     "tools": ["search_flights", "search_hotels", "convert_currency"]},
    {"case_id": "sydney-hotel", "case_name": "Sydney hotel in AUD",
     "input": "How much are 7 nights at a hotel in Sydney?", "expected": "Calls search_hotels: 1,491 AUD total.",
     "tools": ["search_hotels"]},
    {"case_id": "compare-weather", "case_name": "Compare weather",
     "input": "Which is warmer right now, Paris or London?",
     "expected": "Calls get_weather twice; Paris (18 C) is warmer than London (14 C).", "tools": ["get_weather"]},
    {"case_id": "budget-tokyo", "case_name": "Budget check Tokyo",
     "input": "I have 2000 USD. Can I afford flights Seattle to Tokyo on 2026-12-15 and 5 hotel nights?",
     "expected": "Looks up flight and hotel, converts JPY to USD, answers yes with the remaining budget.",
     "tools": ["search_flights", "search_hotels", "convert_currency"]},
]
EXPECTED_TOOLS = {c["input"]: c["tools"] for c in DATASET}
LAST_TOOLS: dict[str, list[str]] = {}
LAST_LATENCY: dict[str, float] = {}
LAST_RESULTS: dict[str, list[str]] = {}

bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")


def make_agent(version: str) -> Agent:
    return Agent(name="travel_planner", model=BedrockModel(model_id=MODEL_ID, region_name="us-east-1"),
                 system_prompt=PROMPTS[version], tools=TOOLS, callback_handler=None)


def judge(question: str, answer: str, expected: str | None = None, tool_results: list[str] | None = None) -> EvalScore:
    """LLM judge: 1-5 correctness and grounding, with a rationale."""
    rubric = (
        "You grade a travel assistant. Score 1-5: 5 = correct, uses tool results, no invented prices; "
        "3 = partially correct or unverified numbers; 1 = wrong or invented. Reply with JSON only: "
        '{"score": <1-5>, "label": "<correct|partially_correct|incorrect>", "reason": "<one or two sentences>"}'
    )
    prompt = f"{rubric}\n\nQuestion: {question}\n"
    if expected:
        prompt += f"Expected behavior: {expected}\n"
    if tool_results is not None:
        prompt += "Tool results the assistant saw:\n" + ("\n".join(tool_results) or "(no tools called)") + "\n"
    prompt += f"Answer: {answer}"
    started = time.time()
    resp = bedrock.converse(modelId=JUDGE_ID, messages=[{"role": "user", "content": [{"text": prompt}]}],
                            inferenceConfig={"maxTokens": 300, "temperature": 0})
    text = resp["output"]["message"]["content"][0]["text"]
    match = re.search(r"\{.*\}", text, re.S)
    data = json.loads(match.group(0)) if match else {"score": 1, "label": "incorrect", "reason": text[:300]}
    return EvalScore(
        name="answer_quality", value=float(data["score"]), label=data.get("label"),
        explanation=data.get("reason"),
        metadata={"opensearch.evaluation.judge.latency_ms": round((time.time() - started) * 1000)},
    )


# ---- offline experiment -------------------------------------------------------------------

def run_experiment(version: str) -> None:
    agent_holder = {"agent": None}

    @observe(name="travel_planner", op=Op.INVOKE_AGENT)
    def task(question: str) -> str:
        agent = make_agent(version)  # fresh agent: no history between cases
        TOOL_LOG.clear()
        TOOL_RESULTS.clear()
        started = time.time()
        enrich(model=MODEL_ID, provider="aws.bedrock")
        result = agent(question)
        LAST_TOOLS[question] = list(TOOL_LOG)
        LAST_RESULTS[question] = list(TOOL_RESULTS)
        LAST_LATENCY[question] = time.time() - started
        agent_holder["agent"] = agent
        return str(result)

    # Explicit verdict: pass only when every expected tool was called.
    @scorer(name="tool_selection", score_min=0, score_max=1,
            evaluator="tool_selection_check", evaluator_type="code")
    def tool_selection(input: str, output: str, expected: str) -> EvalScore:
        want, got = set(EXPECTED_TOOLS[input]), set(LAST_TOOLS.get(input, []))
        hit = len(want & got) / len(want)
        missing = sorted(want - got)
        return EvalScore(
            name="tool_selection", value=hit, label="pass" if hit == 1 else "fail",
            assessment="pass" if hit == 1 else "fail",
            explanation=(f"Called {sorted(got) or 'no tools'}; expected {sorted(want)}."
                         + (f" Missing {missing}." if missing else "")),
        )

    # Lower is better: pass at LATENCY_BUDGET_S or less (assessment computed by the SDK).
    @scorer(name="latency_budget", direction="minimize", threshold=LATENCY_BUDGET_S,
            score_min=0, score_max=2 * LATENCY_BUDGET_S, evaluator_type="code")
    def latency_budget(input: str, output: str, expected: str) -> EvalScore:
        seconds = LAST_LATENCY.get(input, 0.0)
        return EvalScore(
            name="latency_budget", value=round(seconds, 2),
            label="pass" if seconds <= LATENCY_BUDGET_S else "fail",
            explanation=f"{seconds:.1f}s against a {LATENCY_BUDGET_S}s budget.",
        )

    # 1-5 LLM judge: pass at QUALITY_THRESHOLD or more. If the judge call raises, the error
    # grade is recorded as answer_quality (the declared name), not as "quality".
    @scorer(name="answer_quality", threshold=QUALITY_THRESHOLD, score_min=1, score_max=5,
            evaluator="travel_quality_judge", evaluator_type="llm_judge")
    def quality(input: str, output: str, expected: str) -> EvalScore:
        return judge(input, output, expected, LAST_RESULTS.get(input, []))

    result = evaluate(
        name="travel-planner-golden",
        task=task,
        data=[{k: c[k] for k in ("input", "expected", "case_id", "case_name")} for c in DATASET],
        scores=[tool_selection, quality, latency_budget],
        metadata={"agent.version": version, "agent.model": MODEL_ID},
        record_io=True,
    )
    print(result.summary)


# ---- online sessions ----------------------------------------------------------------------

SESSIONS = [
    ["Is it cheaper to stay 4 nights in Paris or London? Totals in USD.", "And which is warmer?"],
    ["I want to fly to Atlantis from Seattle on 2026-12-01. How much?", "Fine, how about Tokyo then?"],
    ["What's 300 GBP in EUR?", "And in JPY?"],
    ["Plan 3 nights in Sydney from New York on 2027-02-14, flights plus hotel in AUD.",
     "Can I do it for under 3000 AUD?"],
    ["Weather in Seattle and New York?", "Book me a hotel in the warmer one for 2 nights."],
]


def run_online(n: int, version: str) -> None:
    for turns in SESSIONS[:n]:
        session_id = f"travel-session-{uuid.uuid4().hex[:12]}"
        agent = make_agent(version)
        transcript = []
        session_tools: list[str] = []
        for user_msg in turns:
            @observe(name="travel_planner", op=Op.INVOKE_AGENT)
            def turn(message: str) -> str:
                enrich(model=MODEL_ID, provider="aws.bedrock", session_id=session_id)
                TOOL_RESULTS.clear()
                answer = str(agent(message))
                # Online eval of this turn, parented to the active invoke_agent span.
                j = judge(message, answer, tool_results=list(TOOL_RESULTS))
                session_tools.extend(TOOL_RESULTS)
                score(name=j.name, value=j.value, label=j.label, explanation=j.explanation,
                      evaluator="travel_quality_judge", evaluator_type="llm_judge",
                      score_min=1.0, score_max=5.0, direction="maximize",
                      threshold=QUALITY_THRESHOLD,
                      conversation_id=session_id, agent_name="travel_planner")
                refusal = any(w in answer.lower() for w in ("no flights", "no result", "couldn't find",
                                                            "could not find", "doesn't exist", "not a real"))
                handled = refusal or "atlantis" not in message.lower()
                score(name="handles_missing_data", value=1.0 if handled else 0.0,
                      label="pass" if handled else "fail", assessment="pass" if handled else "fail",
                      score_min=0.0, score_max=1.0,
                      explanation="Unknown destinations must be reported, not invented.",
                      evaluator="missing_data_check", evaluator_type="code",
                      conversation_id=session_id, agent_name="travel_planner")
                return answer

            transcript.append((user_msg, turn(user_msg)))
        # Session-level eval: did the whole conversation reach the user's goal?
        convo = "\n".join(f"User: {u}\nAssistant: {a}" for u, a in transcript)
        j = judge("Did the assistant achieve the user's goals across this conversation?", convo,
                  tool_results=session_tools)
        score(name="session_goal_success", value=j.value, label=j.label, explanation=j.explanation,
              level="session", conversation_id=session_id, evaluator="travel_session_judge",
              evaluator_type="llm_judge", score_min=1.0, score_max=5.0, direction="maximize",
              threshold=QUALITY_THRESHOLD, agent_name="travel_planner")
        print("session", session_id, "done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["experiment", "online"])
    parser.add_argument("--version", default="v2", choices=list(PROMPTS))
    parser.add_argument("--sessions", type=int, default=5)
    args = parser.parse_args()
    provider = register(endpoint="http://localhost:4318/v1/traces", service_name="travel-planner-sdk",
                        auto_instrument=False)
    try:
        if args.mode == "experiment":
            run_experiment(args.version)
        else:
            run_online(args.sessions, args.version)
    finally:
        provider.force_flush()
