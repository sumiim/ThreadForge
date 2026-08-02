"""Safe verifier process execution for trusted benchmark definitions."""

from dataclasses import dataclass
import shlex
import subprocess
import sys

from ..security import redact_text
from ..workspace import clip

DEFAULT_VERIFIER_TIMEOUT_S = 10
MIN_VERIFIER_TIMEOUT_S = 1
MAX_VERIFIER_TIMEOUT_S = 60


@dataclass(frozen=True)
class VerifierResult:
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    passed: bool
    failure_category: str | None = None


def verifier_argv(task):
    has_argv = "verifier_argv" in task
    has_legacy = "verifier" in task
    if has_argv == has_legacy:
        raise ValueError("task must provide exactly one of verifier_argv or verifier")
    if has_argv:
        value = task["verifier_argv"]
        if not isinstance(value, list) or not value:
            raise ValueError("verifier_argv must be a non-empty list")
        argv = [str(item) for item in value]
    else:
        value = str(task["verifier"]).strip()
        if not value:
            raise ValueError("verifier must not be empty")
        argv = shlex.split(value)
    if not argv or any(not item for item in argv):
        raise ValueError("verifier argv must contain non-empty strings")
    if argv[0].lower() in {"python", "python3", "python.exe", "python3.exe"}:
        argv[0] = sys.executable
    return tuple(argv)


class VerifierRunner:
    def __init__(self, secret_env_names=None, output_limit=4000):
        self.secret_env_names = tuple(secret_env_names or ())
        self.output_limit = int(output_limit)

    def run(self, task, cwd):
        argv = ()
        try:
            argv = verifier_argv(task)
            timeout_s = int(task.get("verifier_timeout_s", DEFAULT_VERIFIER_TIMEOUT_S))
            timeout_s = max(MIN_VERIFIER_TIMEOUT_S, min(MAX_VERIFIER_TIMEOUT_S, timeout_s))
            completed = subprocess.run(
                list(argv),
                cwd=cwd,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            return VerifierResult(
                argv=argv,
                exit_code=None,
                stdout=self._clean(exc.stdout),
                stderr=self._clean(exc.stderr),
                passed=False,
                failure_category="verifier_timeout",
            )
        except Exception as exc:
            return VerifierResult(
                argv=argv,
                exit_code=None,
                stdout="",
                stderr=f"verifier failed: {type(exc).__name__}",
                passed=False,
                failure_category="verifier_error",
            )
        return VerifierResult(
            argv=argv,
            exit_code=completed.returncode,
            stdout=self._clean(completed.stdout),
            stderr=self._clean(completed.stderr),
            passed=completed.returncode == 0,
            failure_category=None if completed.returncode == 0 else "verifier_failed",
        )

    def _clean(self, value):
        if isinstance(value, bytes):
            value = value.decode(errors="replace")
        return redact_text(
            clip(value or "", self.output_limit),
            secret_env_names=self.secret_env_names,
        )
