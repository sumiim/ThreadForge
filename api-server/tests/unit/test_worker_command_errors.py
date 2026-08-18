"""Worker 设备级命令(update/uninstall)被拒时的稳定错误码映射。"""

from threadforge_api.domain.errors import (
    UninstallUnavailableError,
    UpdateBackoffError,
    UpdateUnavailableError,
    WorkerBusyError,
    WorkerCommandFailedError,
)
from threadforge_api.infrastructure.worker_hub import _worker_command_error


def test_update_rejection_maps_stable_codes():
    # Worker 回传的稳定原因 → 明确错误码（前端据此显示可执行文案）。
    assert isinstance(_worker_command_error("worker_busy", command="update"), WorkerBusyError)
    assert isinstance(_worker_command_error("update_unavailable", command="update"), UpdateUnavailableError)
    assert isinstance(_worker_command_error("update_backoff", command="update"), UpdateBackoffError)
    # 未知原因仍落通用失败（带 details.reason 供排查）。
    fallback = _worker_command_error("update_quota_weird", command="update")
    assert isinstance(fallback, WorkerCommandFailedError)


def test_uninstall_rejection_maps_stable_codes():
    assert isinstance(_worker_command_error("worker_busy", command="uninstall"), WorkerBusyError)
    assert isinstance(
        _worker_command_error("uninstall_unavailable", command="uninstall"),
        UninstallUnavailableError,
    )
    assert isinstance(
        _worker_command_error("", command="uninstall"),
        WorkerCommandFailedError,
    )


def test_mapped_codes_carry_frontend_contract():
    assert WorkerBusyError().code == "worker_busy"
    assert UpdateUnavailableError().code == "update_unavailable"
    assert UpdateBackoffError().code == "update_backoff"
    assert UninstallUnavailableError().code == "uninstall_unavailable"
