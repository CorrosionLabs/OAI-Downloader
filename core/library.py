from __future__ import annotations

from pathlib import Path
from typing import Any
import urllib.parse

from . import database
from .auth import get_session, load_cookie
from .config import APP_NAME, BASE_URL, VERSION
from .errors import BackupCLIError
from .http import build_url, request_post
from .paths import get_backup_dir, get_backup_paths
from .resources import resolve_library_to_file_id


def _scope_resources(project_name: str | None) -> list[dict[str, Any]]:
    project_id = (
        database.get_project_id_by_name(project_name)
        if project_name is not None else None
    )
    if project_name is not None and project_id is None:
        return []
    return database.get_scope_resources(project_id)


def find_local_completed_file(
    *,
    file_id: str,
    project_name: str | None = None,
) -> Path | None:
    downloaded = database.get_downloaded_resource_by_file_id(file_id)
    if downloaded and downloaded.get("local_path"):
        candidate = Path(str(downloaded["local_path"]))
        if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
            return candidate

    return None


def collect_pdf_delete_candidates(
    project_name: str | None = None,
) -> list[dict[str, Any]]:
    resources = _scope_resources(project_name)

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for resource in resources:
        if not isinstance(resource, dict):
            continue

        file_id = str(resource.get("file_id") or "").strip()
        library_file_id = str(resource.get("library_file_id") or "").strip()
        name = str(resource.get("name") or "").strip()
        mime_type = str(resource.get("mime_type") or "").strip().lower()

        is_pdf = mime_type == "application/pdf" or name.lower().endswith(".pdf")
        if not is_pdf or not file_id:
            continue

        local_path = find_local_completed_file(
            file_id=file_id,
            project_name=project_name,
        )
        if local_path is None:
            continue

        key = (file_id, library_file_id)
        if key in seen:
            continue
        seen.add(key)

        candidates.append(
            {
                "project_name": project_name,
                "conversation_id": resource.get("conversation_id"),
                "conversation_title": resource.get("conversation_title"),
                "message_id": resource.get("message_id"),
                "resource_kind": resource.get("resource_kind"),
                "file_id": file_id,
                "library_file_id": library_file_id or None,
                "name": name or local_path.name,
                "mime_type": resource.get("mime_type"),
                "local_path": str(local_path),
                "size_bytes": local_path.stat().st_size,
            }
        )

    return candidates


def run_test_delete_pdf(project_name: str | None = None) -> int:
    print()
    print(APP_NAME)
    print("=" * len(APP_NAME))
    print(f"Versión: {VERSION}")
    print()
    print("PRUEBA SEGURA DE BORRADO DE PDF")
    print("-------------------------------")
    print("Este comando NO borra nada.")
    print()

    search_roots: list[str | None] = []

    if project_name:
        search_roots.append(project_name)
    else:
        search_roots.append(None)

        projects_root = get_backup_dir() / "projects"
        if projects_root.exists():
            for project_dir in sorted(
                (path for path in projects_root.iterdir() if path.is_dir()),
                key=lambda path: path.name.casefold(),
            ):
                search_roots.append(project_dir.name)

    all_candidates: list[dict[str, Any]] = []

    for root in search_roots:
        all_candidates.extend(collect_pdf_delete_candidates(root))

    if not all_candidates:
        raise BackupCLIError(
            "No se encontró ningún PDF que cumpla estas condiciones:\n"
            "- aparece en el índice SQLite\n"
            "- tiene file_id\n"
            "- está descargado localmente y no está vacío\n\n"
            "No se ha borrado nada."
        )

    all_candidates.sort(
        key=lambda item: (
            0 if item.get("library_file_id") else 1,
            int(item.get("size_bytes") or 0),
            str(item.get("name") or "").casefold(),
        )
    )

    candidate = all_candidates[0]

    print("CANDIDATO")
    print("---------")
    print(f"Proyecto:         {candidate.get('project_name') or '(fuera de Projects)'}")
    print(f"Conversación:     {candidate.get('conversation_title') or '(sin título)'}")
    print(f"Nombre:           {candidate.get('name') or '(sin nombre)'}")
    print(f"Tipo:             {candidate.get('resource_kind') or '(desconocido)'}")
    print(f"MIME:             {candidate.get('mime_type') or '(desconocido)'}")
    print(f"file_id:          {candidate.get('file_id')}")
    print(f"library_file_id:  {candidate.get('library_file_id') or '(no disponible)'}")
    print(f"conversation_id:  {candidate.get('conversation_id') or '(no disponible)'}")
    print(f"message_id:       {candidate.get('message_id') or '(no disponible)'}")
    print(f"Ruta local:       {candidate.get('local_path')}")
    print(f"Tamaño local:     {candidate.get('size_bytes')} bytes")
    print()
    print("RESULTADO")
    print("---------")
    if candidate.get("library_file_id"):
        print("Candidato apto para estudiar una prueba de borrado remoto de Library.")
    else:
        print(
            "El PDF está respaldado y tiene file_id, pero no dispone de "
            "library_file_id en esta referencia."
        )
    print()
    print("NO se ha enviado ninguna petición DELETE/POST de borrado.")
    return 0


