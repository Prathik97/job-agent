"""Keep API keys out of log files. Some APIs (Adzuna) take their key as a URL query parameter,
and urllib3's retry warnings print the URL, so every log handler gets this redaction filter."""
from __future__ import annotations

import logging
import re

_SECRET = re.compile(r"(?i)\b(app_id|app_key|api_key|apikey|key|token|access_token)=[^&\s'\")]+")


def redact(text: str) -> str:
    return _SECRET.sub(lambda m: f"{m.group(1)}=***", text)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def configure_logging(fmt: str = "%(message)s", level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format=fmt)
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactFilter())
