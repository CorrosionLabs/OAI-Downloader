from __future__ import annotations

from datetime import datetime
from datetime import timezone
from email.utils import parsedate_to_datetime
from time import monotonic, sleep
from typing import Any
import urllib.parse

from .config import BASE_URL
from .errors import BackupCLIError
from .session_log import log_event

try:
    from curl_cffi import requests
except ImportError:
    requests = None


class RequestCadence:
    def __init__(self, delay: float):
        self.delay = max(0.0, delay)
        self._last_request_at: float | None = None

    def wait(self) -> None:
        if self._last_request_at is not None:
            remaining = self.delay - (monotonic() - self._last_request_at)
            if remaining > 0:
                sleep(remaining)
        self._last_request_at = monotonic()


def _safe_endpoint(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return parsed.path or url.split("?", 1)[0]


def _log_http_response(method: str, url: str, response: Any, duration: float) -> None:
    try:
        headers = response.headers
        details = [f"{duration:.2f} s"]
        content_type = headers.get("Content-Type")
        retry_after = headers.get("Retry-After")
        content_length = headers.get("Content-Length")
        if content_type:
            details.append(str(content_type))
        if retry_after:
            details.append(f"retry-after={retry_after}")
        if content_length:
            details.append(f"size={content_length}")
        log_event(
            "HTTP",
            f"{method} {_safe_endpoint(url)} -> {response.status_code} | " + " | ".join(details),
        )
    except Exception:
        pass


def _log_network_error(method: str, url: str, exc: Exception, duration: float) -> None:
    try:
        error_type = type(exc).__name__
        log_event(
            "NETWORK",
            f"{method} {_safe_endpoint(url)} -> {error_type} ({duration:.2f} s)",
        )
    except Exception:
        pass


def request_json(
    url: str,
    *,
    cookie: str,
    token: str | None = None,
    timeout: int = 30,
    retry_rate_limit: bool = False,
    request_cadence: RequestCadence | None = None,
) -> tuple[int, Any]:
    if requests is None:
        raise BackupCLIError(
            "Falta la dependencia curl_cffi.\n"
            "Instálala con: pip install curl_cffi"
        )

    headers = {
        "Accept": "application/json",
        "Cookie": cookie,
    }

    if token:
        headers["Authorization"] = f"Bearer {token}"

    retry_delays = (2.0, 5.0, 10.0) if retry_rate_limit else ()
    for attempt in range(len(retry_delays) + 1):
        try:
            if request_cadence:
                request_cadence.wait()
            started_at = monotonic()
            response = requests.get(
                url,
                headers=headers,
                timeout=timeout,
                impersonate="chrome",
            )
            _log_http_response("GET", url, response, monotonic() - started_at)

            try:
                payload = response.json()
            except Exception:
                payload = {"raw": response.text[:500]}

            if response.status_code != 429 or attempt >= len(retry_delays):
                return response.status_code, payload

            response_headers = {
                str(key).lower(): str(value)
                for key, value in response.headers.items()
            }
            retry_after = parse_retry_after(response_headers)
            sleep(retry_after if retry_after is not None else retry_delays[attempt])
        except Exception as exc:
            _log_network_error("GET", url, exc, monotonic() - started_at if "started_at" in locals() else 0.0)
            raise BackupCLIError(f"Error de red: {exc}") from exc

    raise BackupCLIError("No se pudo completar la petición HTTP.")


def request_bytes(
    url: str,
    *,
    cookie: str | None = None,
    token: str | None = None,
    timeout: int = 60,
) -> tuple[int, bytes, dict[str, str]]:
    if requests is None:
        raise BackupCLIError(
            "Falta la dependencia curl_cffi.\n"
            "Instálala con: pip install curl_cffi"
        )

    headers = {
        "Accept": "*/*",
    }

    if cookie:
        headers["Cookie"] = cookie

    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        started_at = monotonic()
        response = requests.get(
            url,
            headers=headers,
            timeout=timeout,
            impersonate="chrome",
        )
        _log_http_response("GET", url, response, monotonic() - started_at)

        response_headers = {
            key.lower(): value
            for key, value in response.headers.items()
        }

        return response.status_code, response.content, response_headers

    except Exception as exc:
        _log_network_error("GET", url, exc, monotonic() - started_at if "started_at" in locals() else 0.0)
        raise BackupCLIError(f"Error de red: {exc}") from exc


def request_post(
    url: str,
    *,
    cookie: str,
    token: str | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, str], str]:
    if requests is None:
        raise BackupCLIError(
            "Falta la dependencia curl_cffi.\n"
            "Instálala con: pip install curl_cffi"
        )

    headers = {
        "Accept": "application/json, */*",
        "Cookie": cookie,
    }

    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        started_at = monotonic()
        response = requests.post(
            url,
            headers=headers,
            timeout=timeout,
            impersonate="chrome",
            allow_redirects=False,
        )
        _log_http_response("POST", url, response, monotonic() - started_at)
    except Exception as exc:
        _log_network_error("POST", url, exc, monotonic() - started_at if "started_at" in locals() else 0.0)
        raise BackupCLIError(f"Error de red enviando POST: {exc}") from exc

    response_headers = {
        str(key).lower(): str(value)
        for key, value in response.headers.items()
    }

    try:
        body = response.text[:1000]
    except Exception:
        body = ""

    return int(response.status_code), response_headers, body


