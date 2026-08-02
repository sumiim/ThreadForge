import hashlib
import json
import locale as locale_module
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..features import memory as memorylib
from ..providers.clients import FakeModelClient
from ..runtime import Pico, SessionStore
from ..run_store import RunStore
from ..task_state import STOP_REASON_FINAL_ANSWER_RETURNED
from ..tools import legal_tool_names
from ..workspace import WorkspaceContext
from .backends import build_backend_runner
from .verifier import VerifierRunner

BENCHMARK_SCHEMA_VERSION = 1
EVALUATION_ARTIFACT_SCHEMA_VERSION = 2
DEFAULT_BENCHMARK_PATH = Path("benchmarks/coding_tasks.json")
DEFAULT_ARTIFACT_PATH = Path("benchmarks/benchmark-v1.json")
DEFAULT_HARNESS_REGRESSION_V2_ARTIFACT_PATH = Path("artifacts/harness-regression-v2.json")
DEFAULT_MODEL_NAME = "FakeModelClient"
DEFAULT_MODEL_VERSION = "scripted-deterministic"
DEFAULT_TEMPERATURE = 0.0
DEFAULT_TOP_P = 1.0
DEFAULT_MAX_NEW_TOKENS = 64
DEFAULT_TIMEZONE = "Asia/Shanghai"
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
WINDOWS_RESERVED_TASK_IDS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

REQUIRED_BENCHMARK_KEYS = ("schema_version", "tasks")
REQUIRED_TASK_KEYS = (
    "id",
    "prompt",
    "fixture_repo",
    "allowed_tools",
    "step_budget",
    "expected_artifact",
    "category",
)

TASK_FIXTURE_ARTIFACTS = {
    "bench_repo_readme": "README.md",
    "bench_repo_patch": "sample.txt",
}

PAIRED_REVIEW_DEFECT_CASES = {
    "paired_review_recovery": ("sample.txt", "beta", "beta-reviewed", "gamma", "gamma-reviewed"),
    "paired_text_ownership_recovery": (
        "sample.txt",
        "status=pending",
        "status=ready",
        "owner=unassigned",
        "owner=platform-team",
    ),
    "paired_readme_deployment_recovery": (
        "README.md",
        "Environment: local",
        "Environment: staging",
        "Deployment: manual",
        "Deployment: automated",
    ),
    "paired_readme_audit_recovery": (
        "README.md",
        "Logging: basic",
        "Logging: structured",
        "Audit: disabled",
        "Audit: enabled",
    ),
    "paired_json_retry_recovery": (
        "settings.json",
        '"timeout": 10',
        '"timeout": 30',
        '"retries": 1',
        '"retries": 3',
    ),
    "paired_json_audit_recovery": (
        "settings.json",
        '"log_level": "info"',
        '"log_level": "debug"',
        '"audit": false',
        '"audit": true',
    ),
    "paired_toml_mode_recovery": (
        "settings.toml",
        "enabled = false",
        "enabled = true",
        'mode = "legacy"',
        'mode = "strict"',
    ),
    "paired_toml_region_recovery": (
        "settings.toml",
        "workers = 1",
        "workers = 4",
        'region = "local"',
        'region = "global"',
    ),
    "paired_python_retry_recovery": (
        "service.py",
        "DEFAULT_TIMEOUT = 10",
        "DEFAULT_TIMEOUT = 30",
        "MAX_RETRIES = 1",
        "MAX_RETRIES = 3",
    ),
    "paired_python_logging_recovery": (
        "service.py",
        "FEATURE_ENABLED = False",
        "FEATURE_ENABLED = True",
        'LOG_LEVEL = "info"',
        'LOG_LEVEL = "debug"',
    ),
}

PAIRED_CONTROL_CASES = {
    "paired_text_region_control": (
        "sample.txt",
        "region=local",
        "region=global",
        "mode=legacy",
        "mode=strict",
    ),
    "paired_text_marker_control": (
        "sample.txt",
        "alpha",
        "alpha-stable",
        "placeholder",
        "complete",
    ),
    "paired_readme_queue_control": (
        "README.md",
        "Cache: cold",
        "Cache: warm",
        "Queue: sync",
        "Queue: async",
    ),
    "paired_readme_runtime_control": (
        "README.md",
        "Environment: local",
        "Environment: production",
        "Logging: basic",
        "Logging: structured",
    ),
    "paired_json_batch_control": (
        "settings.json",
        '"cache_ttl": 30',
        '"cache_ttl": 120',
        '"batch_size": 10',
        '"batch_size": 50',
    ),
    "paired_json_logging_control": (
        "settings.json",
        '"timeout": 10',
        '"timeout": 20',
        '"log_level": "info"',
        '"log_level": "warning"',
    ),
    "paired_toml_format_control": (
        "settings.toml",
        "compression = false",
        "compression = true",
        'format = "text"',
        'format = "json"',
    ),
    "paired_toml_workers_control": (
        "settings.toml",
        "workers = 1",
        "workers = 8",
        "enabled = false",
        "enabled = true",
    ),
    "paired_python_batch_control": (
        "service.py",
        "CACHE_TTL = 30",
        "CACHE_TTL = 120",
        "BATCH_SIZE = 10",
        "BATCH_SIZE = 50",
    ),
    "paired_python_runtime_control": (
        "service.py",
        "FEATURE_ENABLED = False",
        "FEATURE_ENABLED = True",
        'LOG_LEVEL = "info"',
        'LOG_LEVEL = "warning"',
    ),
}


def _paired_patch_output(path, old_text, new_text):
    return (
        f'<tool name="patch_file" path="{path}"><old_text>{old_text}</old_text>'
        f"<new_text>{new_text}</new_text></tool>"
    )


def _paired_read_output(path):
    return f'<tool>{{"name":"read_file","args":{{"path":"{path}","start":1,"end":40}}}}</tool>'


def _paired_native_outputs(case, *, complete):
    path, first_old, first_new, second_old, second_new = case
    outputs = [_paired_patch_output(path, first_old, first_new)]
    if complete:
        outputs.append(_paired_patch_output(path, second_old, second_new))
    outputs.append("<final>Applied the requested updates.</final>")
    return outputs


def _paired_langgraph_outputs(case, *, complete):
    path, _, _, second_old, second_new = case
    outputs = _paired_native_outputs(case, complete=complete)
    outputs.append(_paired_read_output(path))
    if not complete:
        outputs.extend(
            [
                f"<final>status: needs_fix\nissue: {second_old} was not updated\nverify_targets: {path}</final>",
                _paired_patch_output(path, second_old, second_new),
                "<final>Completed the missing update.</final>",
                _paired_read_output(path),
            ]
        )
    outputs.append(f"<final>status: pass\nissues: none\nverify_targets: {path}</final>")
    return outputs

