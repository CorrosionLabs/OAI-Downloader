from __future__ import annotations

from collections import Counter
from typing import Any
import json

from . import database
from .auth import get_session, load_cookie
from .config import APP_NAME, VERSION
from .errors import BackupCLIError
from .http import request_binary_range
from .inventory import extract_file_id_from_pointer
from .paths import get_backup_paths
from .resources import resolve_file_download, resolve_library_to_file_id
from .state import save_json


def _scope_resources(project_name: str | None) -> list[dict[str, Any]]:
    project_id = (
        database.get_project_id_by_name(project_name)
        if project_name is not None else None
    )
    if project_name is not None and project_id is None:
        return []
    return database.get_scope_resources(project_id)


def run_audit_failures(project_name: str | None = None) -> int:
    paths = get_backup_paths(project_name)
    resources = _scope_resources(project_name)
    if not resources:
        raise BackupCLIError(
            "No hay recursos indexados en SQLite.\n"
            "Ejecuta primero: python main.py --inventory"
        )
    completed = {
        str(resource["identifier"]): resource
        for resource in resources
        if resource.get("download_status") == "downloaded"
    }
    failed = {
        str(resource.get("file_id") or resource["identifier"]): {
            "error_type": resource.get("error_type"),
            "error": resource.get("error_message"),
        }
        for resource in resources
        if resource.get("download_status") == "failed"
    }

    # Índice del inventario por file_id.
    by_file_id: dict[str, list[dict[str, Any]]] = {}
    library_only: list[dict[str, Any]] = []
    no_pointer_no_library: list[dict[str, Any]] = []

    for resource in resources:
        if not isinstance(resource, dict):
            continue

        pointer = resource.get("asset_pointer")
        file_id = str(resource.get("file_id") or "").strip() or (
            extract_file_id_from_pointer(pointer) if pointer else None
        )
        library_file_id = resource.get("library_file_id")

        if file_id:
            by_file_id.setdefault(file_id, []).append(resource)
        elif library_file_id:
            library_only.append(resource)
        else:
            no_pointer_no_library.append(resource)

    error_counter: Counter[str] = Counter()
    kind_counter: Counter[str] = Counter()
    mime_counter: Counter[str] = Counter()
    candidate_context_counter: Counter[int] = Counter()

    failed_rows: list[dict[str, Any]] = []

    for file_id, failure in failed.items():
        if not isinstance(failure, dict):
            failure = {"error": str(failure)}

        error_text = str(failure.get("error") or "")
        error_type = str(failure.get("error_type") or "unknown")

        if "HTTP 403" in error_text or "Forbidden" in error_text:
            error_class = "403 Forbidden"
        elif "HTTP 404" in error_text:
            error_class = "404 Not Found"
        elif "HTTP 429" in error_text:
            error_class = "429 Rate Limit"
        elif "timeout" in error_text.lower():
            error_class = "Timeout"
        else:
            error_class = error_type

        error_counter[error_class] += 1

        candidates = by_file_id.get(file_id, [])

        contexts = {
            str(item.get("conversation_id") or "").strip()
            for item in candidates
            if str(item.get("conversation_id") or "").strip()
        }
        candidate_context_counter[len(contexts)] += 1

        kinds = {
            str(item.get("resource_kind") or "unknown")
            for item in candidates
        }
        for kind in kinds:
            kind_counter[kind] += 1

        mimes = {
            str(item.get("mime_type") or "").strip()
            for item in candidates
            if str(item.get("mime_type") or "").strip()
        }
        for mime in mimes:
            mime_counter[mime] += 1

        failed_rows.append(
            {
                "file_id": file_id,
                "error_class": error_class,
                "error": error_text,
                "candidate_contexts": sorted(contexts),
                "resource_kinds": sorted(kinds),
                "mime_types": sorted(mimes),
                "library_file_ids": sorted(
                    {
                        str(item.get("library_file_id"))
                        for item in candidates
                        if item.get("library_file_id")
                    }
                ),
                "conversations": sorted(
                    {
                        str(item.get("conversation_title") or "(sin título)")
                        for item in candidates
                    }
                ),
            }
        )

    report = {
        "version": VERSION,
        "inventory_resource_references": len(resources),
        "unique_file_ids_in_inventory": len(by_file_id),
        "downloaded_resources_in_sqlite": len(completed),
        "failed_unique_file_ids": len(failed_rows),
        "library_only_references": len(library_only),
        "references_without_asset_or_library_id": len(no_pointer_no_library),
        "errors_by_class": dict(sorted(error_counter.items())),
        "failed_by_resource_kind": dict(sorted(kind_counter.items())),
        "failed_by_mime_type": dict(sorted(mime_counter.items())),
        "failed_by_candidate_context_count": {
            str(key): value
            for key, value in sorted(candidate_context_counter.items())
        },
        "library_only": library_only,
        "failed": failed_rows,
    }

    report_path = paths["root"] / "resource_failure_audit.json"
    save_json(report_path, report)

    print()
    print(APP_NAME)
    print("=" * len(APP_NAME))
    print(f"Versión: {VERSION}")
    print()
    print("AUDITORÍA DE COBERTURA")
    print("----------------------")
    print(f"Referencias totales inventario:       {len(resources)}")
    print(f"File ID únicos inventariados:         {len(by_file_id)}")
    print(f"File ID fallidos únicos:              {len(failed_rows)}")
    print(f"Referencias solo library_file_id:     {len(library_only)}")
    print(f"Sin asset_pointer ni library_file_id: {len(no_pointer_no_library)}")

    print()
    print("FALLOS POR CLASE")
    print("----------------")
    if error_counter:
        for key, value in sorted(error_counter.items()):
            print(f"{key:24} {value}")
    else:
        print("Ninguno.")

    print()
    print("FALLOS POR TIPO DE RECURSO")
    print("--------------------------")
    if kind_counter:
        for key, value in sorted(kind_counter.items()):
            print(f"{key:24} {value}")
    else:
        print("Ninguno.")

    print()
    print("CONTEXTOS DISPONIBLES EN LOS FALLOS")
    print("-----------------------------------")
    for contexts, value in sorted(candidate_context_counter.items()):
        print(f"{contexts:>3} contexto(s): {value}")

    print()
    print(f"Informe completo: {report_path}")
    print()
    print("No se ha descargado ni modificado ningún recurso.")
    return 0


