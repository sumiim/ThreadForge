"""Worker installer packaging contract tests."""

from pathlib import Path


def test_installer_starts_service_directly_without_shell_execute() -> None:
    installer = Path(__file__).resolve().parents[2] / "scripts" / "worker-installer.nsi"
    source = installer.read_text(encoding="utf-8")

    assert 'Exec \'"$INSTDIR\\threadforge-worker-service.exe" service\'' in source
    assert 'ExecShell "open" "$INSTDIR\\threadforge-worker-service.exe" "service"' not in source