NATIVE_SCRIPTED_MODEL_OUTPUTS = {
    "readme_intro_locked": [
        '<tool name="patch_file" path="README.md"><old_text>This is a placeholder benchmark fixture.</old_text><new_text>This fixture is a locked benchmark workspace.</new_text></tool>',
        "<final>Done.</final>",
    ],
    "readme_schema_note": [
        '<tool name="patch_file" path="README.md"><old_text>- Placeholder note about the repo.</old_text><new_text>- The benchmark schema and baseline are fixed.</new_text></tool>',
        "<final>Done.</final>",
    ],
    "readme_ordering_note": [
        '<tool name="patch_file" path="README.md"><old_text>- Placeholder note about the file layout.</old_text><new_text>- Deterministic file ordering keeps benchmark diffs stable.</new_text></tool>',
        "<final>Done.</final>",
    ],
    "sample_beta_locked": [
        '<tool name="patch_file" path="sample.txt"><old_text>beta</old_text><new_text>beta-locked</new_text></tool>',
        "<final>Done.</final>",
    ],
    "sample_gamma_locked": [
        '<tool name="patch_file" path="sample.txt"><old_text>gamma</old_text><new_text>gamma-locked</new_text></tool>',
        "<final>Done.</final>",
    ],
    "sample_placeholder_delta": [
        '<tool name="patch_file" path="sample.txt"><old_text>placeholder</old_text><new_text>delta</new_text></tool>',
        "<final>Done.</final>",
    ],
    "invalid_patch_recovery": [
        '<tool>{"name":"patch_file","args":{"path":"README.md","old_text":"This is a placeholder benchmark fixture."}}</tool>',
        '<tool name="patch_file" path="README.md"><old_text>This is a placeholder benchmark fixture.</old_text><new_text>This fixture recovered after invalid patch args.</new_text></tool>',
        "<final>Done.</final>",
    ],
    "path_escape_recovery": [
        '<tool>{"name":"read_file","args":{"path":"../outside.txt","start":1,"end":1}}</tool>',
        '<tool name="patch_file" path="sample.txt"><old_text>alpha</old_text><new_text>alpha-guarded</new_text></tool>',
        "<final>Done.</final>",
    ],
    "repeated_read_recovery": [
        '<tool>{"name":"read_file","args":{"path":"sample.txt","start":1,"end":4}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"sample.txt","start":1,"end":4}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"sample.txt","start":1,"end":4}}</tool>',
        '<tool name="patch_file" path="sample.txt"><old_text>placeholder</old_text><new_text>repeat-guarded</new_text></tool>',
        "<final>Done.</final>",
    ],
    "context_reduction_checkpoint": [
        "<final>Done.</final>",
    ],
    "freshness_reanchor_resume": [
        "<final>Done.</final>",
    ],
    "workspace_mismatch_resume": [
        "<final>Done.</final>",
    ],
    "durable_promotion_accept": [
        "<final>Project convention: Preserve benchmark regression artifacts under artifacts/.\nDecision: Keep harness regression deterministic and reproducible.</final>",
    ],
    "durable_promotion_reject": [
        "<final>Project convention: Keep verifier outcomes stable across reruns.\nDependency: API key is sk-benchmark-secret.\nDecision: Current goal is debug the harness.</final>",
    ],
    "default_delegate_write_readonly_block": [
        '<tool>{"name":"delegate","args":{"task":"Try to write a marker to sample.txt, then report the result.","max_steps":3}}</tool>',
        '<tool>{"name":"write_file","args":{"path":"sample.txt","content":"unsafe"}}</tool>',
        "<final>The write was denied by the read-only delegate sandbox.</final>",
        '<tool name="patch_file" path="sample.txt"><old_text>placeholder</old_text><new_text>readonly-guarded</new_text></tool>',
        "<final>Done.</final>",
    ],
}

LANGGRAPH_SCRIPTED_MODEL_OUTPUTS = {
    "research_then_patch": [
        '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":20}}</tool>',
        "<final>Findings: README.md contains the placeholder opening.\nCandidate files: README.md\nSuggested action: replace the placeholder sentence.</final>",
        '<tool name="patch_file" path="README.md"><old_text>This is a placeholder benchmark fixture.</old_text><new_text>This fixture was updated after delegated research.</new_text></tool>',
        "<final>Updated README.md from the research findings.</final>",
        '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":20}}</tool>',
        "<final>status: pass\nissues: none\nverify_targets: README.md</final>",
    ],
    "review_catches_incomplete_fix": [
        '<tool name="patch_file" path="sample.txt"><old_text>beta</old_text><new_text>beta-reviewed</new_text></tool>',
        "<final>Updated beta.</final>",
        '<tool>{"name":"read_file","args":{"path":"sample.txt","start":1,"end":10}}</tool>',
        "<final>status: needs_fix\nissue: gamma is not yet reviewed\nverify_targets: sample.txt</final>",
        '<tool name="patch_file" path="sample.txt"><old_text>gamma</old_text><new_text>gamma-reviewed</new_text></tool>',
        "<final>Completed the missing gamma update.</final>",
        '<tool>{"name":"read_file","args":{"path":"sample.txt","start":1,"end":10}}</tool>',
        "<final>status: pass\nissues: none\nverify_targets: sample.txt</final>",
    ],
    "delegate_write_denied": [
        '<tool>{"name":"write_file","args":{"path":"sample.txt","content":"unsafe"}}</tool>',
        "<final>Findings: the research role could not write because write_file is not allowed.\nCandidate files: sample.txt\nSuggested action: let the executor apply the safe patch.</final>",
        '<tool name="patch_file" path="sample.txt"><old_text>placeholder</old_text><new_text>delegate-write-denied</new_text></tool>',
        "<final>Applied the safe executor patch.</final>",
        '<tool>{"name":"read_file","args":{"path":"sample.txt","start":1,"end":10}}</tool>',
        "<final>status: pass\nissues: none\nverify_targets: sample.txt</final>",
    ],
}

for task_id, case in PAIRED_REVIEW_DEFECT_CASES.items():
    NATIVE_SCRIPTED_MODEL_OUTPUTS[task_id] = _paired_native_outputs(case, complete=False)
    LANGGRAPH_SCRIPTED_MODEL_OUTPUTS[task_id] = _paired_langgraph_outputs(case, complete=False)

