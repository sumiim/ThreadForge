"""模型后端适配层。

runtime 只关心一件事：给我一个 prompt，我拿回一段文本。
不同 provider 在 HTTP 接口、响应结构、是否支持 prompt cache 上都有差异，
这些差异都在这里被抹平成统一的 complete() 接口。
"""

import json
import socket
import time
import urllib.error
import urllib.request
from http.client import IncompleteRead, RemoteDisconnected

OPENAI_COMPATIBLE_USER_AGENT = "pico/0.1"
RETRYABLE_HTTP_STATUSES = {408, 425, 429}


class ModelProviderError(RuntimeError):
    """Sanitized provider failure safe to persist and expose to clients."""

    def __init__(self, code, *, retryable=False, attempts=1, status_code=None):
        super().__init__(str(code))
        self.code = str(code)
        self.retryable = bool(retryable)
        self.attempts = max(1, int(attempts))
        self.status_code = int(status_code) if status_code is not None else None


def _provider_http_error(code, attempts):
    status = int(code)
    retryable = status in RETRYABLE_HTTP_STATUSES or status >= 500
    if status == 429:
        error_code = "model_rate_limited"
    elif status in {408, 425}:
        error_code = "model_timeout"
    elif status >= 500:
        error_code = "model_server_error"
    elif status in {401, 403}:
        error_code = "model_auth_error"
    else:
        error_code = "model_request_rejected"
    return ModelProviderError(
        error_code,
        retryable=retryable,
        attempts=attempts,
        status_code=status,
    )


def _retry_delay(attempt, retry_after=""):
    try:
        value = float(str(retry_after).strip())
    except (TypeError, ValueError):
        value = 0.0
    if value > 0:
        return min(value, 8.0)
    return min(0.5 * (2 ** max(0, int(attempt) - 1)), 4.0)


def _invalid_response_metadata(body_text, attempt):
    """Safe, bounded diagnostics for an unusable provider response.

    Only records the shape of the response (length + top-level JSON keys),
    never the raw model text, so nothing sensitive leaks into trace/report.
    """
    text = "" if body_text is None else str(body_text)
    metadata = {
        "provider_error_code": "model_response_invalid",
        "provider_error_retryable": True,
        "provider_request_attempts": int(attempt),
        "response_chars": len(text),
    }
    stripped = text.strip()
    if stripped:
        try:
            value = json.loads(stripped)
        except (TypeError, ValueError):
            value = None
        if isinstance(value, dict):
            metadata["response_top_level_keys"] = sorted(
                str(key)[:64] for key in value
            )[:20]
    return metadata


class FakeModelClient:
    # Offline/scripted clients do not have a separate review response stream.
    # The web runtime uses this marker to avoid consuming the scripted main-loop
    # outputs as review-subagent JSON.
    supports_review_subagent = False
    # §7.8.9 决策（2026-08-18）：scripted 测试不开 planning——避免 planning 消费
    # 顺序输出（真实模型默认开,见 run_native 的 feature_flags）。
    supports_planning = False

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.prompts.append(prompt)
        if not getattr(self, "last_completion_metadata", None):
            self.last_completion_metadata = {}
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        return self.outputs.pop(0)


class OllamaModelClient:
    def __init__(self, model, host, temperature, top_p, timeout):
        self.model = model
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        # Ollama 当前不支持我们这里接入的 prompt cache 语义，
        # 所以 runtime 传下来的缓存参数会被忽略。
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "raw": False,
            "think": False,
            "options": {
                "num_predict": max_new_tokens,
                "temperature": self.temperature,
                "top_p": self.top_p,
            },
        }
        request = urllib.request.Request(
            self.host + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama request failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                "Could not reach Ollama.\n"
                "Make sure `ollama serve` is running and the model is available.\n"
                f"Host: {self.host}\n"
                f"Model: {self.model}"
            ) from exc

        if data.get("error"):
            raise RuntimeError(f"Ollama error: {data['error']}")
        return data.get("response", "")


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _extract_openai_text(data):
    if data.get("output_text"):
        return data["output_text"]

    for item in data.get("output", []):
        for content in item.get("content", []):
            if isinstance(content, dict):
                text = content.get("text")
                if text:
                    return text

    choices = data.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        return text

    return ""


def _extract_openai_function_call(data):
    """Convert one provider-native function call into the runtime protocol.

    §7.7.1 阶段 3（2.1 原生 tool calling）：直接返回原生 dict
    ``{"name": ..., "args": ...}``，AgentLoop/Pico.parse 直接消费，
    不再序列化成 ``<tool>`` 文本往返。
    """

    def normalize(item):
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return None
        name = str(item.get("name", "")).strip()
        if not name:
            return None
        arguments = item.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                # Preserve the invalid shape for Pico.parse() to reject via
                # the bounded protocol-repair path without exposing its text.
                arguments = arguments
        return {"name": name, "args": arguments}

    for item in data.get("output", []):
        action = normalize(item)
        if action:
            return action

    choices = data.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        for call in message.get("tool_calls", []) or []:
            function = call.get("function", {}) if isinstance(call, dict) else {}
            action = normalize({"type": "function_call", **function})
            if action:
                return action
    return ""


def _strip_single_code_fence(text):
    """Remove one surrounding markdown code fence when the whole text is fenced.

    Some providers (e.g. SiliconFlow DeepSeek-V3.2) intermittently wrap JSON
    output in ```json ... ``` fences. The orchestrator's strict JSON parsers
    reject fenced payloads by contract, so the provider adapter normalizes
    the whole-text single-fence shape here; anything else passes through.
    """
    raw = str(text or "").strip()
    if raw.startswith("```") and raw.endswith("```") and len(raw) > 6:
        body = raw[3:].lstrip("\r\n")
        if body.startswith(("json", "JSON")):
            body = body[4:].lstrip()
        raw = body.rsplit("```", 1)[0].strip()
    return raw


