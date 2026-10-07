from __future__ import annotations

from datetime import datetime
from pathlib import Path
from threading import Lock

from .config import TECHNICAL_LOGGING_ENABLED
from .paths import get_logs_dir


_LOG_LOCK = Lock()
_LOG_PATH: Path | None = None


def _get_log_path() -> Path | None:
    global _LOG_PATH

    if not TECHNICAL_LOGGING_ENABLED:
        return None

    with _LOG_LOCK:
        logs_dir = get_logs_dir()
        if _LOG_PATH is None or _LOG_PATH.parent != logs_dir:
            try:
                logs_dir.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
                _LOG_PATH = logs_dir / f"oai-downloader_{timestamp}.log"
                _LOG_PATH.touch(exist_ok=True)
            except Exception:
                return None

        return _LOG_PATH


def log_event(category: str, message: str) -> None:
    """Registra un evento técnico sin propagar errores de escritura."""
    try:
        log_path = _get_log_path()
        if log_path is None:
            return

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{timestamp} [{category}] {message}\n"
        with _LOG_LOCK:
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(line)
    except Exception:
        pass


_get_log_path()
