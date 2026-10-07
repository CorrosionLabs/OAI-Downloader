from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os

from .config import VERSION
from .paths import get_default_conversation_state_file


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")

    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(
                data,
                fh,
                ensure_ascii=False,
                indent=2,
            )
            fh.flush()
            os.fsync(fh.fileno())
        temp_path.replace(path)
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def load_conversation_state(state_file: Path | None = None) -> dict[str, Any]:
    if state_file is None:
        state_file = get_default_conversation_state_file()

    if not state_file.exists():
        return {
            "version": VERSION,
            "completed": {},
            "failed": {},
        }

    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return {
            "version": VERSION,
            "completed": {},
            "failed": {},
        }

    if not isinstance(data, dict):
        data = {}

    if not isinstance(data.get("completed"), dict):
        data["completed"] = {}

    if not isinstance(data.get("failed"), dict):
        data["failed"] = {}

    data["version"] = VERSION
    return data


def save_conversation_state(
    state: dict[str, Any],
    state_file: Path | None = None,
) -> None:
    if state_file is None:
        state_file = get_default_conversation_state_file()

    save_json(state_file, state)