def _normalize_openai_native_text(text, *, native_tools_enabled):
    """Treat a provider-native assistant message as the final agent action.

    OpenAI Responses uses the absence of a function call to signal a normal
    assistant message. The legacy ThreadForge parser still expects an explicit
    control envelope, so normalize only native-tool requests and leave legacy
    XML/JSON protocol responses untouched.
    """

    text = _strip_single_code_fence(text)
    text = str(text or "").strip()
    if not text or not native_tools_enabled:
        return text
    if any(marker in text for marker in ("<tool", "<talk>", "<final>")):
        return text
    return f"<final>{text}</final>"


def _extract_openai_text_from_sse(body_text):
    last_response = None
    deltas = []
    for line in body_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
            continue
        if event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str) and text:
                return text
        part = event.get("part")
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text:
                return text
        item = event.get("item")
        if isinstance(item, dict):
            text = _extract_openai_text({"output": [item]})
            if text:
                return text
        response = event.get("response")
        if isinstance(response, dict):
            last_response = response
            text = _extract_openai_text(response)
            if text:
                return text
        text = _extract_openai_text(event)
        if text:
            return text
    if deltas:
        return "".join(deltas)
    if isinstance(last_response, dict):
        return _extract_openai_text(last_response)
    return ""


def _extract_openai_response_from_sse(body_text):
    last_response = None
    deltas = []
    for line in body_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        response = event.get("response")
        if isinstance(response, dict):
            last_response = response
            if event.get("type") == "response.completed":
                text = _extract_openai_text(response)
                if text:
                    return text, response
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
        elif event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str) and text:
                return text, last_response or {}
        else:
            text = _extract_openai_text(event)
            if text:
                return text, event
    if deltas:
        return "".join(deltas), last_response or {}
    if isinstance(last_response, dict):
        return _extract_openai_text(last_response), last_response
    return "", {}


def _consume_openai_response_stream(response, *, on_text_delta=None, should_cancel=None):
    """Consume an OpenAI Responses SSE body while it is still arriving.

    The old implementation first buffered the entire response and only then
    parsed SSE.  Keeping this parser line-oriented lets the runtime expose a
    safe projection of final-answer deltas and also gives cancellation and
    watchdogs a heartbeat during long reasoning calls.
    """
    last_response = None
    deltas = []
    response_data = {}
    done_text = ""

    def emit_missing_suffix(text):
        current = "".join(deltas)
        if not text.startswith(current):
            return
        suffix = text[len(current):]
        if not suffix:
            return
        deltas.append(suffix)
        if on_text_delta is not None:
            on_text_delta(suffix)

    for raw_line in response:
        if should_cancel is not None:
            should_cancel()
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace")
        else:
            line = str(raw_line)
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        response_payload = event.get("response")
        if isinstance(response_payload, dict):
            last_response = response_payload
            if event.get("type") == "response.completed":
                response_data = response_payload
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                deltas.append(delta)
                if on_text_delta is not None:
                    on_text_delta(delta)
            continue
        if event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str) and text:
                emit_missing_suffix(text)
                done_text = text
                continue
        if event_type == "response.completed" and isinstance(last_response, dict):
            text = _extract_openai_text(last_response)
            if text:
                emit_missing_suffix(text)
                done_text = text
            break
    if done_text:
        return done_text, response_data or last_response or {}
    if deltas:
        return "".join(deltas), response_data or last_response or {}
    return "", response_data or last_response or {}


def _extract_usage_cache_details(data):
    # 把不同 OpenAI-compatible 返回里的 usage 字段整理成统一结构，
    # 让 runtime/trace/report 不需要关心 provider 细节。
    usage = data.get("usage") or {}
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached_tokens = int(input_details.get("cached_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": cached_tokens,
        "cache_hit": cached_tokens > 0,
    }


def _extract_reasoning_summary(data):
    """从 OpenAI-compatible 响应里提取 reasoning 摘要（B 思考回传）。

    只取供应商明确提供的 summary/thinking 文本，截断到 2000 字符；不保留逐字
    思考、不从普通输出猜测。Responses API 的 output[type=reasoning].summary 与
    Chat Completions 的 reasoning_content/reasoning/thinking 字段都覆盖。
    """
    if not isinstance(data, dict):
        return ""
    output = data.get("output")
    if isinstance(output, list):
        parts = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "reasoning":
                continue
            summary = item.get("summary")
            if isinstance(summary, list):
                for part in summary:
                    if isinstance(part, dict) and part.get("type") == "summary_text":
                        text = str(part.get("text", "")).strip()
                        if text:
                            parts.append(text)
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                parts.append(content.strip())
        if parts:
            return "\n".join(parts)[:2000]
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:2000]
    return ""


