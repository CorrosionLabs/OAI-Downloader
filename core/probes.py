from __future__ import annotations

from typing import Any
import urllib.parse

from . import database
from .auth import get_session, load_cookie
from .config import APP_NAME, BASE_URL, VERSION
from .conversations import collect_conversations_summary
from .errors import BackupCLIError
from .http import (
    build_url,
    request_binary_range,
    request_bytes,
    request_probe_payload,
)
from .inventory import extract_file_id_from_pointer
from .library import find_resource_by_file_id
from .paths import get_backup_paths
from .projects import get_projects
from .resources import (
    classify_download_url,
    extract_file_id_from_location,
    get_signed_download_url,
    guess_extension,
    resolve_file_download,
)


def _sqlite_resources_payload(project_name: str | None) -> dict[str, Any]:
    project_id = (
        database.get_project_id_by_name(project_name)
        if project_name is not None else None
    )
    if project_name is not None and project_id is None:
        resources = []
    else:
        resources = database.get_scope_resources(project_id)
    if not resources:
        raise BackupCLIError(
            "No hay recursos indexados en SQLite.\n"
            "Ejecuta primero: python main.py --inventory"
        )
    return {"resources": resources}


def run_check() -> int:
    print()
    print(APP_NAME)
    print("=" * len(APP_NAME))
    print(f"Versión: {VERSION}")
    print()

    print("[1/4] Leyendo sesión local...")
    cookie = load_cookie()
    print("      Cookie encontrada.")

    print("[2/4] Validando sesión con ChatGPT...")
    token, session = get_session(cookie)

    user = session.get("user") or {}
    email = user.get("email") or "(no disponible)"
    name = user.get("name") or "(no disponible)"

    print("      Sesión válida.")
    print(f"      Usuario: {name}")
    print(f"      Email:   {email}")

    print("[3/4] Enumerando conversaciones activas...")
    active = collect_conversations_summary(cookie, token, is_archived=False)

    print("[4/4] Enumerando archivadas y Projects...")
    archived = collect_conversations_summary(cookie, token, is_archived=True)
    projects = get_projects(cookie, token)

    active_count = active["count"]
    archived_count = archived["count"]
    total_count = active_count + archived_count

    print()
    print("RESULTADO")
    print("---------")
    print("Estado:                        OK")
    print(f"Conversaciones activas:        {active_count}")
    print(f"Conversaciones archivadas:     {archived_count}")
    print(f"TOTAL conversaciones visibles: {total_count}")
    print(f"Páginas activas recorridas:    {active['pages']}")
    print(f"Páginas archivadas recorridas: {archived['pages']}")

    if active["reported_total"] is not None:
        print(f"Total activo reportado backend:{active['reported_total']:>6}")

    if archived["reported_total"] is not None:
        print(f"Total archivado backend:       {archived['reported_total']:>6}")

    if active["newest_title"]:
        print(f"Más reciente:                  {active['newest_title']}")

    if projects["count"] is not None:
        print(f"Projects visibles:             {projects['count']}")
    else:
        print("Projects visibles:             ?")
        print(f"Aviso Projects:                {projects['warning']}")

    print()
    print("No se ha descargado ni modificado ningún dato.")
    return 0


def choose_probe_resource(inventory: dict[str, Any]) -> dict[str, Any]:
    resources = inventory.get("resources") or []

    if not isinstance(resources, list):
        raise BackupCLIError("SQLite no contiene una lista válida de recursos.")

    priority = (
        "asset",
        "attachment",
        "dictation_audio",
    )

    for kind in priority:
        for resource in resources:
            if not isinstance(resource, dict):
                continue

            pointer = resource.get("asset_pointer")
            file_id = extract_file_id_from_pointer(pointer) if pointer else None

            if file_id and resource.get("resource_kind") == kind:
                result = dict(resource)
                result["_file_id"] = file_id
                return result

    raise BackupCLIError(
        "No se encontró ningún recurso con asset_pointer descargable."
    )


