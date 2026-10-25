"""JSON logs: one JSON object per line on stdout, which Alloy ships to Loki.

Rules (docs/OBSERVABILITY.md in chat-infra): include `level` and `msg`; later also `trace_id`.
Never log OTP codes, tokens or full phone numbers.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, override

# Attributes every LogRecord has; anything else was passed via `extra=` and is logged as a field.
_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "color_message",  # uvicorn duplicate of msg with terminal colors
}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, pod: str) -> None:
        super().__init__()
        self._static = {"service": service, "pod": pod}

    @override
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            **self._static,
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class _DropHealthChecks(logging.Filter):
    """Kubernetes probes hit /healthz every few seconds: keep them out of the logs."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz" not in record.getMessage()


def setup_logging(level: str, service: str, pod: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service, pod))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # uvicorn configures its own loggers; route them through the same JSON handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    logging.getLogger("uvicorn.access").addFilter(_DropHealthChecks())
    # httpx logs every outgoing request with its full URL at INFO; a provider URL may carry an API
    # key (e.g. Global SMS's getBalance takes ?ApiKey=). Keep only its warnings and errors.
    logging.getLogger("httpx").setLevel(logging.WARNING)