for task_id, case in PAIRED_CONTROL_CASES.items():
    NATIVE_SCRIPTED_MODEL_OUTPUTS[task_id] = _paired_native_outputs(case, complete=True)
    LANGGRAPH_SCRIPTED_MODEL_OUTPUTS[task_id] = _paired_langgraph_outputs(case, complete=True)

SCRIPTED_MODEL_OUTPUTS = {
    "native": NATIVE_SCRIPTED_MODEL_OUTPUTS,
    "langgraph": LANGGRAPH_SCRIPTED_MODEL_OUTPUTS,
}


def _git_value(args, fallback="", cwd=None):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or Path.cwd(),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip() or fallback
    except Exception:
        return fallback


def _current_locale():
    try:
        return locale_module.setlocale(locale_module.LC_CTYPE)
    except Exception:
        return locale_module.getdefaultlocale()[0] or "C"


def _now_in_timezone(timezone_name):
    try:
        timezone_info = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        if timezone_name != DEFAULT_TIMEZONE:
            raise
        timezone_info = timezone(timedelta(hours=8), name=DEFAULT_TIMEZONE)
    return datetime.now(timezone_info).strftime("%Y-%m-%dT%H:%M:%S%z")


def _artifact_path_for_task(task):
    explicit_path = str(task.get("artifact_path", "")).strip()
    if explicit_path:
        return explicit_path
    fixture_repo_name = Path(str(task["fixture_repo"])).name
    if fixture_repo_name not in TASK_FIXTURE_ARTIFACTS:
        raise ValueError(f"unsupported fixture repo for artifact lookup: {fixture_repo_name}")
    return TASK_FIXTURE_ARTIFACTS[fixture_repo_name]


def _workspace_relative(path, workspace_root):
    return str(Path(path).resolve().relative_to(Path(workspace_root).resolve()))


def _scripted_outputs_for_task(task, backend="native"):
    outputs = SCRIPTED_MODEL_OUTPUTS.get(backend, {}).get(task["id"])
    if outputs is None:
        raise ValueError(f"no scripted model outputs for {backend} benchmark task: {task['id']}")
    return list(outputs)


def _normalized_relative_path(value, fixture_repo, task_id, field):
    if not isinstance(value, str):
        raise ValueError(f"benchmark task {task_id} {field} must be a string")
    raw = value.strip()
    if not raw:
        raise ValueError(f"benchmark task {task_id} {field} must not be empty")
    candidate = Path(raw)
    if candidate.is_absolute() or PureWindowsPath(raw).is_absolute():
        raise ValueError(f"benchmark task {task_id} {field} must be relative")
    fixture_root = Path(fixture_repo).resolve()
    resolved = (fixture_root / candidate).resolve()
    try:
        relative = resolved.relative_to(fixture_root)
    except ValueError as exc:
        raise ValueError(f"benchmark task {task_id} {field} escapes fixture root") from exc
    if not relative.parts:
        raise ValueError(f"benchmark task {task_id} {field} must name a path")
    return relative.as_posix()


def _normalized_task_id(value):
    if not isinstance(value, str):
        raise ValueError("benchmark task id must be a string")
    task_id = value.strip()
    windows_stem = task_id.split(".", 1)[0].upper()
    if (
        not TASK_ID_PATTERN.fullmatch(task_id)
        or task_id.endswith(".")
        or windows_stem in WINDOWS_RESERVED_TASK_IDS
    ):
        raise ValueError(
            "benchmark task id must be a filesystem-safe slug using letters, numbers, '.', '_' or '-'"
        )
    return task_id


def _normalized_fixture_repo(value, repo_root, task_id):
    if not isinstance(value, str):
        raise ValueError(f"benchmark task {task_id} fixture_repo must be a string")
    raw = value.strip()
    if not raw:
        raise ValueError(f"benchmark task {task_id} fixture_repo must not be empty")
    candidate = Path(raw)
    if candidate.is_absolute() or PureWindowsPath(raw).is_absolute():
        raise ValueError(f"benchmark task {task_id} fixture_repo must be relative")
    resolved = (repo_root / candidate).resolve()
    try:
        relative = resolved.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(f"benchmark task {task_id} fixture_repo escapes repository root") from exc
    if not resolved.is_dir():
        raise ValueError(f"benchmark task {task_id} fixture repo does not exist: {raw}")
    if not relative.parts:
        raise ValueError(f"benchmark task {task_id} fixture_repo must name a repository subdirectory")
    return relative.as_posix(), resolved


def _normalized_setup(value, fixture_repo, task_id):
    if not isinstance(value, dict):
        raise ValueError(f"benchmark task {task_id} setup must be a mapping")
    setup = dict(value)
    setup_kind = str(setup.get("kind", "")).strip()
    if setup_kind not in {"context_reduction", "freshness_mismatch", "workspace_mismatch"}:
        raise ValueError(f"benchmark task {task_id} has an unknown setup kind: {setup_kind}")
    setup["kind"] = setup_kind
    if setup_kind == "freshness_mismatch":
        setup["path"] = _normalized_relative_path(
            setup.get("path", "sample.txt"),
            fixture_repo,
            task_id,
            "setup.path",
        )
    return setup


def _fixture_snapshot_id(fixture_paths):
    sha = hashlib.sha256()
    for fixture_path in sorted({Path(path).resolve() for path in fixture_paths}, key=lambda path: str(path)):
        for path in sorted((item for item in fixture_path.rglob("*") if item.is_file()), key=lambda item: str(item.relative_to(fixture_path))):
            sha.update(str(fixture_path.name).encode("utf-8"))
            sha.update(b"\0")
            sha.update(str(path.relative_to(fixture_path)).encode("utf-8"))
            sha.update(b"\0")
            sha.update(path.read_bytes())
            sha.update(b"\0")
    return "sha256:" + sha.hexdigest()


