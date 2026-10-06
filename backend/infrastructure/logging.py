import json
import logging
from datetime import UTC, datetime


class ProjectFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Only fixed event names and allowlisted correlation fields enter project logs.
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": record.msg,
        }
        for key in ("request_id", "run_id", "thread_id", "status_code"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload)


def configure_logging(level: str) -> logging.Logger:
    logger = logging.getLogger("enterprise_agent")
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(ProjectFormatter())
        logger.addHandler(handler)
    return logger
