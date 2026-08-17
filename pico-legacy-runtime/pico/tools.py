"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import os
import re
import shutil
import subprocess
import textwrap
import time
from functools import partial
from pathlib import Path

from .execution_hooks import ProcessCleanupFailed, RunCancelled
from .shell_process import ShellProcess
from .subprocess_utils import hidden_process_creation_flags, hidden_process_startupinfo
from .workspace import IGNORED_PATH_NAMES

SEARCH_TIMEOUT_SECONDS = 10
SEARCH_RG_TIMEOUT_SECONDS = 5
SEARCH_MAX_FILES = 5000
SEARCH_MAX_FILE_BYTES = 2 * 1024 * 1024
SEARCH_MAX_MATCHES = 200
SEARCH_IGNORED_PATH_NAMES = IGNORED_PATH_NAMES | {
    "node_modules",
    "build",
    "dist",
    "release-desktop",
    "coverage",
    "target",
}

BASE_TOOL_SPECS = {
    "list_files": {
        "schema": {"path": "str='.'"},
        "risky": False,
        "description": "List files in the workspace.",
    },
    "read_file": {
        "schema": {"path": "str", "start": "int=1", "end": "int=200"},
        "risky": False,
        "description": "Read a UTF-8 file by line range.",
    },
    "search": {
        "schema": {"pattern": "str", "path": "str='.'"},
        "risky": False,
        "description": "Search file contents with a regular expression; this does not search path names.",
    },
    "run_shell": {
        "schema": {"command": "str", "timeout": "int=20"},
        "risky": True,
        "description": "Run a shell command in the repo root.",
    },
    "write_file": {
        "schema": {"path": "str", "content": "str"},
        "risky": True,
        "description": "Write a text file.",
    },
    "patch_file": {
        "schema": {"path": "str", "old_text": "str", "new_text": "str"},
        "risky": True,
        "description": "Replace one exact text block in a file.",
    },
}

DELEGATE_TOOL_SPEC = {
    "schema": {"task": "str", "max_steps": "int=3"},
    "risky": False,
    "description": "Ask a bounded read-only child agent to investigate.",
}

TOOL_SCHEMA_VERSION = "1"


class ToolRegistry:
    """版本化工具注册表——agent 工具面的唯一事实来源。

    ``specs`` 描述「模型能申请哪些动作」（schema / risky / description），
    ``runners`` 是每个动作的执行实现。扩展（MCP / Skill，V1.4）只往注册表
    加条目，不改变循环与 prompt 组装。当前是纯重构：``build_tool_registry``
    是默认注册表的薄投影，保持现有调用点零行为变更。
    """

    def __init__(self, specs=None, runners=None, version=TOOL_SCHEMA_VERSION):
        self.version = version
        self.specs = dict(specs if specs is not None else BASE_TOOL_SPECS)
        self.runners = dict(runners if runners is not None else _TOOL_RUNNERS)

    def register(self, name, spec, runner):
        self.specs[str(name)] = dict(spec)
        if runner is not None:
            self.runners[str(name)] = runner
        return self

    def names(self):
        return set(self.specs) | {"delegate"}

    def build(self, context):
        # 工具不是动态发现的，而是显式注册的。
        # 这样模型看到的是一个有边界、可审计的动作集合。
        tools = {
            name: {**spec, "run": partial(self.runners[name], context)}
            for name, spec in self.specs.items()
        }
        # §7.8.9 阶段 4：取消可调用 delegate——子 agent 从「模型可调用的工具」
        # 降级为「程序调用的验证子流程」（review subagent）。delegate 不再暴露
        # 给模型（模型无法绕过质量门），spawn_delegate/tool_delegate 实现保留
        # 作 review 执行引擎复用，后续版本彻底删除。
        return tools

    def definitions(self, tools):
        return provider_tool_definitions(tools)


def legal_tool_names():
    # §7.8.9 阶段 4：delegate 不再暴露给模型（build() 不注册），但保留在
    # legal 集合——LangGraph 兼容层（回归参照）仍显式传 allowed_tools 含
    # delegate，避免 allowlist 校验报错。后续彻底删除兼容层时一并移除。
    return set(BASE_TOOL_SPECS) | {"delegate"}


