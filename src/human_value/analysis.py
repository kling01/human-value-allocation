"""Analysis validation and orchestration; all model judging uses agent-eval-v2."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from importlib.resources import files
import json
import hashlib
from urllib.parse import urlsplit

from human_value import __version__
from human_value.adapter import analysis_units, validate_dialogue

CATEGORIES = {
    "goal_framing": "Goal framing · 目标定义",
    "information": "Information · 信息补充",
    "constraints": "Constraints · 约束设定",
    "correction": "Correction · 纠错",
    "decision": "Decision · 决策取舍",
    "verification": "Verification · 检验与验收",
}


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text.")
    return value


def _evidence(value, messages):
    if not isinstance(value, dict) or set(value) != {"message_id", "quote"}:
        raise ValueError("Evidence requires exactly message_id and quote.")
    quote = _text(value["quote"], "quote")
    if value["message_id"] not in messages or quote not in messages[value["message_id"]]["content"]:
        raise ValueError("Evidence quote does not occur verbatim in the allowed source message.")


def validate_analysis(value: dict, context: dict, score: int) -> dict:
    if not isinstance(value, dict) or set(value) != {"summary", "contributions", "followup", "limitations"}:
        raise ValueError("Analysis has an invalid shape.")
    _text(value["summary"], "summary")
    contributions = value["contributions"]
    if not isinstance(contributions, list) or len(contributions) > 6:
        raise ValueError("contributions must be a list of at most six category observations.")
    if type(score) is not int or score not in (0, 1) or bool(score) != bool(contributions):
        raise ValueError("Presence flag and contribution list disagree.")
    human = context["human_message"]
    seen = set()
    for c in contributions:
        if not isinstance(c, dict) or set(c) != {"category", "description", "evidence"}:
            raise ValueError("Invalid contribution shape.")
        if c["category"] not in CATEGORIES or c["category"] in seen:
            raise ValueError("Contribution category is unknown or duplicated.")
        seen.add(c["category"])
        _text(c["description"], "description")
        _evidence(c["evidence"], {human["id"]: human})
    followup = value["followup"]
    if not isinstance(followup, dict) or set(followup) != {"status", "description", "evidence"}:
        raise ValueError("Invalid followup shape.")
    if followup["status"] not in {"observed", "not_observed", "unclear", "no_followup"}:
        raise ValueError("Invalid followup status.")
    _text(followup["description"], "followup description")
    available = {m["id"]: m for m in context["followup"] if m["role"] in {"assistant", "tool"}}
    if (not available) != (followup["status"] == "no_followup"):
        raise ValueError("Followup status disagrees with the supplied conversation.")
    if not isinstance(followup["evidence"], list):
        raise ValueError("Followup evidence must be a list.")
    if followup["status"] == "observed" and not followup["evidence"]:
        raise ValueError("Observed uptake requires a visible followup quote.")
    for ev in followup["evidence"]:
        _evidence(ev, available)
    if not isinstance(value["limitations"], list) or not value["limitations"]:
        raise ValueError("At least one limitation must be stated.")
    for limitation in value["limitations"]:
        _text(limitation, "limitation")
    return value


class UsageLedger:
    """Store usage only. No prompt, response body, endpoint or authentication data."""
    def __init__(self):
        self.calls = {}

    def on_start(self, call_id, **kwargs):
        self.calls[call_id] = {"call_id": call_id, "status": "started"}

    def _finish(self, call_id, status, **kwargs):
        keep = {"input_tokens", "output_tokens", "usage_source", "model_id", "latency_ms", "attempt", "finish_reason", "error_type"}
        self.calls[call_id] = {"call_id": call_id, "status": status,
                               **{k: v for k, v in kwargs.items() if k in keep}}

    def on_complete(self, call_id, **kwargs):
        self._finish(call_id, "completed", **kwargs)

    def on_fail(self, call_id, **kwargs):
        self._finish(call_id, "failed", **kwargs)


def make_report(data, units, rows, *, provenance, evaluator_hash, model=None, usage=None):
    counts = Counter(c["category"] for r in rows if r["status"] == "ok" for c in r["analysis"]["contributions"])
    return {
        "schema_version": "hva/report/v0.1", "software_version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provenance": provenance, "session_id": data["id"], "messages": data["messages"],
        "evaluator_sha256": evaluator_hash, "requested_model": model,
        "summary": {"human_turns": len(units), "analyzed_turns": sum(r["status"] == "ok" for r in rows),
                    "unresolved_turns": sum(r["status"] != "ok" for r in rows),
                    "category_occurrences": {k: counts[k] for k in CATEGORIES}},
        "turns": rows, "evaluation_usage": usage or [],
        "limits": [
            "Review each contribution together with its source quote and the assistant's visible followup.",
            "Categories can overlap. Read occurrence counts independently, rather than as percentages of a shared total.",
            "Quote matching verifies the source text. Review whether that evidence supports the interpretation.",
            "Unresolved turns are shown separately and excluded from contribution counts.",
            "Evaluation usage records the model resources consumed by this analysis.",
        ],
    }


def model_options(api_key, base_url, model, *, allow_insecure_http=False):
    if not api_key or not model or not base_url:
        raise ValueError("Set HVA_API_KEY, HVA_BASE_URL and HVA_MODEL before analysis.")
    parsed = urlsplit(base_url)
    if not parsed.hostname or parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Base URL must be an http(s) endpoint without embedded credentials, query or fragment.")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"} and not allow_insecure_http:
        raise ValueError("Use HTTPS for remote endpoints, or explicitly opt in with --allow-insecure-http for a trusted gateway.")
    return {"api_key": api_key, "base_url": base_url.rstrip("/"), "model": model,
            "max_tokens": 8192, "timeout": 120, "retry_attempts": 2,
            "response_format": {"type": "json_object"}, "trust_env": False}


async def analyze(data: dict, *, api_key: str, base_url: str, model: str, concurrency: int = 4, allow_insecure_http: bool = False):
    from human_value._engine.core.pipeline import PipelineConfig, run_combined
    if not 1 <= concurrency <= 20:
        raise ValueError("concurrency must be between 1 and 20.")
    options = model_options(api_key, base_url, model, allow_insecure_http=allow_insecure_http)
    data = validate_dialogue(data)
    units = analysis_units(data)
    rubric = files("human_value").joinpath("evaluator.yaml").read_text(encoding="utf-8")
    ledger = UsageLedger()
    result = await run_combined(units, PipelineConfig(
        llm_yaml=rubric, llm_options=options, usage_sink=ledger, concurrency=concurrency,
        n_runs=1, source_name="human-value-allocation", version=__version__,
    ))
    by_id = {r["full_trace_id"]: r for r in result.evaluator_results}
    rows = []
    for unit in units:
        context = unit["evaluation_context"]["Metadata"]
        row = {"message_id": context["target_message_id"], "status": "technical_error", "analysis": None}
        raw = by_id.get(unit["id"], {})
        if raw.get("execution_status") == "ok":
            try:
                step = raw["steps"][0]
                row["analysis"] = validate_analysis(step["analysis"], context, step["passed"])
                row["status"] = "ok"
            except (ValueError, KeyError, TypeError, IndexError):
                row["status"] = "invalid_analysis"
        # Raw provider exceptions may contain an endpoint. They are not written
        # into a shareable report. Failed units are explicit, never scored zero.
        if row["status"] != "ok":
            row["error"] = "Analysis unavailable. Check provider configuration or retry; no contribution judgment was made."
        rows.append(row)
    return make_report(data, units, rows, provenance="model_analysis", model=model,
                       evaluator_hash=hashlib.sha256(rubric.encode()).hexdigest(), usage=list(ledger.calls.values()))


def demo_report():
    """Offline fixture replay, prominently distinguished from live inference."""
    package = files("human_value")
    data = validate_dialogue(json.loads(package.joinpath("demo.json").read_text(encoding="utf-8")))
    answers = json.loads(package.joinpath("demo-analysis.json").read_text(encoding="utf-8"))
    units = analysis_units(data)
    rows = []
    for unit, answer in zip(units, answers, strict=True):
        context = unit["evaluation_context"]["Metadata"]
        validate_analysis(answer["analysis"], context, answer["score"])
        rows.append({"message_id": context["target_message_id"], "status": "ok", "analysis": answer["analysis"]})
    rubric = package.joinpath("evaluator.yaml").read_bytes()
    return make_report(data, units, rows, provenance="illustrative_fixture_not_model_output",
                       evaluator_hash=hashlib.sha256(rubric).hexdigest())
