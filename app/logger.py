import logging
import logging.handlers
from pathlib import Path

_LOG_DIR = Path("logs")
_configured = False


def setup_logging(level: int = logging.INFO) -> None:
    """
    Call once at application startup (main.py).
    Configures the root logger with a console handler and a rotating file handler.
    The special pickwise.eval logger (JSON-lines) is left untouched — it manages
    its own FileHandler in rag/evaluation.py.
    """
    global _configured
    if _configured:
        return

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console)

    # File logging is best-effort: on hosts where the working directory is
    # not writable (e.g. containers with a read-only fs), fall back to
    # console-only instead of crashing the app at startup.
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        rotating_file = logging.handlers.RotatingFileHandler(
            _LOG_DIR / "app.log",
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=3,
            encoding="utf-8",
        )
        rotating_file.setFormatter(fmt)
        root.addHandler(rotating_file)
    except OSError as e:
        root.warning("File logging disabled (%s not writable): %s", _LOG_DIR, e)

    # Silence noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("watchfiles.main").setLevel(logging.WARNING)  # "N changes detected" reload noise

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """
    Drop-in replacement for `logging.getLogger(__name__)`.
    Usage:
        from app.logger import get_logger
        logger = get_logger(__name__)
    """
    return logging.getLogger(name)


def get_eval_logger(log_file: str = "logs/eval/pipeline_trace.jsonl") -> logging.Logger:
    """
    Returns the structured JSON-lines logger used by the evaluation pipeline.
    Idempotent — safe to call multiple times; the FileHandler is only added once.
    """
    name = "pickwise.eval"
    eval_logger = logging.getLogger(name)
    if eval_logger.handlers:
        return eval_logger

    # Same best-effort policy as setup_logging: an unwritable fs falls back
    # to emitting the JSON lines on stdout instead of raising mid-request.
    try:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as e:
        logging.getLogger(__name__).warning(
            "Eval file logging disabled (%s not writable): %s", log_file, e
        )
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))

    eval_logger.setLevel(logging.INFO)
    eval_logger.propagate = False
    eval_logger.addHandler(handler)
    return eval_logger
