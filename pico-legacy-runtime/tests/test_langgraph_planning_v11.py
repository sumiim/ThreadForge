import json

import pytest
from langgraph_pico.planning import (
    PlanValidationError,
    build_plan_prompt,
    is_plain_conversation_request,
    parse_and_validate_plan,
)

from pico import Pico


MAXIMUM_BUDGETS = {
    "model_rounds": 64,
    "tool_calls": 25,
    "input_tokens": 500_000,
    "output_tokens": 32_000,
    "elapsed_seconds": 3_600,
}


def _plan(**overrides):
    value = {
        "schema_version": "1",
        "plan_id": "plan_test",
        "revision": 1,
        "intent": "read_only",
        "summary": "Inspect the requested file.",
        "steps": [
            {
                "id": "inspect",
                "goal": "Read the file",
                "dependencies": [],
                "required_tools": ["read_file"],
                "required_evidence": ["current file content"],
                "done_when": ["the file has been read"],
            }
        ],
        "acceptance": ["answer is grounded in current workspace evidence"],
        "risk_level": "low",
        "budgets": {
            "model_rounds": 4,
            "tool_calls": 3,
            "input_tokens": 20_000,
            "output_tokens": 2_000,
            "elapsed_seconds": 120,
        },
    }
    value.update(overrides)
    return value


def test_v11_plan_is_strict_json_and_validates_available_tools():
    plan = parse_and_validate_plan(
        json.dumps(_plan()),
        available_tools={"read_file"},
        maximum_budgets=MAXIMUM_BUDGETS,
    )
    assert plan["intent"] == "read_only"
    assert plan["steps"][0]["id"] == "inspect"

    numeric_schema = _plan(schema_version=1)
    parsed_numeric_schema = parse_and_validate_plan(
        json.dumps(numeric_schema),
        available_tools={"read_file"},
        maximum_budgets=MAXIMUM_BUDGETS,
    )
    assert parsed_numeric_schema["schema_version"] == "1"

    unavailable = _plan()
    unavailable["steps"][0]["required_tools"] = ["run_shell"]
    with pytest.raises(PlanValidationError, match="unavailable"):
        parse_and_validate_plan(
            json.dumps(unavailable),
            available_tools={"read_file"},
            maximum_budgets=MAXIMUM_BUDGETS,
        )


def test_v11_plan_rejects_cycles_and_excessive_budgets():
    cyclic = _plan()
    cyclic["steps"] = [
        {**cyclic["steps"][0], "id": "a", "dependencies": ["b"]},
        {**cyclic["steps"][0], "id": "b", "dependencies": ["a"]},
    ]
    with pytest.raises(PlanValidationError, match="cycle"):
        parse_and_validate_plan(
            json.dumps(cyclic),
            available_tools={"read_file"},
            maximum_budgets=MAXIMUM_BUDGETS,
        )

    excessive = _plan()
    excessive["budgets"]["tool_calls"] = 26
    with pytest.raises(PlanValidationError, match="exceeds"):
        parse_and_validate_plan(
            json.dumps(excessive),
            available_tools={"read_file"},
            maximum_budgets=MAXIMUM_BUDGETS,
        )

    incomplete_run = _plan()
    incomplete_run["budgets"].update(
        {
            "model_rounds": 2,
            "input_tokens": 1_000,
            "output_tokens": 256,
            "elapsed_seconds": 30,
        }
    )
    normalized = parse_and_validate_plan(
        json.dumps(incomplete_run),
        available_tools={"read_file"},
        maximum_budgets=MAXIMUM_BUDGETS,
    )
    assert normalized["budgets"] == {
        "elapsed_seconds": 120,
        "input_tokens": 20_000,
        "model_rounds": 4,
        "output_tokens": 2_000,
        "tool_calls": 3,
    }


def test_v11_plan_normalizes_scalar_step_contract_and_allows_zero_tool_budget():
    conversation = _plan(intent="conversation")
    conversation["steps"][0].update(
        {
            "required_tools": [],
            "required_evidence": [],
            "done_when": "a concise response is returned",
        }
    )
    conversation["budgets"]["tool_calls"] = 0

    parsed = parse_and_validate_plan(
        json.dumps(conversation),
        available_tools={"read_file"},
        maximum_budgets=MAXIMUM_BUDGETS,
    )

    assert parsed["steps"][0]["required_evidence"] == []
    assert parsed["steps"][0]["done_when"] == ["a concise response is returned"]
    assert parsed["budgets"]["tool_calls"] == 0


def test_v11_conversation_plan_rejects_workspace_tools_or_evidence():
    conversation = _plan(intent="conversation")
    conversation["steps"][0]["required_tools"] = ["read_file"]
    conversation["steps"][0]["required_evidence"] = ["file content"]

    with pytest.raises(PlanValidationError, match="cannot require tools"):
        parse_and_validate_plan(
            json.dumps(conversation),
            available_tools={"read_file"},
            maximum_budgets=MAXIMUM_BUDGETS,
        )


def test_v11_recognizes_only_short_social_requests_as_direct_conversation():
    assert is_plain_conversation_request("你好")
    assert is_plain_conversation_request("hello!")
    assert is_plain_conversation_request("谢谢")
    assert not is_plain_conversation_request("看看当前工作区")
    assert not is_plain_conversation_request("继续修复停止按钮")


def test_v11_retry_prompt_explains_the_validation_failure_and_array_contract():
    prompt = build_plan_prompt(
        "hello",
        "",
        {"read_file"},
        MAXIMUM_BUDGETS,
        retry=True,
        validation_error="plan_required_evidence_invalid: invalid required_evidence",
    )

    assert "plan_required_evidence_invalid" in prompt
    assert "must be JSON arrays" in prompt
    assert "tool_calls may be 0" in prompt
    assert "minimum_budgets" in prompt


def test_v11_agent_turn_requires_explicit_talk_tool_or_final():
    assert Pico.parse("I am still working.")[0] == "retry"
    assert Pico.parse("<talk>I am checking the workspace.</talk>") == (
        "talk",
        "I am checking the workspace.",
    )
    assert Pico.parse("<final>Done.</final>") == ("final", "Done.")
