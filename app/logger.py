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
    its own FileHandler in conversations/evaluation.py.
    """
    global _configured
    if _configured:
        return

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)

    rotating_file = logging.handlers.RotatingFileHandler(
        _LOG_DIR / "app.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    rotating_file.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console)
    root.addHandler(rotating_file)

    # Silence noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

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

    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)

    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(message)s"))

    eval_logger.setLevel(logging.INFO)
    eval_logger.propagate = False
    eval_logger.addHandler(fh)
    return eval_logger
