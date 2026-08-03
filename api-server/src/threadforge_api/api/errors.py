"""Global exception handlers producing the stable error contract."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..domain.errors import AppError


def request_id_of(request: Request) -> str:
    return str(getattr(request.state, "request_id", ""))


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=exc.to_error_dict(request_id_of(request)),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        detail = "; ".join(
            ".".join(str(loc) for loc in err.get("loc", [])) + ": " + err.get("msg", "")
            for err in errors[:3]
        )
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": detail or "invalid request",
                    "details": {},
                    "request_id": request_id_of(request),
                }
            },
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "internal server error",
                    "details": {},
                    "request_id": request_id_of(request),
                }
            },
        )