class OpenAICompatibleModelClient:
    def __init__(
        self,
        model,
        base_url,
        api_key,
        temperature,
        timeout,
        max_attempts=3,
        *,
        reasoning_effort=None,
        supported_reasoning_efforts=(),
        supports_temperature_with_reasoning=False,
        instructions=None,
    ):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.supported_reasoning_efforts = tuple(
            str(value).strip() for value in supported_reasoning_efforts if str(value).strip()
        )
        self.reasoning_effort = str(reasoning_effort or "").strip().lower()
        if self.reasoning_effort and self.reasoning_effort not in self.supported_reasoning_efforts:
            raise ValueError("reasoning_effort is not supported by this provider/model")
        self.supports_temperature_with_reasoning = bool(supports_temperature_with_reasoning)
        self.instructions = str(instructions or "").strip()
        if not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        self.max_attempts = max_attempts
        # 当前只在明确支持 prompt cache 语义的后端上启用这条链路，
        # 避免对不支持的后端传一个“看起来统一、其实没意义”的伪参数。
        self.supports_prompt_cache = any(host in self.base_url for host in ("openai.com", "right.codes"))
        self.supports_native_tools = True
        self.last_completion_metadata = {}

    def complete(
        self,
        prompt,
        max_new_tokens,
        prompt_cache_key=None,
        prompt_cache_retention=None,
        *,
        deadline_monotonic=None,
        on_retry=None,
        on_text_delta=None,
        should_cancel=None,
        tool_definitions=None,
    ):
        """向 OpenAI-compatible `/responses` 接口发起一次模型调用。

        为什么存在：
        runtime 不应该知道 HTTP 细节、SSE 细节、usage 字段长什么样，
        更不应该自己去判断 prompt cache 参数要不要带。这个函数把这些后端
        细节都包起来，对上层暴露统一的 `complete()` 行为。

        输入 / 输出：
        - 输入：完整 prompt、最大输出 token，以及可选的 prompt cache 参数
        - 输出：模型最终文本；同时把 usage / cached_tokens 等元数据写进
          `self.last_completion_metadata`

        在 agent 链路里的位置：
        它位于 `Pico.ask()` 的模型调用阶段，是稳定前缀缓存复用链路真正
        落到 provider API 的地方。
        """
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": prompt,
                        }
                    ],
                }
            ],
            "max_output_tokens": max_new_tokens,
            "stream": True,
        }
        if self.instructions:
            payload["instructions"] = self.instructions
        native_tools_enabled = bool(tool_definitions) and self.supports_native_tools
        if native_tools_enabled:
            payload["tools"] = list(tool_definitions)
            payload["parallel_tool_calls"] = False
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        if self.temperature is not None and (
            self.reasoning_effort in {"", "none"} or self.supports_temperature_with_reasoning
        ):
            payload["temperature"] = self.temperature
        # runtime 传入的是“稳定前缀”的签名，而不是整段 prompt 的签名。
        # 这样缓存复用针对的是稳定段，不会因为动态 history 每轮变化而失效。
        if self.supports_prompt_cache and prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key
        if self.supports_prompt_cache and prompt_cache_retention:
            payload["prompt_cache_retention"] = prompt_cache_retention

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": OPENAI_COMPATIBLE_USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            self.base_url + "/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        def check_cancelled():
            if should_cancel is not None:
                should_cancel()

        def remaining_timeout(attempt):
            check_cancelled()
            if deadline_monotonic is None:
                return self.timeout
            remaining = float(deadline_monotonic) - time.monotonic()
            if remaining <= 0:
                self.last_completion_metadata = {
                    "provider_error_code": "model_timeout",
                    "provider_error_retryable": True,
                    "provider_request_attempts": int(attempt),
                }
                raise ModelProviderError("model_timeout", retryable=True, attempts=attempt)
            return min(float(self.timeout), remaining)

        def notify_retry(attempt, error, delay):
            if on_retry is not None:
                on_retry(
                    {
                        "attempt": int(attempt),
                        "max_attempts": int(self.max_attempts),
                        "error_code": str(getattr(error, "code", "model_connection_error")),
                        "retry_delay_seconds": float(delay),
                    }
                )

        for attempt in range(1, self.max_attempts + 1):
            check_cancelled()
            try:
                with urllib.request.urlopen(request, timeout=remaining_timeout(attempt)) as response:
                    headers = getattr(response, "headers", {}) or {}
                    content_type = headers.get("Content-Type", "")
                    if content_type.startswith("text/event-stream"):
                        streamed_text, response_data = _consume_openai_response_stream(
                            response,
                            on_text_delta=on_text_delta,
                            should_cancel=check_cancelled,
                        )
                        self.last_completion_metadata = {
                            "requested_reasoning_effort": self.reasoning_effort,
                            "effective_reasoning_effort": self.reasoning_effort,
                            "reasoning_summary": _extract_reasoning_summary(response_data or {}),
                            "prompt_cache_supported": self.supports_prompt_cache,
                            "prompt_cache_key": prompt_cache_key,
                            "prompt_cache_retention": prompt_cache_retention,
                            **_extract_usage_cache_details(response_data or {}),
                        }
                        native_action = _extract_openai_function_call(response_data or {})
                        if native_action:
                            self.last_completion_metadata["native_tool_call"] = True
                            return native_action
                        if streamed_text:
                            normalized_text = _normalize_openai_native_text(
                                streamed_text,
                                native_tools_enabled=native_tools_enabled,
                            )
                            if normalized_text != streamed_text.strip():
                                self.last_completion_metadata["native_text_response"] = True
                            return normalized_text
                        # 流式响应既没有文本也没有原生工具调用：网关返回了空/不可解析的响应。
                        self.last_completion_metadata.update(
                            _invalid_response_metadata("", attempt)
                        )
                        raise ModelProviderError(
                            "model_response_invalid", retryable=True, attempts=attempt
                        )

                    body_text = response.read().decode("utf-8")
                    # 有些兼容后端返回普通 JSON，有些返回 SSE（content-type 未标注）。
                    # 这里两种都接住，并尽量统一抽取文本和 usage/cache 元数据。
                    if body_text.lstrip().startswith("data:"):
                        text, response_data = _extract_openai_response_from_sse(body_text)
                        if isinstance(response_data, dict) and response_data:
                            self.last_completion_metadata = {
                                "requested_reasoning_effort": self.reasoning_effort,
                                "effective_reasoning_effort": self.reasoning_effort,
                                "prompt_cache_supported": self.supports_prompt_cache,
                                "prompt_cache_key": prompt_cache_key,
                                "prompt_cache_retention": prompt_cache_retention,
                                **_extract_usage_cache_details(response_data),
                            }
                        native_action = _extract_openai_function_call(response_data or {})
                        if native_action:
                            self.last_completion_metadata["native_tool_call"] = True
                            return native_action
                        if text:
                            normalized_text = _normalize_openai_native_text(
                                text,
                                native_tools_enabled=native_tools_enabled,
                            )
                            if normalized_text != text.strip():
                                self.last_completion_metadata["native_text_response"] = True
                            return normalized_text
                        self.last_completion_metadata.update(
                            _invalid_response_metadata(body_text, attempt)
                        )
                        raise ModelProviderError(
                            "model_response_invalid", retryable=True, attempts=attempt
                        )

                    try:
                        data = json.loads(body_text)
                    except json.JSONDecodeError as exc:
                        self.last_completion_metadata.update(
                            _invalid_response_metadata(body_text, attempt)
                        )
                        raise ModelProviderError(
                            "model_response_invalid", retryable=True, attempts=attempt
                        ) from exc
                    if data.get("error"):
                        raise ModelProviderError("model_provider_error", attempts=attempt)
                    self.last_completion_metadata = {
                        "requested_reasoning_effort": self.reasoning_effort,
                        "effective_reasoning_effort": self.reasoning_effort,
                        "prompt_cache_supported": self.supports_prompt_cache,
                        "prompt_cache_key": prompt_cache_key,
                        "prompt_cache_retention": prompt_cache_retention,
                        **_extract_usage_cache_details(data),
                    }
                    native_action = _extract_openai_function_call(data)
                    if native_action:
                        self.last_completion_metadata["native_tool_call"] = True
                        return native_action
                    text = _extract_openai_text(data)
                    if not text:
                        self.last_completion_metadata.update(
                            _invalid_response_metadata(body_text, attempt)
                        )
                        raise ModelProviderError(
                            "model_response_invalid", retryable=True, attempts=attempt
                        )
                    normalized_text = _normalize_openai_native_text(
                        text,
                        native_tools_enabled=native_tools_enabled,
                    )
                    if normalized_text != text.strip():
                        self.last_completion_metadata["native_text_response"] = True
                    return normalized_text
            except ModelProviderError as exc:
                delay = _retry_delay(attempt)
                if exc.retryable and attempt < self.max_attempts:
                    if deadline_monotonic is not None and time.monotonic() + delay >= deadline_monotonic:
                        exc.attempts = attempt
                        self.last_completion_metadata = {
                            "provider_error_code": exc.code,
                            "provider_error_retryable": exc.retryable,
                            "provider_request_attempts": attempt,
                        }
                        raise exc
                    notify_retry(attempt, exc, delay)
                    time.sleep(delay)
                    continue
                self.last_completion_metadata = {
                    "provider_error_code": exc.code,
                    "provider_error_retryable": exc.retryable,
                    "provider_request_attempts": exc.attempts,
                }
                raise exc
            except urllib.error.HTTPError as exc:
                provider_error = _provider_http_error(exc.code, attempt)
                delay = _retry_delay(
                    attempt,
                    (getattr(exc, "headers", None) or {}).get("Retry-After", ""),
                )
                if provider_error.retryable and attempt < self.max_attempts:
                    if deadline_monotonic is not None and time.monotonic() + delay >= deadline_monotonic:
                        provider_error.attempts = attempt
                        self.last_completion_metadata = {
                            "provider_error_code": provider_error.code,
                            "provider_error_retryable": provider_error.retryable,
                            "provider_request_attempts": attempt,
                        }
                        raise provider_error from exc
                    notify_retry(attempt, provider_error, delay)
                    time.sleep(delay)
                    continue
                self.last_completion_metadata = {
                    "provider_error_code": provider_error.code,
                    "provider_error_retryable": provider_error.retryable,
                    "provider_request_attempts": provider_error.attempts,
                }
                raise provider_error from exc
            except (
                urllib.error.URLError,
                IncompleteRead,
                RemoteDisconnected,
                TimeoutError,
                socket.timeout,
                ConnectionError,
            ) as exc:
                delay = _retry_delay(attempt)
                reason = getattr(exc, "reason", exc)
                connection_code = (
                    "model_timeout"
                    if isinstance(reason, (TimeoutError, socket.timeout))
                    else "model_connection_error"
                )
                connection_error = ModelProviderError(
                    connection_code,
                    retryable=True,
                    attempts=attempt,
                )
                if attempt < self.max_attempts:
                    if deadline_monotonic is not None and time.monotonic() + delay >= deadline_monotonic:
                        provider_error = ModelProviderError(
                            "model_timeout", retryable=True, attempts=attempt
                        )
                        self.last_completion_metadata = {
                            "provider_error_code": provider_error.code,
                            "provider_error_retryable": True,
                            "provider_request_attempts": attempt,
                        }
                        raise provider_error from exc
                    notify_retry(attempt, connection_error, delay)
                    time.sleep(delay)
                    continue
                provider_error = ModelProviderError(
                    connection_code,
                    retryable=True,
                    attempts=attempt,
                )
                self.last_completion_metadata = {
                    "provider_error_code": provider_error.code,
                    "provider_error_retryable": True,
                    "provider_request_attempts": attempt,
                }
                raise provider_error from exc

        # 防御性兜底：正常情况下循环内必然 return 或 raise（成功、不可重试错误、
        # 或 deadline 提前中止）。仅当 max_attempts 配置异常时才可能走到这里。
        raise ModelProviderError(
            "model_response_invalid",
            retryable=True,
            attempts=self.max_attempts,
        )


