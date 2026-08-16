"""Application container built inside the ASGI lifespan."""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pico.providers.clients import (
    AnthropicCompatibleModelClient,
    OpenAICompatibleModelClient,
    OpenAICompletionsModelClient,
)
from pico.security import redact_artifact
from pico.session_store import SessionStore

from .application.artifact_service import ArtifactService
from .application.provider_service import ProviderService
from .application.session_service import SessionService
from .application.task_service import TaskService
from .config import Settings
from .domain.enums import ApprovalStatus, TaskStatus
from .domain.errors import ApprovalNotFoundError
from .domain.identity import canonical_owner_id
from .infrastructure.approval_gate import ApprovalGate
from .infrastructure.auth import AuthManager, OAuthClient
from .infrastructure.device_store import DeviceStore, PairingCodeStore
from .infrastructure.event_broker import EventBroker
from .infrastructure.event_publisher import EventPublisher
from .infrastructure.jsonutil import secure_directory
from .infrastructure.owner_store import resolve_instance_owner
from .infrastructure.recovery_journal import RecoveryJournal
from .infrastructure.run_reconciliation import (
    converge_run_artifacts,
    repair_terminal_run_artifacts,
    run_artifacts_match,
    terminal_task_from_run,
)
from .infrastructure.run_store_reader import RunStoreReader
from .infrastructure.sqlite_repositories import (
    ControlPlaneMigrator,
    SqliteApprovalRepository,
    SqliteLeaseRepository,
    SqliteProviderRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)
from .infrastructure.sqlite_store import SqliteStore
from .infrastructure.task_runner import TaskRunner
from .infrastructure.worker_hub import WorkerHub
from .infrastructure.worker_releases import WorkerReleaseService
from .infrastructure.workspace_catalog import WorkspaceCatalog
from .infrastructure.workspace_isolation import WorkspaceIsolation


