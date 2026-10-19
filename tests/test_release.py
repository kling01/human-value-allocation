from copy import deepcopy
from importlib.resources import files
import asyncio
import json

import pytest

from human_value.adapter import analysis_units, validate_dialogue
from human_value.analysis import analyze, demo_report, model_options, validate_analysis, UsageLedger
from human_value.cli import main
from human_value.render import render_html


@pytest.fixture
def dialogue():
    return json.loads(files("human_value").joinpath("demo.json").read_text())


@pytest.fixture
def answers():
    return json.loads(files("human_value").joinpath("demo-analysis.json").read_text())


def test_cleaner_identity_and_time_boundary(dialogue):
    dialogue["id"] = 'prefix-shared-with-another-session/full-id/α'
    units = analysis_units(dialogue)
    assert len(units) == 4
    assert len({u["id"] for u in units}) == 4
    assert json.loads(units[0]["id"]) == [dialogue["id"], "u1"]
    assert all(u["thread_id"] == dialogue["id"] and "observations" not in u and "root" in u for u in units)
    ctx = units[1]["evaluation_context"]["Metadata"]
    assert ctx["human_message"]["id"] == "u2"
    assert [m["id"] for m in ctx["history"]] == ["u1", "a1"]
    assert [m["id"] for m in ctx["followup"]] == ["a2"]
    assert dialogue["messages"][4]["content"] not in json.dumps(units[1])


def test_reject_duplicate_ids_and_unsupported_payloads(dialogue):
    dialogue["messages"][1]["id"] = "u1"
    with pytest.raises(ValueError, match="unique"):
        validate_dialogue(dialogue)
    with pytest.raises(ValueError):
        validate_dialogue({"id": "x", "messages": [{"id": "u", "role": "user", "content": [{"image": "x"}]}]})


def test_empty_current_input_does_not_reuse_previous_turn(dialogue):
    from human_value._engine.llm.eval import extract_trace_context
    dialogue["messages"] = dialogue["messages"][:3]
    dialogue["messages"][-1]["content"] = ""
    ctx = extract_trace_context(analysis_units(dialogue)[-1])
    assert ctx["User_Input"] == ""
    assert ctx["Agent_output"] == ""


def test_quotes_must_belong_to_target(dialogue, answers):
    ctx = analysis_units(dialogue)[0]["evaluation_context"]["Metadata"]
    a = answers[0]["analysis"]
    assert validate_analysis(a, ctx, 1) == a
    a["contributions"][0]["evidence"] = {"message_id": "a1", "quote": "Segment A converted at 90%"}
    with pytest.raises(ValueError, match="verbatim"):
        validate_analysis(a, ctx, 1)


def test_no_followup_and_false_observation(dialogue, answers):
    dialogue["messages"] = dialogue["messages"][:1]
    ctx = analysis_units(dialogue)[0]["evaluation_context"]["Metadata"]
    a = answers[0]["analysis"]
    with pytest.raises(ValueError, match="Followup"):
        validate_analysis(a, ctx, 1)
    a["followup"] = {"status": "no_followup", "description": "No later response supplied.", "evidence": []}
    validate_analysis(a, ctx, 1)
    with pytest.raises(ValueError, match="disagree"):
        validate_analysis(a, ctx, 0)


