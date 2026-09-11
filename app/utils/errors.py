from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette import status as http_status


class AppBaseException(Exception):
    http_status: int = http_status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "INTERNAL_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details


class NotFoundException(AppBaseException):
    http_status = http_status.HTTP_404_NOT_FOUND
    code = "NOT_FOUND"

    def __init__(
        self,
        resource_type: str,
        resource_id: str | int | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        if resource_id is None:
            msg = f"{resource_type} not found"
        else:
            msg = f"{resource_type} '{resource_id}' not found"
        super().__init__(msg, details=details)


class ConflictException(AppBaseException):
    http_status = http_status.HTTP_409_CONFLICT
    code = "CONFLICT"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)


class BadRequestException(AppBaseException):
    http_status = http_status.HTTP_400_BAD_REQUEST
    code = "BAD_REQUEST"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)


class UnsupportedMediaTypeException(AppBaseException):
    http_status = http_status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    code = "UNSUPPORTED_MEDIA_TYPE"

    def __init__(
        self,
        message: str = "Unsupported media type",
        *,
        actual_ext: str | None = None,
        allowed: list[str] | None = None,
    ) -> None:
        details: dict[str, Any] = {}
        if actual_ext is not None:
            details["actual_extension"] = actual_ext
        if allowed is not None:
            details["allowed_extensions"] = allowed
        super().__init__(message, details=details or None)


class PayloadTooLargeException(AppBaseException):
    http_status = http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    code = "PAYLOAD_TOO_LARGE"

    def __init__(
        self,
        *,
        max_mb: int | None = None,
        actual_bytes: int | None = None,
    ) -> None:
        details: dict[str, Any] = {}
        message_parts = ["Request payload too large"]
        if max_mb is not None:
            details["max_mb"] = max_mb
            message_parts.append(f"(max {max_mb}MB)")
        if actual_bytes is not None:
            details["actual_bytes"] = actual_bytes
            details["actual_mb"] = round(actual_bytes / 1024 / 1024, 2)
        super().__init__(" ".join(message_parts), details=details or None)


class PipelineStateException(ConflictException):
    code = "PIPELINE_STATE_ERROR"

    def __init__(
        self,
        *,
        task_id: str,
        current_status: str,
        expected_status: list[str] | str,
    ) -> None:
        expected = (
            expected_status
            if isinstance(expected_status, list)
            else [expected_status]
        )
        msg = (
            f"Task '{task_id}' is in status '{current_status}',"
            f" expected one of: {', '.join(expected)}"
        )
        details = {
            "task_id": task_id,
            "current_status": current_status,
            "expected_status": expected,
        }
        super().__init__(msg, code=self.code, details=details)


class LLMCallException(AppBaseException):
    http_status = http_status.HTTP_502_BAD_GATEWAY
    code = "LLM_CALL_ERROR"

    def __init__(
        self,
        message: str,
        *,
        upstream_status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        extra_details: dict[str, Any] = dict(details or {})
        if upstream_status is not None:
            extra_details["upstream_status_code"] = upstream_status
        super().__init__(message, details=extra_details or None)


class LLMResponseFormatException(AppBaseException):
    http_status = http_status.HTTP_502_BAD_GATEWAY
    code = "LLM_RESPONSE_FORMAT_ERROR"

    def __init__(
        self,
        message: str,
        *,
        raw_preview: str | None = None,
    ) -> None:
        details: dict[str, Any] = {}
        if raw_preview is not None:
            details["raw_preview"] = raw_preview
        super().__init__(message, details=details or None)


def _error_body(
    *,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppBaseException)
    async def _app_exception_handler(
        _: Request, exc: AppBaseException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=_error_body(
                code=exc.code,
                message=exc.message,
                details=exc.details,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = {"errors": exc.errors()}
        return JSONResponse(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_error_body(
                code="VALIDATION_ERROR",
                message="Request validation failed",
                details=details,
            ),
        )

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(
        _: Request, exc: HTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(
                code=f"HTTP_{exc.status_code}",
                message=exc.detail if isinstance(exc.detail, str) else str(exc.detail),
                details=None,
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        from app.utils.logger import get_logger

        logger = get_logger("app.unhandled")
        logger.exception(
            "Unhandled exception while processing %s %s: %s",
            request.method,
            request.url.path,
            exc,
        )
        return JSONResponse(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body(
                code="INTERNAL_SERVER_ERROR",
                message="Internal server error",
                details=None,
            ),
        )