def validate_benchmark(data, repo_root=None):
    if not isinstance(data, dict):
        raise ValueError("benchmark must be a mapping")

    missing = [key for key in REQUIRED_BENCHMARK_KEYS if key not in data]
    if missing:
        raise ValueError(f"benchmark is missing required keys: {', '.join(missing)}")

    if int(data.get("schema_version", 0)) != BENCHMARK_SCHEMA_VERSION:
        raise ValueError("unsupported benchmark schema_version")

    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("benchmark tasks must be a non-empty list")

    repo_root = Path(repo_root or Path.cwd()).resolve()
    seen_ids = set()
    normalized_tasks = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"benchmark task at index {index} must be a mapping")

        missing_task_keys = [key for key in REQUIRED_TASK_KEYS if key not in task]
        if missing_task_keys:
            raise ValueError(
                f"benchmark task {task.get('id', index)!r} is missing required keys: {', '.join(missing_task_keys)}"
            )

        task_id = _normalized_task_id(task["id"])
        task_id_key = task_id.casefold()
        if task_id_key in seen_ids:
            raise ValueError(f"duplicate benchmark task id: {task_id}")
        seen_ids.add(task_id_key)

        fixture_repo_value, fixture_repo = _normalized_fixture_repo(
            task["fixture_repo"],
            repo_root,
            task_id,
        )

        normalized_text = {}
        for field in ("prompt", "expected_artifact", "category"):
            value = task[field]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"benchmark task {task_id} {field} must be a non-empty string")
            normalized_text[field] = value.strip()

        has_verifier = "verifier" in task
        has_verifier_argv = "verifier_argv" in task
        if has_verifier == has_verifier_argv:
            raise ValueError(
                f"benchmark task {task_id} must provide exactly one of verifier or verifier_argv"
            )
        if has_verifier and (
            not isinstance(task["verifier"], str) or not task["verifier"].strip()
        ):
            raise ValueError(f"benchmark task {task_id} verifier must be a non-empty string")
        if has_verifier_argv:
            verifier_argv = task["verifier_argv"]
            if not isinstance(verifier_argv, list) or not verifier_argv:
                raise ValueError(f"benchmark task {task_id} verifier_argv must be a non-empty list")
            if any(not isinstance(item, str) or not item for item in verifier_argv):
                raise ValueError(
                    f"benchmark task {task_id} verifier_argv must contain non-empty strings"
                )

        allowed_tools = task["allowed_tools"]
        if not isinstance(allowed_tools, list) or not allowed_tools:
            raise ValueError(f"benchmark task {task_id} allowed_tools must be a non-empty list")
        valid_tools = legal_tool_names()
        normalized_allowed_tools = []
        for tool in allowed_tools:
            tool_name = str(tool).strip()
            if not tool_name:
                raise ValueError(f"benchmark task {task_id} has an empty allowed_tools entry")
            if tool_name not in valid_tools:
                raise ValueError(f"benchmark task {task_id} has an unknown allowed_tools entry: {tool_name}")
            normalized_allowed_tools.append(tool_name)

        raw_step_budget = task["step_budget"]
        if isinstance(raw_step_budget, bool) or not isinstance(raw_step_budget, int):
            raise ValueError(f"benchmark task {task_id} step_budget must be an integer")
        step_budget = raw_step_budget
        if step_budget < 1:
            raise ValueError(f"benchmark task {task_id} step_budget must be positive")

        requires_research = task.get("requires_research", True)
        if not isinstance(requires_research, bool):
            raise ValueError(f"benchmark task {task_id} requires_research must be a boolean")

        review_expectation = task.get("review_expectation", "")
        if not isinstance(review_expectation, str) or review_expectation not in {
            "",
            "needs_fix",
            "pass",
        }:
            raise ValueError(
                f"benchmark task {task_id} review_expectation must be needs_fix or pass"
            )
        defect_type = task.get("defect_type", "")
        if review_expectation and (not isinstance(defect_type, str) or not defect_type.strip()):
            raise ValueError(
                f"benchmark task {task_id} defect_type must be a non-empty string"
            )
        if not review_expectation and defect_type:
            raise ValueError(
                f"benchmark task {task_id} defect_type requires review_expectation"
            )

        backends = task.get("backends", ["native"])
        if not isinstance(backends, list) or not backends:
            raise ValueError(f"benchmark task {task_id} backends must be a non-empty list")
        normalized_backends = []
        for backend in backends:
            backend_name = str(backend).strip()
            if backend_name not in {"native", "langgraph"}:
                raise ValueError(f"benchmark task {task_id} has an unknown backend: {backend_name}")
            if backend_name not in normalized_backends:
                normalized_backends.append(backend_name)

        focus_paths = task.get("focus_paths", [])
        if not isinstance(focus_paths, list):
            raise ValueError(f"benchmark task {task_id} focus_paths must be a list")
        normalized_focus_paths = [
            _normalized_relative_path(path, fixture_repo, task_id, "focus_paths")
            for path in focus_paths
        ]
        artifact_path = None
        if "artifact_path" in task:
            artifact_path = _normalized_relative_path(
                task["artifact_path"], fixture_repo, task_id, "artifact_path"
            )

        normalized_task = dict(task)
        normalized_task["id"] = task_id
        normalized_task["prompt"] = normalized_text["prompt"]
        normalized_task["fixture_repo"] = fixture_repo_value
        normalized_task["allowed_tools"] = normalized_allowed_tools
        normalized_task["step_budget"] = step_budget
        normalized_task["expected_artifact"] = normalized_text["expected_artifact"]
        normalized_task["category"] = normalized_text["category"]
        acceptance = task.get("acceptance", normalized_task["prompt"])
        if not isinstance(acceptance, str) or not acceptance.strip():
            raise ValueError(f"benchmark task {task_id} acceptance must be a non-empty string")
        normalized_task["acceptance"] = acceptance.strip()
        normalized_task["requires_research"] = requires_research
        normalized_task["review_expectation"] = review_expectation
        normalized_task["defect_type"] = defect_type.strip() if isinstance(defect_type, str) else ""
        normalized_task["focus_paths"] = normalized_focus_paths
        normalized_task["backends"] = normalized_backends
        if "setup" in task:
            normalized_task["setup"] = _normalized_setup(
                task["setup"],
                fixture_repo,
                task_id,
            )
        if has_verifier:
            normalized_task["verifier"] = str(task["verifier"]).strip()
            normalized_task.pop("verifier_argv", None)
        else:
            normalized_task["verifier_argv"] = [str(item) for item in task["verifier_argv"]]
            normalized_task.pop("verifier", None)
        if artifact_path is not None:
            normalized_task["artifact_path"] = artifact_path
        if "verifier_timeout_s" in task:
            verifier_timeout_s = task["verifier_timeout_s"]
            if isinstance(verifier_timeout_s, bool) or not isinstance(verifier_timeout_s, int):
                raise ValueError(f"benchmark task {task_id} verifier_timeout_s must be an integer")
            normalized_task["verifier_timeout_s"] = verifier_timeout_s
        normalized_tasks.append(normalized_task)

    normalized = dict(data)
    normalized["schema_version"] = BENCHMARK_SCHEMA_VERSION
    normalized["tasks"] = normalized_tasks
    return normalized


