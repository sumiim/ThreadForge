"""Stable domain error contract: code / message / details / http status."""

from __future__ import annotations


class AppError(Exception):
    http_status = 500
    code = "internal_error"

    def __init__(self, message="", details=None):
        super().__init__(message or "")
        self.message = message or self.code
        self.details = details or {}

    def to_error_dict(self, request_id: str) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "request_id": request_id,
            }
        }


class NotFoundError(AppError):
    http_status = 404
    code = "not_found"


class SessionNotFoundError(NotFoundError):
    code = "session_not_found"


class TaskNotFoundError(NotFoundError):
    code = "task_not_found"


class ApprovalNotFoundError(NotFoundError):
    code = "approval_not_found"


class RunNotFoundError(NotFoundError):
    code = "run_not_found"


class SessionCorruptedError(AppError):
    http_status = 500
    code = "session_corrupted"


class ActiveTaskExistsError(AppError):
    http_status = 409
    code = "active_task_exists"

    def __init__(self, task_id: str):
        super().__init__("another root task is active", {"task_id": task_id})


class TaskTerminalError(AppError):
    http_status = 409
    code = "task_terminal"

    def __init__(self, task_id: str):
        super().__init__("task has already reached a terminal state", {"task_id": task_id})


class RenameConflictError(AppError):
    http_status = 409
    code = "rename_conflict"


class ApprovalAlreadyResolvedError(AppError):
    http_status = 409
    code = "approval_already_resolved"


class ApprovalStaleError(AppError):
    http_status = 409
    code = "approval_stale"


class ApprovalExpiredError(AppError):
    http_status = 409
    code = "approval_expired"


class TaskRunnerUnavailableError(AppError):
    http_status = 503
    code = "task_runner_unavailable"


class PersistenceUnavailableError(AppError):
    http_status = 503
    code = "persistence_unavailable"


class ModelNotConfiguredError(AppError):
    http_status = 503
    code = "model_not_configured"


class ArtifactTooLargeError(AppError):
    http_status = 413
    code = "artifact_too_large"


class InputTooLongError(AppError):
    http_status = 422
    code = "input_too_long"

    def __init__(self, max_chars: int):
        super().__init__("input exceeds maximum length", {"max_chars": max_chars})


class NotReadyError(AppError):
    http_status = 503
    code = "not_ready"


class AuthenticationRequiredError(AppError):
    http_status = 401
    code = "authentication_required"


class AuthorizationDeniedError(AppError):
    http_status = 403
    code = "authorization_denied"


class OAuthStateInvalidError(AppError):
    http_status = 400
    code = "oauth_state_invalid"


class OAuthProviderError(AppError):
    http_status = 502
    code = "oauth_provider_error"


class DeviceNotFoundError(NotFoundError):
    code = "device_not_found"


class PairingCodeInvalidError(AppError):
    http_status = 400
    code = "pairing_code_invalid"


class WorkerOfflineError(AppError):
    http_status = 409
    code = "worker_offline"


class WorkerCapabilityUnavailableError(AppError):
    http_status = 409
    code = "worker_capability_unavailable"


class WorkerCommandPendingError(AppError):
    http_status = 409
    code = "worker_command_pending"


class WorkerCommandFailedError(AppError):
    http_status = 422
    code = "worker_command_failed"


class WorkerConcurrencyLimitError(AppError):
    http_status = 409
    code = "worker_concurrency_limit"

    def __init__(self, device_id: str, limit: int):
        super().__init__("Worker concurrency limit reached", {"device_id": device_id, "limit": limit})


class WorkerProtocolError(AppError):
    http_status = 400
    code = "worker_protocol_error"


class WorkerReleaseUnavailableError(AppError):
    http_status = 503
    code = "worker_release_unavailable"