def build_url(endpoint: str, params: dict[str, Any]) -> str:
    clean = {}
    for key, value in params.items():
        if isinstance(value, bool):
            clean[key] = str(value).lower()
        else:
            clean[key] = str(value)

    return f"{BASE_URL}{endpoint}?{urllib.parse.urlencode(clean)}"


def parse_retry_after(headers: dict[str, str]) -> float | None:
    value = str(headers.get("retry-after") or "").strip()
    if not value:
        return None

    try:
        return max(0.0, float(value))
    except ValueError:
        pass

    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def request_probe(
    url: str,
    *,
    cookie: str,
    token: str | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, str], str]:
    """Hace una petición mínima para probar resolución sin descargar el recurso completo."""
    if requests is None:
        raise BackupCLIError(
            "Falta la dependencia curl_cffi.\n"
            "Instálala con: pip install curl_cffi"
        )

    headers = {
        "Accept": "application/json, */*",
        "Cookie": cookie,
        "Range": "bytes=0-0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=timeout,
            impersonate="chrome",
            allow_redirects=False,
        )
    except Exception as exc:
        raise BackupCLIError(f"Error de red probando endpoint: {exc}") from exc

    response_headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
    content_type = response_headers.get("content-type", "")

    preview = ""
    if "json" in content_type.lower() or "text" in content_type.lower():
        try:
            preview = response.text[:300]
        except Exception:
            preview = ""

    location = response_headers.get("location")
    if location:
        preview = f"Location: {location}" + (f" | {preview}" if preview else "")

    return int(response.status_code), response_headers, preview


def request_binary_range(
    url: str,
    *,
    cookie: str,
    token: str | None = None,
    timeout: int = 30,
) -> tuple[int, int, dict[str, str]]:
    """Lee como máximo 1 byte del endpoint final para confirmar acceso binario real."""
    if requests is None:
        raise BackupCLIError(
            "Falta la dependencia curl_cffi.\n"
            "Instálala con: pip install curl_cffi"
        )

    if url.startswith("/"):
        url = f"{BASE_URL}{url}"

    headers = {
        "Accept": "*/*",
        "Cookie": cookie,
        "Range": "bytes=0-0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=timeout,
            impersonate="chrome",
            allow_redirects=True,
            stream=True,
        )
    except Exception as exc:
        raise BackupCLIError(f"Error de red probando contenido binario: {exc}") from exc

    response_headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
    sample = b""
    try:
        for chunk in response.iter_content(chunk_size=1):
            if chunk:
                sample = chunk[:1]
                break
    finally:
        try:
            response.close()
        except Exception:
            pass

    return int(response.status_code), len(sample), response_headers


def request_probe_payload(
    url: str,
    *,
    cookie: str,
    token: str | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, str], Any]:
    """Prueba un endpoint sin seguir redirecciones ni descargar el recurso completo."""
    if requests is None:
        raise BackupCLIError(
            "Falta la dependencia curl_cffi.\n"
            "Instálala con: pip install curl_cffi"
        )

    headers = {
        "Accept": "application/json, */*",
        "Cookie": cookie,
        "Range": "bytes=0-0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=timeout,
            impersonate="chrome",
            allow_redirects=False,
        )
    except Exception as exc:
        raise BackupCLIError(f"Error de red probando endpoint: {exc}") from exc

    response_headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
    payload: Any = None
    content_type = response_headers.get("content-type", "")
    if "json" in content_type.lower():
        try:
            payload = response.json()
        except Exception:
            payload = None

    return int(response.status_code), response_headers, payload


def absolute_download_url(download_url: str) -> str:
    value = str(download_url or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("/"):
        return BASE_URL + value
    raise BackupCLIError(f"Endpoint de descarga no reconocido: {value or '(vacío)'}")