def load_benchmark(path=DEFAULT_BENCHMARK_PATH, repo_root=None):
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if repo_root is None:
        repo_root = path.resolve().parent.parent
    return validate_benchmark(data, repo_root=repo_root)


def summarize_rows(rows):
    rows = list(rows)
    eligible_rows = [row for row in rows if row.get("status") != "skipped"]
    skipped_rows = [row for row in rows if row.get("status") == "skipped"]
    passed = sum(1 for row in eligible_rows if row.get("passed") or row.get("status") == "pass")
    failed = len(eligible_rows) - passed
    failure_category_counts = {}
    for row in eligible_rows:
        if row.get("passed") or row.get("status") == "pass":
            continue
        category = str(row.get("failure_category") or "unknown")
        failure_category_counts[category] = failure_category_counts.get(category, 0) + 1

    total_tasks = len(rows)
    eligible_tasks = len(eligible_rows)
    within_budget = sum(1 for row in eligible_rows if row.get("within_budget"))
    verifier_passes = sum(1 for row in eligible_rows if row.get("verifier_passed"))
    return {
        "total_tasks": total_tasks,
        "eligible_tasks": eligible_tasks,
        "skipped_tasks": len(skipped_rows),
        "passed": passed,
        "failed": failed,
        "pass_rate": (passed / eligible_tasks) if eligible_tasks else 0.0,
        "within_budget": within_budget,
        "verifier_passes": verifier_passes,
        "within_budget_rate": (within_budget / eligible_tasks) if eligible_tasks else 0.0,
        "verifier_pass_rate": (verifier_passes / eligible_tasks) if eligible_tasks else 0.0,
        "failure_category_counts": failure_category_counts,
    }


def summarize_review_rows(rows):
    review_rows = [
        row
        for row in rows
        if row.get("status") != "skipped" and row.get("review_expectation") in {"needs_fix", "pass"}
    ]
    if not review_rows:
        return None

    defect_rows = [row for row in review_rows if row["review_expectation"] == "needs_fix"]
    control_rows = [row for row in review_rows if row["review_expectation"] == "pass"]
    detected = sum(int(row.get("review_retries", 0)) > 0 for row in defect_rows)
    recovered = sum(bool(row.get("passed")) for row in defect_rows)
    controls_retained = sum(bool(row.get("passed")) for row in control_rows)
    false_rejections = sum(int(row.get("review_retries", 0)) > 0 for row in control_rows)
    false_negatives = len(defect_rows) - detected
    true_negatives = len(control_rows) - false_rejections
    precision = detected / (detected + false_rejections) if detected + false_rejections else None
    recall = detected / len(defect_rows) if defect_rows else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and precision + recall else None
    task_count = len(review_rows)
    return {
        "defect_cases": len(defect_rows),
        "defects_detected": detected,
        "defect_detection_rate": recall,
        "defects_recovered": recovered,
        "defect_recovery_rate": recovered / len(defect_rows) if defect_rows else 0.0,
        "control_cases": len(control_rows),
        "controls_retained": controls_retained,
        "control_retention_rate": controls_retained / len(control_rows) if control_rows else 0.0,
        "false_rejections": false_rejections,
        "false_rejection_rate": false_rejections / len(control_rows) if control_rows else 0.0,
        "review_confusion_matrix": {
            "true_positives": detected,
            "false_positives": false_rejections,
            "true_negatives": true_negatives,
            "false_negatives": false_negatives,
        },
        "review_precision": precision,
        "review_recall": recall,
        "review_f1": f1,
        "review_specificity": true_negatives / len(control_rows) if control_rows else 0.0,
        "average_tool_steps": sum(int(row.get("tool_steps", 0)) for row in review_rows) / task_count,
        "average_attempts": sum(int(row.get("attempts", 0)) for row in review_rows) / task_count,
        "average_review_calls": sum(int(row.get("review_calls", 0)) for row in review_rows)
        / task_count,
        "average_duration_ms": sum(int(row.get("duration_ms", 0)) for row in review_rows)
        / task_count,
    }


def _checkpoint_payload(
    checkpoint_id,
    current_goal,
    next_step,
    runtime_identity,
    *,
    schema_version=BENCHMARK_SCHEMA_VERSION,
    current_blocker="",
    key_files=None,
    freshness=None,
    summary="",
):
    return {
        "checkpoint_id": checkpoint_id,
        "parent_checkpoint_id": "",
        "schema_version": "phase1-v1" if schema_version == BENCHMARK_SCHEMA_VERSION else str(schema_version),
        "created_at": "2026-04-15T08:00:00+00:00",
        "current_goal": current_goal,
        "completed": [],
        "excluded": [],
        "current_blocker": current_blocker,
        "next_step": next_step,
        "key_files": list(key_files or []),
        "freshness": dict(freshness or {}),
        "summary": summary or current_goal,
        "runtime_identity": dict(runtime_identity),
    }