def test_demo_and_html_escape(tmp_path):
    report = demo_report()
    assert report["provenance"] == "illustrative_fixture_not_model_output"
    assert report["summary"]["analyzed_turns"] == 4
    assert report["evaluation_usage"] == []
    report["messages"][0]["content"] = '<script>alert("x")</script>'
    html = render_html(report)
    assert '<script>' not in html
    assert '&lt;script&gt;' in html
    assert 'No model was called' in html
    assert main(["demo", "--out", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out/report.html").exists()
    assert main(["demo", "--out", str(tmp_path / "out")]) == 1


def test_real_engine_integration_with_mock_provider(monkeypatch, dialogue, answers):
    # Only the provider boundary is replaced. Adapter, cleaner, evaluator
    # compilation, run_combined, parsing and reporting all execute normally.
    from human_value._engine.llm import eval as engine_eval
    calls = []

    async def provider(prompt, client):
        ctx = json.loads(prompt.split('】\n')[-1])
        i = int(ctx["target_message_id"][1:]) - 1
        calls.append(ctx)
        return json.dumps({**answers[i], "reason": "Fixture-backed provider response."})

    monkeypatch.setattr(engine_eval, "call_llm", provider)
    result = asyncio.run(analyze(dialogue, api_key="test-only", base_url="http://127.0.0.1:1/v1", model="test-model"))
    assert len(calls) == 4
    assert result["summary"]["unresolved_turns"] == 0
    assert result["summary"]["category_occurrences"]["correction"] == 1
    assert result["turns"][3]["analysis"]["contributions"] == []
    assert result["provenance"] == "model_analysis"


@pytest.mark.parametrize("mode,expected", [("provider_error", "technical_error"), ("bad_quote", "invalid_analysis"), ("bad_schema", "invalid_analysis")])
def test_failures_never_become_zero_value(monkeypatch, dialogue, answers, mode, expected):
    from human_value._engine.llm import eval as engine_eval
    dialogue["messages"] = dialogue["messages"][:2]

    async def provider(prompt, client):
        if mode == "provider_error":
            raise RuntimeError("private-endpoint-and-secret-must-not-leak")
        answer = deepcopy(answers[0])
        if mode == "bad_quote":
            answer["analysis"]["contributions"][0]["evidence"]["quote"] = "invented evidence"
        else:
            answer["analysis"] = {}
        return json.dumps({**answer, "reason": "Test"})

    monkeypatch.setattr(engine_eval, "call_llm", provider)
    result = asyncio.run(analyze(dialogue, api_key="test-only", base_url="http://127.0.0.1:1", model="test-model"))
    assert result["turns"][0]["status"] == expected
    assert result["turns"][0]["analysis"] is None
    assert result["summary"]["unresolved_turns"] == 1
    assert result["summary"]["analyzed_turns"] == 0
    assert "private-endpoint-and-secret-must-not-leak" not in json.dumps(result)


def test_provider_http_retry_and_usage(monkeypatch):
    import httpx
    from human_value._engine.llm.client import call_llm_with_usage
    from human_value._engine.llm.runtime import model_runtime
    attempts = []
    ledger = UsageLedger()

    def transport(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(200, json={"model": "actual-test-model", "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 12, "completion_tokens": 3}})

    async def no_sleep(delay):
        pass

    monkeypatch.setattr("human_value._engine.llm.client.asyncio.sleep", no_sleep)

    async def run():
        with model_runtime(options=model_options("test-only", "https://example.test/v1", "requested-model"), usage_sink=ledger):
            async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
                return await call_llm_with_usage("test prompt", client)

    reply = asyncio.run(run())
    assert reply.input_tokens == 12 and reply.output_tokens == 3
    assert len(attempts) == 2 and str(attempts[0].url) == "https://example.test/v1/chat/completions"
    records = list(ledger.calls.values())
    assert records[0]["input_tokens"] is None
    assert records[1]["model_id"] == "actual-test-model"
    assert all("api_key" not in c and "payload" not in c for c in records)


@pytest.mark.parametrize("url", ["https://u:p@example.test/v1", "https://example.test/v1?key=x", "http://example.test/v1", "file:///tmp/data"])
def test_credentials_and_insecure_endpoint_rejected(url):
    with pytest.raises(ValueError):
        model_options("test-only", url, "model")


def test_public_engine_excludes_business_features():
    from human_value._engine.core.feature_registry import feature_manifest
    categories = {f["category"] for f in feature_manifest()}
    assert not categories & {"debt_collection", "stage_routing_warning", "sys_context", "profile"}


def test_input_limits_are_explicit(dialogue):
    dialogue["messages"][0]["content"] = "x" * 200_001
    with pytest.raises(ValueError, match="Nothing was truncated"):
        validate_dialogue(dialogue)
