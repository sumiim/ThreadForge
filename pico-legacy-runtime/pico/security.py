"""Security and redaction helpers for runtime artifacts."""

import os

SENSITIVE_ENV_NAME_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
REDACTED_VALUE = "<redacted>"

# Read-only tool previews are useful in the Web/Desktop timeline, but the
# Worker must never stream unrestricted command arguments or full file contents.
PUBLIC_READ_ONLY_TOOLS = frozenset({"list_files", "read_file", "search", "delegate"})
PUBLIC_TOOL_RESULT_TOOLS = PUBLIC_READ_ONLY_TOOLS | {"run_shell"}
PUBLIC_TOOL_RESULT_MAX_CHARS = 8000
PUBLIC_TOOL_ARG_MAX_CHARS = 1200

_PUBLIC_TOOL_ARG_FIELDS = {
    "list_files": ("path",),
    "read_file": ("path", "start", "end"),
    "search": ("pattern", "path"),
    "delegate": ("task", "max_steps"),
}


def _normalized_secret_names(secret_env_names):
    return {str(name).upper() for name in (secret_env_names or ())}


def looks_sensitive_env_name(name):
    upper = str(name).upper()
    return any(upper == marker or upper.endswith(marker) or upper.endswith(f"_{marker}") for marker in SENSITIVE_ENV_NAME_MARKERS)


def is_secret_env_name(name, secret_env_names=None):
    upper = str(name).upper()
    return upper in _normalized_secret_names(secret_env_names) or looks_sensitive_env_name(upper)


def configured_secret_env_items(env=None, secret_env_names=None):
    env = os.environ if env is None else env
    configured_names = _normalized_secret_names(secret_env_names)
    items = [
        (name, value)
        for name, value in env.items()
        if str(name).upper() in configured_names and value
    ]
    items.sort(key=lambda item: item[0])
    return items


def detected_secret_env_items(env=None, secret_env_names=None):
    env = os.environ if env is None else env
    items = [
        (name, value)
        for name, value in env.items()
        if is_secret_env_name(name, secret_env_names=secret_env_names) and value
    ]
    items.sort(key=lambda item: item[0])
    return items


def secret_env_summary(env=None, secret_env_names=None):
    names = [name for name, _ in configured_secret_env_items(env=env, secret_env_names=secret_env_names)]
    return {
        "secret_env_count": len(names),
        "secret_env_names": names,
    }


def detected_secret_env_summary(env=None, secret_env_names=None):
    names = [name for name, _ in detected_secret_env_items(env=env, secret_env_names=secret_env_names)]
    return {
        "secret_env_count": len(names),
        "secret_env_names": names,
    }


def redact_text(text, env=None, secret_env_names=None):
    text = str(text)
    for _, value in sorted(
        detected_secret_env_items(env=env, secret_env_names=secret_env_names),
        key=lambda item: len(item[1]),
        reverse=True,
    ):
        text = text.replace(value, REDACTED_VALUE)
    return text


def redact_artifact(value, key=None, env=None, secret_env_names=None):
    if key and is_secret_env_name(key, secret_env_names=secret_env_names):
        return REDACTED_VALUE
    if isinstance(value, dict):
        return {
            str(item_key): redact_artifact(item_value, key=item_key, env=env, secret_env_names=secret_env_names)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact_artifact(item, key=key, env=env, secret_env_names=secret_env_names) for item in value]
    if isinstance(value, tuple):
        return [redact_artifact(item, key=key, env=env, secret_env_names=secret_env_names) for item in value]
    if isinstance(value, str):
        return redact_text(value, env=env, secret_env_names=secret_env_names)
    return value


def _clip_public_text(value, limit):
    text = redact_text(value)
    if len(text) <= limit:
        return text, False
    return text[: max(0, limit - 20)] + "\n...[truncated]", True


def public_tool_args_preview(tool_name, args):
    """Return bounded, allowlisted arguments for a read-only tool event."""
    if str(tool_name) not in PUBLIC_READ_ONLY_TOOLS or not isinstance(args, dict):
        return {}
    result = {}
    for field in _PUBLIC_TOOL_ARG_FIELDS[str(tool_name)]:
        if field not in args:
            continue
        value = args[field]
        if isinstance(value, str):
            value, _ = _clip_public_text(value, PUBLIC_TOOL_ARG_MAX_CHARS)
        elif isinstance(value, bool):
            value = bool(value)
        elif isinstance(value, int):
            value = value
        else:
            value = str(value)[:PUBLIC_TOOL_ARG_MAX_CHARS]
        result[field] = value
    return redact_artifact(result)


def public_tool_result_preview(tool_name, content):
    """Return a bounded, redacted preview for explicitly allowed tool results."""
    if str(tool_name) not in PUBLIC_TOOL_RESULT_TOOLS or content is None:
        return "", False
    return _clip_public_text(content, PUBLIC_TOOL_RESULT_MAX_CHARS)


def shell_env(env=None, allowlist=(), root="."):
    env = os.environ if env is None else env
    filtered = {
        name: env[name]
        for name in allowlist
        if name in env
    }
    filtered["PWD"] = str(root)
    if "PATH" not in filtered and env.get("PATH"):
        filtered["PATH"] = env["PATH"]
    return filtered
