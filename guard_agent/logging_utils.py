import json
import logging
import os
import weakref

logger = logging.getLogger("guard_agent")

# the handlers this module attached, so a reconfigure never touches a host's own
_agent_handlers: weakref.WeakSet[logging.Handler] = weakref.WeakSet()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            log_entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(log_entry, default=str)


def _create_formatter(log_format: str) -> logging.Formatter:
    if log_format == "json":
        return JsonFormatter()
    return logging.Formatter("[%(name)s] %(asctime)s - %(levelname)s - %(message)s")


class _YieldToHostRootHandlers(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not logging.getLogger().handlers


def logger_is_configured(candidate: logging.Logger) -> bool:
    return bool(candidate.handlers)


def _attach_file_handler(
    logger: logging.Logger, log_file: str, formatter: logging.Formatter
) -> None:
    try:
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        _agent_handlers.add(file_handler)
    except Exception as e:
        logger.warning(f"Failed to create log file {log_file}: {e}")


def _attach_handlers(
    logger: logging.Logger, log_file: str | None, log_format: str
) -> logging.Logger:
    formatter = _create_formatter(log_format)

    if not logger_is_configured(logger):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.addFilter(_YieldToHostRootHandlers())
        logger.addHandler(console_handler)
        _agent_handlers.add(console_handler)

    if log_file:
        _attach_file_handler(logger, log_file, formatter)

    return logger


def _has_agent_file_handler(logger: logging.Logger, log_file: str) -> bool:
    target = os.path.abspath(log_file)
    return any(
        isinstance(handler, logging.FileHandler)
        and handler in _agent_handlers
        and handler.baseFilename == target
        for handler in logger.handlers
    )


def setup_agent_logging(
    log_file: str | None = None,
    log_format: str = "text",
    *,
    reconfigure: bool = True,
) -> logging.Logger:
    logger = logging.getLogger("guard_agent")

    if not reconfigure:
        if logger_is_configured(logger):
            if log_file and not _has_agent_file_handler(logger, log_file):
                _attach_file_handler(logger, log_file, _create_formatter(log_format))
            return logger
        if logger.level == logging.NOTSET:
            logger.setLevel(logging.INFO)
        return _attach_handlers(logger, log_file, log_format)

    for handler in logger.handlers[:]:
        if handler in _agent_handlers:
            handler.close()
            logger.removeHandler(handler)

    logger.setLevel(logging.INFO)

    return _attach_handlers(logger, log_file, log_format)
