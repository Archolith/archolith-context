"""OpenAI-formatted error responses."""

from __future__ import annotations

from fastapi.responses import JSONResponse


class OpenAIError(Exception):
    """Base error that produces an OpenAI-formatted error response."""

    def __init__(
        self,
        message: str,
        error_type: str = "server_error",
        code: str | None = None,
        param: str | None = None,
        status_code: int = 500,
    ):
        self.message = message
        self.error_type = error_type
        self.code = code
        self.param = param
        self.status_code = status_code
        super().__init__(message)

    def to_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content={
                "error": {
                    "message": self.message,
                    "type": self.error_type,
                    "param": self.param,
                    "code": self.code,
                }
            },
        )


class InvalidRequestError(OpenAIError):
    def __init__(self, message: str, param: str | None = None):
        super().__init__(
            message=message,
            error_type="invalid_request_error",
            param=param,
            status_code=400,
        )


class AuthenticationError(OpenAIError):
    def __init__(self, message: str = "Invalid API key"):
        super().__init__(
            message=message,
            error_type="authentication_error",
            code="invalid_api_key",
            status_code=401,
        )


class UpstreamError(OpenAIError):
    def __init__(self, message: str):
        super().__init__(
            message=message,
            error_type="upstream_error",
            status_code=502,
        )


class UpstreamTimeoutError(OpenAIError):
    def __init__(self, message: str = "Upstream API request timed out"):
        super().__init__(
            message=message,
            error_type="upstream_timeout",
            code="timeout",
            status_code=504,
        )


def make_error_response(
    status_code: int,
    message: str,
    error_type: str = "server_error",
    param: str | None = None,
    code: str | None = None,
) -> JSONResponse:
    """Build an OpenAI-formatted error response without raising."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "param": param,
                "code": code,
            }
        },
    )