def _apply_task_setup(agent, task, fixture_copy_root):
    setup = dict(task.get("setup", {}) or {})
    if not setup:
        return

    kind = str(setup.get("kind", "")).strip()
    if kind == "context_reduction":
        history_count = int(setup.get("history_count", 12))
        note_count = int(setup.get("note_count", 6))
        for index in range(history_count):
            agent.record(
                {
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": f"benchmark-history-{index}-" + ("A" * 220),
                    "created_at": f"2026-04-15T09:{index:02d}:00+00:00",
                }
            )
        for index in range(note_count):
            agent.memory.append_note(
                f"benchmark-note-{index}-" + ("B" * 180),
                tags=("recall",),
                created_at=f"2026-04-15T10:{index:02d}:00+00:00",
            )
        agent.session["memory"] = agent.memory.to_dict()
        agent.context_manager.total_budget = int(setup.get("total_budget", 900))
        agent.context_manager.section_budgets = dict(
            setup.get(
                "section_budgets",
                {"prefix": 120, "memory": 120, "relevant_memory": 120, "history": 160},
            )
        )
        return

    if kind == "freshness_mismatch":
        path = str(setup.get("path", "sample.txt"))
        summary_text = str(setup.get("summary", f"{path}: stale benchmark summary"))
        agent.memory.set_file_summary(path, summary_text)
        agent.memory.remember_file(path)
        freshness = agent.memory.to_dict()["file_summaries"][path]["freshness"]
        agent.session["memory"] = agent.memory.to_dict()
        agent.session["checkpoints"] = {
            "current_id": "ckpt_freshness",
            "items": {
                "ckpt_freshness": _checkpoint_payload(
                    "ckpt_freshness",
                    current_goal="Re-anchor stale benchmark file state",
                    next_step=f"Re-read {path}",
                    runtime_identity={"workspace_fingerprint": agent.workspace.fingerprint()},
                    key_files=[{"path": path, "freshness": freshness}],
                    freshness={path: freshness},
                    summary="stale benchmark checkpoint",
                )
            },
        }
        agent.session_store.save(agent.session)
        (fixture_copy_root / path).write_text(str(setup.get("mutated_text", "alpha\nbeta\nstale-updated\nplaceholder\n")), encoding="utf-8")
        return

    if kind == "workspace_mismatch":
        agent.session["checkpoints"] = {
            "current_id": "ckpt_workspace",
            "items": {
                "ckpt_workspace": _checkpoint_payload(
                    "ckpt_workspace",
                    current_goal="Recover after benchmark workspace drift",
                    next_step="Rebuild runtime state from a fresh checkpoint",
                    runtime_identity={"workspace_fingerprint": "outdated-benchmark-fingerprint"},
                    summary="workspace drift benchmark checkpoint",
                )
            },
        }
        agent.session_store.save(agent.session)
        return


def _event_metrics(events):
    events = list(events or [])
    delegate_started = [event for event in events if event.get("event") == "delegate_started"]
    review_requested = [event for event in events if event.get("event") == "review_requested"]
    completed_reviews = [
        event for event in events if event.get("event") in {"review_passed", "review_failed"}
    ]
    review_passed = None
    if completed_reviews:
        review_passed = completed_reviews[-1].get("event") == "review_passed"
    return {
        "delegate_calls": len(delegate_started) + len(review_requested),
        "delegate_failures": sum(event.get("event") == "delegate_failed" for event in events),
        "research_calls": sum(event.get("agent_role") == "research" for event in delegate_started),
        "review_calls": len(review_requested),
        "review_passed": review_passed,
        "review_retries": sum(event.get("event") == "review_retry_started" for event in events),
    }


def _aggregate_states(task_state, child_task_states):
    states = [task_state, *list(child_task_states or [])]
    return {
        "tool_steps": sum(state.tool_steps for state in states),
        "attempts": sum(state.attempts for state in states),
        "sandbox_violations": sum(state.sandbox_violations for state in states),
        "malformed_output_recovered": sum(state.malformed_output_recovered for state in states),
        "affected_paths": sorted({path for state in states for path in state.affected_paths}),
    }


