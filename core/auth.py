from __future__ import annotations

from typing import Any

from .config import BASE_URL, SESSION_ENDPOINT
from .errors import AuthenticationDownloadError, BackupCLIError, GlobalAuthenticationAbort
from .http import request_json
from .session_log import log_event


_session_cookie: str | None = None


def set_cookie(value: str) -> None:
    global _session_cookie
    _session_cookie = value


def load_cookie() -> str:
    if not _session_cookie:
        raise BackupCLIError("No hay una sesión autenticada activa.")
    return _session_cookie


def get_session(cookie: str) -> tuple[str, dict[str, Any]]:
    status, session = request_json(
        BASE_URL + SESSION_ENDPOINT,
        cookie=cookie,
        timeout=20,
    )

    if status != 200 or not isinstance(session, dict):
        raise BackupCLIError(
            f"No se pudo obtener la sesión. HTTP {status}\n"
            "La cookie puede haber caducado o ChatGPT puede haber rechazado la petición."
        )

    structure = [f"session_keys={list(session.keys())}"]
    user = session.get("user")
    if isinstance(user, dict):
        structure.append(f"user_keys={list(user.keys())}")
    account = session.get("account")
    if isinstance(account, dict):
        structure.append(f"account_keys={list(account.keys())}")
    if "accounts" in session:
        accounts = session["accounts"]
        accounts_structure = f"accounts_type={type(accounts).__name__}"
        if isinstance(accounts, list):
            accounts_structure += f" accounts_count={len(accounts)}"
            if accounts and isinstance(accounts[0], dict):
                accounts_structure += (
                    f" first_account_keys={list(accounts[0].keys())}"
                )
        structure.append(accounts_structure)
    log_event("SESSION_STRUCTURE", " | ".join(structure))

    token = session.get("accessToken")
    if not token:
        raise BackupCLIError(
            "La sesión respondió correctamente, pero no contiene accessToken."
        )

    return token, session


def session_is_valid(cookie: str) -> bool:
    try:
        get_session(cookie)
    except BackupCLIError:
        return False
    return True


def is_global_authentication_error(exc: Exception) -> bool:
    if isinstance(exc, (AuthenticationDownloadError, GlobalAuthenticationAbort)):
        return True
    message = str(exc).casefold()
    return "http 401" in message or "accessToken".casefold() in message
