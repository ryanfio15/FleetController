"""Structured JSON logging.

Every line carries `vehicle_id` and `batch_id` where they are known, so a single
batch can be followed across all seven hops of the pipeline with one grep.
Plain-text logs are a false economy in an async fan-out like this one.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

# Attributes LogRecord always has; anything else was passed via `extra=` and is
# therefore interesting.
_STANDARD = set(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", None) or _SERVICE,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and key != "service":
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_SERVICE = "unknown"


def setup(service: str) -> logging.Logger:
    """Install the JSON formatter and return the service's root logger."""
    global _SERVICE
    _SERVICE = service

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

    # uvicorn installs its own handlers; make them use ours too.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True

    return logging.getLogger(service)
