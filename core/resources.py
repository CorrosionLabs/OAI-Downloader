from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
from typing import Any
import json
import re
import time
import urllib.parse

from .auth import get_session, load_cookie, session_is_valid
from . import database
from .config import (
    APP_NAME,
    BASE_URL,
    GLOBAL_COOLDOWN_DEFAULT_SECONDS,
    RESOURCE_AUTH_ABORT_THRESHOLD,
    RESOURCE_BACKOFF_BASE_SECONDS,
    RESOURCE_DELAY_SECONDS,
    RESOURCE_MAX_ATTEMPTS,
    RESOURCE_TRANSIENT_COOLDOWN_SECONDS,
    RESOURCE_TRANSIENT_COOLDOWN_THRESHOLD,
    TRANSIENT_HTTP_STATUSES,
    VERSION,
)
from .errors import (
    AuthenticationDownloadError,
    BackupCLIError,
    GlobalAuthenticationAbort,
    ResourceAccessDeniedError,
    TransientDownloadError,
)
from .http import (
    absolute_download_url,
    build_url,
    parse_retry_after,
    request_bytes,
    request_json,
    request_probe_payload,
    requests,
)
from .inventory import extract_file_id_from_pointer
from .paths import get_backup_paths, get_default_resources_dir
from .session_log import log_event


class UnavailableResourceError(BackupCLIError):
    """The resource binary is no longer available."""


def _handle_resource_auth_status(
    status: int,
    resource_id: str,
    cookie: str,
    *,
    download_url: str | None = None,
    headers: dict[str, str] | None = None,
    response: Any = None,
) -> None:
    if status not in (401, 403):
        return

    if status == 403 and download_url is not None:
        parsed_url = urllib.parse.urlparse(download_url)
        try:
            response_body = str(response.text).replace("\r", " ").replace("\n", " ")[:500]
        except Exception:
            response_body = ""
        response_headers = headers or {}
        log_event(
            "HTTP403",
            f"file_id={resource_id} | host={parsed_url.hostname or ''} | "
            f"path={parsed_url.path} | "
            f"content-type={response_headers.get('content-type', '')} | "
            f"server={response_headers.get('server', '')} | body={response_body}",
        )

    log_event("AUTH", f"HTTP {status} en recurso {resource_id}; comprobando sesion")
    if session_is_valid(cookie):
        log_event(
            "AUTH",
            f"Sesion valida; recurso {resource_id} clasificado como access_denied",
        )
        raise ResourceAccessDeniedError(
            f"Recurso {resource_id} rechazado con HTTP {status}, "
            "pero la sesion sigue siendo valida."
        )

    log_event(
        "AUTH",
        f"Sesion invalida tras HTTP {status} en recurso {resource_id}; "
        "aborto por autenticacion",
    )
    raise AuthenticationDownloadError(
        f"El recurso {resource_id} rechazo la peticion con HTTP {status}."
    )


def get_signed_download_url(
    *,
    cookie: str,
    token: str,
    file_id: str,
    conversation_id: str,
) -> tuple[str, dict[str, Any]]:
    url = build_url(
        f"/backend-api/files/download/{urllib.parse.quote(file_id)}",
        {
            "conversation_id": conversation_id,
            "inline": False,
        },
    )

    status, payload = request_json(
        url,
        cookie=cookie,
        token=token,
        timeout=30,
    )

    _handle_resource_auth_status(status, file_id, cookie)

    if status != 200 or not isinstance(payload, dict):
        raise BackupCLIError(
            f"No se pudo resolver {file_id}. HTTP {status}\n"
            f"Respuesta: {str(payload)[:300]}"
        )

    signed_url = (
        payload.get("download_url")
        or payload.get("url")
        or payload.get("signed_url")
    )

    if not isinstance(signed_url, str) or not signed_url.startswith(("http://", "https://")):
        raise BackupCLIError(
            "El backend respondió, pero no se encontró una URL firmada reconocible.\n"
            f"Claves recibidas: {', '.join(sorted(payload.keys()))}"
        )

    return signed_url, payload


def guess_extension(
    *,
    resource: dict[str, Any],
    headers: dict[str, str],
) -> str:
    name = resource.get("name")
    if isinstance(name, str):
        suffix = Path(name).suffix
        if suffix:
            return suffix

    fmt = resource.get("format")
    if isinstance(fmt, str) and fmt:
        return "." + fmt.lstrip(".")

    content_type = (
        headers.get("content-type")
        or resource.get("mime_type")
        or ""
    )

    content_type = str(content_type).split(";", 1)[0].strip().lower()

    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "audio/mp4": ".m4a",
        "audio/m4a": ".m4a",
        "audio/mpeg": ".mp3",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "video/mp4": ".mp4",
        "application/pdf": ".pdf",
        "text/plain": ".txt",
        "text/markdown": ".md",
        "application/json": ".json",
        "application/zip": ".zip",
    }

    return mapping.get(content_type, ".bin")