def run_probe_resource(project_name: str | None = None) -> int:
    paths = get_backup_paths(project_name)
    resources_dir = paths["resources"]
    inventory = _sqlite_resources_payload(project_name)

    resource = choose_probe_resource(inventory)
    file_id = resource["_file_id"]

    print()
    print(APP_NAME)
    print("=" * len(APP_NAME))
    print(f"Versión: {VERSION}")
    print()
    print("PRUEBA DE RECURSO")
    print("-----------------")
    print(f"Tipo:          {resource.get('resource_kind')}")
    print(f"Conversación:  {resource.get('conversation_title')}")
    print(f"File ID:       {file_id}")
    print(f"MIME esperado: {resource.get('mime_type') or '(desconocido)'}")
    print()

    cookie = load_cookie()
    token, _session = get_session(cookie)

    print("[1/2] Resolviendo URL firmada...")
    conversation_id = str(resource.get("conversation_id") or "").strip()

    if not conversation_id:
        raise BackupCLIError(
            "El recurso no contiene conversation_id y no puede resolverse correctamente."
        )

    signed_url, _metadata = get_signed_download_url(
        cookie=cookie,
        token=token,
        file_id=file_id,
        conversation_id=conversation_id,
    )
    print("      OK")

    print("[2/2] Descargando bytes...")
    status, data, headers = request_bytes(
        signed_url,
        cookie=cookie,
        token=token,
        timeout=120,
    )

    if status != 200:
        raise BackupCLIError(
            f"La URL firmada devolvió HTTP {status}."
        )

    if not data:
        raise BackupCLIError("La descarga devolvió 0 bytes.")

    extension = guess_extension(
        resource=resource,
        headers=headers,
    )

    resources_dir.mkdir(parents=True, exist_ok=True)
    output_path = resources_dir / f"{file_id}{extension}"
    output_path.write_bytes(data)

    print("      OK")
    print()
    print("RESULTADO")
    print("---------")
    print(f"Archivo:       {output_path}")
    print(f"Tamaño:        {len(data) / 1024:.1f} KB")
    print(f"Content-Type:  {headers.get('content-type', '(no informado)')}")
    print()
    print("Se ha descargado únicamente 1 recurso de prueba.")
    return 0


def choose_library_only_resource(inventory: dict[str, Any]) -> dict[str, Any]:
    resources = inventory.get("resources") or []

    if not isinstance(resources, list):
        raise BackupCLIError("SQLite no contiene una lista válida de recursos.")

    for resource in resources:
        if not isinstance(resource, dict):
            continue

        library_file_id = resource.get("library_file_id")
        pointer = resource.get("asset_pointer")

        if library_file_id and not pointer:
            return resource

    raise BackupCLIError(
        "No se encontró ningún recurso que dependa solo de library_file_id."
    )


def run_probe_library_resource(project_name: str | None = None) -> int:
    paths = get_backup_paths(project_name)
    library_dir = paths["library_files"]
    inventory = _sqlite_resources_payload(project_name)

    resource = choose_library_only_resource(inventory)
    library_file_id = str(resource.get("library_file_id") or "").strip()

    print()
    print(APP_NAME)
    print("=" * len(APP_NAME))
    print(f"Versión: {VERSION}")
    print()
    print("PRUEBA DE LIBRARY FILE")
    print("----------------------")
    print(f"Conversación:    {resource.get('conversation_title') or '(sin título)'}")
    print(f"Library File ID: {library_file_id}")
    print(f"Nombre:          {resource.get('name') or '(desconocido)'}")
    print(f"MIME esperado:   {resource.get('mime_type') or '(desconocido)'}")
    print()

    cookie = load_cookie()
    token, _session = get_session(cookie)

    url = f"{BASE_URL}/api/library/files/{urllib.parse.quote(library_file_id)}/download"

    print("[1/1] Descargando desde File Library...")

    status, data, headers = request_bytes(
        url,
        cookie=cookie,
        token=token,
        timeout=120,
    )

    if status != 200:
        body_preview = data[:300].decode("utf-8", errors="replace")
        raise BackupCLIError(
            f"Library devolvió HTTP {status}\n"
            f"Respuesta: {body_preview}"
        )

    if not data:
        raise BackupCLIError("La descarga devolvió 0 bytes.")

    extension = guess_extension(
        resource=resource,
        headers=headers,
    )

    library_dir.mkdir(parents=True, exist_ok=True)

    output_path = library_dir / f"{library_file_id}{extension}"
    output_path.write_bytes(data)

    print("      OK")
    print()
    print("RESULTADO")
    print("---------")
    print(f"Archivo:       {output_path}")
    print(f"Tamaño:        {len(data) / 1024:.1f} KB")
    print(f"Content-Type:  {headers.get('content-type', '(no informado)')}")
    print()
    print("Se ha descargado únicamente 1 archivo de Library como prueba.")
    return 0


