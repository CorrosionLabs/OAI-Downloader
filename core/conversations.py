from __future__ import annotations

from datetime import datetime
from typing import Any
from collections.abc import Callable
from pathlib import Path
import sqlite3
import time

from .auth import get_session, load_cookie
from . import database
from .config import (
    APP_NAME,
    BASE_URL,
    CONVERSATIONS_ENDPOINT,
    CONVERSATION_ENDPOINT,
    VERSION,
)
from .errors import BackupCLIError
from .http import RequestCadence, build_url, request_json
from .paths import get_backup_paths, get_default_conversations_dir
from .projects import collect_project_conversation_items, resolve_project
from .state import load_conversation_state, save_conversation_state, save_json


def remote_updated_after(remote_updated_at: Any, previous_updated_at: str | None) -> bool:
    if not isinstance(remote_updated_at, str) or not isinstance(previous_updated_at, str):
        return False
    try:
        remote = datetime.fromisoformat(remote_updated_at.replace("Z", "+00:00"))
        previous = datetime.fromisoformat(previous_updated_at.replace("Z", "+00:00"))
        return remote > previous
    except (TypeError, ValueError):
        return False


def get_conversation_page(
    cookie: str,
    token: str,
    *,
    offset: int,
    limit: int,
    is_archived: bool,
    retry_rate_limit: bool = False,
    request_cadence: RequestCadence | None = None,
) -> dict[str, Any]:
    url = build_url(
        CONVERSATIONS_ENDPOINT,
        {
            "offset": offset,
            "limit": limit,
            "order": "updated",
            "is_archived": is_archived,
        },
    )

    status, payload = request_json(
        url, cookie=cookie, token=token, retry_rate_limit=retry_rate_limit,
        request_cadence=request_cadence,
    )

    if status != 200 or not isinstance(payload, dict):
        raise BackupCLIError(
            f"Error listando conversaciones. HTTP {status}\n"
            f"Respuesta: {str(payload)[:300]}"
        )

    return payload


def collect_conversation_items(
    cookie: str,
    token: str,
    *,
    is_archived: bool,
    retry_rate_limit: bool = False,
    request_cadence: RequestCadence | None = None,
) -> list[dict[str, Any]]:
    offset = 0
    page_size = 100
    seen: dict[str, dict[str, Any]] = {}

    while True:
        payload = get_conversation_page(
            cookie,
            token,
            offset=offset,
            limit=page_size,
            is_archived=is_archived,
            retry_rate_limit=retry_rate_limit,
            request_cadence=request_cadence,
        )

        items = payload.get("items") or []

        if not isinstance(items, list):
            raise BackupCLIError("La respuesta contiene un campo 'items' no válido.")

        for item in items:
            if not isinstance(item, dict):
                continue

            conversation_id = item.get("id")
            if conversation_id:
                seen[str(conversation_id)] = item

        if len(items) < page_size:
            break

        offset += page_size

    return list(seen.values())


def collect_conversations_summary(
    cookie: str,
    token: str,
    *,
    is_archived: bool,
) -> dict[str, Any]:
    offset = 0
    page_size = 100
    seen: dict[str, dict[str, Any]] = {}
    reported_total: int | None = None
    pages = 0

    while True:
        payload = get_conversation_page(
            cookie,
            token,
            offset=offset,
            limit=page_size,
            is_archived=is_archived,
        )
        pages += 1

        items = payload.get("items") or []

        if isinstance(payload.get("total"), int):
            reported_total = payload["total"]

        for item in items:
            if not isinstance(item, dict):
                continue

            conversation_id = item.get("id")
            if conversation_id:
                seen[str(conversation_id)] = item

        if len(items) < page_size:
            break

        offset += page_size

    newest_title = None
    if seen:
        newest_item = next(iter(seen.values()))
        newest_title = newest_item.get("title")

    return {
        "count": len(seen),
        "reported_total": reported_total,
        "pages": pages,
        "newest_title": newest_title,
    }


def get_full_conversation(
    cookie: str,
    token: str,
    conversation_id: str,
    retry_rate_limit: bool = False,
    request_cadence: RequestCadence | None = None,
) -> dict[str, Any]:
    endpoint = CONVERSATION_ENDPOINT.format(conversation_id=conversation_id)
    url = BASE_URL + endpoint

    status, payload = request_json(
        url,
        cookie=cookie,
        token=token,
        timeout=60,
        retry_rate_limit=retry_rate_limit,
        request_cadence=request_cadence,
    )

    if status != 200 or not isinstance(payload, dict):
        raise BackupCLIError(
            f"Error descargando conversación {conversation_id}. HTTP {status}"
        )

    return payload


