from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

class ApiError(Exception):
    def __init__(self, type: str, message: str, status: int, retry_after: int | None = None):
        self.type, self.message, self.status, self.retry_after = type, message, status, retry_after
        super().__init__(message)

    @classmethod
    def auth(cls, m="Invalid or missing API key"): return cls("authentication_error", m, 401)
    @classmethod
    def forbidden(cls, m="Forbidden"): return cls("permission_error", m, 403)
    @classmethod
    def not_found(cls, m="Not found"): return cls("not_found", m, 404)
    @classmethod
    def invalid(cls, m="Invalid request"): return cls("invalid_request", m, 422)
    @classmethod
    def rate_limited(cls, retry_after: int, m="Rate limit exceeded"):
        return cls("rate_limit", m, 429, retry_after=retry_after)
    @classmethod
    def session_busy(cls, m="Session is processing another message"): return cls("session_busy", m, 409)
    @classmethod
    def overloaded(cls, m="Server at capacity"): return cls("overloaded", m, 503)
    @classmethod
    def agent_error(cls, m="Agent run failed"): return cls("agent_error", m, 502)

def _envelope(req: Request, type: str, message: str, status: int, retry_after=None):
    rid = getattr(req.state, "request_id", "-")
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse(status_code=status, headers=headers,
                        content={"error": {"type": type, "message": message, "request_id": rid}})

def install_error_handlers(app: FastAPI):
    @app.exception_handler(ApiError)
    async def _api(req, exc: ApiError):
        return _envelope(req, exc.type, exc.message, exc.status, exc.retry_after)

    @app.exception_handler(RequestValidationError)
    async def _val(req, exc):
        return _envelope(req, "invalid_request", str(exc.errors()), 422)

    @app.exception_handler(Exception)
    async def _unhandled(req, exc):
        import logging; logging.getLogger("app").exception("unhandled")
        return _envelope(req, "internal_error", "Internal server error", 500)