def choose_resolution_probe_resources(inventory: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    resources = inventory.get("resources") or []
    if not isinstance(resources, list):
        raise BackupCLIError("SQLite no contiene una lista válida de recursos.")

    file_resource = None
    library_resource = None
    library_fallback = None

    for resource in resources:
        if not isinstance(resource, dict):
            continue
        if file_resource is None and resource.get("file_id"):
            file_resource = resource
        if resource.get("library_file_id"):
            if library_fallback is None:
                library_fallback = resource
            mime_type = str(resource.get("mime_type") or "").lower()
            if library_resource is None and not mime_type.startswith("image/"):
                library_resource = resource

    if file_resource is None:
        raise BackupCLIError("No hay ningún recurso con file_id en el inventario.")
    if library_resource is None:
        library_resource = library_fallback
    if library_resource is None:
        raise BackupCLIError("No hay ningún recurso con library_file_id en el inventario.")

    return file_resource, library_resource


def run_probe_resolution(project_name: str | None = None) -> int:
    inventory = _sqlite_resources_payload(project_name)

    resources = inventory.get("resources") or []
    if not isinstance(resources, list):
        raise BackupCLIError("SQLite no contiene una lista válida de recursos.")

    file_resources = [r for r in resources if isinstance(r, dict) and r.get("file_id")]
    library_resources = [r for r in resources if isinstance(r, dict) and r.get("library_file_id")]

    if not file_resources:
        raise BackupCLIError("No hay ningún recurso con file_id en el inventario.")
    if not library_resources:
        raise BackupCLIError("No hay ningún recurso con library_file_id en el inventario.")

    cookie = load_cookie()
    token, _session = get_session(cookie)

    print()
    print(APP_NAME)
    print("=" * len(APP_NAME))
    print(f"Versión: {VERSION}")
    print()
    print("PRUEBA DE RESOLUCIÓN")
    print("--------------------")
    print("No se descargará ningún recurso completo.")
    print()

    print("RESOLUCIÓN DE file_id DIRECTOS")
    print("------------------------------")

    direct_success = 0
    direct_failed = 0
    direct_estuary = 0
    direct_project_content = 0
    direct_binary_ok = 0

    for index, file_resource in enumerate(file_resources, start=1):
        file_id = str(file_resource.get("file_id") or "").strip()
        conversation_id = str(file_resource.get("conversation_id") or "").strip()
        gizmo_id = str(file_resource.get("gizmo_id") or "").strip()
        name = str(file_resource.get("name") or "(sin nombre)")
        kind = str(file_resource.get("kind") or file_resource.get("resource_kind") or "(sin tipo)")
        conversation_title = str(file_resource.get("conversation_title") or "(sin título)")

        probe_url = None
        context_used = "(sin contexto)"
        if conversation_id:
            probe_url = build_url(
                f"/backend-api/files/download/{urllib.parse.quote(file_id)}",
                {"conversation_id": conversation_id, "inline": False},
            )
            context_used = "conversation_id"
        elif gizmo_id:
            probe_url = build_url(
                f"/backend-api/files/{urllib.parse.quote(file_id)}/download",
                {"gizmo_id": gizmo_id},
            )
            context_used = "gizmo_id"

        status = None
        payload_status = None
        download_url = None
        endpoint_type = "(sin endpoint final)"
        binary_status = None
        binary_bytes = 0
        binary_content_range = None

        if probe_url:
            status, _headers, payload = request_probe_payload(
                probe_url, cookie=cookie, token=token, timeout=30
            )
            if isinstance(payload, dict):
                payload_status = payload.get("status")
                download_url = payload.get("download_url")
            endpoint_type = classify_download_url(download_url)

            if status == 200 and payload_status == "success" and download_url:
                binary_status, binary_bytes, binary_headers = request_binary_range(
                    download_url, cookie=cookie, token=token, timeout=30
                )
                binary_content_range = binary_headers.get("content-range")

        ok = (
            status == 200
            and payload_status == "success"
            and endpoint_type != "(sin endpoint final)"
            and binary_status in (200, 206)
            and binary_bytes == 1
        )
        if binary_status in (200, 206) and binary_bytes == 1:
            direct_binary_ok += 1

        if ok:
            direct_success += 1
        else:
            direct_failed += 1

        if endpoint_type == "estuary":
            direct_estuary += 1
        elif endpoint_type == "project-content":
            direct_project_content += 1

        print(f"[{index:02d}/{len(file_resources):02d}] {name}")
        print(f"  Tipo:      {kind}")
        print(f"  Chat:      {conversation_title}")
        print(f"  file_id:   {file_id}")
        print(f"  Contexto:  {context_used}")
        if status is None:
            print("  HTTP:      NO EJECUTADA")
        else:
            print(f"  HTTP:      {status}")
        print(f"  status:    {payload_status or '(sin status)'}")
        print(f"  Final:     {endpoint_type}")
        if download_url:
            print(f"  Endpoint:  {download_url}")
        if binary_status is None:
            print("  Binario:   NO EJECUTADO")
        else:
            range_note = f" · {binary_content_range}" if binary_content_range else ""
            print(f"  Binario:   HTTP {binary_status} · {binary_bytes} byte{range_note}")
        print()

    print("RESUMEN file_id DIRECTOS")
    print("------------------------")
    print(f"Probados:               {len(file_resources)}")
    print(f"Verificados success:    {direct_success}")
    print(f"Final en estuary:       {direct_estuary}")
    print(f"Final project-content:  {direct_project_content}")
    print(f"Binario accesible:      {direct_binary_ok}")
    print(f"Sin resolución completa:{direct_failed:>5}")
    print()

    print("RESOLUCIÓN DE library_file_id")
    print("-----------------------------")

    resolved_count = 0
    verified_count = 0
    estuary_count = 0
    project_content_count = 0
    failed_count = 0

    for index, resource in enumerate(library_resources, start=1):
        library_file_id = str(resource.get("library_file_id") or "").strip()
        conversation_id = str(resource.get("conversation_id") or "").strip()
        gizmo_id = str(resource.get("gizmo_id") or "").strip()
        name = str(resource.get("name") or "(desconocido)")
        mime_type = str(resource.get("mime_type") or "(sin MIME)")

        library_url = f"{BASE_URL}/api/library/files/{urllib.parse.quote(library_file_id)}/download"
        status, headers, _payload = request_probe_payload(
            library_url, cookie=cookie, token=token, timeout=30
        )
        location = headers.get("location")
        resolved_file_id = extract_file_id_from_location(location)

        verify_status = None
        final_download_url = None

        if resolved_file_id:
            resolved_count += 1
            verify_url = None
            if conversation_id:
                verify_url = build_url(
                    f"/backend-api/files/download/{urllib.parse.quote(resolved_file_id)}",
                    {"conversation_id": conversation_id, "inline": False},
                )
            elif gizmo_id:
                verify_url = build_url(
                    f"/backend-api/files/{urllib.parse.quote(resolved_file_id)}/download",
                    {"gizmo_id": gizmo_id},
                )

            if verify_url:
                verify_status, _verify_headers, verify_payload = request_probe_payload(
                    verify_url, cookie=cookie, token=token, timeout=30
                )
                if isinstance(verify_payload, dict):
                    final_download_url = verify_payload.get("download_url")
                    if verify_payload.get("status") == "success":
                        verified_count += 1

        endpoint_type = classify_download_url(final_download_url)
        if endpoint_type == "estuary":
            estuary_count += 1
        elif endpoint_type == "project-content":
            project_content_count += 1

        if not resolved_file_id or verify_status != 200 or endpoint_type == "(sin endpoint final)":
            failed_count += 1

        print(f"[{index:02d}/{len(library_resources):02d}] {name}")
        print(f"  MIME:      {mime_type}")
        print(f"  libfile:   {library_file_id}")
        print(f"  Library:   HTTP {status}")
        print(f"  file_id:   {resolved_file_id or 'NO RESUELTO'}")
        if verify_status is not None:
            print(f"  Verifica:  HTTP {verify_status}")
        else:
            print("  Verifica:  NO EJECUTADA")
        print(f"  Final:     {endpoint_type}")
        if final_download_url:
            print(f"  Endpoint:  {final_download_url}")
        print()

    print("RESUMEN")
    print("-------")
    print(f"Library File ID probados:   {len(library_resources)}")
    print(f"Con file_id resuelto:       {resolved_count}")
    print(f"Verificados como success:   {verified_count}")
    print(f"Final en estuary:           {estuary_count}")
    print(f"Final en project-content:   {project_content_count}")
    print(f"Sin resolución completa:    {failed_count}")
    print()
    print("No se ha guardado ni descargado ningún recurso completo.")
    return 0


def run_probe_orphan_file(known_file_id: str, project_name: str | None = None) -> int:
    inventory = _sqlite_resources_payload(project_name)

    resources = inventory.get("resources") or []
    if not isinstance(resources, list):
        raise BackupCLIError("SQLite no contiene una lista válida de recursos.")

    known_file_id = str(known_file_id or "").strip()
    if not known_file_id.startswith("file_"):
        raise BackupCLIError("El valor indicado no parece un file_id válido.")

    cookie = load_cookie()
    token, _session = get_session(cookie)

    unresolved_resource = None
    unresolved_status = None

    for resource in resources:
        if not isinstance(resource, dict):
            continue
        library_file_id = str(resource.get("library_file_id") or "").strip()
        if not library_file_id:
            continue

        library_url = f"{BASE_URL}/api/library/files/{urllib.parse.quote(library_file_id)}/download"
        status, headers, _payload = request_probe_payload(
            library_url, cookie=cookie, token=token, timeout=30
        )
        if status != 302 or not extract_file_id_from_location(headers.get("location")):
            unresolved_resource = resource
            unresolved_status = status
            break

    if unresolved_resource is None:
        raise BackupCLIError(
            "No hay ningún library_file_id sin resolución en el inventario actual."
        )

    conversation_id = str(unresolved_resource.get("conversation_id") or "").strip()
    gizmo_id = str(unresolved_resource.get("gizmo_id") or "").strip()
    library_file_id = str(unresolved_resource.get("library_file_id") or "").strip()
    name = str(unresolved_resource.get("name") or "(desconocido)")

    print()
    print(APP_NAME)
    print("=" * len(APP_NAME))
    print(f"Versión: {VERSION}")
    print()
    print("PRUEBA DE FILE_ID HUÉRFANO")
    print("--------------------------")
    print(f"Recurso:          {name}")
    print(f"Library File ID:  {library_file_id}")
    print(f"Library actual:   HTTP {unresolved_status}")
    print(f"File ID conocido: {known_file_id}")
    print()

    attempts: list[tuple[str, str]] = []
    q_file = urllib.parse.quote(known_file_id)

    if conversation_id:
        attempts.extend([
            (
                "download/{file_id} + conversation_id",
                build_url(
                    f"/backend-api/files/download/{q_file}",
                    {"conversation_id": conversation_id, "inline": False},
                ),
            ),
            (
                "{file_id}/download + conversation_id",
                build_url(
                    f"/backend-api/files/{q_file}/download",
                    {"conversation_id": conversation_id},
                ),
            ),
        ])

    if gizmo_id:
        attempts.extend([
            (
                "{file_id}/download + gizmo_id",
                build_url(
                    f"/backend-api/files/{q_file}/download",
                    {"gizmo_id": gizmo_id},
                ),
            ),
            (
                "download/{file_id} + gizmo_id",
                build_url(
                    f"/backend-api/files/download/{q_file}",
                    {"gizmo_id": gizmo_id, "inline": False},
                ),
            ),
        ])

    # Último intento: acceso directo a Estuary sin firma. Normalmente debería
    # rechazarse, pero el LAB lo prueba de forma controlada por si el blob
    # físico sigue accesible mediante el file_id.
    attempts.append((
        "estuary directo por file_id",
        build_url("/backend-api/estuary/content", {"id": known_file_id}),
    ))

    resolver_success = False
    binary_success = False

    print("RUTAS PROBADAS")
    print("--------------")

    for index, (label, url) in enumerate(attempts, start=1):
        print(f"[{index:02d}/{len(attempts):02d}] {label}")

        if label == "estuary directo por file_id":
            try:
                binary_status, binary_bytes, binary_headers = request_binary_range(
                    url, cookie=cookie, token=token, timeout=30
                )
                content_range = binary_headers.get("content-range")
                note = f" · {content_range}" if content_range else ""
                print(f"  Binario:   HTTP {binary_status} · {binary_bytes} byte{note}")
                if binary_status in (200, 206) and binary_bytes == 1:
                    binary_success = True
            except Exception as exc:
                print(f"  ERROR:     {type(exc).__name__}: {exc}")
            print()
            continue

        try:
            status, _headers, payload = request_probe_payload(
                url, cookie=cookie, token=token, timeout=30
            )
        except Exception as exc:
            print(f"  ERROR:     {type(exc).__name__}: {exc}")
            print()
            continue

        download_url = payload.get("download_url") if isinstance(payload, dict) else None
        payload_status = payload.get("status") if isinstance(payload, dict) else None
        endpoint_type = classify_download_url(download_url)

        print(f"  HTTP:      {status}")
        print(f"  status:    {payload_status or '(sin status)'}")
        print(f"  Final:     {endpoint_type}")
        if download_url:
            print(f"  Endpoint:  {download_url}")

        if status == 200 and payload_status == "success" and download_url:
            resolver_success = True
            try:
                binary_status, binary_bytes, binary_headers = request_binary_range(
                    download_url, cookie=cookie, token=token, timeout=30
                )
                content_range = binary_headers.get("content-range")
                note = f" · {content_range}" if content_range else ""
                print(f"  Binario:   HTTP {binary_status} · {binary_bytes} byte{note}")
                if binary_status in (200, 206) and binary_bytes == 1:
                    binary_success = True
            except Exception as exc:
                print(f"  Binario:   ERROR · {type(exc).__name__}: {exc}")
        else:
            print("  Binario:   NO EJECUTADO")
        print()

    print("RESULTADO")
    print("---------")
    if binary_success:
        database.set_resolved_file_id(
            str(unresolved_resource["identifier_kind"]),
            str(unresolved_resource["identifier"]),
            known_file_id,
        )
        print("Existe al menos una ruta que entrega contenido binario real.")
        print(f"Relación histórica conservada: {library_file_id} -> {known_file_id}")
    elif resolver_success:
        print("El file_id aún resuelve, pero NINGUNA ruta probada entrega contenido binario real.")
        print("Estado: referencia viva, binario inaccesible por las rutas conocidas.")
    else:
        print("El file_id ya no resuelve con ninguna de las rutas conocidas.")
    print()
    print("No se ha descargado ningún recurso completo; solo se ha leído 1 byte por ruta binaria válida.")
    return 0


def run_probe_file_id(file_id: str) -> int:
    file_id = str(file_id or "").strip()

    if not file_id.startswith("file_"):
        raise BackupCLIError("El valor indicado no parece un file_id válido.")

    found = find_resource_by_file_id(file_id)
    if found is None:
        raise BackupCLIError(
            f"No se encontró {file_id} en ninguno de los inventarios locales."
        )

    project_name, resource = found

    print()
    print(APP_NAME)
    print("=" * len(APP_NAME))
    print(f"Versión: {VERSION}")
    print()
    print("PRUEBA DIRECTA DE FILE_ID")
    print("-------------------------")
    print(f"file_id:       {file_id}")
    print(f"Project:       {project_name or '(fuera de Projects)'}")
    print(f"Conversación:  {resource.get('conversation_title') or '(sin título)'}")
    print(f"Nombre:        {resource.get('name') or '(sin nombre)'}")
    print()
    print("Se leerá como máximo 1 byte. No se descargará ni borrará nada.")
    print()

    cookie = load_cookie()
    token, _session = get_session(cookie)

    try:
        download_url, context_used, endpoint_type = resolve_file_download(
            file_id=file_id,
            resource=resource,
            cookie=cookie,
            token=token,
        )
    except Exception as exc:
        print("RESULTADO")
        print("---------")
        print(f"El file_id ya no resuelve: {type(exc).__name__}: {exc}")
        return 1

    binary_status, binary_bytes, headers = request_binary_range(
        download_url,
        cookie=cookie,
        token=token,
        timeout=30,
    )

    print("RESULTADO")
    print("---------")
    print(f"Resolver:       OK")
    print(f"Contexto:       {context_used}")
    print(f"Endpoint final: {endpoint_type}")
    print(f"HTTP binario:   {binary_status}")
    print(f"Bytes leídos:   {binary_bytes}")

    content_range = headers.get("content-range")
    if content_range:
        print(f"Content-Range:  {content_range}")

    print()

    if binary_status in (200, 206) and binary_bytes == 1:
        print("El binario físico sigue siendo accesible por file_id.")
        return 0

    print("El binario no está accesible por esta ruta.")
    return 1
