import json, logging, sys, time, uuid
from contextvars import ContextVar
from starlette.middleware.base import BaseHTTPMiddleware

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

class _JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": time.time(), "level": record.levelname,
            "logger": record.name, "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)

def setup_logging():
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.INFO)
    # Alembic's plugin/runtime chatter is noisy on every boot; only surface warnings.
    for noisy in ("alembic.runtime.plugins", "alembic.runtime.migration", "alembic.autogenerate"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(rid)
        request.state.request_id = rid
        try:
            resp = await call_next(request)
        finally:
            request_id_var.reset(token)
        resp.headers["x-request-id"] = rid
        return resp