def run_resolve_library_file(library_file_id: str) -> int:
    library_file_id = str(library_file_id or "").strip()

    if not library_file_id.startswith("libfile_"):
        raise BackupCLIError(
            "El valor indicado no parece un library_file_id válido."
        )

    print()
    print(APP_NAME)
    print("=" * len(APP_NAME))
    print(f"Versión: {VERSION}")
    print()
    print("RESOLUCIÓN DE LIBRARY FILE")
    print("--------------------------")
    print(f"Library File ID: {library_file_id}")
    print()
    print("No se descargará ni borrará ningún archivo.")
    print()

    cookie = load_cookie()
    token, _session = get_session(cookie)

    file_id = resolve_library_to_file_id(
        library_file_id=library_file_id,
        cookie=cookie,
        token=token,
    )

    print("RESULTADO")
    print("---------")
    print(f"library_file_id: {library_file_id}")
    print(f"file_id:         {file_id}")
    print()
    print("Resolución completada. No se ha modificado ningún dato remoto.")
    return 0


def run_delete_library_file(
    library_file_id: str,
    file_id: str,
    file_name: str,
) -> int:
    library_file_id = str(library_file_id or "").strip()
    file_id = str(file_id or "").strip()
    file_name = str(file_name or "").strip()

    if not library_file_id.startswith("libfile_"):
        raise BackupCLIError("library_file_id no válido.")

    if not file_id.startswith("file_"):
        raise BackupCLIError("file_id no válido.")

    if not file_name:
        raise BackupCLIError("El nombre del archivo no puede estar vacío.")

    print()
    print(APP_NAME)
    print("=" * len(APP_NAME))
    print(f"Versión: {VERSION}")
    print()
    print("BORRADO REMOTO DE LIBRARY FILE")
    print("------------------------------")
    print(f"Nombre:          {file_name}")
    print(f"library_file_id: {library_file_id}")
    print(f"file_id:         {file_id}")
    print()
    print("ATENCIÓN: esta operación enviará un soft delete real al servidor.")
    print()

    confirmation = input(
        'Escribe BORRAR para continuar: '
    ).strip()

    if confirmation != "BORRAR":
        print()
        print("Cancelado. No se ha enviado ninguna petición de borrado.")
        return 0

    cookie = load_cookie()
    token, _session = get_session(cookie)

    endpoint = (
        f"{BASE_URL}/backend-api/files/library/files/"
        f"{urllib.parse.quote(library_file_id)}/delete_stream"
    )

    url = build_url(
        endpoint.removeprefix(BASE_URL),
        {
            "file_id": file_id,
            "file_name": file_name,
            "soft_delete": True,
        },
    )

    print()
    print("Enviando soft delete...")

    status, headers, body = request_post(
        url,
        cookie=cookie,
        token=token,
        timeout=30,
    )

    print()
    print("RESPUESTA")
    print("---------")
    print(f"HTTP: {status}")

    request_id = headers.get("x-oai-request-id")
    if request_id:
        print(f"x-oai-request-id: {request_id}")

    if body:
        print(f"Body: {body}")

    print()

    if 200 <= status < 300:
        print("El servidor ha aceptado la petición de borrado.")
        print("Ahora conviene verificar que Library ya no resuelve el archivo.")
        return 0

    if status == 404:
        print(
            "El endpoint ha devuelto 404. No asumimos que el archivo se haya borrado."
        )
        return 1

    print("La petición no confirmó el borrado.")
    return 1