def _extract_anthropic_text(data):
    for item in data.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                return text
    return ""


def _run_provider_request(
    request,
    *,
    consume,
    max_attempts,
    timeout,
    deadline_monotonic=None,
    should_cancel=None,
    on_retry=None,
    no_retry_codes=(),
):
    """Anthropic / chat-completions 客户端共用的重试骨架。

    从 OpenAI 客户端的重试循环中抽取：deadline 计算、取消检查、Retry-After
    延迟、HTTP/网络错误映射与重试。`consume(response, attempt) -> dict` 由
    调用方传入，负责读 body / 解析 SSE；骨架不写任何 client 状态，错误
    metadata 由调用方在 except ModelProviderError 处统一写三键。

    ``no_retry_codes``：命中这些错误码时立即上抛（不按 max_attempts 重试），
    由调用方在更高层换策略重试（如 §7.8.9 阶段 4 收尾的 thinking 预算
    耗尽 → 关闭 thinking 重试）。
    """

    def check_cancelled():
        if should_cancel is not None:
            should_cancel()

    def remaining_timeout(attempt):
        check_cancelled()
        if deadline_monotonic is None:
            return timeout
        remaining = float(deadline_monotonic) - time.monotonic()
        if remaining <= 0:
            raise ModelProviderError("model_timeout", retryable=True, attempts=attempt)
        return min(float(timeout), remaining)

    def notify_retry(attempt, error, delay):
        if on_retry is not None:
            on_retry(
                {
                    "attempt": int(attempt),
                    "max_attempts": int(max_attempts),
                    "error_code": str(getattr(error, "code", "model_connection_error")),
                    "retry_delay_seconds": float(delay),
                }
            )

    for attempt in range(1, max_attempts + 1):
        check_cancelled()
        try:
            with urllib.request.urlopen(request, timeout=remaining_timeout(attempt)) as response:
                return consume(response, attempt)
        except ModelProviderError as exc:
            if exc.code in no_retry_codes:
                raise exc
            delay = _retry_delay(attempt)
            if exc.retryable and attempt < max_attempts:
                if deadline_monotonic is not None and time.monotonic() + delay >= deadline_monotonic:
                    exc.attempts = attempt
                    raise exc
                notify_retry(attempt, exc, delay)
                time.sleep(delay)
                continue
            raise exc
        except urllib.error.HTTPError as exc:
            provider_error = _provider_http_error(exc.code, attempt)
            delay = _retry_delay(
                attempt,
                (getattr(exc, "headers", None) or {}).get("Retry-After", ""),
            )
            if provider_error.retryable and attempt < max_attempts:
                if deadline_monotonic is not None and time.monotonic() + delay >= deadline_monotonic:
                    provider_error.attempts = attempt
                    raise provider_error from exc
                notify_retry(attempt, provider_error, delay)
                time.sleep(delay)
                continue
            raise provider_error from exc
        except (
            urllib.error.URLError,
            IncompleteRead,
            RemoteDisconnected,
            TimeoutError,
            socket.timeout,
            ConnectionError,
        ) as exc:
            delay = _retry_delay(attempt)
            reason = getattr(exc, "reason", exc)
            connection_code = (
                "model_timeout"
                if isinstance(reason, (TimeoutError, socket.timeout))
                else "model_connection_error"
            )
            connection_error = ModelProviderError(
                connection_code,
                retryable=True,
                attempts=attempt,
            )
            if attempt < max_attempts:
                if deadline_monotonic is not None and time.monotonic() + delay >= deadline_monotonic:
                    raise ModelProviderError(
                        "model_timeout", retryable=True, attempts=attempt
                    ) from exc
                notify_retry(attempt, connection_error, delay)
                time.sleep(delay)
                continue
            raise connection_error from exc

    # 防御性兜底：正常情况下循环内必然 return 或 raise（成功、不可重试错误、
    # 或 deadline 提前中止）。仅当 max_attempts 配置异常时才可能走到这里。
    raise ModelProviderError(
        "model_response_invalid",
        retryable=True,
        attempts=max_attempts,
    )


