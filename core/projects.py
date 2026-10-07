from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from itertools import count
from threading import Lock, local
from typing import Any
import urllib.parse

from .auth import get_session, is_global_authentication_error, load_cookie
from .config import (
    APP_NAME,
    BASE_URL,
    PROJECTS_ENDPOINT,
    PROJECT_CONVERSATIONS_ENDPOINT,
    RESOURCE_DELAY_SECONDS,
    DEFAULT_ANALYSIS_REQUEST_DELAY,
    VERSION,
)
from .errors import BackupCLIError
from .http import RequestCadence, build_url, request_json
from .inventory import run_inventory
from .paths import get_logs_dir
from .resources import run_download_resources_resolved


def get_projects_payload(
    cookie: str,
    token: str,
    *,
    retry_rate_limit: bool = False,
    request_cadence: RequestCadence | None = None,
) -> dict[str, Any]:
    all_items: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    pages = 0

    while True:
        params: dict[str, Any] = {
            "owned_only": True,
            "conversations_per_gizmo": 0,
            "limit": 50,
        }

        if cursor:
            params["cursor"] = cursor

        url = build_url(PROJECTS_ENDPOINT, params)
        status, payload = request_json(
            url, cookie=cookie, token=token, retry_rate_limit=retry_rate_limit,
            request_cadence=request_cadence,
        )

        if status != 200 or not isinstance(payload, dict):
            raise BackupCLIError(
                f"Projects no accesible. HTTP {status}\n"
                f"Respuesta: {str(payload)[:300]}"
            )

        pages += 1

        items = payload.get("items") or []
        if not isinstance(items, list):
            raise BackupCLIError(
                "La respuesta de Projects no contiene una lista 'items' válida."
            )

        for item in items:
            if isinstance(item, dict):
                all_items.append(item)

        next_cursor = (
            payload.get("next_cursor")
            or payload.get("nextCursor")
            or payload.get("cursor")
        )

        if not next_cursor:
            break

        next_cursor = str(next_cursor)

        if next_cursor == cursor or next_cursor in seen_cursors:
            break

        seen_cursors.add(next_cursor)
        cursor = next_cursor

        if not items:
            break

    return {
        "items": all_items,
        "_pages": pages,
        "_last_cursor": cursor,
    }


