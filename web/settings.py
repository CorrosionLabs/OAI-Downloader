from __future__ import annotations

import json
import math
from pathlib import Path

from core.config import DEFAULT_ANALYSIS_REQUEST_DELAY
from core.paths import get_backup_dir, get_data_root, set_data_root
from web.jobs import WebError


class Settings:
    def __init__(self, file: Path):
        self.file = file
        self.analysis_request_delay = DEFAULT_ANALYSIS_REQUEST_DELAY

    def load(self):
        if not self.file.exists():
            return
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            value = data["data_root"]
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError("Invalid data root")
            delay = data.get("analysis_request_delay", DEFAULT_ANALYSIS_REQUEST_DELAY)
            if (isinstance(delay, bool) or not isinstance(delay, (int, float))
                    or not math.isfinite(delay) or delay < 0):
                raise ValueError("Invalid analysis request delay")
            self.analysis_request_delay = float(delay)
            set_data_root(value)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"Configuración web no válida: {self.file}") from exc

    def save(self, value: str):
        path = Path(value.strip()).expanduser()
        if not value.strip() or not path.is_absolute():
            raise WebError("invalid_folder")
        try:
            path = path.resolve()
            if not path.is_dir():
                raise WebError("invalid_folder")
            self.file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.file.with_suffix(".tmp")
            temporary.write_text(json.dumps({
                "data_root": str(path),
                "analysis_request_delay": self.analysis_request_delay,
            }, indent=2), encoding="utf-8")
            temporary.replace(self.file)
        except OSError as exc:
            raise WebError("storage_failed") from exc
        set_data_root(path)

    def save_analysis_request_delay(self, value: float):
        if not math.isfinite(value) or value < 0:
            raise WebError("invalid_input")
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.file.with_suffix(".tmp")
            temporary.write_text(json.dumps({
                "data_root": str(get_data_root()),
                "analysis_request_delay": value,
            }, indent=2), encoding="utf-8")
            temporary.replace(self.file)
        except OSError as exc:
            raise WebError("storage_failed") from exc
        self.analysis_request_delay = value

    def status(self):
        return {
            "storage_root": str(get_data_root()),
            "backup_dir": str(get_backup_dir()),
            "analysis_request_delay": self.analysis_request_delay,
        }