def provider_tool_definitions(tools):
    """Project the runtime registry into OpenAI Responses function tools.

    Runtime validation remains authoritative. The provider schema makes the
    action boundary explicit so capable models do not have to reproduce the
    legacy XML envelope before a tool can run.
    """
    type_map = {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "boolean",
    }
    definitions = []
    for name, spec in tools.items():
        properties = {}
        schema = spec.get("schema", {})
        for argument, declaration in schema.items():
            type_name = str(declaration).split("=", 1)[0].strip()
            properties[str(argument)] = {
                "type": type_map.get(type_name, "string"),
            }
        definitions.append(
            {
                "type": "function",
                "name": str(name),
                "description": str(spec.get("description", "")),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    # Strict Responses schemas require every property to be
                    # listed. Runtime defaults remain useful for legacy calls.
                    "required": list(properties),
                    "additionalProperties": False,
                },
                "strict": True,
            }
        )
    return definitions


TOOL_EXAMPLES = {
    "list_files": '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
    "read_file": '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
    "search": '<tool>{"name":"search","args":{"pattern":"binary_search","path":"."}}</tool>',
    "run_shell": '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
    "write_file": '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
    "patch_file": '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
    "delegate": '<tool>{"name":"delegate","args":{"task":"inspect README.md","max_steps":3}}</tool>',
}


def build_tool_registry(context):
    """把默认工具注册表投影成当前 context 的运行时工具表（薄投影，零行为变更）。"""
    return _DEFAULT_REGISTRY.build(context)


def tool_example(name):
    return TOOL_EXAMPLES.get(name, "")


def validate_tool(context, name, args):
    args = args or {}

    if name == "list_files":
        path = context.path(args.get("path", "."))
        if not path.is_dir():
            raise ValueError("path is not a directory")
        return

    if name == "read_file":
        path = context.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        start = int(args.get("start", 1))
        end = int(args.get("end", 200))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        return

    if name == "search":
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        context.path(args.get("path", "."))
        return

    if name == "run_shell":
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command must not be empty")
        timeout = int(args.get("timeout", 20))
        if timeout < 1 or timeout > 120:
            raise ValueError("timeout must be in [1, 120]")
        return

    if name == "write_file":
        path = context.path(args["path"])
        if path.exists() and path.is_dir():
            raise ValueError("path is a directory")
        if "content" not in args:
            raise ValueError("missing content")
        return

    if name == "patch_file":
        # patch_file 故意做得很严格：old_text 必须精确命中且只能出现一次，
        # 这样修改行为才是确定的，失败原因也更容易解释。
        path = context.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        text = path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        return

    if name == "delegate":
        task = str(args.get("task", "")).strip()
        if not task:
            raise ValueError("task must not be empty")
        if context.depth >= context.max_depth:
            raise ValueError("delegate depth exceeded")
        return


def tool_list_files(context, args):
    path = context.path(args.get("path", "."))
    if not path.is_dir():
        raise ValueError("path is not a directory")
    entries = [
        item for item in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        if item.name not in IGNORED_PATH_NAMES
    ]
    lines = []
    for entry in entries[:200]:
        kind = "[D]" if entry.is_dir() else "[F]"
        lines.append(f"{kind} {entry.relative_to(context.root)}")
    return "\n".join(lines) or "(empty)"


def tool_read_file(context, args):
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    start = int(args.get("start", 1))
    end = int(args.get("end", 200))
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    body = "\n".join(f"{number:>4}: {line}" for number, line in enumerate(lines[start - 1:end], start=start))
    return f"# {path.relative_to(context.root)}\n{body}"


def tool_search(context, args):
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    path = context.path(args.get("path", "."))
    deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS

    rg_path = shutil.which("rg")
    if rg_path:
        # 优先用 rg，因为搜索会非常频繁，搜索延迟会直接影响 agent 控制循环。
        try:
            result = subprocess.run(
                [
                    rg_path,
                    "-n",
                    "--smart-case",
                    "--max-count",
                    str(SEARCH_MAX_MATCHES),
                    "--max-filesize",
                    str(SEARCH_MAX_FILE_BYTES),
                    pattern,
                    str(path),
                ],
                cwd=context.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=SEARCH_RG_TIMEOUT_SECONDS,
                creationflags=hidden_process_creation_flags(),
                startupinfo=hidden_process_startupinfo(),
            )
        except (OSError, subprocess.TimeoutExpired):
            # Packaged Workers may not inherit the interactive shell PATH, and
            # an external rg process may still stall. The bounded fallback
            # below preserves regex semantics without blocking the whole Run.
            pass
        else:
            if result.returncode in {0, 1}:
                return result.stdout.strip() or "(no matches)"
            if result.stderr.strip():
                return result.stderr.strip()

    flags = 0 if any(character.isupper() for character in pattern) else re.IGNORECASE
    try:
        expression = re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"invalid search pattern: {exc}") from exc

    matches = []
    scanned_files = 0
    if path.is_file():
        files = (path,)
    else:
        def iter_files():
            for directory, names, filenames in os.walk(path):
                names[:] = [name for name in names if name not in SEARCH_IGNORED_PATH_NAMES]
                for filename in filenames:
                    yield Path(directory) / filename

        files = iter_files()

    for file_path in files:
        if scanned_files >= SEARCH_MAX_FILES or time.monotonic() >= deadline:
            break
        scanned_files += 1
        try:
            if file_path.stat().st_size > SEARCH_MAX_FILE_BYTES:
                continue
            raw = file_path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw:
            continue
        for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), start=1):
            if expression.search(line):
                matches.append(f"{file_path.relative_to(context.root)}:{number}:{line}")
                if len(matches) >= SEARCH_MAX_MATCHES:
                    return "\n".join(matches)
    if matches:
        return "\n".join(matches)
    if scanned_files >= SEARCH_MAX_FILES or time.monotonic() >= deadline:
        return "(no matches; fallback search reached its scan limit)"
    return "(no matches)"