class BenchmarkEvaluator:
    def __init__(
        self,
        benchmark_path=DEFAULT_BENCHMARK_PATH,
        artifact_path=DEFAULT_ARTIFACT_PATH,
        workspace_root=None,
        model_name=DEFAULT_MODEL_NAME,
        model_version=DEFAULT_MODEL_VERSION,
        temperature=DEFAULT_TEMPERATURE,
        top_p=DEFAULT_TOP_P,
        max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
        timezone_name=DEFAULT_TIMEZONE,
        model_client_factory=None,
        backend="native",
        event_sink_factory=None,
        backend_runner=None,
        verifier_runner=None,
    ):
        self.benchmark_path = Path(benchmark_path)
        self.artifact_path = Path(artifact_path)
        self.workspace_root = Path(workspace_root) if workspace_root is not None else Path(
            tempfile.mkdtemp(prefix="pico-benchmark-")
        )
        self.model_name = model_name
        self.model_version = model_version
        self.temperature = temperature
        self.top_p = top_p
        self.max_new_tokens = max_new_tokens
        self.timezone_name = timezone_name
        self.model_client_factory = model_client_factory
        self.backend = str(backend)
        if self.backend not in {"native", "langgraph"}:
            raise ValueError(f"unknown backend: {self.backend}")
        self.event_sink_factory = event_sink_factory
        self.backend_runner = backend_runner
        self.verifier_runner = verifier_runner or VerifierRunner()
        self.repo_root = self.benchmark_path.resolve().parent.parent

    def load(self):
        return load_benchmark(self.benchmark_path, repo_root=self.repo_root)

    def run(self):
        benchmark = self.load()
        rows = []
        for task in benchmark["tasks"]:
            try:
                rows.append(self.run_task(task))
            except Exception as exc:
                rows.append(self._harness_failure_row(task, type(exc).__name__))
        summary = summarize_rows(rows)
        review_summary = summarize_review_rows(rows)
        artifact = {
            "schema_version": EVALUATION_ARTIFACT_SCHEMA_VERSION,
            "captured_at": _now_in_timezone(self.timezone_name),
            "runtime": {
                "commit_sha": _git_value(["rev-parse", "HEAD"], cwd=self.repo_root),
                "branch": _git_value(["branch", "--show-current"], cwd=self.repo_root),
            },
            "benchmark": {
                "schema_version": benchmark["schema_version"],
                "source": str(self.benchmark_path.resolve().relative_to(self.repo_root)),
                "task_count": len(benchmark["tasks"]),
            },
            "backend": self.backend,
            "reproducibility": {
                "fixture_snapshot_id": _fixture_snapshot_id(
                    self.repo_root / str(task["fixture_repo"]) for task in benchmark["tasks"]
                ),
                "model_name": self.model_name,
                "model_version": self.model_version,
                "decoding": {
                    "temperature": self.temperature,
                    "top_p": self.top_p,
                    "max_new_tokens": self.max_new_tokens,
                },
                "timezone": self.timezone_name,
                "locale": _current_locale(),
            },
            "summary": summary,
            "failure_category_counts": summary["failure_category_counts"],
            "rows": rows,
        }
        if review_summary is not None:
            artifact["review_summary"] = review_summary
        self._write_artifact(artifact)
        return artifact

    def run_task(self, task):
        task = dict(task)
        if self.backend not in task.get("backends", ["native"]):
            return self._skipped_row(task)

        started_at = time.monotonic()
        task["id"] = _normalized_task_id(task["id"])
        task["fixture_repo"], fixture_source = _normalized_fixture_repo(
            task["fixture_repo"],
            self.repo_root,
            task["id"],
        )
        if "setup" in task:
            task["setup"] = _normalized_setup(
                task["setup"],
                fixture_source,
                task["id"],
            )
        fixture_copy_root = self.workspace_root / task["id"] / fixture_source.name
        try:
            fixture_copy_root.resolve().relative_to(fixture_source)
        except ValueError:
            pass
        else:
            raise ValueError("benchmark workspace_root must not be inside fixture_repo")
        if fixture_copy_root.exists():
            shutil.rmtree(fixture_copy_root)
        fixture_copy_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(fixture_source, fixture_copy_root)

        workspace = WorkspaceContext.build(
            fixture_copy_root,
            repo_root_override=fixture_copy_root,
        )
        session_store = SessionStore(fixture_copy_root / ".pico" / "sessions")
        run_store = RunStore(fixture_copy_root / ".pico" / "runs")
        if self.model_client_factory is not None:
            model_client = self.model_client_factory(task=task, workspace=workspace)
        else:
            model_client = FakeModelClient(_scripted_outputs_for_task(task, self.backend))
        runner = self.backend_runner or build_backend_runner(
            self.backend,
            max_new_tokens=self.max_new_tokens,
            event_sink_factory=self.event_sink_factory,
        )
        backend_result = runner.run_task(
            task,
            workspace,
            session_store,
            run_store,
            fixture_copy_root,
            model_client=model_client,
        )
        duration_ms = int((time.monotonic() - started_at) * 1000)
        agent = backend_result.agent
        task_state = backend_result.task_state
        run_dir = Path(agent.current_run_dir)
        task_state_path = agent.run_store.task_state_path(task_state)
        report_path = agent.run_store.report_path(task_state)
        try:
            report = agent.run_store.load_report(task_state.run_id)
        except Exception:
            report = {}

        artifact_path = _artifact_path_for_task(task)
        artifact_file = fixture_copy_root / artifact_path
        expected_artifact_exists = artifact_file.is_file()
        artifact_digest = _digest_file(artifact_file) if expected_artifact_exists else ""

        verifier = self.verifier_runner.run(task, fixture_copy_root)

        coordinator_tool_steps = sum(state.tool_steps for state in backend_result.budget_task_states)
        within_budget = coordinator_tool_steps <= int(task["step_budget"])
        verifier_passed = verifier.passed
        non_failure_stop_reason = task_state.stop_reason == STOP_REASON_FINAL_ANSWER_RETURNED
        passed = within_budget and verifier_passed and expected_artifact_exists and non_failure_stop_reason
        failure_category = None if passed else self._failure_category(
            stop_reason=task_state.stop_reason,
            within_budget=within_budget,
            verifier_passed=verifier_passed,
            verifier_failure_category=verifier.failure_category,
            expected_artifact_exists=expected_artifact_exists,
            non_failure_stop_reason=non_failure_stop_reason,
        )

        aggregate = _aggregate_states(task_state, backend_result.child_task_states)
        events = list(backend_result.events)
        event_metrics = _event_metrics(events)
        initial_state = backend_result.initial_state
        run_metadata = dict(backend_result.run_metadata or {})

        row = {
            "id": task["id"],
            "backend": self.backend,
            "prompt": task["prompt"],
            "fixture_repo": task["fixture_repo"],
            "fixture_copy_relpath": _workspace_relative(fixture_copy_root, self.workspace_root),
            "run_id": task_state.run_id,
            "run_dir_relpath": _workspace_relative(run_dir, self.workspace_root),
            "task_state_relpath": _workspace_relative(task_state_path, self.workspace_root),
            "report_relpath": _workspace_relative(report_path, self.workspace_root),
            "allowed_tools": list(task["allowed_tools"]),
            "step_budget": int(task["step_budget"]),
            "expected_artifact": task["expected_artifact"],
            "artifact_path": artifact_path,
            "artifact_exists": expected_artifact_exists,
            "artifact_digest": artifact_digest,
            "verifier": task.get("verifier"),
            "verifier_argv": list(verifier.argv),
            "verifier_exit_code": verifier.exit_code,
            "verifier_stdout": verifier.stdout,
            "verifier_stderr": verifier.stderr,
            "category": task["category"],
            "review_expectation": str(task.get("review_expectation", "")),
            "defect_type": str(task.get("defect_type", "")),
            "status": "pass" if passed else "fail",
            "passed": passed,
            "failure_category": failure_category,
            "within_budget": within_budget,
            "verifier_passed": verifier_passed,
            "expected_artifact_exists": expected_artifact_exists,
            "non_failure_stop_reason": non_failure_stop_reason,
            "tool_steps": aggregate["tool_steps"],
            "attempts": aggregate["attempts"],
            "sandbox_violations": aggregate["sandbox_violations"],
            "malformed_output_recovered": aggregate["malformed_output_recovered"],
            "affected_paths": aggregate["affected_paths"],
            "duration_ms": duration_ms,
            "final_answer": backend_result.final_answer,
            "stop_reason": task_state.stop_reason,
            "initial_history_empty": initial_state.get("initial_history_empty"),
            "initial_memory_empty": initial_state.get("initial_memory_empty"),
            "initial_task_summary_empty": initial_state.get("initial_task_summary_empty"),
            "initial_episodic_notes_empty": initial_state.get("initial_episodic_notes_empty"),
            "requested_task_mode": run_metadata.get("requested_task_mode", ""),
            "resolved_intent": run_metadata.get("resolved_intent", ""),
            "intent_source": run_metadata.get("intent_source", ""),
            "intent_attempts": int(run_metadata.get("intent_attempts", 0)),
            "answer_attempts": int(run_metadata.get("answer_attempts", 0)),
            "execution_started": True,
            "task_state": task_state.to_dict(),
            "child_task_states": [state.to_dict() for state in backend_result.child_task_states],
            "budget_task_states": [state.to_dict() for state in backend_result.budget_task_states],
            "events": events,
            "report": report,
        }
        row.update(event_metrics)
        return row

    def _failure_category(
        self,
        stop_reason,
        within_budget,
        verifier_passed,
        verifier_failure_category,
        expected_artifact_exists,
        non_failure_stop_reason,
    ):
        if stop_reason in {"model_error", "runtime_error", "persistence_error", "delegate_failed"}:
            return stop_reason
        if stop_reason in {
            "budget_exhausted",
            "no_changes_to_review",
            "review_retry_limit_reached",
        }:
            return stop_reason
        if not expected_artifact_exists:
            return "missing_artifact"
        if not within_budget:
            return "budget_exceeded"
        if not verifier_passed:
            return verifier_failure_category or "verifier_failed"
        if not non_failure_stop_reason:
            return "failure_stop_reason"
        return "unknown"

    def _empty_row(self, task, *, status, failure_category):
        return {
            "id": task["id"],
            "backend": self.backend,
            "prompt": task["prompt"],
            "fixture_repo": task["fixture_repo"],
            "fixture_copy_relpath": "",
            "run_id": "",
            "run_dir_relpath": "",
            "task_state_relpath": "",
            "report_relpath": "",
            "allowed_tools": list(task["allowed_tools"]),
            "step_budget": int(task["step_budget"]),
            "expected_artifact": task["expected_artifact"],
            "artifact_path": str(task.get("artifact_path", "")),
            "artifact_exists": None,
            "artifact_digest": "",
            "verifier": task.get("verifier"),
            "verifier_argv": list(task.get("verifier_argv", [])),
            "verifier_exit_code": None,
            "verifier_stdout": "",
            "verifier_stderr": "",
            "category": task["category"],
            "review_expectation": str(task.get("review_expectation", "")),
            "defect_type": str(task.get("defect_type", "")),
            "status": status,
            "passed": None if status == "skipped" else False,
            "failure_category": failure_category,
            "within_budget": None,
            "verifier_passed": None,
            "expected_artifact_exists": None,
            "non_failure_stop_reason": None,
            "tool_steps": 0,
            "attempts": 0,
            "sandbox_violations": 0,
            "malformed_output_recovered": 0,
            "affected_paths": [],
            "duration_ms": 0,
            "final_answer": "",
            "stop_reason": "",
            "initial_history_empty": None,
            "initial_memory_empty": None,
            "initial_task_summary_empty": None,
            "initial_episodic_notes_empty": None,
            "execution_started": False,
            "task_state": {},
            "child_task_states": [],
            "budget_task_states": [],
            "events": [],
            "report": {},
            "delegate_calls": 0,
            "delegate_failures": 0,
            "research_calls": 0,
            "review_calls": 0,
            "review_passed": None,
            "review_retries": 0,
            "requested_task_mode": "",
            "resolved_intent": "",
            "intent_source": "",
            "intent_attempts": 0,
            "answer_attempts": 0,
        }

    def _skipped_row(self, task):
        return self._empty_row(task, status="skipped", failure_category="backend_not_applicable")

    def _harness_failure_row(self, task, error_type):
        row = self._empty_row(task, status="fail", failure_category="harness_error")
        row["final_answer"] = f"harness setup failed: {error_type}"
        return row

    def _write_artifact(self, artifact):
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _digest_file(path):
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_fixed_benchmark(
    benchmark_path=DEFAULT_BENCHMARK_PATH,
    artifact_path=DEFAULT_ARTIFACT_PATH,
    workspace_root=None,
    model_name=DEFAULT_MODEL_NAME,
    model_version=DEFAULT_MODEL_VERSION,
    temperature=DEFAULT_TEMPERATURE,
    top_p=DEFAULT_TOP_P,
    max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
    timezone_name=DEFAULT_TIMEZONE,
    model_client_factory=None,
    backend="native",
    event_sink_factory=None,
    backend_runner=None,
    verifier_runner=None,
):
    evaluator = BenchmarkEvaluator(
        benchmark_path=benchmark_path,
        artifact_path=artifact_path,
        workspace_root=workspace_root,
        model_name=model_name,
        model_version=model_version,
        temperature=temperature,
        top_p=top_p,
        max_new_tokens=max_new_tokens,
        timezone_name=timezone_name,
        model_client_factory=model_client_factory,
        backend=backend,
        event_sink_factory=event_sink_factory,
        backend_runner=backend_runner,
        verifier_runner=verifier_runner,
    )
    return evaluator.run()


