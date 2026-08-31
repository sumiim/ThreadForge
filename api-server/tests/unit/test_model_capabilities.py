"""§2.2 模型×档位矩阵：控制面 _parse_model_capabilities 向后兼容透传可选字段。"""

import pytest

from threadforge_api.domain.errors import WorkerProtocolError
from threadforge_api.infrastructure.worker_hub import _parse_model_capabilities


def test_parse_model_capabilities_passthroughs_optional_fields():
    """能识别最大 token / context_window / usage_fields / supports_temperature。"""
    raw = {
        "provider": "chat-completions",
        "models": [
            {
                "id": "deepseek-v4-flash",
                "display_name": "deepseek-v4-flash",
                "reasoning_efforts": ["none", "high"],
                "max_output_tokens": 8192,
                "context_window": 128000,
                "usage_fields": ["input_tokens", "output_tokens"],
                "supports_temperature": False,
            },
            {
                "id": "deepseek-v4-pro",
                "display_name": "deepseek-v4-pro",
                "reasoning_efforts": ["none", "low", "max"],
            },
        ],
    }
    parsed = _parse_model_capabilities(raw, "fallback")
    assert parsed["provider"] == "chat-completions"
    assert len(parsed["models"]) == 2

    first = parsed["models"][0]
    assert first["id"] == "deepseek-v4-flash"
    assert first["reasoning_efforts"] == ["none", "high"]
    assert first["max_output_tokens"] == 8192
    assert first["context_window"] == 128000
    assert first["usage_fields"] == ["input_tokens", "output_tokens"]
    assert first["supports_temperature"] is False

    # 无可选字段的模型：仅保留 id/display_name/reasoning_efforts（向后兼容）
    second = parsed["models"][1]
    assert second["id"] == "deepseek-v4-pro"
    assert second["reasoning_efforts"] == ["none", "low", "max"]
    assert "max_output_tokens" not in second


def test_parse_model_capabilities_validates_efforts():
    """推理档位不合法时抛 WorkerProtocolError。"""
    raw = {
        "provider": "openai-compatible",
        "models": [{"id": "m", "display_name": "m", "reasoning_efforts": ["turbo"]}],
    }
    with pytest.raises(WorkerProtocolError):
        _parse_model_capabilities(raw, "fallback")


def test_parse_model_capabilities_falls_back_when_empty():
    """空模型列表回退到 fallback_model 单条 none 档（向后兼容）。"""
    parsed = _parse_model_capabilities({"provider": "anthropic", "models": []}, "gpt-5.6-sol")
    assert parsed["models"] == [
        {"id": "gpt-5.6-sol", "display_name": "gpt-5.6-sol", "reasoning_efforts": ["none"]}
    ]
