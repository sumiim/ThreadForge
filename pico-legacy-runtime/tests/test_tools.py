from pathlib import Path
from unittest.mock import patch

from pico.tool_context import ToolContext
from pico.tools import (
    build_tool_registry,
    provider_tool_definitions,
    tool_delegate,
    tool_read_file,
    tool_search,
)


def test_tool_context_supports_file_tools_without_full_pico(tmp_path):
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
    )

    result = tool_read_file(context, {"path": "sample.txt", "start": 1, "end": 1})

    assert "# sample.txt" in result
    assert "alpha" in result


def test_delegate_uses_context_spawn_without_runtime_import(tmp_path):
    calls = []
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: Path(tmp_path / raw_path),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: calls.append(args) or "delegate_result:\nDone",
    )

    result = tool_delegate(context, {"task": "inspect README.md", "max_steps": 2})

    assert result == "delegate_result:\nDone"
    assert calls == [{"task": "inspect README.md", "max_steps": 2}]


def test_build_tool_registry_binds_runners_to_tool_context(tmp_path):
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: Path(tmp_path / raw_path),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=1,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
    )

    tools = build_tool_registry(context)

    assert "read_file" in tools
    assert "delegate" not in tools


def test_tool_registry_register_and_build_preserve_legacy_shape(tmp_path):
    from pico.tools import TOOL_SCHEMA_VERSION, ToolRegistry

    registry = ToolRegistry()
    assert registry.version == TOOL_SCHEMA_VERSION
    assert "read_file" in registry.names()
    assert "delegate" in registry.names()

    def _noop(context, args):
        return "ok"

    registry.register(
        "ping",
        {"schema": {"x": "str"}, "risky": False, "description": "noop"},
        _noop,
    )
    assert "ping" in registry.names()

    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: Path(tmp_path / raw_path),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=1,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
    )
    tools = registry.build(context)

    assert "ping" in tools
    assert "delegate" not in tools  # depth == max_depth
    assert callable(tools["ping"]["run"])


def test_provider_tool_definitions_are_strict_responses_functions(tmp_path):
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: Path(tmp_path / raw_path),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=1,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
    )

    definitions = provider_tool_definitions(build_tool_registry(context))
    read_file = next(item for item in definitions if item["name"] == "read_file")

    assert read_file["type"] == "function"
    assert read_file["strict"] is True
    assert read_file["parameters"] == {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start": {"type": "integer"},
            "end": {"type": "integer"},
        },
        "required": ["path", "start", "end"],
        "additionalProperties": False,
    }


def test_search_fallback_preserves_regex_semantics_and_skips_ignored_directories(tmp_path):
    (tmp_path / "README.md").write_text(
        "The api-server communicates with the client.\n",
        encoding="utf-8",
    )
    ignored = tmp_path / ".git"
    ignored.mkdir()
    (ignored / "private.txt").write_text("api-server\n", encoding="utf-8")
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
    )

    with patch("pico.tools.shutil.which", return_value=None):
        result = tool_search(context, {"pattern": "api-server|client", "path": "."})

    assert "README.md:1:The api-server communicates with the client." in result
    assert "private.txt" not in result


def test_search_fallback_rejects_invalid_regular_expression(tmp_path):
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
    )

    with patch("pico.tools.shutil.which", return_value=None):
        try:
            tool_search(context, {"pattern": "[", "path": "."})
        except ValueError as exc:
            assert "invalid search pattern" in str(exc)
        else:
            raise AssertionError("invalid regex must be rejected")


def test_legal_tool_names_tracks_registered_tools(tmp_path):
    """Allowlist cross-point: register()'d tools must be seen by legal_tool_names().

    Previously legal_tool_names() hardcoded set(BASE_TOOL_SPECS) while build()/
    names() use the registry's self.specs, so a register()'d tool would be
    rejected as 'unknown allowed tool' in _apply_tool_allowlist. Verify the two
    agree so the cross-point stays fixed.
    """
    from pico.tools import _DEFAULT_REGISTRY, legal_tool_names

    def _noop(context, args):
        return "ok"

    # Save original specs, register a temp tool, restore in finally so the
    # module-level singleton is not polluted.
    original_specs = dict(_DEFAULT_REGISTRY.specs)
    original_runners = dict(_DEFAULT_REGISTRY.runners)
    try:
        _DEFAULT_REGISTRY.register(
            "ping",
            {"schema": {"x": "str"}, "risky": False, "description": "noop"},
            _noop,
        )
        assert "ping" in _DEFAULT_REGISTRY.names()
        assert "ping" in legal_tool_names()
        # Consistent with build(): the new tool appears in the runtime surface.
        context = ToolContext(
            root=tmp_path,
            path_resolver=lambda raw_path: Path(tmp_path / raw_path),
            shell_env_provider=lambda: {"PWD": str(tmp_path)},
            depth=1,
            max_depth=1,
            spawn_delegate=lambda args: "unused",
        )
        assert "ping" in _DEFAULT_REGISTRY.build(context)
    finally:
        _DEFAULT_REGISTRY.specs = original_specs
        _DEFAULT_REGISTRY.runners = original_runners
    assert "ping" not in legal_tool_names()
