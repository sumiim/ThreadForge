import sys

from pico.evaluation.verifier import VerifierRunner, verifier_argv


def test_legacy_python_verifier_is_normalized():
    argv = verifier_argv({"verifier": "python -c \"print('ok')\""})

    assert argv[0] == sys.executable
    assert argv[1] == "-c"


def test_verifier_requires_exactly_one_definition():
    for task in ({}, {"verifier": "python -V", "verifier_argv": ["python", "-V"]}):
        try:
            verifier_argv(task)
        except ValueError as exc:
            assert "exactly one" in str(exc)
        else:
            raise AssertionError("invalid verifier definition was accepted")


def test_verifier_runs_without_shell_and_redacts_output(monkeypatch, tmp_path):
    monkeypatch.setenv("PICO_TEST_TOKEN", "secret-value")
    result = VerifierRunner(secret_env_names=("PICO_TEST_TOKEN",)).run(
        {
            "verifier_argv": [
                sys.executable,
                "-c",
                "import os; print(os.environ['PICO_TEST_TOKEN'])",
            ]
        },
        tmp_path,
    )

    assert result.passed is True
    assert result.stdout.strip() == "<redacted>"


def test_verifier_timeout_has_stable_category(tmp_path):
    result = VerifierRunner().run(
        {
            "verifier_argv": [sys.executable, "-c", "import time; time.sleep(2)"],
            "verifier_timeout_s": 1,
        },
        tmp_path,
    )

    assert result.passed is False
    assert result.failure_category == "verifier_timeout"
    assert result.exit_code is None


def test_malformed_legacy_verifier_is_a_stable_verifier_error(tmp_path):
    result = VerifierRunner().run({"verifier": "python -c \"unterminated"}, tmp_path)

    assert result.passed is False
    assert result.failure_category == "verifier_error"
    assert result.argv == ()
    assert result.stderr == "verifier failed: ValueError"