class AppContainer:
    def __init__(
        self,
        settings: Settings,
        *,
        model_client_factory: Callable | None = None,
        oauth_client: OAuthClient | None = None,
    ):
        self.settings = settings
        self.workspace_catalog = WorkspaceCatalog(settings.workspaces_file, settings.data_dir)
        # Workspace/data disjointness is validated before this creates data_dir.
        secure_directory(settings.data_dir)
        self.owner_id = resolve_instance_owner(settings.data_dir, settings.instance_owner_id)
        self.auth_manager = (
            AuthManager(settings, self.owner_id, oauth_client)
            if settings.identity_mode == "github_oauth"
            else None
        )
        self.session_store = SessionStore(settings.data_dir / "sessions")
        self.control_store = SqliteStore(settings.data_dir / "control.sqlite3")
        self.task_repo = SqliteTaskRepository(self.control_store, json_root=settings.data_dir / "tasks")
        self.approval_repo = SqliteApprovalRepository(self.control_store, json_root=settings.data_dir / "approvals")
        self.run_repo = SqliteRunRepository(self.control_store)
        self.lease_repo = SqliteLeaseRepository(self.control_store)
        self.provider_repo = SqliteProviderRepository(
            self.control_store, json_root=settings.data_dir / "providers"
        )
        self.migrator = ControlPlaneMigrator(self.task_repo, self.approval_repo)
        self.isolation = WorkspaceIsolation(
            settings.data_dir, self.lease_repo, self.workspace_catalog
        )
        self.device_store = DeviceStore(settings.data_dir / "devices")
        self.pairing_store = PairingCodeStore(settings.worker_pairing_ttl_seconds)
        self.worker_release_service = WorkerReleaseService(
            settings.worker_release_dir, settings.worker_release_max_bytes
        )
        self._assign_legacy_ownership()
        self.runs_dir = settings.data_dir / "runs"
        secure_directory(self.runs_dir)
        self.run_store_reader = RunStoreReader(settings.data_dir, settings.artifact_max_bytes)
        self._loop = asyncio.get_running_loop()
        self.broker = EventBroker(self._loop, queue_size=settings.sse_queue_size)
        self.publisher = EventPublisher(self.broker)
        self.worker_hub = WorkerHub(
            loop=self._loop,
            settings=settings,
            device_store=self.device_store,
            task_repo=self.task_repo,
            approval_repo=self.approval_repo,
            session_store=self.session_store,
            publisher=self.publisher,
        )
        self.recovery_journal = RecoveryJournal(settings.data_dir / "recovery.jsonl")
        self.approval_gate = ApprovalGate(
            approval_repo=self.approval_repo,
            task_repo=self.task_repo,
            publisher=self.publisher,
            hmac_key=os.urandom(32),
            timeout_seconds=settings.approval_timeout_seconds,
            preview_max_chars=settings.approval_preview_max_chars,
            redact_artifact=redact_artifact,
            recovery_journal=self.recovery_journal,
        )
        self.runner = TaskRunner(
            settings=settings,
            workspace_catalog=self.workspace_catalog,
            session_store=self.session_store,
            task_repo=self.task_repo,
            approval_gate=self.approval_gate,
            broker=self.broker,
            publisher=self.publisher,
            model_client_factory=model_client_factory or self._default_model_client_factory,
            isolation=self.isolation,
        )
        self.approval_gate.set_degraded_callback(self.runner.mark_degraded)
        self.session_service = SessionService(
            self.session_store,
            self.workspace_catalog,
            self.task_repo,
            approval_repo=self.approval_repo,
            runs_root=self.runs_dir,
            device_store=self.device_store,
            worker_hub=self.worker_hub,
            allow_backend_workspaces=settings.identity_mode != "github_oauth",
        )
        self.task_service = TaskService(
            settings=settings,
            session_service=self.session_service,
            task_repo=self.task_repo,
            approval_repo=self.approval_repo,
            runner=self.runner,
            publisher=self.publisher,
            run_store_reader=self.run_store_reader,
            worker_hub=self.worker_hub,
            device_store=self.device_store,
        )
        self.artifact_service = ArtifactService(self.run_store_reader, self.task_repo)
        self.provider_service = ProviderService(self.provider_repo)
        self.ready = False

    def _assign_legacy_ownership(self) -> None:
        """Claim records created by the pre-ownership V1 instance."""
        for session_id in self.session_store.list_ids():
            try:
                session = self.session_store.load(session_id)
            except Exception:
                # Reconciliation owns corruption handling and will keep the
                # service not-ready without preventing diagnostic startup.
                continue
            if session.get("owner_id"):
                continue
            session["owner_id"] = self.owner_id
            self.session_store.save(session)
        self.task_repo.assign_legacy_owner(self.owner_id)
        self.approval_repo.assign_legacy_owner(self.owner_id)

    def is_ready(self) -> bool:
        if not self.ready or self.runner.is_degraded():
            return False
        try:
            self.workspace_catalog.validate_all()
            self._validate_sessions()
            self._validate_control_repositories()
            self.recovery_journal.incomplete()
            for directory in {
                self.settings.data_dir,
                self.session_store.root,
                self.task_repo.root,
                self.approval_repo.root,
                self.device_store.root,
                self.runs_dir,
            }:
                with tempfile.NamedTemporaryFile(dir=directory, prefix=".ready-", delete=True):
                    pass
        except Exception:
            return False
        return True

    def _validate_sessions(self) -> None:
        for session_id in self.session_store.list_ids():
            session = self.session_store.load(session_id)
            canonical_owner_id(session["owner_id"])

    def _validate_control_repositories(self) -> None:
        # JSON compatibility mirror is the cross-check source: corruption must
        # fail readiness even though SQLite is the authoritative query store.
        if self.task_repo.mirror is not None:
            for record_id in self.task_repo.mirror.ids():
                self.task_repo.mirror.read(record_id)
        if self.approval_repo.mirror is not None:
            for record_id in self.approval_repo.mirror.ids():
                self.approval_repo.mirror.read(record_id)
        for task_id in self.task_repo.list_stable():
            task = self.task_repo.get(task_id)
            if (
                task.execution_environment != "local_worker"
                and task.status.terminal
                and not run_artifacts_match(self.settings.data_dir, task)
            ):
                raise ValueError(f"terminal Task artifacts are inconsistent: {task.task_id}")
        for approval_id in self.approval_repo.list_stable():
            self.approval_repo.get(approval_id)
        for path in self.device_store.root.glob("dev_*.json"):
            self.device_store.get(path.stem)

    def _default_model_client_factory(self):
        settings = self.settings
        if settings.model_provider == "anthropic":
            return AnthropicCompatibleModelClient(
                model=settings.pico_anthropic_model,
                base_url=settings.pico_anthropic_api_base,
                api_key=settings.pico_anthropic_api_key,
                temperature=settings.model_temperature,
                timeout=settings.model_timeout_seconds,
                max_attempts=1,
            )
        if settings.model_provider == "chat_completions":
            return OpenAICompletionsModelClient(
                model=settings.pico_chat_completions_model,
                base_url=settings.pico_chat_completions_api_base,
                api_key=settings.pico_chat_completions_api_key,
                temperature=settings.model_temperature,
                timeout=settings.model_timeout_seconds,
                max_attempts=1,
            )
        return OpenAICompatibleModelClient(
            model=settings.pico_openai_model,
            base_url=settings.pico_openai_api_base,
            api_key=settings.pico_openai_api_key,
            temperature=settings.model_temperature,
            timeout=settings.model_timeout_seconds,
            max_attempts=1,
        )

    def reconcile(self) -> bool:
        """Reconcile non-terminal records from a prior run. Returns False
        (ready must stay false) when an uncorrectable corruption is found."""
        _RECONCILE_ACTIVE = {
            TaskStatus.QUEUED,
            TaskStatus.RUNNING,
            TaskStatus.WAITING_FOR_APPROVAL,
            TaskStatus.CANCEL_REQUESTED,
        }
        ok = True
        try:
            self.workspace_catalog.validate_all()
            self._validate_sessions()
        except Exception:
            ok = False
        try:
            self.migrator.import_json(owner_id=self.owner_id)
        except Exception:
            ok = False
        try:
            self.isolation.recover_expired()
        except Exception:
            ok = False
        for task_id in self.task_repo.list_stable():
            try:
                task = self.task_repo.get(task_id)
            except Exception:
                # Corrupted record — ready must not report healthy.
                ok = False
                import logging

                logging.getLogger("threadforge.reconcile").warning(
                    "reconciliation: cannot read task %s; not ready", task_id
                )
                continue
            if task.status in _RECONCILE_ACTIVE:
                try:
                    if task.execution_environment == "local_worker":
                        self.task_repo.update(task.task_id, _set_failed_restarted)
                        continue
                    recovered = terminal_task_from_run(self.settings.data_dir, task)
                    if recovered is None:
                        self._fail_service_restarted(task)
                    else:
                        repair_terminal_run_artifacts(self.settings.data_dir, task, recovered)
                        status, stop_reason, final_answer = recovered
                        self.task_repo.update(
                            task.task_id,
                            lambda t, _status=status, _reason=stop_reason, _answer=final_answer: _set_recovered_terminal(
                                t, _status, _reason, _answer
                            ),
                        )
                except Exception:
                    ok = False
            elif (
                task.execution_environment != "local_worker"
                and task.status.terminal
                and not run_artifacts_match(self.settings.data_dir, task)
            ):
                try:
                    converge_run_artifacts(
                        self.settings.data_dir,
                        task,
                        status=task.status,
                        stop_reason=task.stop_reason or "runtime_error",
                        final_answer=task.final_answer or "",
                    )
                except Exception:
                    ok = False
        for approval_id in self.approval_repo.list_stable():
            try:
                approval = self.approval_repo.get(approval_id)
            except Exception:
                ok = False
                continue
            if approval.status is ApprovalStatus.PENDING:
                try:
                    self.approval_repo.update(
                        approval_id,
                        lambda a: _set_approval_status(a, ApprovalStatus.EXPIRED, "expired"),
                    )
                except Exception:
                    ok = False
        try:
            incomplete = self.recovery_journal.incomplete()
        except Exception:
            return False
        for transition in incomplete:
            approval_id = str(transition.get("approval_id", ""))
            try:
                approval = self.approval_repo.get(approval_id)
                if approval.status in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}:
                    self.approval_repo.update(
                        approval_id,
                        lambda a: _set_approval_status(a, ApprovalStatus.EXPIRED, "service_restarted"),
                    )
            except ApprovalNotFoundError:
                pass
            except Exception:
                ok = False
                continue
            try:
                self.recovery_journal.commit(str(transition["transition_id"]))
            except Exception:
                ok = False
        return ok

    def _fail_service_restarted(self, task) -> None:
        converge_run_artifacts(
            self.settings.data_dir,
            task,
            status=TaskStatus.INTERRUPTED,
            stop_reason="service_restarted",
            final_answer="agent run interrupted by service restart",
        )
        self.task_repo.update(
            task.task_id,
            lambda t: _set_failed_restarted(t),
        )