def find_resource_by_file_id(file_id: str) -> tuple[str | None, dict[str, Any]] | None:
    file_id = str(file_id or "").strip()
    resource = database.find_resource_by_file_id(file_id)
    if resource is None:
        return None
    if not resource.get("file_id"):
        resource["file_id"] = file_id
    return resource.get("project_name"), resource


def run_list_pdfs(project_name: str | None = None) -> int:
    print()
    print(APP_NAME)
    print("=" * len(APP_NAME))
    print(f"Versión: {VERSION}")
    print()
    print("LISTADO DE PDF")
    print("--------------")
    print()

    search_roots: list[str | None] = []

    if project_name:
        search_roots.append(project_name)
    else:
        search_roots.append(None)

        projects_root = get_backup_dir() / "projects"
        if projects_root.exists():
            for project_dir in sorted(
                (path for path in projects_root.iterdir() if path.is_dir()),
                key=lambda path: path.name.casefold(),
            ):
                search_roots.append(project_dir.name)

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for root in search_roots:
        resources = _scope_resources(root)

        for resource in resources:
            if not isinstance(resource, dict):
                continue

            name = str(resource.get("name") or "").strip()
            mime_type = str(resource.get("mime_type") or "").strip().lower()
            file_id = str(resource.get("file_id") or "").strip()
            library_file_id = str(resource.get("library_file_id") or "").strip()

            is_pdf = (
                mime_type == "application/pdf"
                or name.lower().endswith(".pdf")
            )

            if not is_pdf:
                continue

            key = (
                root or "",
                file_id,
                library_file_id,
            )

            if key in seen:
                continue

            seen.add(key)

            local_path = None
            size_bytes = None

            if file_id:
                local_path = find_local_completed_file(
                    file_id=file_id,
                    project_name=root,
                )

                if local_path:
                    size_bytes = local_path.stat().st_size

            rows.append(
                {
                    "project": root or "(fuera de Projects)",
                    "conversation": resource.get("conversation_title")
                    or "(sin título)",
                    "name": name or "(sin nombre)",
                    "file_id": file_id or None,
                    "library_file_id": library_file_id or None,
                    "local_path": str(local_path) if local_path else None,
                    "size_bytes": size_bytes,
                }
            )

    if not rows:
        print("No se encontraron PDF en los inventarios.")
        return 0

    rows.sort(
        key=lambda item: (
            str(item["project"]).casefold(),
            str(item["name"]).casefold(),
        )
    )

    for index, row in enumerate(rows, start=1):
        size_text = (
            f"{row['size_bytes'] / 1024 / 1024:.2f} MB"
            if row["size_bytes"] is not None
            else "(no descargado)"
        )

        print(f"[{index:03d}] {row['name']}")
        print(f"      Project:          {row['project']}")
        print(f"      Conversación:     {row['conversation']}")
        print(f"      Tamaño local:     {size_text}")
        print(f"      file_id:          {row['file_id'] or '(no disponible)'}")
        print(
            f"      library_file_id:  "
            f"{row['library_file_id'] or '(no disponible)'}"
        )
        print(
            f"      Ruta local:       "
            f"{row['local_path'] or '(no disponible)'}"
        )
        print()

    print("RESUMEN")
    print("-------")
    print(f"PDF encontrados: {len(rows)}")

    downloaded = sum(
        1 for row in rows
        if row["local_path"]
    )

    with_library = sum(
        1 for row in rows
        if row["library_file_id"]
    )

    print(f"Descargados localmente: {downloaded}")
    print(f"Con library_file_id:    {with_library}")

    return 0