def _serialize_tool_call(name, arguments):
    """把原生工具调用序列化成运行时的 <tool>{...}</tool> 文本协议。"""
    payload = {"name": name, "args": arguments}
    return "<tool>" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "</tool>"


def _parse_tool_arguments(raw):
    """解析流式累积的工具参数 JSON；失败保留原字符串由运行时拒绝。"""
    if isinstance(raw, dict):
        return raw
    raw = str(raw or "").strip()
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return raw


def _normalize_anthropic_usage(usage):
    """把 Anthropic 的 cache_read_input_tokens 映射进统一 usage 形状。"""
    usage = dict(usage or {})
    cached_tokens = usage.get("cache_read_input_tokens") or 0
    if cached_tokens:
        details = dict(usage.get("input_tokens_details") or {})
        details["cached_tokens"] = cached_tokens
        usage["input_tokens_details"] = details
    return usage


def _normalize_completions_base_url(base_url):
    base = str(base_url).rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _consume_anthropic_stream(lines, *, on_text_delta=None, should_cancel=None, attempts_used=1):
    """消费 Anthropic Messages SSE 流，产出运行时形状 {"text", "tool", "usage"}。

    覆盖的 Anthropic 事件（参考 pi-main 的 anthropic-messages.ts 适配点）：
    message_start（usage 初值）、content_block_start（tool_use 建槽）、
    content_block_delta（text_delta / input_json_delta 逐片累积）、
    message_delta（output_tokens 终值）、message_stop（结束）。
    """
    text_parts = []
    tool_slots = {}  # index -> {"name": str, "arguments": str}
    usage = {}
    for raw_line in lines:
        if should_cancel is not None:
            should_cancel()
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace")
        else:
            line = str(raw_line)
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload:
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "error":
            raise ModelProviderError("model_provider_error", attempts=attempts_used)
        if event_type == "message_start":
            message = event.get("message") or {}
            usage = dict(message.get("usage") or {})
            continue
        if event_type == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                index = int(event.get("index") or 0)
                tool_slots[index] = {"name": str(block.get("name") or ""), "arguments": ""}
            continue
        if event_type == "content_block_delta":
            delta = event.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                text = delta.get("text")
                if text:
                    text_parts.append(text)
                    if on_text_delta is not None:
                        on_text_delta(text)
            elif delta_type == "input_json_delta":
                index = int(event.get("index") or 0)
                slot = tool_slots.get(index)
                if slot is not None:
                    slot["arguments"] += str(delta.get("partial_json") or "")
            continue
        if event_type == "message_delta":
            message_usage = event.get("usage")
            if isinstance(message_usage, dict) and message_usage:
                usage = dict(usage)
                usage.update(message_usage)
            continue
        if event_type == "message_stop":
            break
    tool = None
    if tool_slots:
        index = min(tool_slots)
        slot = tool_slots[index]
        if slot["name"]:
            tool = {"name": slot["name"], "arguments": _parse_tool_arguments(slot["arguments"])}
    return {"text": "".join(text_parts), "tool": tool, "usage": usage}


