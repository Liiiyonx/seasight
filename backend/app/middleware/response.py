"""统一响应中间件与异常处理。

约定：HTTP 状态码统一 200，业务错误通过 body.code 判断。
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppException, ErrorCode


def _envelope(
    code: int,
    message: str,
    data=None,
    trace_id: str | None = None,
    http_status: int = 200,
) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={
            "code": code,
            "message": message,
            "data": data,
            "trace_id": trace_id,
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器。"""

    @app.exception_handler(AppException)
    async def _app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", None)
        logger.warning(f"[业务异常] {exc.code} {exc.message} | {request.url.path}")
        return _envelope(
            exc.code, exc.message, exc.detail or None, trace_id, exc.http_status
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", None)
        errors = exc.errors()
        detail = [
            {"field": ".".join(str(p) for p in e.get("loc", [])), "msg": e.get("msg")}
            for e in errors
        ]
        logger.warning(f"[参数校验失败] {request.url.path} | {detail}")
        return _envelope(
            ErrorCode.PARAM_INVALID,
            "请求参数校验失败",
            detail,
            trace_id,
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", None)
        code_map = {
            401: ErrorCode.UNAUTHORIZED,
            403: ErrorCode.FORBIDDEN,
            404: ErrorCode.NOT_FOUND,
        }
        code = code_map.get(exc.status_code, ErrorCode.UNKNOWN)
        return _envelope(code, str(exc.detail), None, trace_id, exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", None)
        logger.exception(f"[未捕获异常] {request.url.path} | trace_id={trace_id}")
        return _envelope(
            ErrorCode.UNKNOWN,
            "服务器内部错误，请稍后重试",
            None,
            trace_id,
            http_status=500,
        )


async def trace_id_middleware(request: Request, call_next):
    """为每个请求注入 trace_id，便于链路排查。"""
    trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:16]
    request.state.trace_id = trace_id
    response = await call_next(request)
    response.headers["X-Trace-Id"] = trace_id
    return response