def tool_run_shell(context, args):
    command = str(args.get("command", "")).strip()
    if not command:
        raise ValueError("command must not be empty")
    timeout = int(args.get("timeout", 20))
    if timeout < 1 or timeout > 120:
        raise ValueError("timeout must be in [1, 120]")
    # 受管 Shell：取消/超时时终止整个进程树；stdout/stderr 有界保留。
    # 这里传入的是过滤后的环境变量，而不是直接继承整个父 shell 环境，
    # 目的是减少敏感信息被意外带进命令执行环境的风险。
    # 当调用方注入了 shell_factory(如 Docker 沙箱)时，用它替代宿主机 Shell；
    # 沙箱后端自身 fail-closed，绝不允许静默回退到宿主机无限制执行。
    if context.shell_factory is not None:
        shell = context.shell_factory(
            command,
            cwd=context.root,
            env=context.shell_env(),
            timeout=timeout,
            output_max_bytes=context.shell_output_max_bytes,
            cancellation_token=context.cancellation_token,
            cleanup_grace_seconds=context.shell_cleanup_grace_seconds,
        )
    else:
        shell = ShellProcess(
            command,
            cwd=context.root,
            env=context.shell_env(),
            timeout=timeout,
            output_max_bytes=context.shell_output_max_bytes,
            cancellation_token=context.cancellation_token,
            cleanup_grace_seconds=context.shell_cleanup_grace_seconds,
        )
    context.register_shell(shell)
    try:
        result = shell.run()
    except Exception:
        if not shell.is_running():
            context.release_shell(shell)
        raise
    if getattr(result, "cleanup_succeeded", True):
        context.release_shell(shell)
    if not getattr(result, "cleanup_succeeded", True):
        raise ProcessCleanupFailed()
    if result.cancelled:
        raise RunCancelled()
    suffix = ""
    if result.timed_out:
        suffix += f"\n[tool timed out after {timeout}s]"
    if result.output_truncated:
        suffix += "\n[output truncated]"
    return textwrap.dedent(
        f"""\
        exit_code: {result.returncode}
        stdout:
        {result.stdout.strip() or "(empty)"}
        stderr:
        {result.stderr.strip() or "(empty)"}
        """
    ).strip() + suffix


def tool_write_file(context, args):
    path = context.path(args["path"])
    content = str(args["content"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {path.relative_to(context.root)} ({len(content)} chars)"


def tool_patch_file(context, args):
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    old_text = str(args.get("old_text", ""))
    if not old_text:
        raise ValueError("old_text must not be empty")
    if "new_text" not in args:
        raise ValueError("missing new_text")
    text = path.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count != 1:
        raise ValueError(f"old_text must occur exactly once, found {count}")
    path.write_text(text.replace(old_text, str(args["new_text"]), 1), encoding="utf-8")
    return f"patched {path.relative_to(context.root)}"


def tool_delegate(context, args):
    if context.depth >= context.max_depth:
        raise ValueError("delegate depth exceeded")
    task = str(args.get("task", "")).strip()
    if not task:
        raise ValueError("task must not be empty")
    return context.spawn_delegate(args)


_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "search": tool_search,
    "run_shell": tool_run_shell,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
}


# 默认注册表（builtin 6 工具 + 条件 delegate）。模块级单例只在构建时
# 持有 specs 与 runner 函数引用，不持有任何 context 绑定的 partial，
# 因此跨 context 复用安全。
_DEFAULT_REGISTRY = ToolRegistry()