def _extract_anthropic_response(data):
    """非流式 Anthropic JSON 兜底：(text, tool_or_None, usage)。"""
    text_parts = []
    tool = None
    for item in data.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = item.get("text")
            if text:
                text_parts.append(text)
        elif item.get("type") == "tool_use" and tool is None:
            name = str(item.get("name") or "")
            if name:
                tool = {"name": name, "arguments": item.get("input")}
    return "".join(text_parts), tool, data.get("usage")


def _consume_completions_stream(lines, *, on_text_delta=None, on_thinking_delta=None, should_cancel=None, attempts_used=1):
    """消费 chat/completions SSE 流，产出运行时形状 {"text", "tool", "usage"}。

    对齐网关 translate_stream 的翻译点：delta.content 增量、delta.tool_calls
    按 index 累积 arguments 分片；兼容最终 chunk 把完整 tool_calls 放在
    choice.message 的情况；usage 取流中最后一个非空 chunk。
    §7.8.9 阶段 4（2026-08-18）：DeepSeek 思考在 delta.reasoning_content，
    经 on_thinking_delta 回传（前端 thinking 折叠区），不进正文 content。
    同时记录「只思考无正文」与 finish_reason，供调用方做空响应兜底
    （effort=max + 小预算时 reasoning 吃光 max_tokens，content 为空）。
    """
    text_parts = []
    tool_slots = {}  # index -> {"name": str, "arguments": str}
    usage = None
    saw_thinking = False
    finish_reason = None
    for raw_line in lines:
        if should_cancel is not None:
            should_cancel()
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace")
        else:
            line = str(raw_line)
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if chunk.get("error"):
            raise ModelProviderError("model_provider_error", attempts=attempts_used)
        if chunk.get("usage"):
            usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        if choice.get("finish_reason"):
            finish_reason = choice.get("finish_reason")
        delta = choice.get("delta") or {}
        # DeepSeek 思考：reasoning_content 独立于 content，回传 thinking 回调。
        thinking = delta.get("reasoning_content")
        if thinking:
            saw_thinking = True
            if on_thinking_delta is not None:
                on_thinking_delta(thinking)
        content = delta.get("content")
        if content:
            text_parts.append(content)
            if on_text_delta is not None:
                on_text_delta(content)
        # 有的后端把完整 tool_calls 放在最终 chunk 的 message 里而不是 delta 里。
        raw_calls = delta.get("tool_calls") or (choice.get("message") or {}).get("tool_calls") or []
        for tc in raw_calls:
            if not isinstance(tc, dict):
                continue
            index = int(tc.get("index") or 0)
            slot = tool_slots.setdefault(index, {"name": "", "arguments": ""})
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]
    tool = None
    if tool_slots:
        index = min(tool_slots)
        slot = tool_slots[index]
        if slot["name"]:
            tool = {"name": slot["name"], "arguments": _parse_tool_arguments(slot["arguments"])}
    return {
        "text": "".join(text_parts),
        "tool": tool,
        "usage": usage,
        "saw_thinking": saw_thinking,
        "finish_reason": finish_reason,
    }


