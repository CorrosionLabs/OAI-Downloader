from __future__ import annotations

from pathlib import Path
import re


APP_ROOT = Path(__file__).resolve().parent.parent

DATA_ROOT = Path.home() / "OAI-Downloader"

_active_data_root = DATA_ROOT

BACKUP_DIR = DATA_ROOT / "backup"

DEFAULT_CONVERSATIONS_DIR = BACKUP_DIR / "conversations"

DEFAULT_RESOURCES_DIR = BACKUP_DIR / "resources"

DEFAULT_CONVERSATION_STATE_FILE = BACKUP_DIR / "conversation_state.json"


def set_data_root(path: str | Path) -> Path:
    global _active_data_root
    _active_data_root = Path(path).expanduser().resolve()
    return _active_data_root


def get_data_root() -> Path:
    return _active_data_root


def get_cookie_file() -> Path:
    return Path(__file__).resolve().parent / ".secrets" / "cookies.txt"


def get_backup_dir() -> Path:
    return get_data_root() / "backup"


def get_database_file() -> Path:
    return get_data_root() / "oai_downloader.sqlite3"


def get_default_conversations_dir() -> Path:
    return get_backup_dir() / "conversations"


def get_default_resources_dir() -> Path:
    return get_backup_dir() / "resources"


def get_default_conversation_state_file() -> Path:
    return get_backup_dir() / "conversation_state.json"


def get_logs_dir() -> Path:
    return get_backup_dir() / "logs"


def safe_folder_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*]+', "_", value.strip())
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or "project"


def get_backup_paths(project_name: str | None = None) -> dict[str, Path]:
    backup_dir = get_backup_dir()
    if project_name:
        root = backup_dir / "projects" / safe_folder_name(project_name)
    else:
        root = backup_dir

    return {
        "root": root,
        "conversations": root / "conversations",
        "resources": root / "resources",
        "conversation_state": root / "conversation_state.json",
        "library_files": root / "library_files",
        "project_metadata": root / "project.json",
    }