def extract_project_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("items") or []
    if not isinstance(items, list):
        return []

    projects: list[dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            continue

        outer_gizmo = item.get("gizmo")
        if isinstance(outer_gizmo, dict):
            gizmo = outer_gizmo.get("gizmo")
            if not isinstance(gizmo, dict):
                gizmo = outer_gizmo
        else:
            gizmo = item

        if not isinstance(gizmo, dict):
            continue

        project_id = gizmo.get("id")
        display = gizmo.get("display") or {}
        name = None

        if isinstance(display, dict):
            name = display.get("name")

        name = name or gizmo.get("name") or gizmo.get("title")

        if not project_id or not name:
            continue

        if not str(project_id).startswith("g-p-"):
            continue

        projects.append(
            {
                "id": str(project_id),
                "name": str(name),
                "raw": item,
            }
        )

    return projects


def get_ordered_project_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return Projects in the same visible order used by --list-projects."""
    return sorted(
        extract_project_records(payload),
        key=lambda project: project["name"].casefold(),
    )


def get_projects(cookie: str, token: str) -> dict[str, Any]:
    try:
        payload = get_projects_payload(cookie, token)
        projects = extract_project_records(payload)
        return {
            "status": 200,
            "count": len(projects),
            "warning": None,
        }
    except BackupCLIError as exc:
        return {
            "status": None,
            "count": None,
            "warning": str(exc),
        }


def collect_projects_overview(
    cookie: str,
    token: str,
    progress=None,
    on_conversation_items=None,
    *,
    retry_rate_limit: bool = False,
    request_delay: float = DEFAULT_ANALYSIS_REQUEST_DELAY,
) -> dict[str, Any]:
    request_cadence = RequestCadence(request_delay)
    payload = get_projects_payload(
        cookie, token, retry_rate_limit=retry_rate_limit,
        request_cadence=request_cadence,
    )
    records = get_ordered_project_records(payload)
    rows = []
    project_conversation_ids = set()
    total_scopes = len(records) + 1
    if progress:
        progress(phase="project_conversations", current=0, total=total_scopes,
                 current_project="")
    for index, project in enumerate(records, start=1):
        if progress:
            progress(phase="project_conversations", current=index - 1, total=total_scopes,
                     current_project=project["name"])
        items = collect_project_conversation_items(
            cookie, token, project_id=project["id"],
            retry_rate_limit=retry_rate_limit,
            request_cadence=request_cadence,
        )
        project_conversation_ids.update(
            str(item.get("id") or item.get("conversation_id") or "")
            for item in items
        )
        if on_conversation_items:
            on_conversation_items(project, items)
        rows.append({"id": project["id"], "name": project["name"],
                     "conversation_count": len(items)})
        if progress:
            progress(phase="project_conversations", current=index, total=total_scopes,
                     current_project=project["name"])
    from .conversations import collect_all_conversation_items

    if progress:
        progress(phase="general_conversations", current=len(records), total=total_scopes,
                 current_project="")
    root_items = [
        item
        for item in collect_all_conversation_items(
            cookie, token, retry_rate_limit=retry_rate_limit,
            request_cadence=request_cadence,
        )
        if str(item.get("id") or item.get("conversation_id") or "") not in project_conversation_ids
    ]
    if on_conversation_items:
        on_conversation_items(None, root_items)
    root_conversation_count = len(root_items)
    if progress:
        progress(phase="general_conversations", current=total_scopes, total=total_scopes,
                 current_project="")
    return {"projects": rows, "total_projects": len(rows),
            "total_conversations": sum(row["conversation_count"] for row in rows),
            "root_conversation_count": root_conversation_count,
            "current": total_scopes, "total": total_scopes,
            "pages": payload.get("_pages", 1)}


def run_list_projects() -> int:
    print()
    print(APP_NAME)
    print("=" * len(APP_NAME))
    print(f"Versión: {VERSION}")
    print()
    print("LISTADO DE PROYECTOS")
    print("--------------------")

    cookie = load_cookie()
    token, _session = get_session(cookie)

    overview = collect_projects_overview(cookie, token)
    projects = overview["projects"]

    print(f"Páginas consultadas: {overview['pages']}")
    print()

    if not projects:
        print("No se encontraron Projects visibles.")
        return 0

    total_conversations = 0

    for index, project in enumerate(projects, start=1):
        conversation_count = project["conversation_count"]
        total_conversations += conversation_count

        print(f"[{index:02d}] {project['name']}")
        print(f"     ID:              {project['id']}")
        print(f"     Conversaciones:  {conversation_count}")
        print()

    print("RESUMEN")
    print("-------")
    print(f"Projects visibles:    {len(projects)}")
    print(f"Conversaciones:       {total_conversations}")

    return 0


def resolve_project(
    cookie: str,
    token: str,
    project_name: str,
) -> dict[str, Any]:
    payload = get_projects_payload(cookie, token)
    projects = extract_project_records(payload)

    wanted = project_name.strip().casefold()
    exact = [p for p in projects if p["name"].strip().casefold() == wanted]

    if len(exact) == 1:
        return exact[0]

    partial = [p for p in projects if wanted in p["name"].strip().casefold()]

    if len(partial) == 1:
        return partial[0]

    if not exact and not partial:
        available = ", ".join(sorted(p["name"] for p in projects)) or "(ninguno)"
        raise BackupCLIError(
            f'No se encontró el Project "{project_name}".\n'
            f"Projects visibles: {available}"
        )

    matches = exact or partial
    raise BackupCLIError(
        f'El nombre "{project_name}" coincide con varios Projects: '
        + ", ".join(sorted(p["name"] for p in matches))
    )


def get_project_conversation_page(
    cookie: str,
    token: str,
    *,
    project_id: str,
    cursor: str | None,
    retry_rate_limit: bool = False,
    request_cadence: RequestCadence | None = None,
) -> dict[str, Any]:
    endpoint = PROJECT_CONVERSATIONS_ENDPOINT.format(
        project_id=urllib.parse.quote(project_id)
    )

    params: dict[str, Any] = {}
    if cursor is not None:
        params["cursor"] = cursor

    url = build_url(endpoint, params) if params else BASE_URL + endpoint
    status, payload = request_json(
        url, cookie=cookie, token=token, retry_rate_limit=retry_rate_limit,
        request_cadence=request_cadence,
    )

    if status != 200 or not isinstance(payload, dict):
        raise BackupCLIError(
            f"Error listando conversaciones del Project. HTTP {status}\n"
            f"Respuesta: {str(payload)[:300]}"
        )

    return payload


def collect_project_conversation_items(
    cookie: str,
    token: str,
    *,
    project_id: str,
    retry_rate_limit: bool = False,
    request_cadence: RequestCadence | None = None,
) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    cursor: str | None = "0"
    seen_cursors: set[str] = set()

    while True:
        payload = get_project_conversation_page(
            cookie,
            token,
            project_id=project_id,
            cursor=cursor,
            retry_rate_limit=retry_rate_limit,
            request_cadence=request_cadence,
        )

        items = payload.get("items") or payload.get("conversations") or []
        if not isinstance(items, list):
            raise BackupCLIError(
                "La respuesta del Project no contiene una lista de conversaciones válida."
            )

        for item in items:
            if not isinstance(item, dict):
                continue

            conversation_id = item.get("id") or item.get("conversation_id")
            if conversation_id:
                entry = dict(item)
                entry["_project_id"] = project_id
                seen[str(conversation_id)] = entry

        next_cursor = (
            payload.get("next_cursor")
            or payload.get("cursor")
            or payload.get("next")
        )

        if not next_cursor:
            break

        next_cursor = str(next_cursor)

        if next_cursor == cursor or next_cursor in seen_cursors:
            break

        seen_cursors.add(next_cursor)
        cursor = next_cursor

        if not items:
            break

    return list(seen.values())


def print_all_projects_summary(
    *,
    found: int,
    processed: int,
    completed: list[str],
    empty: list[str],
    errors: list[tuple[str, str]],
    auth_aborted: bool,
) -> None:
    print()
    print("RESUMEN GLOBAL")
    print("--------------")
    print(f"Proyectos encontrados:    {found}")
    print(f"Proyectos procesados:     {processed}")
    print(f"Proyectos completados:    {len(completed)}")
    print(f"Proyectos vacíos:         {len(empty)}")
    print(f"Proyectos con errores:    {len(errors)}")
    print(f"Abortado por auth:        {'sí' if auth_aborted else 'no'}")

    if errors:
        print()
        print("PROYECTOS CON ERRORES")
        print("---------------------")
        for name, reason in errors:
            print(f"{name} -> {reason}")


def _process_project(
    project: dict[str, Any],
    resource_delay: float,
    index: int,
    total: int,
) -> dict[str, Any]:
    from .conversations import run_download_conversations

    name = project["name"]
    project_errors: list[str] = []
    is_empty = False

    print()
    print("-" * 50)
    print(f"[{index}/{total}] {name}")
    print("-" * 50)

    try:
        print()
        print("Conversaciones...")
        conversation_result = run_download_conversations(
            name,
            project_record=project,
            allow_empty=True,
        )
        is_empty = conversation_result == 2
        if conversation_result == 1:
            project_errors.append("la descarga de conversaciones tuvo fallos")
    except Exception as exc:
        if is_global_authentication_error(exc):
            print(f"ABORTADO POR AUTENTICACIÓN: {exc}")
            return {"name": name, "status": "auth_abort", "errors": project_errors}
        project_errors.append(f"conversaciones: {type(exc).__name__}: {exc}")
        print(f"ERROR EN CONVERSACIONES: {type(exc).__name__}: {exc}")

    inventory_ready = False
    try:
        print()
        print("Inventario...")
        inventory_result = run_inventory(name, allow_empty=is_empty)
        inventory_ready = inventory_result == 0
        if inventory_result != 0:
            project_errors.append(f"inventario: código {inventory_result}")
    except Exception as exc:
        project_errors.append(f"inventario: {type(exc).__name__}: {exc}")
        print(f"ERROR EN INVENTARIO: {type(exc).__name__}: {exc}")

    if inventory_ready:
        try:
            print()
            print("Recursos...")
            resource_result = run_download_resources_resolved(
                name,
                resource_delay=resource_delay,
                raise_on_auth_abort=True,
            )
            if resource_result != 0:
                project_errors.append(
                    f"la descarga de recursos terminó con código {resource_result}"
                )
        except Exception as exc:
            if is_global_authentication_error(exc):
                print(f"ABORTADO POR AUTENTICACIÓN: {exc}")
                return {"name": name, "status": "auth_abort", "errors": project_errors}
            project_errors.append(f"recursos: {type(exc).__name__}: {exc}")
            print(f"ERROR EN RECURSOS: {type(exc).__name__}: {exc}")
    else:
        print()
        print("Recursos... OMITIDO porque el inventario no está disponible.")

    if project_errors:
        status = "error"
        print()
        print(f"PROYECTO CON ERRORES: {name}")
    elif is_empty:
        status = "empty"
        print()
        print(f"PROYECTO VACÍO: {name}")
    else:
        status = "completed"
        print()
        print(f"PROYECTO COMPLETADO: {name}")

    return {"name": name, "status": status, "errors": project_errors}


def run_download_all_projects(
    resource_delay: float = RESOURCE_DELAY_SECONDS,
    workers: int = 1,
) -> int:
    if resource_delay < 0:
        raise BackupCLIError("--resource-delay no puede ser negativo.")

    if workers < 1:
        raise BackupCLIError("--workers debe ser mayor o igual que 1.")

    logs_dir = get_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_time = datetime.now()
    while True:
        log_path = logs_dir / f"download_all_projects_{log_time:%Y%m%d_%H%M%S}.log"
        try:
            with log_path.open("x", encoding="utf-8"):
                pass
            break
        except FileExistsError:
            log_time += timedelta(seconds=1)
    log_lock = Lock()

    def log_event(worker_id: str, project_name: str, event: str) -> None:
        project_name = " ".join(project_name.splitlines())
        event = " ".join(event.splitlines())
        with log_lock:
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(
                    f"{datetime.now():%H:%M:%S} [{worker_id}] [{project_name}] {event}\n"
                )

    log_event("MAIN", "-", "EJECUCION_INICIO")

    found = 0
    processed = 0
    completed: list[str] = []
    empty: list[str] = []
    errors: list[tuple[str, str]] = []
    auth_aborted = False

    print()
    print(APP_NAME)
    print("=" * len(APP_NAME))
    print(f"Versión: {VERSION}")
    print()
    if workers == 1:
        print("DESCARGA SECUENCIAL DE TODOS LOS PROJECTS")
    else:
        print("DESCARGA CONCURRENTE DE TODOS LOS PROJECTS")
    print("------------------------------------------")

    try:
        cookie = load_cookie()
        token, _session = get_session(cookie)
        payload = get_projects_payload(cookie, token)
        projects = get_ordered_project_records(payload)
        found = len(projects)
    except Exception as exc:
        auth_aborted = is_global_authentication_error(exc)
        marker = "AUTH_ABORT" if auth_aborted else "ERROR"
        log_event("MAIN", "-", f"{marker} -> {type(exc).__name__}: {exc}")
        print(f"No se pudo obtener la lista de Projects: {type(exc).__name__}: {exc}")
        print_all_projects_summary(
            found=found,
            processed=processed,
            completed=completed,
            empty=empty,
            errors=errors,
            auth_aborted=auth_aborted,
        )
        return 2 if auth_aborted else 1

    total = len(projects)
    next_index = 0
    active = set()
    worker_ids = count(1)
    worker_context = local()

    def initialize_worker() -> None:
        worker_context.worker_id = f"W{next(worker_ids)}"

    def process_with_worker_label(
        project: dict[str, Any],
        resource_delay: float,
        index: int,
        total: int,
    ) -> dict[str, Any]:
        worker_id = worker_context.worker_id if workers > 1 else "MAIN"
        name = project["name"]
        log_event(worker_id, name, "ASIGNADO")
        log_event(worker_id, name, "INICIO")
        if workers > 1:
            print(f"[{worker_id}] INICIO -> {name}", flush=True)
        try:
            result = _process_project(project, resource_delay, index, total)
        except BaseException as exc:
            log_event(worker_id, name, f"ERROR -> {type(exc).__name__}: {exc}")
            if workers > 1:
                print(f"[{worker_id}] ERROR -> {name}", flush=True)
            raise
        marker = {
            "completed": "FIN",
            "empty": "FIN",
            "error": "ERROR",
            "auth_abort": "AUTH_ABORT",
        }[result["status"]]
        event = marker
        if marker == "ERROR":
            event += " -> " + "; ".join(result["errors"])
        log_event(worker_id, name, event)
        if workers > 1:
            print(f"[{worker_id}] {marker} -> {name}", flush=True)
        return result

    executor = (
        ThreadPoolExecutor(max_workers=workers, initializer=initialize_worker)
        if workers > 1 else None
    )
    try:
        while next_index < total or active:
            if executor is None:
                project = projects[next_index]
                next_index += 1
                results = [process_with_worker_label(project, resource_delay, next_index, total)]
            else:
                while not auth_aborted and next_index < total and len(active) < workers:
                    project = projects[next_index]
                    next_index += 1
                    active.add(executor.submit(
                        process_with_worker_label, project, resource_delay, next_index, total
                    ))
                if not active:
                    break
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                active.difference_update(done)
                results = [future.result() for future in done if not future.cancelled()]

            for result in results:
                processed += 1
                name = result["name"]
                status = result["status"]
                if status == "auth_abort":
                    auth_aborted = True
                elif status == "error":
                    errors.append((name, "; ".join(result["errors"])))
                elif status == "empty":
                    empty.append(name)
                else:
                    completed.append(name)

            if auth_aborted:
                next_index = total
                for future in list(active):
                    if future.cancel():
                        active.remove(future)
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    print_all_projects_summary(
        found=found,
        processed=processed,
        completed=completed,
        empty=empty,
        errors=errors,
        auth_aborted=auth_aborted,
    )

    if auth_aborted:
        return 2
    return 1 if errors else 0
