import json
import logging
import os
import sys


class _JSONFormatter(logging.Formatter):
    def format(self, record):
        obj = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            obj["exc"] = self.formatException(record.exc_info)
        for key in (
            "method",
            "status",
            "barcode",
            "query",
            "operation",
            "tool",
            "duration_s",
            "error",
        ):
            val = getattr(record, key, None)
            if val is not None:
                obj[key] = val
        return json.dumps(obj)


_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(_JSONFormatter())

_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.addHandler(_handler)
        logger.setLevel(_level)
    return logger