def run_sample(limit: int) -> int:
    if limit < 1:
        raise BackupCLIError("--sample debe ser mayor que 0.")

    print()
    print(APP_NAME)
    print("=" * len(APP_NAME))
    print(f"Versión: {VERSION}")
    print()

    cookie = load_cookie()
    token, _session = get_session(cookie)

    print("Enumerando conversaciones activas...")
    items = collect_conversation_items(cookie, token, is_archived=False)
    selected = items[:limit]

    if not selected:
        raise BackupCLIError("No hay conversaciones disponibles para descargar.")

    conversations_dir = get_default_conversations_dir()
    conversations_dir.mkdir(parents=True, exist_ok=True)

    print(f"Descargando muestra: {len(selected)} conversación(es)")
    print()

    ok = 0
    failed = 0

    for index, item in enumerate(selected, start=1):
        conversation_id = str(item.get("id"))
        title = item.get("title") or "(sin título)"

        print(f"[{index}/{len(selected)}] {title}")

        try:
            payload = get_full_conversation(cookie, token, conversation_id)

            output_path = conversations_dir / f"{conversation_id}.json"
            save_json(output_path, payload)

            size_kb = output_path.stat().st_size / 1024
            print(f"      OK -> {output_path.name} ({size_kb:.1f} KB)")
            ok += 1

        except Exception as exc:
            print(f"      ERROR -> {type(exc).__name__}: {exc}")
            failed += 1

        time.sleep(0.3)

    print()
    print("RESULTADO")
    print("---------")
    print(f"Descargadas: {ok}")
    print(f"Fallidas:    {failed}")
    print(f"Carpeta:     {conversations_dir}")

    return 0 if failed == 0 else 1


def collect_all_conversation_items(
    cookie: str,
    token: str,
    *,
    retry_rate_limit: bool = False,
    request_cadence: RequestCadence | None = None,
) -> list[dict[str, Any]]:
    active = collect_conversation_items(
        cookie,
        token,
        is_archived=False,
        retry_rate_limit=retry_rate_limit,
        request_cadence=request_cadence,
    )

    archived = collect_conversation_items(
        cookie,
        token,
        is_archived=True,
        retry_rate_limit=retry_rate_limit,
        request_cadence=request_cadence,
    )

    merged: dict[str, dict[str, Any]] = {}

    for item in active:
        if not isinstance(item, dict):
            continue

        conversation_id = item.get("id")
        if not conversation_id:
            continue

        entry = dict(item)
        entry["_is_archived"] = False
        merged[str(conversation_id)] = entry

    for item in archived:
        if not isinstance(item, dict):
            continue

        conversation_id = item.get("id")
        if not conversation_id:
            continue

        entry = dict(item)
        entry["_is_archived"] = True
        merged[str(conversation_id)] = entry

    return list(merged.values())


def run_download_conversations(
    project_name: str | None = None,
    refresh: bool = False,
    *,
    project_record: dict[str, Any] | None = None,
    allow_empty: bool = False,
) -> int:
    print()
    print(APP_NAME)
    print("=" * len(APP_NAME))
    print(f"Versión: {VERSION}")
    print()

    cookie = load_cookie()
    token, _session = get_session(cookie)

    paths = get_backup_paths(project_name)
    conversations_dir = paths["conversations"]
    conversation_state_file = paths["conversation_state"]

    project = project_record

    if project_name:
        if project is None:
            project = resolve_project(cookie, token, project_name)
        print(f'Project: {project["name"]}')
        print(f'ID:      {project["id"]}')
        print()
        print("Enumerando únicamente conversaciones de este Project...")
        items = collect_project_conversation_items(
            cookie,
            token,
            project_id=project["id"],
        )

        save_json(
            paths["project_metadata"],
            {
                "version": VERSION,
                "id": project["id"],
                "name": project["name"],
                "raw": project["raw"],
            },
        )
    else:
        print("Enumerando conversaciones activas y archivadas...")
        items = collect_all_conversation_items(cookie, token)

    if not items:
        if project_name:
            if allow_empty:
                conversations_dir.mkdir(parents=True, exist_ok=True)
                save_conversation_state(
                    load_conversation_state(conversation_state_file),
                    conversation_state_file,
                )
                print(f'No se encontraron conversaciones en el Project "{project_name}".')
                print("Proyecto vacío; se continúa con las fases restantes.")
                return 2
            raise BackupCLIError(
                f'No se encontraron conversaciones en el Project "{project_name}".'
            )
        raise BackupCLIError("No se encontraron conversaciones.")

    result = download_conversation_items(
        cookie, token, items, paths=paths, project=project, refresh=refresh,
    )
    return 0 if result["failed"] == 0 else 1


