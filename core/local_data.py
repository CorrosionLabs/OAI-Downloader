from __future__ import annotations

from pathlib import Path
import shutil

from .paths import get_backup_dir, get_data_root, get_database_file


class LocalDataCleanupError(Exception):
    pass


def _remove_path(path: Path, managed_root: Path) -> int:
    if not path.exists() and not path.is_symlink():
        return 0
    if path.is_symlink():
        path.unlink()
        return 1
    if not path.resolve().is_relative_to(managed_root):
        raise LocalDataCleanupError("Se rechazÃ³ una ruta fuera de los datos gestionados.")
    if path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)
    return 1


def _clear_project_backups(
    projects_dir: Path,
    managed_root: Path,
    include_downloaded: bool,
) -> int:
    removed = 0
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir() or project_dir.is_symlink():
            removed += _remove_path(project_dir, managed_root)
            continue
        if not project_dir.resolve().is_relative_to(managed_root):
            raise LocalDataCleanupError("Se rechazÃ³ una ruta fuera de los datos gestionados.")
        for entry in project_dir.iterdir():
            if not include_downloaded and entry.name in {"resources", "library_files"}:
                continue
            removed += _remove_path(entry, managed_root)
        if include_downloaded:
            for directory in sorted(project_dir.rglob("*"), key=lambda path: len(path.parts), reverse=True):
                if directory.is_dir() and not directory.is_symlink():
                    try:
                        directory.rmdir()
                        removed += 1
                    except OSError:
                        pass
            try:
                project_dir.rmdir()
                removed += 1
            except OSError:
                pass
    return removed


def clear_local_data(*, include_downloaded: bool = False) -> dict[str, int | bool]:
    """Remove managed local state while preserving configuration and credentials."""
    data_root = get_data_root().resolve()
    backup_dir = get_backup_dir().resolve()
    database_file = get_database_file().resolve()

    if backup_dir.parent != data_root or database_file.parent != data_root:
        raise LocalDataCleanupError("La ruta de datos activa no es vÃ¡lida.")

    removed = 0
    for database_path in (
        database_file,
        database_file.with_name(f"{database_file.name}-wal"),
        database_file.with_name(f"{database_file.name}-shm"),
    ):
        removed += _remove_path(database_path, data_root)

    if backup_dir.exists():
        for entry in backup_dir.iterdir():
            if entry.name == "projects" and entry.is_dir() and not entry.is_symlink():
                removed += _clear_project_backups(
                    entry, backup_dir, include_downloaded
                )
            elif not include_downloaded and entry.name in {"resources", "library_files"}:
                continue
            else:
                removed += _remove_path(entry, backup_dir)

    return {"cleared": True, "removed_entries": removed,
            "downloaded_files_removed": include_downloaded}
