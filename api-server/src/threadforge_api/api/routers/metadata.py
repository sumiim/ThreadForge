"""Read-only runtime metadata used by the Web and desktop clients."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...domain.enums import ExecutionEnvironment
from ..dependencies import get_actor, get_settings

router = APIRouter()

_SKILLS = (
    {"id": "code-review", "name": "code-review", "description": "代码评审"},
    {"id": "security-review", "name": "security-review", "description": "安全审查"},
    {"id": "simplify", "name": "simplify", "description": "代码简化"},
)

_MCP_SERVERS = (
    {"id": "filesystem", "name": "filesystem", "description": "本地文件系统访问"},
    {"id": "github", "name": "github", "description": "GitHub 仓库与 Issue 操作"},
    {"id": "fetch", "name": "fetch", "description": "HTTP 抓取与网页内容读取"},
)


@router.get("/api/v1/config")
def get_runtime_config(settings=Depends(get_settings)) -> dict:
    multi_user = settings.identity_mode == "github_oauth"
    return {
        "model": "" if multi_user else settings.pico_openai_model,
        "model_configured": False if multi_user else settings.model_configured(),
        "execution_environment": (
            ExecutionEnvironment.LOCAL_WORKER.value
            if multi_user
            else ExecutionEnvironment.BACKEND_PROCESS.value
        ),
        "container_sandbox_enabled": False,
        "identity_mode": settings.identity_mode,
        "multi_user_enabled": multi_user,
    }


@router.get("/api/v1/skills", dependencies=[Depends(get_actor)])
def list_skills() -> dict:
    return {
        "items": [
            {**skill, "status": "planned", "available": False}
            for skill in _SKILLS
        ]
    }


@router.get("/api/v1/mcp/servers", dependencies=[Depends(get_actor)])
def list_mcp_servers() -> dict:
    return {
        "items": [
            {**server, "status": "not_configured", "connected": False}
            for server in _MCP_SERVERS
        ]
    }
