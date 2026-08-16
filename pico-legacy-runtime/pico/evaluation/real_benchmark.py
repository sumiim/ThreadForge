"""真模型评测闭环：用真实 provider 跑 coding_tasks.json 中的改文件任务。

为什么存在（roadmap §10）：
- 现有 harness 只用 FakeModelClient（脚本化确定性），CI 里验证的是机制契约
  而非「真模型能不能把任务改对」。改 prompt/预算后不知道真实通过率是升是降。
- 本模块提供一个「真模型 + 真实小任务 + verifier 断言 + 通过率门禁」的最小闭环：
  跑真模型端到端改文件任务，产出可比的通过率作为回归护栏。

边界：
- 不引入外部评测平台；复用现有 `evaluation/` 的 evaluator/verifier/metrics。
- 只跑 `real_model: true` 的任务（改文件类）；checkpoint/resume/durable 等
  harness 机制任务需要脚本化触发，真模型跑会误报，跳过。
- 分数只做「上线前自检」，不做自动发布。

用法：
    PICO_DEEPSEEK_API_KEY=sk-xxx \
    PICO_DEEPSEEK_MODEL=deepseek-v4-flash \
    python -m pico.evaluation.real_benchmark [--min-pass-rate 0.8]

产物：
    benchmarks/results/deepseek-<model>-<date>.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..providers.clients import OpenAICompletionsModelClient
from .evaluator import (
    DEFAULT_BENCHMARK_PATH,
    BenchmarkEvaluator,
    load_benchmark,
)

DEFAULT_RESULTS_ROOT = Path("benchmarks/results")
DEFAULT_MIN_PASS_RATE = 0.8
# deepseek-v4-flash 是推理模型：流式响应先花 reasoning tokens 再输出 content。
# max_tokens 太小（如 512）时预算全被思考吃掉 → content 为空 → model_response_invalid。
DEFAULT_MAX_NEW_TOKENS = 4096
DEFAULT_TIMEOUT_SECONDS = 300


def create_deepseek_client_factory(*, max_new_tokens: int = DEFAULT_MAX_NEW_TOKENS):
    """返回 evaluator 需要的 model_client_factory(task, workspace) -> client。

    DeepSeek 提供 OpenAI 兼容的 chat/completions 端点；worker 侧把
    protocol=deepseek 映射到 OpenAICompletionsModelClient（见
    local-worker runtime.py），这里复用同一客户端与 base_url 约定。
    """

    def load_env() -> dict:
        api_key = os.environ.get("PICO_DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError(
                "PICO_DEEPSEEK_API_KEY (or DEEPSEEK_API_KEY) is not configured; "
                "set it before running the real-model benchmark"
            )
        return {
            "api_key": api_key,
            "model": os.environ.get("PICO_DEEPSEEK_MODEL", "deepseek-v4-flash"),
            "base_url": os.environ.get("PICO_DEEPSEEK_API_BASE", "https://api.deepseek.com"),
        }

    env = load_env()

    def factory(task, workspace):
        del task, workspace
        return OpenAICompletionsModelClient(
            model=env["model"],
            base_url=env["base_url"],
            api_key=env["api_key"],
            temperature=0.0,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            max_attempts=3,
        )

    factory.max_new_tokens = int(max_new_tokens)
    factory.profile = env
    return factory


def _real_tasks(benchmark_path: Path) -> list[dict]:
    """只保留 real_model: true 的任务；保留原始顺序。"""
    benchmark = load_benchmark(benchmark_path)
    tasks = [task for task in benchmark["tasks"] if task.get("real_model") is True]
    if not tasks:
        raise RuntimeError(
            f"no real_model tasks found in {benchmark_path}; "
            "mark tasks with \"real_model\": true to include them in the real-model benchmark"
        )
    return tasks


def _default_artifact_path(model: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_model = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in model)
    return DEFAULT_RESULTS_ROOT / f"deepseek-{safe_model}-{stamp}.json"


def run_real_benchmark(
    benchmark_path=DEFAULT_BENCHMARK_PATH,
    artifact_path=None,
    min_pass_rate=DEFAULT_MIN_PASS_RATE,
    max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
    workspace_root=None,
    repetitions=1,
):
    """跑真模型评测。

    ``repetitions``：真模型有随机性 + API 波动，单次通过率不可靠。
    多次跑时聚合「每个任务至少通过一次」的通过率（宽松口径）与
    「所有重复全部通过」的通过率（严格口径），并取平均值作为基线。
    """
    benchmark_path = Path(benchmark_path)
    tasks = _real_tasks(benchmark_path)
    factory = create_deepseek_client_factory(max_new_tokens=max_new_tokens)
    profile = factory.profile
    if artifact_path is None:
        artifact_path = _default_artifact_path(profile["model"])
    artifact_path = Path(artifact_path)
    repetitions = max(1, int(repetitions))

    payloads = []
    for repetition in range(repetitions):
        # 每次重复用独立 workspace，避免 fixture 污染；artifact 写临时文件，
        # 最终聚合后统一写最终 artifact。
        temp_artifact = artifact_path.with_suffix(f".rep{repetition}.json")
        evaluator = BenchmarkEvaluator(
            benchmark_path=benchmark_path,
            artifact_path=temp_artifact,
            workspace_root=workspace_root,
            model_name="deepseek",
            model_version=profile["model"],
            temperature=0.0,
            max_new_tokens=max_new_tokens,
            model_client_factory=factory,
            backend="native",
        )
        # BenchmarkEvaluator.run() 遍历 self.load()（完整任务集）。真模型评测
        # 只跑 real_model 任务：把 benchmark 数据替换成过滤后的任务集。
        benchmark = evaluator.load()
        benchmark["tasks"] = tasks
        evaluator.load = lambda: benchmark
        payload = evaluator.run()
        temp_artifact.unlink(missing_ok=True)
        payloads.append(payload)

    total = len(tasks)
    any_pass = {task["id"]: False for task in tasks}
    all_pass = {task["id"]: True for task in tasks}
    per_task = {task["id"]: [] for task in tasks}
    for payload in payloads:
        for row in payload["rows"]:
            if row["status"] != "skipped":
                per_task.setdefault(row["id"], []).append(bool(row["passed"]))
    for task_id, results in per_task.items():
        if results:
            any_pass[task_id] = any(results)
            all_pass[task_id] = all(results)
    any_rate = sum(any_pass.values()) / total
    all_rate = sum(all_pass.values()) / total
    pass_rate = (any_rate + all_rate) / 2.0

    payload = payloads[0]
    payload["_real_benchmark"] = {
        "provider": "deepseek",
        "model": profile["model"],
        "min_pass_rate": min_pass_rate,
        "repetitions": repetitions,
        "task_ids": [task["id"] for task in tasks],
        "aggregate": {
            "pass_rate": round(pass_rate, 4),
            "any_pass_rate": round(any_rate, 4),
            "all_pass_rate": round(all_rate, 4),
            "per_task_any_pass": {k: bool(v) for k, v in any_pass.items()},
            "per_task_all_pass": {k: bool(v) for k, v in all_pass.items()},
            "per_task_results": {k: [bool(r) for r in v] for k, v in per_task.items()},
        },
    }
    artifact_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(
        f"real-model benchmark: {total} tasks x {repetitions} runs -> "
        f"pass rate {pass_rate:.1%} (any {any_rate:.1%} / all {all_rate:.1%}), "
        f"required >= {min_pass_rate:.1%}"
    )
    print(f"artifact: {artifact_path}")
    for task_id, results in per_task.items():
        status = "pass" if any_pass[task_id] else "fail"
        print(f"  [{status}] {task_id} ({', '.join('pass' if r else 'fail' for r in results)})")
    if pass_rate < min_pass_rate:
        raise SystemExit(
            f"real-model benchmark gate failed: pass rate {pass_rate:.1%} "
            f"below required {min_pass_rate:.1%}"
        )
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pico.evaluation.real_benchmark",
        description="Run the real-model coding-task benchmark (DeepSeek) with a pass-rate gate.",
    )
    parser.add_argument(
        "--benchmark",
        default=str(DEFAULT_BENCHMARK_PATH),
        help="path to the benchmark JSON (default: benchmarks/coding_tasks.json)",
    )
    parser.add_argument(
        "--artifact",
        default=None,
        help="artifact output path (default: benchmarks/results/deepseek-<model>-<ts>.json)",
    )
    parser.add_argument(
        "--workspace-root",
        default=None,
        help="workspace root for fixture copies (default: temp dir)",
    )
    parser.add_argument(
        "--min-pass-rate",
        type=float,
        default=DEFAULT_MIN_PASS_RATE,
        help=f"required pass rate; exit non-zero below it (default: {DEFAULT_MIN_PASS_RATE})",
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=DEFAULT_MAX_NEW_TOKENS,
        help=f"max tokens per model call (default: {DEFAULT_MAX_NEW_TOKENS})",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=1,
        help="how many times to run the task set (default: 1; use 3+ for a stable baseline)",
    )
    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        run_real_benchmark(
            benchmark_path=args.benchmark,
            artifact_path=args.artifact,
            min_pass_rate=args.min_pass_rate,
            max_new_tokens=args.max_new_tokens,
            workspace_root=args.workspace_root,
            repetitions=args.repetitions,
        )
        return 0
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"real-model benchmark failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