def is_transient_error(exc: Exception) -> bool:
    if isinstance(exc, TransientDownloadError):
        return True

    text = str(exc).casefold()
    markers = (
        "error de red",
        "timeout",
        "timed out",
        "connection reset",
        "connection aborted",
        "connection refused",
        "connection closed",
        "remote end closed",
        "recv failure",
        "send failure",
        "broken pipe",
        "could not connect",
        "temporarily unavailable",
    )
    return any(marker in text for marker in markers)


def _activate_global_cooldown(
    *,
    status: int,
    headers: dict[str, str],
    activate_global_cooldown: Callable[[float, str], None] | None,
) -> None:
    if not activate_global_cooldown:
        return

    retry_after = parse_retry_after(headers)
    if status == 429:
        seconds = (
            retry_after
            if retry_after is not None
            else GLOBAL_COOLDOWN_DEFAULT_SECONDS
        )
        origin = "429" if retry_after is not None else "429 sin Retry-After"
    elif retry_after is not None:
        seconds = retry_after
        origin = f"HTTP {status}"
    else:
        return

    try:
        activate_global_cooldown(seconds, origin)
    except Exception:
        pass


def retry_delay(exc: Exception, failed_attempt: int) -> float:
    backoff = RESOURCE_BACKOFF_BASE_SECONDS * (2 ** (failed_attempt - 1))
    retry_after = getattr(exc, "retry_after", None)
    if isinstance(retry_after, (int, float)):
        return max(backoff, float(retry_after))
    return backoff


def find_partial_file(resources_dir: Path, file_id: str) -> Path | None:
    if not resources_dir.exists():
        return None

    matches = sorted(resources_dir.glob(f"{file_id}.*.part"))
    if len(matches) > 1:
        names = ", ".join(path.name for path in matches)
        raise BackupCLIError(
            f"Hay varios archivos parciales para {file_id}: {names}"
        )
    return matches[0] if matches else None


def resource_state_key(resource: dict[str, Any]) -> str:
    pointer = resource.get("asset_pointer")
    library_file_id = resource.get("library_file_id")
    kind = resource.get("resource_kind")
    conversation_id = resource.get("conversation_id")
    message_id = resource.get("message_id")
    source = resource.get("source")

    return "|".join(
        str(value or "")
        for value in (
            kind,
            pointer,
            library_file_id,
            conversation_id,
            message_id,
            source,
        )
    )


def download_one_inventory_resource(
    *,
    resources: list[dict[str, Any]],
    cookie: str,
    token: str,
    resources_dir: Path | None = None,
) -> tuple[Path, int, str, str]:
    if resources_dir is None:
        resources_dir = get_default_resources_dir()

    if not resources:
        raise BackupCLIError("No hay candidatos para descargar el recurso.")

    first = resources[0]
    pointer = first.get("asset_pointer")
    file_id = extract_file_id_from_pointer(pointer) if pointer else None

    if not file_id:
        raise BackupCLIError("El recurso no contiene un asset_pointer descargable.")

    tried_conversations: list[str] = []
    last_error: Exception | None = None
    seen_conversations: set[str] = set()

    for resource in resources:
        conversation_id = str(resource.get("conversation_id") or "").strip()

        if not conversation_id or conversation_id in seen_conversations:
            continue

        seen_conversations.add(conversation_id)
        tried_conversations.append(conversation_id)

        try:
            signed_url, _metadata = get_signed_download_url(
                cookie=cookie,
                token=token,
                file_id=file_id,
                conversation_id=conversation_id,
            )

            status, data, headers = request_bytes(
                signed_url,
                cookie=cookie,
                token=token,
                timeout=120,
            )

            _handle_resource_auth_status(status, file_id, cookie)

            if status != 200:
                raise BackupCLIError(f"La URL firmada devolvió HTTP {status}.")

            if not data:
                raise BackupCLIError("La descarga devolvió 0 bytes.")

            extension = guess_extension(
                resource=resource,
                headers=headers,
            )

            resources_dir.mkdir(parents=True, exist_ok=True)
            output_path = resources_dir / f"{file_id}{extension}"

            if not output_path.exists() or output_path.stat().st_size != len(data):
                output_path.write_bytes(data)

            return (
                output_path,
                len(data),
                headers.get("content-type", ""),
                conversation_id,
            )

        except ResourceAccessDeniedError:
            raise
        except Exception as exc:
            last_error = exc

    if not tried_conversations:
        raise BackupCLIError(f"{file_id} no tiene conversation_id utilizable.")

    raise BackupCLIError(
        f"No se pudo descargar {file_id} usando "
        f"{len(tried_conversations)} contexto(s) de conversación. "
        f"Último error: {last_error}"
    )


