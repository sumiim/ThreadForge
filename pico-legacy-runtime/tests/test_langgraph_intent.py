import pytest

from langgraph_pico.intent import (
    INTENT_CODE_CHANGE,
    INTENT_CONVERSATION,
    INTENT_READ_ONLY,
    normalize_task_mode,
    parse_conversation_output,
    parse_intent_output,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("auto", "auto"),
        (" conversation ", INTENT_CONVERSATION),
        ("READ_ONLY", INTENT_READ_ONLY),
        ("code_change", INTENT_CODE_CHANGE),
    ],
)
def test_normalize_task_mode_accepts_only_canonical_modes(value, expected):
    assert normalize_task_mode(value) == expected


@pytest.mark.parametrize("value", [None, "", "unknown", 1, object()])
def test_normalize_task_mode_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="task_mode"):
        normalize_task_mode(value)


def test_parse_intent_output_accepts_strict_json_and_disables_conversation_research():
    assert parse_intent_output(
        '{"intent":"read_only","requires_research":true}'
    ) == (INTENT_READ_ONLY, True)
    assert parse_intent_output(
        '{"intent":"conversation","requires_research":true}'
    ) == (INTENT_CONVERSATION, False)


@pytest.mark.parametrize(
    "payload",
    [
        '```json\n{"intent":"read_only","requires_research":true}\n```',
        '{"intent":"read_only","requires_research":true,"confidence":1}',
        '{"intent":"write","requires_research":false}',
        '{"intent":"read_only","requires_research":"true"}',
        '["read_only", true]',
    ],
)
def test_parse_intent_output_rejects_non_contract_payloads(payload):
    with pytest.raises((ValueError, TypeError)):
        parse_intent_output(payload)


def test_parse_conversation_output_is_strict_but_allows_tool_shaped_text():
    assert parse_conversation_output('{"answer":"literal <tool> text"}') == "literal <tool> text"

    for payload in ('{"answer":""}', '{"answer":"ok","extra":1}', "plain text"):
        with pytest.raises(ValueError):
            parse_conversation_output(payload)