def _set_failed_restarted(task):
    task.status = TaskStatus.INTERRUPTED
    task.stop_reason = "service_restarted"
    task.pending_approval = None
    return task


def _set_recovered_terminal(task, status, stop_reason, final_answer):
    task.status = status
    task.stop_reason = stop_reason or None
    task.final_answer = final_answer or None
    task.pending_approval = None
    return task


def _set_approval_status(approval, status: ApprovalStatus, decision: str):
    approval.status = status
    approval.decision = decision
    from datetime import datetime, timezone

    approval.decided_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return approval


def build_lifespan(
    *,
    settings: Settings | None,
    model_client_factory: Callable | None = None,
    oauth_client: OAuthClient | None = None,
):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        resolved_settings = settings or Settings().freeze_provider_env()
        app.state.settings = resolved_settings
        container = AppContainer(
            resolved_settings,
            model_client_factory=model_client_factory,
            oauth_client=oauth_client,
        )
        app.state.container = container
        if container.reconcile():
            container.ready = True
        else:
            # Reconciliation failure means control state cannot be trusted.
            # Keep queries available for diagnosis but reject all new work.
            container.runner.mark_degraded()
        try:
            yield
        finally:
            container.ready = False
            container.runner.shutdown()
            container.broker.close_all()

    return lifespan