def run_download_resources(project_name: str | None = None) -> int:
    return run_download_resources_resolved(project_name)


def classify_download_url(download_url: str | None) -> str:
    if not download_url:
        return "(sin endpoint final)"
    if "/project-content" in download_url:
        return "project-content"
    if "/backend-api/estuary/content" in download_url:
        return "estuary"
    return "otro"


def extract_file_id_from_location(location: str | None) -> str | None:
    if not location:
        return None

    try:
        parsed = urllib.parse.urlparse(location)
        query = urllib.parse.parse_qs(parsed.query)
        candidate = (query.get("id") or [None])[0]
        if isinstance(candidate, str) and candidate.startswith("file_"):
            return candidate
    except Exception:
        pass

    match = re.search(r"(?:^|[?&#/])(file_[A-Za-z0-9_-]+)(?:$|[?&#/])", location)
    if match:
        return match.group(1)
    return None


def resolve_file_download(
    *,
    file_id: str,
    resource: dict[str, Any],
    cookie: str,
    token: str,
    wait_for_global_cooldown: Callable[[], None] | None = None,
    activate_global_cooldown: Callable[[float, str], None] | None = None,
) -> tuple[str, str, str]:
    conversation_id = str(resource.get("conversation_id") or "").strip()
    gizmo_id = str(resource.get("gizmo_id") or "").strip()
    attempts: list[tuple[str, str]] = []

    if conversation_id:
        attempts.append((
            "conversation_id",
            build_url(
                f"/backend-api/files/download/{urllib.parse.quote(file_id)}",
                {"conversation_id": conversation_id, "inline": False},
            ),
        ))

    if gizmo_id:
        attempts.append((
            "gizmo_id",
            build_url(
                f"/backend-api/files/{urllib.parse.quote(file_id)}/download",
                {"gizmo_id": gizmo_id},
            ),
        ))

    if not attempts:
        raise BackupCLIError(f"{file_id} no tiene conversation_id ni gizmo_id utilizable.")

    errors: list[str] = []
    not_found_attempts = 0
    for context_name, url in attempts:
        try:
            if wait_for_global_cooldown:
                wait_for_global_cooldown()
            status, headers, payload = request_probe_payload(
                url, cookie=cookie, token=token, timeout=30
            )
            payload_status = payload.get("status") if isinstance(payload, dict) else None
            download_url = payload.get("download_url") if isinstance(payload, dict) else None
            if status == 200 and payload_status == "success" and isinstance(download_url, str) and download_url:
                return absolute_download_url(download_url), context_name, classify_download_url(download_url)
            if status == 403:
                parsed_url = urllib.parse.urlparse(url)
                try:
                    response_body = (
                        json.dumps(payload, ensure_ascii=False)
                        if isinstance(payload, (dict, list)) else str(payload)
                    )
                    response_body = response_body.replace("\r", " ").replace("\n", " ")[:500]
                except Exception:
                    response_body = ""
                log_event(
                    "HTTP403",
                    f"file_id={file_id} | host={parsed_url.hostname or ''} | "
                    f"path={parsed_url.path} | "
                    f"content-type={headers.get('content-type', '')} | "
                    f"server={headers.get('server', '')} | body={response_body}",
                )
            _handle_resource_auth_status(status, file_id, cookie)

            if status == 401:
                raise AuthenticationDownloadError(
                    f"El resolver rechazó {file_id} con HTTP 401."
                )
            if status == 404:
                not_found_attempts += 1
                errors.append(f"{context_name}: HTTP 404, status={payload_status!r}")
                continue
            if status in TRANSIENT_HTTP_STATUSES:
                _activate_global_cooldown(
                    status=status,
                    headers=headers,
                    activate_global_cooldown=activate_global_cooldown,
                )
                raise TransientDownloadError(
                    f"El resolver devolvió HTTP {status} para {file_id}.",
                    retry_after=parse_retry_after(headers),
                )
            errors.append(f"{context_name}: HTTP {status}, status={payload_status!r}")
        except (
            AuthenticationDownloadError,
            ResourceAccessDeniedError,
            TransientDownloadError,
        ):
            raise
        except Exception as exc:
            if is_transient_error(exc):
                raise TransientDownloadError(
                    f"Error temporal resolviendo {file_id}: {exc}"
                ) from exc
            errors.append(f"{context_name}: {type(exc).__name__}: {exc}")

    if not_found_attempts == len(attempts):
        raise UnavailableResourceError(
            f"El recurso {file_id} ya no está disponible en OpenAI (HTTP 404)."
        )
    raise BackupCLIError(
        f"No se pudo resolver {file_id}. " + "; ".join(errors)
    )


