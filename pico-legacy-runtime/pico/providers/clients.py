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


class FakeModelClient:
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
    """Convert one provider-native function call into the runtime protocol."""

    def normalize(item):
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return ""
        name = str(item.get("name", "")).strip()
        if not name:
            return ""
        arguments = item.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                # Preserve the invalid shape for Pico.parse() to reject via
                # the bounded protocol-repair path without exposing its text.
                pass
        payload = {"name": name, "args": arguments}
        return "<tool>" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "</tool>"

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


def _normalize_openai_native_text(text, *, native_tools_enabled):
    """Treat a provider-native assistant message as the final agent action.

    OpenAI Responses uses the absence of a function call to signal a normal
    assistant message. The legacy ThreadForge parser still expects an explicit
    control envelope, so normalize only native-tool requests and leave legacy
    XML/JSON protocol responses untouched.
    """

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
                        raise ModelProviderError("model_response_invalid", attempts=attempt)
                    else:
                        body_text = response.read().decode("utf-8")
                        response_data = {}
                break
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

        # 有些兼容后端返回普通 JSON，有些返回 SSE。
        # 这里两种都接住，并尽量统一抽取文本和 usage/cache 元数据。
        if content_type.startswith("text/event-stream") or body_text.lstrip().startswith("data:"):
            text, response_data = _extract_openai_response_from_sse(body_text)
            if isinstance(response_data, dict) and response_data:
                # 这些元数据会一路传回 runtime，进入 trace 和 report，
                # 用来观察 prompt cache 是否真的命中。
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
            raise ModelProviderError("model_response_invalid", attempts=attempt)

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise ModelProviderError("model_response_invalid", attempts=attempt) from exc
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
            raise ModelProviderError("model_response_invalid", attempts=attempt)
        normalized_text = _normalize_openai_native_text(
            text,
            native_tools_enabled=native_tools_enabled,
        )
        if normalized_text != text.strip():
            self.last_completion_metadata["native_text_response"] = True
        return normalized_text


def _extract_anthropic_text(data):
    for item in data.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                return text
    return ""


class AnthropicCompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

    def complete(
        self,
        prompt,
        max_new_tokens,
        prompt_cache_key=None,
        prompt_cache_retention=None,
        **kwargs,
    ):
        # 为了保持统一接口，runtime 仍然会传缓存参数进来；
        # 这里只是显式丢弃，因为当前 Anthropic-compatible 路径没有接缓存复用。
        del prompt_cache_key, prompt_cache_retention, kwargs
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
            "stream": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        request = urllib.request.Request(
            self.base_url + "/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body_text = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Anthropic-compatible request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected) as exc:
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "Could not reach the Anthropic-compatible backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Anthropic-compatible error: backend returned non-JSON content that could not be parsed"
            ) from exc
        if data.get("error"):
            raise RuntimeError(f"Anthropic-compatible error: {data['error']}")
        text = _extract_anthropic_text(data)
        if text:
            return text
        raise RuntimeError("Anthropic-compatible error: could not extract text from response")