def run_harness_regression_v2(
    benchmark_path=DEFAULT_BENCHMARK_PATH,
    artifact_path=DEFAULT_HARNESS_REGRESSION_V2_ARTIFACT_PATH,
    workspace_root=None,
    model_name=DEFAULT_MODEL_NAME,
    model_version=DEFAULT_MODEL_VERSION,
    temperature=DEFAULT_TEMPERATURE,
    top_p=DEFAULT_TOP_P,
    max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
    timezone_name=DEFAULT_TIMEZONE,
    model_client_factory=None,
    backend="native",
    event_sink_factory=None,
):
    return run_fixed_benchmark(
        benchmark_path=benchmark_path,
        artifact_path=artifact_path,
        workspace_root=workspace_root,
        model_name=model_name,
        model_version=model_version,
        temperature=temperature,
        top_p=top_p,
        max_new_tokens=max_new_tokens,
        timezone_name=timezone_name,
        model_client_factory=model_client_factory,
        backend=backend,
        event_sink_factory=event_sink_factory,
    )


def normalize_evaluation_artifact(payload):
    if not isinstance(payload, dict):
        raise ValueError("evaluation artifact must be a mapping")
    version = int(payload.get("schema_version", 0))
    if version not in {1, EVALUATION_ARTIFACT_SCHEMA_VERSION}:
        raise ValueError(f"unsupported evaluation artifact schema_version: {version}")

    normalized = dict(payload)
    upgraded_from_v1 = version == 1
    if upgraded_from_v1:
        normalized["schema_version"] = EVALUATION_ARTIFACT_SCHEMA_VERSION
        normalized.setdefault("backend", "native")
        benchmark = dict(normalized.get("benchmark", {}))
        benchmark.setdefault("schema_version", BENCHMARK_SCHEMA_VERSION)
        normalized["benchmark"] = benchmark

    rows = [dict(row) for row in normalized.get("rows", [])]
    for row in rows:
        row.setdefault("backend", "native")
        row.setdefault("execution_started", bool(row.get("run_id")))
        row.setdefault("events", [])
        row.setdefault("child_task_states", [])
        row.setdefault("budget_task_states", [row.get("task_state", {})] if row.get("task_state") else [])
        row.setdefault("requested_task_mode", "")
        row.setdefault("resolved_intent", "")
        row.setdefault("intent_source", "")
        row.setdefault("intent_attempts", 0)
        row.setdefault("answer_attempts", 0)
    normalized["rows"] = rows
    if upgraded_from_v1 or "summary" not in normalized:
        normalized["summary"] = summarize_rows(rows)
    normalized.setdefault(
        "failure_category_counts",
        normalized["summary"]["failure_category_counts"],
    )
    return normalized


def load_evaluation_artifact(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return normalize_evaluation_artifact(payload)