def resolve_library_to_file_id(
    *,
    library_file_id: str,
    cookie: str,
    token: str,
    wait_for_global_cooldown: Callable[[], None] | None = None,
    activate_global_cooldown: Callable[[float, str], None] | None = None,
) -> str:
    library_url = f"{BASE_URL}/api/library/files/{urllib.parse.quote(library_file_id)}/download"
    if wait_for_global_cooldown:
        wait_for_global_cooldown()
    status, headers, _payload = request_probe_payload(
        library_url, cookie=cookie, token=token, timeout=30
    )
    location = headers.get("location")
    file_id = extract_file_id_from_location(location)
    if status == 302 and file_id:
        return file_id
    _handle_resource_auth_status(status, library_file_id, cookie)

    if status == 401:
        raise AuthenticationDownloadError(
            f"Library rechazó {library_file_id} con HTTP 401."
        )
    if status == 404:
        raise UnavailableResourceError(
            f"El recurso Library {library_file_id} ya no está disponible en OpenAI (HTTP 404)."
        )
    if status in TRANSIENT_HTTP_STATUSES:
        _activate_global_cooldown(
            status=status,
            headers=headers,
            activate_global_cooldown=activate_global_cooldown,
        )
        raise TransientDownloadError(
            f"Library devolvió HTTP {status} para {library_file_id}.",
            retry_after=parse_retry_after(headers),
        )
    raise BackupCLIError(
        f"Library no resolvió {library_file_id}: HTTP {status}"
    )


def resolve_library_to_file_id_with_retries(
    *,
    library_file_id: str,
    cookie: str,
    token: str,
    wait_for_global_cooldown: Callable[[], None] | None = None,
    activate_global_cooldown: Callable[[float, str], None] | None = None,
) -> str:
    for attempt in range(1, RESOURCE_MAX_ATTEMPTS + 1):
        try:
            return resolve_library_to_file_id(
                library_file_id=library_file_id,
                cookie=cookie,
                token=token,
                wait_for_global_cooldown=wait_for_global_cooldown,
                activate_global_cooldown=activate_global_cooldown,
            )
        except (AuthenticationDownloadError, ResourceAccessDeniedError):
            raise
        except Exception as exc:
            if not is_transient_error(exc) or attempt >= RESOURCE_MAX_ATTEMPTS:
                raise
            delay = retry_delay(exc, attempt)
            print(
                f"      reintento Library {attempt + 1}/{RESOURCE_MAX_ATTEMPTS} "
                f"en {delay:.1f} s",
                flush=True,
            )
            time.sleep(delay)

    raise BackupCLIError(f"No se pudo resolver Library {library_file_id}.")