class AnthropicCompatibleModelClient:
    """Anthropic Messages API 原生适配（流式 + 工具调用 + usage）。

    与 OpenAI 客户端一样，把原生 function call 序列化成 <tool>{...}</tool>
    文本协议返回，纯文本按 _normalize_openai_native_text 包 <final>；运行时
    不需要感知 Anthropic 的协议细节。
    """

    def __init__(
        self,
        model,
        base_url,
        api_key,
        temperature,
        timeout,
        max_attempts=3,
        *,
        instructions=None,
    ):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        if not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        self.max_attempts = max_attempts
        self.instructions = str(instructions or "").strip()
        self.supports_prompt_cache = False
        self.supports_native_tools = True
        self.last_completion_metadata = {}

    def complete(
        self,
        prompt,
        max_new_tokens,
        prompt_cache_key=None,
        prompt_cache_retention=None,
        *,
        deadline_monotonic=None,
        on_retry=None,
        on_text_delta=None,
        should_cancel=None,
        tool_definitions=None,
    ):
        """向 Anthropic-compatible `/messages` 接口发起一次流式模型调用。

        请求与流式事件在 _consume_anthropic_stream 里翻译回运行时协议；
        重试骨架复用 _run_provider_request；错误统一抛 ModelProviderError
        并写 provider_error_* 三键 metadata。
        """
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                        }
                    ],
                }
            ],
            "max_tokens": max_new_tokens,
            "stream": True,
        }
        if self.instructions:
            payload["system"] = [{"type": "text", "text": self.instructions}]
        native_tools_enabled = bool(tool_definitions) and self.supports_native_tools
        if native_tools_enabled:
            # OpenAI function schema -> Anthropic tools（丢 strict，description 兜底空串）。
            payload["tools"] = [
                {
                    "name": tool["name"],
                    "description": tool.get("description") or "",
                    "input_schema": tool.get("parameters") or {},
                }
                for tool in tool_definitions
                if isinstance(tool, dict) and tool.get("type") == "function" and tool.get("name")
            ]
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": OPENAI_COMPATIBLE_USER_AGENT,
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        request = urllib.request.Request(
            self.base_url + "/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        def consume(response, attempt):
            response_headers = getattr(response, "headers", {}) or {}
            content_type = response_headers.get("Content-Type", "")
            if content_type.startswith("text/event-stream"):
                result = _consume_anthropic_stream(
                    response,
                    on_text_delta=on_text_delta,
                    should_cancel=should_cancel,
                    attempts_used=attempt,
                )
            else:
                body_text = response.read().decode("utf-8")
                # 有些兼容后端未标注 content-type 但返回 SSE。
                if body_text.lstrip().startswith("data:"):
                    result = _consume_anthropic_stream(
                        body_text.splitlines(),
                        on_text_delta=on_text_delta,
                        should_cancel=should_cancel,
                        attempts_used=attempt,
                    )
                else:
                    try:
                        data = json.loads(body_text)
                    except json.JSONDecodeError as exc:
                        raise ModelProviderError(
                            "model_response_invalid", retryable=True, attempts=attempt
                        ) from exc
                    if data.get("error"):
                        raise ModelProviderError("model_provider_error", attempts=attempt)
                    text, tool, usage = _extract_anthropic_response(data)
                    result = {"text": text, "tool": tool, "usage": usage}
            if not result.get("tool") and not (result.get("text") or "").strip():
                raise ModelProviderError("model_response_invalid", retryable=True, attempts=attempt)
            return result

        try:
            result = _run_provider_request(
                request,
                consume=consume,
                max_attempts=self.max_attempts,
                timeout=self.timeout,
                deadline_monotonic=deadline_monotonic,
                should_cancel=should_cancel,
                on_retry=on_retry,
            )
        except ModelProviderError as exc:
            self.last_completion_metadata = {
                "provider_error_code": exc.code,
                "provider_error_retryable": exc.retryable,
                "provider_request_attempts": exc.attempts,
            }
            raise

        self.last_completion_metadata = {
            "prompt_cache_supported": self.supports_prompt_cache,
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_retention": prompt_cache_retention,
            **_extract_usage_cache_details({"usage": _normalize_anthropic_usage(result.get("usage") or {})}),
        }
        tool = result.get("tool")
        if tool and tool.get("name"):
            self.last_completion_metadata["native_tool_call"] = True
            # §2.1 原生 tool calling：直接返回 dict，不序列化 <tool> 文本。
            return {"name": tool["name"], "args": tool.get("arguments") or {}}
        text = (result.get("text") or "").strip()
        normalized_text = _normalize_openai_native_text(
            text,
            native_tools_enabled=native_tools_enabled,
        )
        if normalized_text != text:
            self.last_completion_metadata["native_text_response"] = True
        return normalized_text


class OpenAICompletionsModelClient:
    """OpenAI chat/completions 协议原生适配（流式 + 工具调用 + usage）。

    面向 SiliconFlow、DeepSeek 等只提供 /chat/completions 的平台；与 Anthropic
    客户端共享 _run_provider_request 重试骨架与 <tool>/<final> 文本协议序列化。

    §2.1 兼容：DeepSeek 的思考档位通过 ``reasoning_effort`` + ``thinking``
    传递；``medium/xhigh`` 在构造时映射为 ``high``（DeepSeek 官方枚举仅
    low/high/max，见 api-docs.deepseek.com/guides/thinking_mode）。
    """

    # DeepSeek 官方档位 → 实际发送值（兼容映射）。
    _DEEPSEEK_EFFORT_COMPAT = {
        "none": None,  # thinking disabled
        "minimal": None,
        "low": "low",
        "medium": "high",
        "high": "high",
        "xhigh": "high",
        "max": "max",
    }

    def __init__(
        self,
        model,
        base_url,
        api_key,
        temperature,
        timeout,
        max_attempts=3,
        *,
        reasoning_effort="",
        supported_reasoning_efforts=(),
    ):
        self.model = model
        self.base_url = _normalize_completions_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        if not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        self.max_attempts = max_attempts
        self.supports_prompt_cache = False
        self.supports_native_tools = True
        self.last_completion_metadata = {}
        self.supported_reasoning_efforts = tuple(
            str(value).strip()
            for value in supported_reasoning_efforts
            if str(value).strip()
        )
        self.reasoning_effort = str(reasoning_effort or "").strip().lower()
        if self.reasoning_effort and self.reasoning_effort not in self.supported_reasoning_efforts:
            raise ValueError("reasoning_effort is not supported by this provider/model")

    def complete(
        self,
        prompt,
        max_new_tokens,
        prompt_cache_key=None,
        prompt_cache_retention=None,
        *,
        deadline_monotonic=None,
        on_retry=None,
        on_text_delta=None,
        on_thinking_delta=None,
        should_cancel=None,
        tool_definitions=None,
        finalization_only=False,
    ):
        """向 chat/completions 接口发起一次流式模型调用。

        §7.8.9 阶段 4 收尾（2026-08-18）：``finalization_only=True``（收尾轮，
        只出最终答案、不调工具）时把 DeepSeek 思考档位压到 ``high``——最终
        答案不需要最高强度思考，且能保证 reasoning 不吃光 max_tokens 预算
        导致正文空响应（effort=max + 512 预算实测空响应）。
        """
        self.last_completion_metadata = {}

        def build_request(*, thinking_override=None):
            """构造请求体。``thinking_override``：
            - None → 按配置的 reasoning_effort 正常发
            - "disabled" → 强制关闭思考（空响应兜底重试）
            """
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_new_tokens,
                "stream": True,
            }
            # §2.1 兼容：DeepSeek 思考模式（thinking + reasoning_effort）。
            # none/minimal → thinking disabled（不思考）；low/high/max → 映射后发送。
            # 收尾轮（finalization_only）把最高档思考（max）压到 high，防 reasoning
            # 吃光输出预算导致正文空响应（effort=max + 512 预算实测空响应）。
            if thinking_override == "disabled":
                payload["thinking"] = {"type": "disabled"}
            elif self.reasoning_effort:
                compat_effort = self._DEEPSEEK_EFFORT_COMPAT.get(self.reasoning_effort, "high")
                if compat_effort is None:
                    payload["thinking"] = {"type": "disabled"}
                else:
                    if finalization_only and compat_effort == "max":
                        compat_effort = "high"
                    payload["reasoning_effort"] = compat_effort
                    payload["thinking"] = {"type": "enabled"}
            native_tools_enabled = bool(tool_definitions) and self.supports_native_tools
            if native_tools_enabled:
                payload["tools"] = [
                    {
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool.get("description"),
                            "parameters": tool.get("parameters"),
                        },
                    }
                    for tool in tool_definitions
                    if isinstance(tool, dict) and tool.get("type") == "function" and tool.get("name")
                ]
                payload["parallel_tool_calls"] = False
            if self.temperature is not None:
                payload["temperature"] = self.temperature
            return payload

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": OPENAI_COMPATIBLE_USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        def make_request(payload):
            return urllib.request.Request(
                # _normalize_completions_base_url 已保证 base_url 带 /chat/completions。
                self.base_url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )

        def consume(response, attempt):
            response_headers = getattr(response, "headers", {}) or {}
            content_type = response_headers.get("Content-Type", "")
            if content_type.startswith("text/event-stream"):
                result = _consume_completions_stream(
                    response,
                    on_text_delta=on_text_delta,
                    on_thinking_delta=on_thinking_delta,
                    should_cancel=should_cancel,
                    attempts_used=attempt,
                )
            else:
                body_text = response.read().decode("utf-8")
                if body_text.lstrip().startswith("data:"):
                    result = _consume_completions_stream(
                        body_text.splitlines(),
                        on_text_delta=on_text_delta,
                        on_thinking_delta=on_thinking_delta,
                        should_cancel=should_cancel,
                        attempts_used=attempt,
                    )
                else:
                    try:
                        data = json.loads(body_text)
                    except json.JSONDecodeError as exc:
                        raise ModelProviderError(
                            "model_response_invalid", retryable=True, attempts=attempt
                        ) from exc
                    if data.get("error"):
                        raise ModelProviderError("model_provider_error", attempts=attempt)
                    native_action = _extract_openai_function_call(data)
                    text = _extract_openai_text(data)
                    result = {
                        "text": text,
                        "tool": None,
                        "usage": data.get("usage"),
                        "native": native_action,
                        "saw_thinking": bool(_extract_reasoning_summary(data)),
                        "finish_reason": (data.get("choices") or [{}])[0].get("finish_reason"),
                    }
            if (
                not result.get("native")
                and not result.get("tool")
                and not (result.get("text") or "").strip()
            ):
                # §7.8.9 阶段 4 收尾：空响应兜底——只有思考（reasoning_content）
                # 无正文且 finish_reason=length，说明思考吃光 max_tokens 预算。
                # 这不是协议错误，交由外层关闭 thinking 重试一次。
                if result.get("saw_thinking") and result.get("finish_reason") == "length":
                    raise ModelProviderError(
                        "model_thinking_budget_exhausted",
                        retryable=True,
                        attempts=attempt,
                    )
                raise ModelProviderError("model_response_invalid", retryable=True, attempts=attempt)
            return result

        def run(thinking_override=None):
            return _run_provider_request(
                make_request(build_request(thinking_override=thinking_override)),
                consume=consume,
                max_attempts=self.max_attempts,
                timeout=self.timeout,
                deadline_monotonic=deadline_monotonic,
                should_cancel=should_cancel,
                on_retry=on_retry,
                # thinking 预算耗尽不该按原参数空转重试——交给外层关闭思考再试。
                no_retry_codes=("model_thinking_budget_exhausted",),
            )

        try:
            result = run()
        except ModelProviderError as exc:
            if (
                exc.code == "model_thinking_budget_exhausted"
                and self.reasoning_effort
            ):
                # 关闭思考重试一次：正文不再被 reasoning 挤占。
                recovered = True
                try:
                    result = run(thinking_override="disabled")
                except ModelProviderError as retry_exc:
                    self.last_completion_metadata = {
                        "provider_error_code": retry_exc.code,
                        "provider_error_retryable": retry_exc.retryable,
                        "provider_request_attempts": retry_exc.attempts,
                    }
                    raise
            else:
                self.last_completion_metadata = {
                    "provider_error_code": exc.code,
                    "provider_error_retryable": exc.retryable,
                    "provider_request_attempts": exc.attempts,
                }
                raise
        else:
            recovered = False

        self.last_completion_metadata = {
            "prompt_cache_supported": self.supports_prompt_cache,
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_retention": prompt_cache_retention,
            "thinking_budget_recovered": recovered,
            **_extract_usage_cache_details({"usage": result.get("usage") or {}}),
        }
        native_action = result.get("native")
        if native_action:
            self.last_completion_metadata["native_tool_call"] = True
            return native_action
        tool = result.get("tool")
        if tool and tool.get("name"):
            self.last_completion_metadata["native_tool_call"] = True
            # §2.1 原生 tool calling：直接返回 dict，不序列化 <tool> 文本。
            return {"name": tool["name"], "args": tool.get("arguments") or {}}
        text = (result.get("text") or "").strip()
        normalized_text = _normalize_openai_native_text(
            text,
            native_tools_enabled=bool(tool_definitions) and self.supports_native_tools,
        )
        if normalized_text != text:
            self.last_completion_metadata["native_text_response"] = True
        return normalized_text
