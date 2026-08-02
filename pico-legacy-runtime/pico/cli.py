"""命令行入口。

这个模块负责把“用户怎么启动 pico”翻译成 runtime 能理解的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
import os
import shutil
import sys
import textwrap

from .config import load_project_env, provider_env
from .providers.clients import AnthropicCompatibleModelClient, OllamaModelClient, OpenAICompatibleModelClient
from .runtime import Pico, SessionStore
from .workspace import WorkspaceContext, middle

DEFAULT_SECRET_ENV_NAMES = (
    "PICO_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "PICO_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "PICO_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "PICO_RIGHT_CODES_API_KEY",
    "RIGHT_CODES_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)

WELCOME_ART = (
    "        /\\___/\\\\",
    "       (  o o  )",
    "       /   ^   \\\\",
    "      /|       |\\\\",
)
WELCOME_NAME = "pico"
WELCOME_SUBTITLE = "local coding agent"
WELCOME_STATUS = "calm shell, ready for work"
HELP_DETAILS = textwrap.dedent(
    """\
    Commands:
    /help    Show this help message.
    /memory  Show the agent's distilled working memory.
    /session Show the path to the saved session file.
    /reset   Clear the current session history and memory.
    /exit    Exit the agent.
    """
).strip()


DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OPENAI_MODEL = "gpt-5.4"
DEFAULT_OPENAI_BASE_URL = "https://www.right.codes/codex/v1"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_ANTHROPIC_BASE_URL = "https://www.right.codes/claude/v1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"
SECRET_ENV_NAMES_VAR = "PICO_SECRET_ENV_NAMES"


def _effective_model(args, provider):
    # 模型选择优先级：
    # 1. 用户显式传入 --model
    # 2. provider 对应的环境变量
    # 3. 代码里的默认值
    explicit_model = getattr(args, "model", None)
    if explicit_model:
        return explicit_model
    if provider == "openai":
        model = provider_env("PICO_OPENAI_MODEL", ("OPENAI_MODEL",))
        if model:
            return model
        return DEFAULT_OPENAI_MODEL
    if provider == "anthropic":
        model = provider_env("PICO_ANTHROPIC_MODEL", ("ANTHROPIC_MODEL",))
        if model:
            return model
        return DEFAULT_ANTHROPIC_MODEL
    if provider == "deepseek":
        model = provider_env("PICO_DEEPSEEK_MODEL", ("DEEPSEEK_MODEL",))
        if model:
            return model
        return DEFAULT_DEEPSEEK_MODEL
    return DEFAULT_OLLAMA_MODEL


def _configured_secret_names(args):
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    extra_names = os.environ.get(SECRET_ENV_NAMES_VAR, "")
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper()
            for item in extra_names.split(",")
            if item.strip()
        )
    return sorted(configured_secret_names)


def _build_model_client(args, *, model_override=None, temperature_override=None):
    provider = getattr(args, "provider", "deepseek")
    temperature = args.temperature if temperature_override is None else temperature_override
    # CLI 只负责把 provider 选择翻译成具体 client。
    # 真正的提示词格式、缓存支持、HTTP 协议差异，都封装在 models.py 里。
    if provider == "openai":
        model = model_override or _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("PICO_OPENAI_API_BASE", ("OPENAI_API_BASE",), DEFAULT_OPENAI_BASE_URL)
        api_key = provider_env(
            "PICO_OPENAI_API_KEY",
            ("OPENAI_API_KEY", "PICO_RIGHT_CODES_API_KEY", "RIGHT_CODES_API_KEY", "PICO_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
        )
        return OpenAICompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )
    if provider == "anthropic":
        model = model_override or _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("PICO_ANTHROPIC_API_BASE", ("ANTHROPIC_API_BASE",), DEFAULT_ANTHROPIC_BASE_URL)
        api_key = provider_env(
            "PICO_ANTHROPIC_API_KEY",
            ("ANTHROPIC_API_KEY", "PICO_RIGHT_CODES_API_KEY", "RIGHT_CODES_API_KEY", "PICO_OPENAI_API_KEY", "OPENAI_API_KEY"),
        )
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )
    if provider == "deepseek":
        model = model_override or _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("PICO_DEEPSEEK_API_BASE", ("DEEPSEEK_API_BASE",), DEFAULT_DEEPSEEK_BASE_URL)
        api_key = provider_env("PICO_DEEPSEEK_API_KEY", ("DEEPSEEK_API_KEY",))
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )

    model = model_override or _effective_model(args, provider)
    host = getattr(args, "host", DEFAULT_OLLAMA_HOST)
    return OllamaModelClient(
        model=model,
        host=host,
        temperature=temperature,
        top_p=args.top_p,
        timeout=args.ollama_timeout,
    )


def build_welcome(agent, model, host):
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width

    def row(text):
        body = middle(text, width - 4)
        return f"| {body.ljust(width - 4)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    def cell(label, value, size):
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    line = divider("=")
    rows = [center(text) for text in WELCOME_ART]
    rows.extend(
        [
            center(WELCOME_NAME),
            center(WELCOME_SUBTITLE),
            center(WELCOME_STATUS),
            divider("-"),
            row(""),
            row("WORKSPACE  " + middle(agent.workspace.cwd, inner - 11)),
            pair("MODEL", model, "BRANCH", agent.workspace.branch),
            pair("APPROVAL", agent.approval_policy, "SESSION", agent.session["id"]),
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])


def build_agent(args):
    """根据 CLI 参数装配出一个可运行的 Pico 实例。

    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把“启动参数”翻译成“agent 运行现场”。

    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`
    - 输出：一个新的 `Pico`，或一个从旧 session 恢复出来的 `Pico`

    在 agent 链路里的位置：
    它是整个程序启动链路里最靠近 runtime 的装配点。`main()` 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 `ask()`。
    """
    # 这里是 CLI 到 runtime 的装配点：
    # 先采集工作区快照和加载项目级环境，再整理 secret 名单、模型后端和 session。
    workspace = WorkspaceContext.build(args.cwd)
    load_project_env(workspace.repo_root)
    configured_secret_names = _configured_secret_names(args)
    store = SessionStore(workspace.repo_root + "/.pico/sessions")
    model = _build_model_client(args)
    progress_callback = None if getattr(args, "quiet", False) else print_progress
    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest()
    if session_id:
        return Pico.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            secret_env_names=configured_secret_names,
            progress_callback=progress_callback,
        )
    return Pico(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=configured_secret_names,
        progress_callback=progress_callback,
    )


def build_arg_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Minimal coding agent for DeepSeek, OpenAI-compatible, Anthropic-compatible, or Ollama models.",
    )
    _add_run_arguments(parser)
    return parser


def _add_run_arguments(parser):
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument(
        "--backend",
        choices=("native", "langgraph"),
        default="native",
        help="Agent orchestration backend.",
    )
    parser.add_argument("--provider", choices=("ollama", "openai", "anthropic", "deepseek"), default="deepseek", help="Model backend to use.")
    parser.add_argument(
        "--model",
        default=None,
        help="Model name override. Defaults to qwen3.5:4b for Ollama, PICO_OPENAI_MODEL for openai, PICO_ANTHROPIC_MODEL for anthropic, and PICO_DEEPSEEK_MODEL for deepseek when set.",
    )
    parser.add_argument("--host", default=DEFAULT_OLLAMA_HOST, help="Ollama server URL.")
    parser.add_argument("--base-url", default=None, help="Provider API base URL for deepseek, openai, or anthropic.")
    parser.add_argument("--ollama-timeout", type=int, default=300, help="Ollama request timeout in seconds.")
    parser.add_argument("--openai-timeout", type=int, default=300, help="OpenAI-compatible request timeout in seconds.")
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    parser.add_argument("--approval", choices=("ask", "auto", "never"), default="ask", help="Approval policy for risky tools.")
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to treat as secrets for trace/report redaction.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=6,
        help="Maximum native tool calls or LangGraph Coordinator tool steps per request.",
    )
    parser.add_argument("--max-new-tokens", type=int, default=512, help="Maximum model output tokens per step.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature sent to Ollama.")
    parser.add_argument("--top-p", type=float, default=0.9, help="Top-p sampling value sent to Ollama.")
    parser.add_argument(
        "--task-mode",
        choices=("auto", "conversation", "read_only", "code_change"),
        default="auto",
        help="LangGraph task intent; auto uses the constrained intent router.",
    )
    parser.add_argument(
        "--router-model",
        default=None,
        help="Model name override for LangGraph auto intent routing.",
    )
    parser.add_argument(
        "--acceptance",
        default=None,
        help="Acceptance criteria for the LangGraph review role. Defaults to the prompt.",
    )
    parser.add_argument(
        "--focus-path",
        dest="focus_paths",
        action="append",
        default=[],
        help="Workspace-relative review path for LangGraph; may be repeated.",
    )
    parser.add_argument(
        "--research",
        dest="requires_research",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable the LangGraph research delegate.",
    )
    parser.add_argument("--quiet", action="store_true", help="Hide per-step progress messages.")


def _add_eval_subcommand(subparsers):
    parser = subparsers.add_parser("eval", help="run eval harness against benchmark tasks")
    parser.add_argument("--tasks", default="benchmarks/coding_tasks.json")
    parser.add_argument("--out", default=None, help="output JSON (default: benchmarks/results/<ts>-eval.json)")
    parser.add_argument("--backend", choices=("native", "langgraph"), default="native")


def _build_command_parser():
    parser = argparse.ArgumentParser(description="Pico coding agent and evaluation harness.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="run the Pico agent")
    _add_run_arguments(run_parser)
    _add_eval_subcommand(subparsers)
    return parser


def _run_eval(args):
    from datetime import datetime
    from pathlib import Path

    from .evaluation.evaluator import run_fixed_benchmark

    output_path = args.out or f"benchmarks/results/{datetime.now().strftime('%Y%m%d-%H%M%S')}-eval.json"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    result = run_fixed_benchmark(
        benchmark_path=args.tasks,
        artifact_path=output_path,
        backend=args.backend,
    )
    summary = result["summary"]
    print(
        f"tasks:{summary['total_tasks']}  passed:{summary['passed']}  "
        f"failed:{summary['failed']}  ({summary['pass_rate']:.0%})"
    )
    print(f"-> {output_path}")
    if summary["eligible_tasks"] == 0:
        return 2
    return 0 if summary["failed"] == 0 else 1


def _validate_run_args(args):
    backend = getattr(args, "backend", "native")
    task_mode = getattr(args, "task_mode", "auto")
    router_model = getattr(args, "router_model", None)
    requires_research = getattr(args, "requires_research", None)
    focus_paths = getattr(args, "focus_paths", ())
    acceptance = getattr(args, "acceptance", None)

    if backend == "native":
        if task_mode != "auto":
            raise ValueError("--task-mode is only valid with --backend langgraph")
        if router_model is not None:
            raise ValueError("--router-model is only valid with --backend langgraph")
        if requires_research is not None:
            raise ValueError("--research/--no-research is only valid with --backend langgraph")
        if focus_paths:
            raise ValueError("--focus-path is only valid with --backend langgraph")
        if acceptance is not None:
            raise ValueError("--acceptance is only valid with --backend langgraph")
        return

    if router_model is not None and task_mode != "auto":
        raise ValueError("--router-model requires --task-mode auto")
    if task_mode in {"conversation", "read_only"} and focus_paths:
        raise ValueError(f"--focus-path is not valid with --task-mode {task_mode}")
    if task_mode in {"conversation", "read_only"} and acceptance is not None:
        raise ValueError(f"--acceptance is not valid with --task-mode {task_mode}")
    if task_mode == "conversation" and requires_research is True:
        raise ValueError("--research is not valid with --task-mode conversation")


def _run_request(agent, prompt, args, router_model_client=None):
    if getattr(args, "backend", "native") == "native":
        answer = agent.ask(prompt)
        return answer, True, agent.current_task_state.stop_reason
    try:
        from langgraph_pico import run_agent
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "langgraph backend is optional; install examples/langgraph-pico first"
        ) from exc
    result = run_agent(
        agent,
        prompt,
        acceptance=getattr(args, "acceptance", None),
        step_budget=args.max_steps,
        requires_research=getattr(args, "requires_research", None),
        focus_paths=getattr(args, "focus_paths", ()),
        task_mode=getattr(args, "task_mode", "auto"),
        router_model_client=router_model_client,
        record_session=True,
    )
    succeeded = result.task_state.status == "completed"
    return result.final_answer, succeeded, result.task_state.stop_reason


def print_progress(message):
    print(f"[pico] {message}", file=sys.stderr, flush=True)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"run", "eval"}:
        parser = _build_command_parser()
        args = parser.parse_args(argv)
        if args.command == "eval":
            return _run_eval(args)
    else:
        parser = build_arg_parser()
        args = parser.parse_args(argv)
    try:
        _validate_run_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    agent = build_agent(args)
    router_model_client = None
    if args.backend == "langgraph" and args.task_mode == "auto":
        router_model_client = _build_model_client(
            args,
            model_override=args.router_model,
            temperature_override=0.0,
        )

    model = getattr(agent.model_client, "model", getattr(args, "model", DEFAULT_OLLAMA_MODEL))
    host = getattr(agent.model_client, "host", getattr(agent.model_client, "base_url", getattr(args, "host", DEFAULT_OLLAMA_HOST)))
    print(build_welcome(agent, model=model, host=host))

    if args.prompt:
        # one-shot 模式：只跑一次 ask，不进入 REPL 循环。
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            try:
                answer, succeeded, stop_reason = _run_request(
                    agent,
                    prompt,
                    args,
                    router_model_client,
                )
                print(answer)
                if not succeeded:
                    print(f"[pico] stopped: {stop_reason}", file=sys.stderr)
                    return 1
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        return 0

    while True:
        # 交互模式：每次读取一条用户输入，交给同一个 agent，
        # 因此 session history 和 working memory 会跨轮延续。
        try:
            user_input = input("\npico> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0

        if not user_input:
            continue
        if user_input in {"/exit", "/quit"}:
            return 0
        if user_input == "/help":
            print(HELP_DETAILS)
            continue
        if user_input == "/memory":
            print(agent.memory_text())
            continue
        if user_input == "/session":
            print(agent.session_path)
            continue
        if user_input == "/reset":
            agent.reset()
            print("session reset")
            continue

        print()
        try:
            answer, succeeded, stop_reason = _run_request(
                agent,
                user_input,
                args,
                router_model_client,
            )
            print(answer)
            if not succeeded:
                print(f"[pico] stopped: {stop_reason}", file=sys.stderr)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