def download_resolved_file(
    *,
    file_id: str,
    download_url: str,
    resource: dict[str, Any],
    resources_dir: Path,
    cookie: str,
    token: str,
    account_id: str = "",
    wait_for_global_cooldown: Callable[[], None] | None = None,
    activate_global_cooldown: Callable[[float, str], None] | None = None,
) -> tuple[Path, int, str, int, bool]:
    if requests is None:
        raise BackupCLIError(
            "Falta la dependencia curl_cffi.\n"
            "Instálala con: pip install curl_cffi"
        )

    resources_dir.mkdir(parents=True, exist_ok=True)
    temp_path = find_partial_file(resources_dir, file_id)
    resume_offset = temp_path.stat().st_size if temp_path else 0

    request_headers = {
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    download_host = (urllib.parse.urlparse(download_url).hostname or "").lower()
    if download_host == "chatgpt.com" or download_host.endswith(".chatgpt.com"):
        request_headers["Cookie"] = cookie
    request_headers["Range"] = f"bytes={resume_offset}-"

    response = None
    try:
        if wait_for_global_cooldown:
            wait_for_global_cooldown()
        response = requests.get(
            download_url,
            headers=request_headers,
            timeout=180,
            impersonate="chrome",
            allow_redirects=True,
            stream=True,
        )
    except Exception as exc:
        raise TransientDownloadError(
            f"Error de red descargando {file_id}: {exc}"
        ) from exc

    headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
    status = int(response.status_code)

    try:
        if status == 415:
            parsed_url = urllib.parse.urlparse(download_url)
            try:
                response_body = str(response.text)[:500]
            except Exception as body_error:
                response_body = (
                    f"<no disponible: {type(body_error).__name__}: {body_error}>"
                )[:500]
            log_event(
                "HTTP415",
                f"file_id={file_id} | host={parsed_url.hostname or ''} | "
                f"path={parsed_url.path} | "
                f"content-type={headers.get('content-type', '')} | "
                f"server={headers.get('server', '')} | body={response_body}",
            )

        _handle_resource_auth_status(
            status,
            file_id,
            cookie,
            download_url=download_url,
            headers=headers,
            response=response,
        )

        if status == 401:
            raise AuthenticationDownloadError(
                f"El binario rechazó {file_id} con HTTP 401."
            )
        if status == 404:
            raise UnavailableResourceError(
                "El recurso ya no est\u00e1 disponible en OpenAI (HTTP 404)."
            )
        if status in TRANSIENT_HTTP_STATUSES:
            _activate_global_cooldown(
                status=status,
                headers=headers,
                activate_global_cooldown=activate_global_cooldown,
            )
            raise TransientDownloadError(
                f"El binario devolvió HTTP {status} para {file_id}.",
                retry_after=parse_retry_after(headers),
            )

        if status == 416 and resume_offset:
            content_range = headers.get("content-range", "")
            match = re.fullmatch(r"bytes \*/(\d+)", content_range.strip())
            total_size = int(match.group(1)) if match else None
            if total_size == resume_offset and temp_path is not None:
                output_path = temp_path.with_suffix("")
                temp_path.replace(output_path)
                return (
                    output_path,
                    total_size,
                    headers.get("content-type", ""),
                    resume_offset,
                    False,
                )

        if status not in (200, 206):
            raise BackupCLIError(f"El binario devolvió HTTP {status}.")

        restarted = bool(resume_offset and status == 200)
        write_offset = 0 if restarted else resume_offset
        expected_total: int | None = None

        if status == 206:
            content_range = headers.get("content-range", "")
            match = re.fullmatch(
                r"bytes (\d+)-(\d+)/(\d+|\*)",
                content_range.strip(),
            )
            if not match:
                raise BackupCLIError(
                    f"Content-Range no válido al descargar {file_id}: "
                    f"{content_range or '(ausente)'}"
                )
            range_start = int(match.group(1))
            range_end = int(match.group(2))
            if range_start != write_offset or range_end < range_start:
                raise BackupCLIError(
                    f"Content-Range inesperado para {file_id}: se pidió "
                    f"{write_offset} y el servidor respondió {content_range}."
                )
            if match.group(3) != "*":
                expected_total = int(match.group(3))
        else:
            content_length = headers.get("content-length")
            if content_length and content_length.isdigit():
                expected_total = int(content_length)

        if temp_path is None:
            extension = guess_extension(resource=resource, headers=headers)
            output_path = resources_dir / f"{file_id}{extension}"
            temp_path = output_path.with_suffix(output_path.suffix + ".part")
        else:
            output_path = temp_path.with_suffix("")

        mode = "wb" if write_offset == 0 else "ab"
        received = 0
        try:
            with temp_path.open(mode) as fh:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    fh.flush()
                    received += len(chunk)
        except Exception as exc:
            raise TransientDownloadError(
                f"Descarga interrumpida de {file_id}: {exc}"
            ) from exc

        final_size = temp_path.stat().st_size
        if received == 0 and final_size == 0:
            raise TransientDownloadError(
                f"La descarga de {file_id} devolvió 0 bytes."
            )
        if expected_total is not None and final_size != expected_total:
            raise TransientDownloadError(
                f"Tamaño incompleto para {file_id}: "
                f"{final_size} de {expected_total} bytes."
            )

        temp_path.replace(output_path)
        return (
            output_path,
            final_size,
            headers.get("content-type", ""),
            resume_offset,
            restarted,
        )
    finally:
        try:
            response.close()
        except Exception:
            pass


def download_resource_with_retries(
    *,
    file_id: str,
    resource: dict[str, Any],
    resources_dir: Path,
    cookie: str,
    token: str,
    account_id: str = "",
    wait_for_global_cooldown: Callable[[], None] | None = None,
    activate_global_cooldown: Callable[[float, str], None] | None = None,
) -> tuple[Path, int, str, str, str, int, bool]:
    for attempt in range(1, RESOURCE_MAX_ATTEMPTS + 1):
        try:
            # Se resuelve de nuevo en cada intento: las URLs firmadas caducan.
            download_url, context_used, endpoint_type = resolve_file_download(
                file_id=file_id,
                resource=resource,
                cookie=cookie,
                token=token,
                wait_for_global_cooldown=wait_for_global_cooldown,
                activate_global_cooldown=activate_global_cooldown,
            )
            output_path, size_bytes, content_type, resume_offset, restarted = (
                download_resolved_file(
                    file_id=file_id,
                    download_url=download_url,
                    resource=resource,
                    resources_dir=resources_dir,
                    cookie=cookie,
                    token=token,
                    account_id=account_id,
                    wait_for_global_cooldown=wait_for_global_cooldown,
                    activate_global_cooldown=activate_global_cooldown,
                )
            )
            return (
                output_path,
                size_bytes,
                content_type,
                context_used,
                endpoint_type,
                resume_offset,
                restarted,
            )
        except (AuthenticationDownloadError, ResourceAccessDeniedError):
            raise
        except Exception as exc:
            if not is_transient_error(exc) or attempt >= RESOURCE_MAX_ATTEMPTS:
                raise
            delay = retry_delay(exc, attempt)
            partial = find_partial_file(resources_dir, file_id)
            partial_size = partial.stat().st_size if partial else 0
            print(
                f"      reintento {attempt + 1}/{RESOURCE_MAX_ATTEMPTS} "
                f"en {delay:.1f} s · parcial={partial_size} bytes",
                flush=True,
            )
            time.sleep(delay)

    raise BackupCLIError(f"No se pudo descargar {file_id}.")


def run_download_resources_resolved(
    project_name: str | None = None,
    resource_delay: float = RESOURCE_DELAY_SECONDS,
    *,
    raise_on_auth_abort: bool = False,
    progress: Callable[..., None] | None = None,
    on_resource_success: Callable[[], None] | None = None,
    on_transient_resource_failure: Callable[[], None] | None = None,
    get_resource_delay: Callable[[], float] | None = None,
    wait_for_global_cooldown: Callable[[], None] | None = None,
    activate_global_cooldown: Callable[[float, str], None] | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> int:
    if resource_delay < 0:
        raise BackupCLIError("--resource-delay no puede ser negativo.")

    paths = get_backup_paths(project_name)
    resources_dir = paths["resources"]

    project_id = None
    if project_name is not None:
        try:
            metadata = json.loads(
                paths["project_metadata"].read_text(encoding="utf-8")
            )
            project_id = str(metadata["id"]).strip()
            if not project_id:
                raise ValueError("Project id vacío")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BackupCLIError(
                "No se puede identificar el proyecto en SQLite.\n"
                "Ejecuta primero el inventario del alcance."
            ) from exc

    resources = database.get_scope_resources(
        project_id,
        download_statuses=("pending", "failed"),
    )
    status_counts = database.get_download_status_counts(project_id)
    if not resources and not any(status_counts.values()):
        raise BackupCLIError(
            "No hay recursos indexados en SQLite.\n"
            "Ejecuta primero: python main.py --inventory"
        )
    if not resources:
        return 0

    cookie = load_cookie()
    token, _session = get_session(cookie)
    account = _session.get("account")
    account_id = str(account.get("id") or "").strip() if isinstance(account, dict) else ""

    print()
    print(APP_NAME)
    print("=" * len(APP_NAME))
    print(f"Versión: {VERSION}")
    print()
    print("DESCARGA RESUELTA DE RECURSOS")
    print("-----------------------------")
    print(f"Recursos pendientes o fallidos: {len(resources)}")
    print(f"Pausa entre recursos:      {resource_delay:.2f} s")
    print(f"Intentos máximos:          {RESOURCE_MAX_ATTEMPTS}")
    print()

    resolved_cache: dict[str, str] = {}
    seen_file_ids: set[str] = set()
    downloaded = 0
    skipped = status_counts["downloaded"]
    duplicates = 0
    unresolved = 0
    unavailable = 0
    errors = 0
    consecutive_unauthorized = 0
    consecutive_transient_failures = 0
    auth_aborted = False

    def report(current: int, name: str = "", phase: str = "resource_download") -> None:
        if progress:
            progress(current=current, total=len(resources), current_resource=name, phase=phase,
                     downloaded=downloaded, skipped=skipped, unavailable=unavailable,
                     failed=errors + unresolved)

    report(0)
    for index, resource in enumerate(resources, start=1):
        if stop_requested and stop_requested():
            break
        if not isinstance(resource, dict):
            continue

        name = str(resource.get("name") or "(sin nombre)")
        kind = str(resource.get("resource_kind") or "unknown")
        file_id = str(resource.get("file_id") or "").strip()
        library_file_id = str(resource.get("library_file_id") or "").strip()
        identifier_kind = str(resource["identifier_kind"])
        identifier = str(resource["identifier"])

        print(f"[{index:02d}/{len(resources):02d}] {kind} | {name}")
        display_name = name if name != "(sin nombre)" else (
            file_id or library_file_id or str(resource.get("asset_pointer") or name)
        )
        report(index - 1, display_name)

        try:
            if library_file_id and file_id:
                resolved_cache[library_file_id] = file_id
                database.set_resolved_file_id(
                    identifier_kind, identifier, file_id
                )

            if not file_id and library_file_id:
                if library_file_id in resolved_cache:
                    file_id = resolved_cache[library_file_id]
                    print(f"      histórico -> {file_id}")
                else:
                    file_id = resolve_library_to_file_id_with_retries(
                        library_file_id=library_file_id,
                        cookie=cookie,
                        token=token,
                        wait_for_global_cooldown=wait_for_global_cooldown,
                        activate_global_cooldown=activate_global_cooldown,
                    )
                    resolved_cache[library_file_id] = file_id
                    database.set_resolved_file_id(
                        identifier_kind, identifier, file_id
                    )
                    print(f"      libfile -> {file_id}")

            if name == "(sin nombre)" and file_id:
                display_name = file_id
                report(index - 1, display_name)

            if not file_id:
                unresolved += 1
                database.mark_resource_failed(
                    identifier_kind,
                    identifier,
                    file_id=None,
                    error_type="UnresolvedResource",
                    error_message="No hay file_id ni library_file_id resoluble.",
                )
                print("      NO RESUELTO")
                continue

            previous = database.get_downloaded_resource_by_file_id(file_id)
            if previous and previous.get("local_path"):
                local_path = Path(str(previous["local_path"]))
                local_invalid_reason: str | None = None
                try:
                    local_size = local_path.stat().st_size
                    local_file_is_valid = local_path.is_file() and local_size > 0
                except FileNotFoundError:
                    local_file_is_valid = False
                    local_invalid_reason = "archivo ausente"
                except OSError:
                    local_file_is_valid = False
                    local_invalid_reason = "archivo no verificable"

                if local_file_is_valid:
                    if local_size != int(previous.get("size_bytes") or 0):
                        log_event(
                            "VERIFY",
                            f"TamaÃ±o corregido desde disco | file_id={file_id}",
                        )
                    database.mark_resource_downloaded(
                        identifier_kind,
                        identifier,
                        file_id=file_id,
                        local_path=str(local_path),
                        size_bytes=local_size,
                        content_type=previous.get("content_type"),
                    )
                    skipped += 1
                    display_name = local_path.name
                    print(f"      SKIP -> {display_name}")
                    continue

                if local_invalid_reason is None:
                    if not local_path.exists():
                        local_invalid_reason = "archivo ausente"
                    elif local_path.is_file():
                        local_invalid_reason = "archivo vacÃ­o"
                    else:
                        local_invalid_reason = "ruta no es archivo"
                log_event(
                    "VERIFY",
                    f"Estado descargado pero {local_invalid_reason} | file_id={file_id}",
                )

            if file_id in seen_file_ids:
                duplicates += 1
                database.mark_resource_failed(
                    identifier_kind,
                    identifier,
                    file_id=file_id,
                    error_type="DuplicateResource",
                    error_message="El mismo file_id aparece más de una vez en el alcance.",
                )
                print(f"      DUPLICADO -> {file_id}")
                continue
            seen_file_ids.add(file_id)

            download_args = {
                "resource": resource,
                "resources_dir": resources_dir,
                "cookie": cookie,
                "token": token,
                "account_id": account_id,
                "wait_for_global_cooldown": wait_for_global_cooldown,
                "activate_global_cooldown": activate_global_cooldown,
            }
            try:
                (
                    output_path,
                    size_bytes,
                    content_type,
                    context_used,
                    endpoint_type,
                    resume_offset,
                    restarted,
                ) = download_resource_with_retries(
                    file_id=file_id,
                    **download_args,
                )
            except (
                AuthenticationDownloadError,
                ResourceAccessDeniedError,
            ):
                raise
            except Exception as file_id_error:
                if not library_file_id or is_transient_error(file_id_error):
                    raise

                try:
                    resolved_file_id = resolve_library_to_file_id_with_retries(
                        library_file_id=library_file_id,
                        cookie=cookie,
                        token=token,
                        wait_for_global_cooldown=wait_for_global_cooldown,
                        activate_global_cooldown=activate_global_cooldown,
                    )
                    log_event(
                        "LIBRARY_FALLBACK",
                        f"Resuelto | library_file_id={library_file_id} | file_id={resolved_file_id}",
                    )
                except Exception as library_error:
                    log_event(
                        "LIBRARY_FALLBACK",
                        f"Fallo | library_file_id={library_file_id} | "
                        f"error={type(library_error).__name__}: {library_error}",
                    )
                    raise file_id_error

                file_id = resolved_file_id
                resolved_cache[library_file_id] = file_id
                database.set_resolved_file_id(
                    identifier_kind, identifier, file_id
                )
                print(f"      libfile -> {file_id}")
                (
                    output_path,
                    size_bytes,
                    content_type,
                    context_used,
                    endpoint_type,
                    resume_offset,
                    restarted,
                ) = download_resource_with_retries(
                    file_id=file_id,
                    **download_args,
                )
            display_name = output_path.name

            database.mark_resource_downloaded(
                identifier_kind,
                identifier,
                file_id=file_id,
                local_path=str(output_path),
                size_bytes=size_bytes,
                content_type=content_type,
            )

            downloaded += 1
            if on_resource_success:
                try:
                    on_resource_success()
                except Exception:
                    pass
            consecutive_unauthorized = 0
            consecutive_transient_failures = 0
            resume_note = ""
            if resume_offset:
                if restarted:
                    resume_note = f" · Range no aceptado, reiniciado desde 0"
                else:
                    resume_note = f" · reanudado desde {resume_offset} bytes"
            print(
                f"      OK -> {output_path.name} "
                f"({size_bytes / 1024:.1f} KB) · {endpoint_type} · {context_used}"
                f"{resume_note}"
            )

        except UnavailableResourceError as exc:
            unavailable += 1
            database.mark_resource_failed(
                identifier_kind,
                identifier,
                file_id=file_id or None,
                error_type="UnavailableResource",
                error_message=str(exc),
            )
            print(f"      NO DISPONIBLE -> {exc}")
            consecutive_unauthorized = 0
            consecutive_transient_failures = 0

        except Exception as exc:
            if library_file_id and not file_id:
                unresolved += 1
            else:
                errors += 1
            database.mark_resource_failed(
                identifier_kind,
                identifier,
                file_id=file_id or None,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            print(f"      ERROR -> {type(exc).__name__}: {exc}")

            if isinstance(exc, AuthenticationDownloadError):
                consecutive_unauthorized += 1
            else:
                consecutive_unauthorized = 0

            transient_error = is_transient_error(exc)
            if transient_error:
                consecutive_transient_failures += 1
                if on_transient_resource_failure:
                    try:
                        on_transient_resource_failure()
                    except Exception:
                        pass
            else:
                consecutive_transient_failures = 0

            if consecutive_unauthorized >= RESOURCE_AUTH_ABORT_THRESHOLD:
                auth_aborted = True
                print()
                print(
                    "ABORTADO: tres recursos consecutivos devolvieron HTTP 401."
                )
                print(
                    "Se conserva el progreso y no se harán más peticiones."
                )
                break

            if (
                consecutive_transient_failures
                >= RESOURCE_TRANSIENT_COOLDOWN_THRESHOLD
            ):
                print(
                    f"      Pausa de protección: "
                    f"{RESOURCE_TRANSIENT_COOLDOWN_SECONDS:.0f} s tras "
                    f"{RESOURCE_TRANSIENT_COOLDOWN_THRESHOLD} fallos "
                    "transitorios consecutivos.",
                    flush=True,
                )
                report(index - 1, display_name, "resource_cooldown")
                time.sleep(RESOURCE_TRANSIENT_COOLDOWN_SECONDS)
                consecutive_transient_failures = 0
        finally:
            report(index, display_name)

        if stop_requested and stop_requested():
            break

        effective_resource_delay = resource_delay
        if get_resource_delay:
            try:
                effective_resource_delay = get_resource_delay()
            except Exception:
                effective_resource_delay = resource_delay
        if effective_resource_delay:
            report(index, display_name, "resource_wait")
            time.sleep(effective_resource_delay)

    print()
    print("RESULTADO")
    print("---------")
    print(f"Descargados ahora:      {downloaded}")
    print(f"Ya descargados:         {skipped}")
    print(f"Referencias duplicadas: {duplicates}")
    print(f"No resolubles:          {unresolved}")
    print(f"Errores de descarga:    {errors}")
    print(f"No disponibles:         {unavailable}")
    print(f"Abortado por HTTP 401:  {'sí' if auth_aborted else 'no'}")
    print(f"Recursos en:            {resources_dir}")
    print("Estado de recursos:     SQLite")

    if auth_aborted and raise_on_auth_abort:
        raise GlobalAuthenticationAbort(
            "Tres recursos consecutivos devolvieron HTTP 401."
        )

    return 0 if errors == 0 and unresolved == 0 and not auth_aborted else 1