def run_audit_resolution(project_name: str | None = None) -> int:
    paths = get_backup_paths(project_name)
    report_path = paths["root"] / "resolution_audit.json"
    resources = _scope_resources(project_name)
    if not resources:
        raise BackupCLIError(
            "No hay recursos indexados en SQLite.\n"
            "Ejecuta primero: python main.py --inventory"
        )

    print()
    print(APP_NAME, flush=True)
    print("=" * len(APP_NAME), flush=True)
    print(f"Versión: {VERSION}", flush=True)
    print()
    print("AUDITORÍA REAL DE RESOLUCIÓN", flush=True)
    print("----------------------------", flush=True)
    print("Preparando inventario...", flush=True)

    cookie = load_cookie()
    print("Autenticando...", flush=True)
    token, _session = get_session(cookie)
    print("Autenticación OK.", flush=True)

    resolved_ids = {
        str(resource["library_file_id"]): str(
            resource.get("resolved_file_id") or resource.get("file_id") or ""
        )
        for resource in resources
        if resource.get("library_file_id")
    }

    # Un representante por file_id y un representante por libfile que todavía no tenga file_id directo.
    direct_by_file: dict[str, dict[str, Any]] = {}
    library_only: dict[str, dict[str, Any]] = {}
    without_ids = 0

    for resource in resources:
        if not isinstance(resource, dict):
            continue
        file_id = str(resource.get("file_id") or "").strip()
        library_file_id = str(resource.get("library_file_id") or "").strip()
        if file_id:
            direct_by_file.setdefault(file_id, resource)
        elif library_file_id:
            library_only.setdefault(library_file_id, resource)
        else:
            without_ids += 1

    rows: list[dict[str, Any]] = []
    counters = Counter()
    total = len(direct_by_file) + len(library_only)

    print()
    print(f"Referencias inventariadas:       {len(resources)}", flush=True)
    print(f"file_id únicos directos:         {len(direct_by_file)}")
    print(f"library_file_id únicos sin file: {len(library_only)}")
    print(f"Sin identificador utilizable:    {without_ids}")
    print(f"Pruebas binarias máximas:        {total}")
    print()

    def test_file(file_id: str, resource: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        nonlocal token

        def _attempt() -> tuple[str, dict[str, Any]]:
            download_url, context_used, endpoint_type = resolve_file_download(
                file_id=file_id, resource=resource, cookie=cookie, token=token
            )
            binary_status, binary_bytes, headers = request_binary_range(
                download_url, cookie=cookie, token=token, timeout=30
            )
            if binary_status in (200, 206) and binary_bytes == 1:
                return "recoverable", {
                    "file_id": file_id,
                    "context": context_used,
                    "endpoint_type": endpoint_type,
                    "binary_http": binary_status,
                    "content_range": headers.get("content-range"),
                }
            if binary_status == 401:
                raise BackupCLIError("El binario devolvió HTTP 401.")
            return "binary_inaccessible", {
                "file_id": file_id,
                "context": context_used,
                "endpoint_type": endpoint_type,
                "binary_http": binary_status,
                "content_range": headers.get("content-range"),
            }

        try:
            return _attempt()
        except Exception as exc:
            first_error = str(exc)

            if "401" in first_error:
                try:
                    print("      HTTP 401 -> renovando accessToken y reintentando...", flush=True)
                    token, _session = get_session(cookie)
                    return _attempt()
                except Exception as retry_exc:
                    retry_text = str(retry_exc)
                    if "401" in retry_text:
                        return "unauthorized", {
                            "file_id": file_id,
                            "error": f"{type(retry_exc).__name__}: {retry_exc}",
                        }
                    exc = retry_exc

            error_text = str(exc)
            if "429" in error_text:
                status = "rate_limited"
            elif "403" in error_text:
                status = "forbidden"
            elif "404" in error_text:
                status = "not_found"
            else:
                status = "unresolved"
            return status, {"file_id": file_id, "error": f"{type(exc).__name__}: {exc}"}

    current = 0
    consecutive_unauthorized = 0
    auth_abort = False

    for file_id, resource in direct_by_file.items():
        current += 1
        print(f"[{current}/{total}] file_id {file_id} ...", flush=True)
        status, detail = test_file(file_id, resource)
        counters[status] += 1

        if status == "unauthorized":
            consecutive_unauthorized += 1
        else:
            consecutive_unauthorized = 0

        rows.append({
            "source": "direct_file_id",
            "status": status,
            "library_file_id": resource.get("library_file_id"),
            "conversation_id": resource.get("conversation_id"),
            "gizmo_id": resource.get("gizmo_id"),
            "name": resource.get("name"),
            **detail,
        })

        if consecutive_unauthorized >= 5:
            auth_abort = True
            print()
            print("ABORTADO: 5 recursos consecutivos siguen devolviendo HTTP 401", flush=True)
            print("incluso después de renovar el accessToken.", flush=True)
            print("La auditoría no seguirá clasificando recursos como perdidos.", flush=True)
            break

    if not auth_abort:
        library_iterator = library_only.items()
    else:
        library_iterator = []

    for library_file_id, resource in library_iterator:
        current += 1
        print(f"[{current}/{total}] libfile {library_file_id} ...", flush=True)
        known_file_id = str(resolved_ids.get(library_file_id) or "").strip()
        resolution_source = "historical" if known_file_id else "library"
        file_id = known_file_id

        if not file_id:
            try:
                file_id = resolve_library_to_file_id(
                    library_file_id=library_file_id, cookie=cookie, token=token
                )
            except Exception as exc:
                first_text = str(exc)
                if "401" in first_text:
                    try:
                        print("      HTTP 401 -> renovando accessToken y reintentando Library...", flush=True)
                        token, _session = get_session(cookie)
                        file_id = resolve_library_to_file_id(
                            library_file_id=library_file_id, cookie=cookie, token=token
                        )
                    except Exception as retry_exc:
                        exc = retry_exc

                text = str(exc)
                if file_id:
                    pass
                elif "401" in text:
                    status = "unauthorized"
                elif "429" in text:
                    status = "rate_limited"
                elif "403" in text:
                    status = "forbidden"
                elif "404" in text:
                    status = "library_404"
                else:
                    status = "library_unresolved"

                if file_id:
                    status = ""
                else:
                    counters[status] += 1
                    rows.append({
                        "source": "library_file_id",
                        "resolution_source": resolution_source,
                        "status": status,
                        "library_file_id": library_file_id,
                        "conversation_id": resource.get("conversation_id"),
                        "gizmo_id": resource.get("gizmo_id"),
                        "name": resource.get("name"),
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    if current % 25 == 0 or current == total:
                        print(f"[{current}/{total}] recuperables={counters['recoverable']} · fallos={current-counters['recoverable']}")
                    continue

        status, detail = test_file(file_id, resource)
        counters[status] += 1
        rows.append({
            "source": "library_file_id",
            "resolution_source": resolution_source,
            "status": status,
            "library_file_id": library_file_id,
            "conversation_id": resource.get("conversation_id"),
            "gizmo_id": resource.get("gizmo_id"),
            "name": resource.get("name"),
            **detail,
        })

    report = {
        "version": VERSION,
        "project": project_name,
        "inventory_references": len(resources),
        "unique_direct_file_ids": len(direct_by_file),
        "unique_library_only": len(library_only),
        "without_usable_id": without_ids,
        "aborted_for_authentication": auth_abort,
        "summary": dict(counters),
        "results": rows,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    failed_total = sum(v for k, v in counters.items() if k != "recoverable")
    print()
    print("RESULTADO")
    print("---------")
    print(f"Recuperables ahora:         {counters['recoverable']}")
    print(f"HTTP 401 / autenticación:   {counters['unauthorized']}")
    print(f"Binario inaccesible:        {counters['binary_inaccessible']}")
    print(f"Library 404:                {counters['library_404']}")
    print(f"404 directo:                {counters['not_found']}")
    print(f"403:                        {counters['forbidden']}")
    print(f"Rate limit:                 {counters['rate_limited']}")
    print(f"Otros no resolubles:        {counters['unresolved'] + counters['library_unresolved']}")
    print(f"Total con fallo:            {failed_total}")
    print(f"Sin identificador usable:   {without_ids}")
    print(f"Informe:                    {report_path}")
    print()
    print("No se ha descargado ningún recurso completo; solo 1 byte por recurso resoluble.")
    return 0