def download_conversation_items(
    cookie: str,
    token: str,
    items: list[dict[str, Any]],
    *,
    paths: dict[str, Path],
    project: dict[str, Any] | None = None,
    refresh: bool = False,
    progress: Callable[..., None] | None = None,
    output: Callable[..., None] = print,
) -> dict[str, int]:
    """Download an enumerated scope; shared by the CLI and the web adapter."""
    conversations_dir = paths["conversations"]
    conversation_state_file = paths["conversation_state"]
    output(f"Conversaciones detectadas: {len(items)}")
    output()

    conversations_dir.mkdir(parents=True, exist_ok=True)

    state = load_conversation_state(conversation_state_file)
    completed = state["completed"]
    failed = state["failed"]

    ok = 0
    refreshed = 0
    skipped = 0
    errors = 0
    detail_request_cadence = RequestCadence(0.25)

    def report(current: int) -> None:
        if progress:
            progress(current=current, total=len(items), downloaded=ok,
                     refreshed=refreshed, skipped=skipped, failed=errors)

    report(0)

    for index, item in enumerate(items, start=1):
        conversation_id = str(item.get("id") or item.get("conversation_id"))
        if (conversation_id in {"", "None", ".", ".."}
                or any(char in conversation_id for char in '/\\:*?"<>|')):
            raise BackupCLIError("Identificador de conversación no válido.")
        title = item.get("title") or "(sin título)"
        is_archived = bool(item.get("_is_archived"))

        output_path = conversations_dir / f"{conversation_id}.json"

        output(
            f"[{index}/{len(items)}] "
            f"{'[ARCHIVADA] ' if is_archived else ''}{title}"
        )

        previous = completed.get(conversation_id)

        local_exists = output_path.exists() and output_path.stat().st_size > 0
        remote_updated_at = item.get("update_time") or item.get("updated_at")
        automatic_refresh = False
        if local_exists and not refresh and remote_updated_at is not None:
            try:
                previous_updated_at = database.get_conversation_updated_at(conversation_id)
            except (OSError, sqlite3.Error):
                previous_updated_at = None
            automatic_refresh = remote_updated_after(remote_updated_at, previous_updated_at)

        should_refresh = local_exists and (refresh or automatic_refresh)
        if local_exists and not should_refresh:
            if not isinstance(previous, dict):
                completed[conversation_id] = {
                    "path": str(output_path),
                    "size_bytes": output_path.stat().st_size,
                    "title": title,
                    "is_archived": is_archived,
                    "project_id": project["id"] if project else None,
                    "project_name": project["name"] if project else None,
                }
                save_conversation_state(state, conversation_state_file)

            output("      SKIP -> ya descargada")
            skipped += 1
            if failed.pop(conversation_id, None) is not None:
                save_conversation_state(state, conversation_state_file)
            database.mark_conversation_downloaded(
                conversation_id, remote_updated_at,
            )
            report(index)
            continue

        try:
            payload = get_full_conversation(
                cookie,
                token,
                conversation_id,
                retry_rate_limit=True,
                request_cadence=detail_request_cadence,
            )

            temporary = output_path.with_suffix(".json.tmp")
            save_json(temporary, payload)
            temporary.replace(output_path)

            size_bytes = output_path.stat().st_size

            completed[conversation_id] = {
                "path": str(output_path),
                "size_bytes": size_bytes,
                "title": title,
                "is_archived": is_archived,
                "project_id": project["id"] if project else None,
                "project_name": project["name"] if project else None,
            }

            failed.pop(conversation_id, None)
            save_conversation_state(state, conversation_state_file)
            database.mark_conversation_downloaded(
                conversation_id, remote_updated_at,
            )

            if should_refresh:
                output(
                    f"      REFRESH -> {output_path.name} "
                    f"({size_bytes / 1024:.1f} KB)"
                )
                refreshed += 1
            else:
                output(
                    f"      OK -> {output_path.name} "
                    f"({size_bytes / 1024:.1f} KB)"
                )
                ok += 1

        except Exception as exc:
            failed[conversation_id] = {
                "error_type": type(exc).__name__,
                "error": str(exc),
                "title": title,
                "is_archived": is_archived,
                "project_id": project["id"] if project else None,
                "project_name": project["name"] if project else None,
            }

            save_conversation_state(state, conversation_state_file)
            database.mark_conversation_failed(conversation_id)

            output(f"      ERROR -> {type(exc).__name__}: {exc}")
            errors += 1

            if "403" in str(exc):
                try:
                    output("      Revalidando sesión...")
                    token, _session = get_session(cookie)
                    output("      Sesión válida de nuevo.")
                except Exception:
                    output("      Sesión no recuperable con la cookie actual.")

        report(index)

    output()
    output("RESULTADO")
    output("---------")
    output(f"Descargadas ahora: {ok}")
    output(f"Actualizadas ahora: {refreshed}")
    output(f"Ya existentes:      {skipped}")
    output(f"Fallidas:           {errors}")
    output(f"Estado guardado en: {conversation_state_file}")
    output(f"Conversaciones en:  {conversations_dir}")

    if project:
        output(f"Project aislado en: {paths['root']}")

    if errors:
        output()
        output("Puedes volver a ejecutar el mismo comando.")
        output("Las completadas se omitirán y solo se reintentará lo pendiente.")

    save_conversation_state(state, conversation_state_file)
    return {"downloaded": ok, "refreshed": refreshed, "skipped": skipped, "failed": errors}
