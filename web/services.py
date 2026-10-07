from __future__ import annotations

import json
import re
import sqlite3
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Lock
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from core import auth, conversations, database, inventory, local_data, projects, resources
from core.errors import GlobalAuthenticationAbort
from core.config import (
    AUTO_SAFE_MODE_DEFAULT,
    AUTO_SAFE_MODE_TRANSIENT_FAILURE_THRESHOLD,
    GLOBAL_COOLDOWN_DEFAULT_SECONDS,
    SAFE_MODE_DEFAULT,
    SAFE_MODE_RESOURCE_DELAY_SECONDS,
    SAFE_MODE_WORKERS,
    RESOURCE_DELAY_SECONDS,
    VERSION,
)
from core.paths import get_backup_dir, get_backup_paths, get_cookie_file, safe_folder_name
from core.session_log import log_event
from core.state import save_json
from web.jobs import WebError


def clear_local_data(*, include_downloaded: bool) -> dict:
    try:
        return local_data.clear_local_data(include_downloaded=include_downloaded)
    except (OSError, local_data.LocalDataCleanupError) as exc:
        raise WebError("operation_failed") from exc


def check_for_updates(release_api_url: str | None, installed_version: str) -> dict:
    if not isinstance(release_api_url, str) or urlsplit(release_api_url).scheme != "https":
        raise WebError("updates_unavailable")
    try:
        request = Request(release_api_url, headers={
            "Accept": "application/json",
            "User-Agent": "OAI-Downloader",
        })
        with urlopen(request, timeout=10) as response:
            release = json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebError("updates_unavailable") from exc

    if not isinstance(release, dict):
        raise WebError("updates_unavailable")
    latest = str(release.get("tag_name") or release.get("version") or "").strip().lstrip("vV")
    if not _version_parts(latest):
        raise WebError("updates_unavailable")
    if _version_parts(latest) <= _version_parts(installed_version):
        return {"status": "current"}

    release_url = release.get("html_url") or release.get("release_url")
    if not isinstance(release_url, str) or urlsplit(release_url).scheme != "https":
        raise WebError("updates_unavailable")
    return {"status": "available", "version": latest, "release_url": release_url}


def _version_parts(value: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"\d+(?:\.\d+){0,3}", value.strip().lstrip("vV"))
    return tuple(int(part) for part in match.group().split(".")) if match else None


def public_user(session: dict) -> dict:
    user = session.get("user") or {}
    return {key: str(user.get(key) or "") for key in ("name", "email")}


def session_status() -> dict:
    found = get_cookie_file().is_file()
    result = {"cookie_found": found, "authenticated": False, "user": None, "error": None}
    if not found:
        return result
    try:
        _, session = auth.get_session(auth.load_cookie())
        result.update(authenticated=True, user=public_user(session))
    except Exception as exc:
        if auth.is_global_authentication_error(exc):
            result["error"] = "invalid_session"
    return result


def save_cookie(value: str) -> dict:
    value = value.strip()
    if value.lower().startswith("cookie:"):
        value = value.split(":", 1)[1].strip()
    if not value or "=" not in value or any(ord(char) < 32 for char in value):
        raise WebError("invalid_cookie")
    try:
        _, session = auth.get_session(value)
    except Exception as exc:
        raise WebError("invalid_session") from exc
    path = get_cookie_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(value, encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        raise WebError("cookie_save_failed") from exc
    return {"saved": True, "cookie_found": True, "authenticated": True,
            "user": public_user(session), "error": None}


def connect_webview(progress, window, local_url: str) -> dict:
    progress(phase="waiting_browser_login")
    deadline = monotonic() + 600
    visited_remote = False
    try:
        window.load_url("https://chatgpt.com")
        while monotonic() < deadline:
            current_url = window.get_current_url() or ""
            host = urlsplit(current_url).hostname
            if host == "chatgpt.com":
                visited_remote = True
                value = "; ".join(
                    f"{name}={morsel.coded_value}"
                    for cookie in window.get_cookies()
                    for name, morsel in cookie.items()
                )
                if value:
                    try:
                        return save_cookie(value)
                    except WebError as exc:
                        if str(exc) != "invalid_session":
                            raise
            elif host and host != "127.0.0.1":
                visited_remote = True
            elif visited_remote and current_url.startswith(local_url):
                raise WebError("login_cancelled")
            sleep(1.5)
        raise WebError("login_timeout")
    finally:
        try:
            window.load_url(local_url)
        except Exception:
            pass


def conversation_id(item):
    return str(item.get("id") or item.get("conversation_id") or "")


def list_account_projects(progress, request_delay: float | None = None) -> dict:
    try:
        cookie = auth.load_cookie()
        token, _ = auth.get_session(cookie)
    except Exception as exc:
        raise WebError("invalid_session") from exc
    try:
        remote_records = []

        def on_conversation_items(project, items):
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("id") or item.get("conversation_id") or "").strip()
                if not item_id:
                    continue
                remote_records.append({
                    "id": item_id,
                    "project_id": project["id"] if project else None,
                    "project_name": project["name"] if project else None,
                    "title": item.get("title"),
                    "update_time": item.get("update_time"),
                    "updated_at": item.get("updated_at"),
                })

        options = {"request_delay": request_delay} if request_delay is not None else {}
        summary = projects.collect_projects_overview(
            cookie, token, progress=progress, retry_rate_limit=True, **options,
            on_conversation_items=on_conversation_items,
        )
        conversation_status = database.classify_remote_conversations(remote_records)
        summary.update({
            "conversations_pending": conversation_status["pending"],
            "conversations_modified": conversation_status["modified"],
            "conversations_downloaded": conversation_status["downloaded"],
            "conversations_failed": conversation_status["failed"],
        })
    except Exception as exc:
        if auth.is_global_authentication_error(exc):
            raise WebError("invalid_session") from exc
        if "HTTP 429" in str(exc):
            raise WebError("rate_limited") from exc
        raise
    save_json(get_backup_dir() / "web_projects_overview.json", summary)
    return summary


def backup_account(progress, project_ids: list[str] | None = None, include_unassigned: bool = True) -> dict:
    try:
        cookie = auth.load_cookie()
        token, _ = auth.get_session(cookie)
    except Exception as exc:
        raise WebError("invalid_session") from exc
    progress(phase="general_conversations")
    general = conversations.collect_all_conversation_items(cookie, token) if include_unassigned else []
    records = projects.get_ordered_project_records(projects.get_projects_payload(cookie, token))
    selected_ids = set(project_ids) if project_ids is not None else {str(p["id"]) for p in records}
    if selected_ids - {str(p["id"]) for p in records}:
        raise WebError("invalid_scope")
    scopes = []
    project_conversation_ids = set()
    folders = set()
    for index, project in enumerate(records, start=1):
        selected = str(project["id"]) in selected_ids
        if not selected and not include_unassigned:
            continue
        progress(phase="project_conversations", current=index, total=len(records),
                 current_project=project["name"])
        items = projects.collect_project_conversation_items(cookie, token, project_id=project["id"])
        project_conversation_ids.update(conversation_id(item) for item in items)
        if not selected:
            continue
        folder = safe_folder_name(project["name"]).casefold()
        if folder in folders:
            raise WebError("project_folder_collision")
        folders.add(folder)
        paths = get_backup_paths(project["name"])
        if paths["project_metadata"].exists():
            metadata = json.loads(paths["project_metadata"].read_text(encoding="utf-8"))
            if metadata.get("id") != project["id"]:
                raise WebError("project_folder_collision")
        scopes.append((project, items, paths))
    standalone = [item for item in general if conversation_id(item) not in project_conversation_ids]
    unique_ids = {conversation_id(item) for _, items, _ in scopes for item in items}
    unique_ids.update(conversation_id(item) for item in standalone)
    summary = {
        "projects": [{"id": project["id"], "name": project["name"], "conversation_count": len(items)}
                     for project, items, _ in scopes],
        "standalone_conversations": len(standalone), "total_projects": len(scopes),
        "total_conversations": len(unique_ids), "downloaded": 0, "refreshed": 0,
        "skipped": 0, "failed": 0,
    }
    if include_unassigned:
        scopes.insert(0, (None, standalone, get_backup_paths()))
    total = sum(len(items) for _, items, _ in scopes)
    offset = 0
    for project, items, paths in scopes:
        if project:
            save_json(paths["project_metadata"], {"version": VERSION, **project})

        def update(**values):
            values.update(current=offset + values["current"], total=total,
                          phase="conversation_details", current_project=project["name"] if project else "")
            progress(**values)

        result = conversations.download_conversation_items(
            cookie, token, items, paths=paths, project=project, progress=update,
            output=lambda *args, **kwargs: None,
        )
        for key in ("downloaded", "refreshed", "skipped", "failed"):
            summary[key] += result[key]
        offset += len(items)
    summary["partial"] = summary["failed"] > 0
    summary["scopes"] = [
        {"project_id": project["id"] if project else None,
         "project_name": project["name"] if project else None,
         "conversation_ids": [conversation_id(item) for item in items]}
        for project, items, _ in scopes
    ]
    save_json(get_backup_dir() / "web_overview.json", summary)
    return summary


def inventory_local(progress, project_ids: list[str], include_unassigned: bool = False) -> dict:
    if not project_ids and not include_unassigned:
        raise WebError("empty_scope")
    downloaded = backup_account(progress, project_ids, include_unassigned)
    all_files = []
    inventory_error_files = []
    database_scopes = []
    prepared_scopes = []
    for scope in downloaded["scopes"]:
        paths = get_backup_paths(scope["project_name"])
        selected_ids = set(scope["conversation_ids"])
        files = [file for file in sorted(paths["conversations"].glob("*.json"))
                 if file.stem in selected_ids]
        prepared_scopes.append((scope, paths, files))

    inventory_total = sum(len(scope["conversation_ids"]) for scope in downloaded["scopes"])
    inventory_current = 0
    for scope, paths, files in prepared_scopes:
        scope_offset = inventory_current
        scope_name = scope["project_name"] or ""

        def update_inventory(**values):
            values.update(
                phase="inventory",
                current=scope_offset + values["current"],
                total=inventory_total,
                current_project=scope_name,
            )
            progress(**values)

        payload = inventory.build_inventory(files, progress=update_inventory)
        database_scopes.append({
            "is_root": scope["project_id"] is None,
            "project": {
                "id": scope["project_id"],
                "name": scope["project_name"],
            } if scope["project_id"] else None,
            "inventory": payload,
        })
        for name in payload["errors"]:
            inventory_error_files.append(
                str((paths["conversations"] / name).resolve())
            )
        all_files.extend(files)
        inventory_current += len(files)
    progress(phase="inventory_summary", current=inventory_current,
             total=inventory_total, current_project="")
    payload = inventory.build_inventory(all_files)
    summary = {key: payload[key] for key in (
        "conversation_files_scanned", "resource_count", "total_references",
        "counts_by_kind", "errors",
    )}
    summary["inventory_error_files"] = inventory_error_files
    summary["scope"] = {"project_ids": project_ids, "include_unassigned": include_unassigned}
    summary["downloaded"] = downloaded["downloaded"]
    summary["skipped"] = downloaded["skipped"]
    summary["download_failed"] = downloaded["failed"]
    summary["partial"] = bool(payload["errors"]) or downloaded["partial"]
    summary["current"] = inventory_current
    summary["total"] = inventory_total
    summary["database_comparison"] = database.update_inventory_index(
        scopes=database_scopes,
        conversation_count=payload["conversation_files_scanned"],
        reference_count=payload["total_references"],
        resource_count=payload["resource_count"],
        error_count=len(payload["errors"]),
        project_ids=project_ids,
        include_unassigned=include_unassigned,
        partial=summary["partial"],
        expected_conversation_count=inventory_total,
    )
    download_status = {"pending": 0, "failed": 0}
    scope_ids: list[str | None] = [None] if include_unassigned else []
    scope_ids.extend(dict.fromkeys(project_ids))
    for project_id in scope_ids:
        counts = database.get_download_status_counts(project_id)
        download_status["pending"] += counts["pending"]
        download_status["failed"] += counts["failed"]
    summary["download_status"] = download_status
    inventory_run = database.get_latest_inventory_run()
    last_seen = inventory_run["created_at"] if inventory_run else None
    summary["usable"] = inventory_scope_is_usable(
        project_ids, include_unassigned, last_seen=last_seen,
    )
    summary["diagnostic_failures"] = len(scope_download_failures(
        project_ids, include_unassigned, last_seen=last_seen,
    ))
    save_json(get_backup_dir() / "web_inventory.json", summary)
    return summary


def inventory_scope_is_usable(
    project_ids: list[str], include_unassigned: bool, *, last_seen: str | None = None,
) -> bool:
    scope_ids: list[str | None] = [None] if include_unassigned else []
    scope_ids.extend(dict.fromkeys(project_ids))
    try:
        return bool(scope_ids) and any(
            any(database.get_download_status_counts(
                project_id, last_seen=last_seen,
            ).values())
            for project_id in scope_ids
        )
    except (OSError, sqlite3.Error):
        return False


def scope_download_failures(
    project_ids: list[str], include_unassigned: bool, *, last_seen: str | None = None,
) -> list[dict]:
    scope_ids: list[str | None] = [None] if include_unassigned else []
    scope_ids.extend(dict.fromkeys(project_ids))
    failures = []
    for project_id in scope_ids:
        failures.extend(database.get_scope_resources(
            project_id, download_statuses=("failed",), last_seen=last_seen,
        ))
    return failures


def inventory_overview() -> dict | None:
    run = database.get_latest_inventory_run()
    if run is None:
        legacy = saved_summary("web_inventory.json")
        scope = legacy.get("scope") if legacy else None
        project_ids = scope.get("project_ids") if isinstance(scope, dict) else None
        include_unassigned = scope.get("include_unassigned") if isinstance(scope, dict) else False
        if (legacy and isinstance(project_ids, list)
                and all(isinstance(value, str) for value in project_ids)):
            database.backfill_latest_inventory_scope(
                project_ids=project_ids,
                include_unassigned=bool(include_unassigned),
                partial=bool(legacy.get("partial")),
                expected_conversation_count=int(
                    legacy.get("total") or legacy.get("conversation_files_scanned") or 0
                ),
                conversation_count=int(legacy.get("conversation_files_scanned") or 0),
                reference_count=int(legacy.get("total_references") or 0),
                resource_count=int(legacy.get("resource_count") or 0),
            )
            run = database.get_latest_inventory_run()
    if run is None:
        return None

    project_ids = run["project_ids"]
    include_unassigned = run["include_unassigned"]
    details = database.get_inventory_scope_details(project_ids, include_unassigned)
    projects_summary = [
        {
            "id": str(project["id"]),
            "name": str(project["name"]),
            "conversation_count": int(project["conversation_count"]),
        }
        for project in details["projects"]
    ]
    root_count = int(details["root_conversation_count"])
    conversation_count = int(run["conversation_count"])
    resource_count = int(run["resource_count"])
    expected_count = int(run["expected_conversation_count"] or conversation_count)
    try:
        failures = scope_download_failures(
            project_ids, include_unassigned, last_seen=run["created_at"],
        )
        download_status = {"pending": 0, "failed": 0}
        scope_ids: list[str | None] = [None] if include_unassigned else []
        scope_ids.extend(dict.fromkeys(project_ids))
        for project_id in scope_ids:
            counts = database.get_download_status_counts(project_id)
            download_status["pending"] += counts["pending"]
            download_status["failed"] += counts["failed"]
        usable = inventory_scope_is_usable(
            project_ids, include_unassigned, last_seen=run["created_at"],
        )
    except (OSError, sqlite3.Error):
        failures = []
        download_status = {"pending": 0, "failed": 0}
        usable = False
    return {
        "conversation_files_scanned": conversation_count,
        "resource_count": resource_count,
        "total_references": int(run["reference_count"]),
        "errors": [None] * int(run["error_count"]),
        "partial": bool(run["partial"]),
        "current": conversation_count,
        "total": expected_count,
        "scope": {
            "project_ids": project_ids,
            "include_unassigned": include_unassigned,
        },
        "database_comparison": {
            "known_conversations": conversation_count,
            "known_resources": resource_count,
            "new_conversations": 0,
            "new_resources": 0,
        },
        "diagnostic_failures": len(failures),
        "download_status": download_status,
        "usable": usable,
        "analysis": {
            "projects": projects_summary,
            "root_conversation_count": root_count,
            "standalone_conversations": root_count,
            "total_projects": len(projects_summary),
            "total_conversations": sum(
                project["conversation_count"] for project in projects_summary
            ) + root_count,
            "partial": False,
        },
    }


def download_resources(progress, project_ids: list[str], include_unassigned: bool,
                       resource_delay: float | None = None, workers: int = 1,
                       safe_mode: bool = SAFE_MODE_DEFAULT,
                       auto_safe_mode: bool = AUTO_SAFE_MODE_DEFAULT,
                       stop_requested=None) -> dict:
    if safe_mode:
        workers = SAFE_MODE_WORKERS
        resource_delay = SAFE_MODE_RESOURCE_DELAY_SECONDS
        log_event(
            "DOWNLOAD",
            f"Safe Mode activo | workers={workers} | resource_delay={resource_delay}",
        )
    else:
        log_event(
            "DOWNLOAD",
            f"Safe Mode desactivado | workers={workers} | resource_delay={resource_delay}",
        )
    if workers < 1:
        raise WebError("invalid_input")
    if not project_ids and not include_unassigned:
        raise WebError("download_empty_scope")
    overview = saved_summary("web_projects_overview.json") or {}
    records = {str(project["id"]): project for project in overview.get("projects", [])}
    if any(project_id not in records for project_id in project_ids):
        persisted = database.get_inventory_scope_details(project_ids, False)
        records.update({
            str(project["id"]): {"id": str(project["id"]), "name": str(project["name"])}
            for project in persisted["projects"]
        })
    if any(project_id not in records for project_id in project_ids):
        raise WebError("invalid_scope")
    scopes = [(None, None)] if include_unassigned else []
    scopes.extend((project_id, records[project_id]["name"]) for project_id in dict.fromkeys(project_ids))
    total = 0
    pending_scopes = []
    scope_totals = []
    scope_downloaded = []
    completed_scope_downloaded = 0
    folders = set()
    for project_id, name in scopes:
        paths = get_backup_paths(name)
        folder = paths["root"].resolve()
        if folder in folders:
            raise WebError("project_folder_collision")
        folders.add(folder)
        try:
            if project_id is not None:
                metadata = json.loads(paths["project_metadata"].read_text(encoding="utf-8"))
                if str(metadata["id"]) != project_id:
                    raise WebError("project_folder_collision")
            if not database.inventory_scope_exists(project_id):
                raise ValueError("Missing SQLite inventory scope")
            counts = database.get_download_status_counts(project_id)
            remaining = counts["pending"] + counts["failed"]
            if remaining == 0:
                completed_scope_downloaded += counts["downloaded"]
                continue
            total += remaining
            pending_scopes.append((project_id, name))
            scope_totals.append(remaining)
            scope_downloaded.append(counts["downloaded"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise WebError("download_inventory_required") from exc
    limit = min(workers, len(pending_scopes))
    auto_recovery_enabled = auto_safe_mode and not safe_mode
    effective_resource_delay = (
        resource_delay if resource_delay is not None else RESOURCE_DELAY_SECONDS
    )
    coordinator = {
        "transient_failures": 0,
        "degraded": False,
        "resource_delay": effective_resource_delay,
        "worker_limit": limit,
        "workers": workers,
        "cooldown_until": 0.0,
        "global_cooldowns": 0,
        "global_cooldown_seconds": 0.0,
    }
    summary = {"downloaded": 0, "skipped": 0, "unavailable": 0, "failed": 0,
               "partial": False, "aborted": False, "stopped_by_user": False,
               "auto_safe_mode_triggered": False,
               "effective_workers": workers,
               "effective_resource_delay": effective_resource_delay,
               "global_cooldowns": 0,
               "global_cooldown_seconds": 0.0}
    if not pending_scopes:
        summary["skipped"] = completed_scope_downloaded
        summary["workers"] = 0
        progress(phase="resource_download", current_project="", current_resource="", current=0, total=0,
                 downloaded=0, skipped=completed_scope_downloaded, unavailable=0, failed=0,
                 active_workers=0,
                 workers=workers, worker_limit=0, worker_status=[])
        return summary
    state_lock = Lock()
    states = [{"current_project": name, "current_resource": "", "current": 0,
               "total": count, "downloaded": 0, "skipped": downloaded_count, "unavailable": 0,
               "failed": 0,
               "status": "pending", "phase": "resource_download"}
              for (_, name), count, downloaded_count
              in zip(pending_scopes, scope_totals, scope_downloaded)]

    def get_effective_resource_delay() -> float:
        with state_lock:
            return coordinator["resource_delay"]

    def wait_for_global_cooldown() -> None:
        waiting = False
        while True:
            with state_lock:
                remaining = coordinator["cooldown_until"] - monotonic()
            if remaining <= 0:
                return
            if not waiting:
                log_event("COOLDOWN", f"Worker esperando | restante={remaining:.1f} s")
                waiting = True
            sleep(remaining)

    def activate_global_cooldown(seconds: float, origin: str) -> None:
        duration = max(0.0, float(seconds))
        now = monotonic()
        requested_until = now + duration
        with state_lock:
            current_until = coordinator["cooldown_until"]
            if requested_until <= current_until:
                return
            was_active = current_until > now
            coordinator["cooldown_until"] = requested_until
            coordinator["global_cooldowns"] += 1
            coordinator["global_cooldown_seconds"] += duration
            summary["global_cooldowns"] = coordinator["global_cooldowns"]
            summary["global_cooldown_seconds"] = coordinator["global_cooldown_seconds"]
            if was_active:
                log_event(
                    "COOLDOWN",
                    f"Extendido | restante={requested_until - now:.1f} s",
                )
            elif origin == "429 sin Retry-After":
                log_event(
                    "COOLDOWN",
                    f"Activado | {GLOBAL_COOLDOWN_DEFAULT_SECONDS:.1f} s | "
                    "origen=429 sin Retry-After",
                )
            else:
                log_event(
                    "COOLDOWN",
                    f"Activado | {duration:.1f} s | origen={origin}",
                )

    def on_resource_success() -> None:
        if not auto_recovery_enabled:
            return
        with state_lock:
            if coordinator["degraded"] or not coordinator["transient_failures"]:
                return
            coordinator["transient_failures"] = 0
            log_event("AUTO_SAFE", "Contador transitorio reiniciado tras recurso correcto")

    def on_transient_resource_failure() -> None:
        if not auto_recovery_enabled:
            return
        with state_lock:
            if coordinator["degraded"]:
                return
            coordinator["transient_failures"] += 1
            failures = coordinator["transient_failures"]
            log_event(
                "AUTO_SAFE",
                f"Fallo transitorio consecutivo {failures}/"
                f"{AUTO_SAFE_MODE_TRANSIENT_FAILURE_THRESHOLD}",
            )
            if failures >= AUTO_SAFE_MODE_TRANSIENT_FAILURE_THRESHOLD:
                coordinator.update(
                    degraded=True,
                    resource_delay=SAFE_MODE_RESOURCE_DELAY_SECONDS,
                    worker_limit=SAFE_MODE_WORKERS,
                    workers=SAFE_MODE_WORKERS,
                )
                summary.update(
                    auto_safe_mode_triggered=True,
                    effective_workers=SAFE_MODE_WORKERS,
                    effective_resource_delay=SAFE_MODE_RESOURCE_DELAY_SECONDS,
                )
                log_event(
                    "AUTO_SAFE",
                    "Activado | workers=1 | resource_delay=5.0",
                )

    def publish(latest):
        summary["downloaded"] = sum(state["downloaded"] for state in states)
        summary["skipped"] = completed_scope_downloaded + sum(state["skipped"] for state in states)
        summary["unavailable"] = sum(state["unavailable"] for state in states)
        summary["failed"] = sum(state["failed"] for state in states)
        active_states = [dict(state) for state in states if state["status"] == "running"]
        worker_states = {}
        for state in states:
            if state["status"] != "pending" and state.get("worker_id") is not None:
                worker_states[state["worker_id"]] = dict(state)
        progress(phase=latest["phase"], current_project=latest["current_project"],
                 current_resource=latest["current_resource"],
                 current=sum(state["current"] for state in states), total=total,
                 downloaded=summary["downloaded"], skipped=summary["skipped"],
                 unavailable=summary["unavailable"], failed=summary["failed"],
                 active_workers=len(active_states), workers=coordinator["workers"],
                 worker_limit=coordinator["worker_limit"],
                 worker_status=list(worker_states.values()))

    def run_scope(index, worker_id):
        state = states[index]
        with state_lock:
            state.update(status="running", worker_id=worker_id)
            publish(state)

        def update(**values):
            with state_lock:
                state.update(values)
                publish(state)

        options = {"resource_delay": resource_delay} if resource_delay is not None else {}
        if auto_recovery_enabled:
            options.update(
                get_resource_delay=get_effective_resource_delay,
                on_resource_success=on_resource_success,
                on_transient_resource_failure=on_transient_resource_failure,
            )
        options.update(
            wait_for_global_cooldown=wait_for_global_cooldown,
            activate_global_cooldown=activate_global_cooldown,
        )
        try:
            code = resources.run_download_resources_resolved(
                state["current_project"], progress=update, raise_on_auth_abort=True,
                stop_requested=stop_requested, **options,
            )
            with state_lock:
                state["status"] = "completed" if code == 0 else "error"
                summary["partial"] = summary["partial"] or code != 0
        except GlobalAuthenticationAbort:
            with state_lock:
                state["status"] = "error"
                summary.update(partial=True, aborted=True)
        except Exception:
            with state_lock:
                state["status"] = "error"
                summary["partial"] = True
                summary.setdefault("errors", []).append({"project_name": state["current_project"],
                                                         "error": "operation_failed"})
        finally:
            with state_lock:
                publish(state)

    with ThreadPoolExecutor(max_workers=limit) as executor:
        active = {}
        available = list(range(1, limit + 1))
        next_index = 0
        while next_index < len(pending_scopes) or active:
            with state_lock:
                while (
                    not summary["aborted"]
                    and not (stop_requested and stop_requested())
                    and available
                    and next_index < len(pending_scopes)
                    and len(active) < coordinator["worker_limit"]
                ):
                    worker_id = available.pop(0)
                    active[executor.submit(run_scope, next_index, worker_id)] = worker_id
                    next_index += 1
            if not active:
                break
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                available.append(active.pop(future))
                future.result()
    summary["stopped_by_user"] = bool(stop_requested and stop_requested())
    summary["partial"] = summary["partial"] or summary["failed"] > 0
    summary["workers"] = workers
    summary["effective_workers"] = coordinator["workers"]
    summary["effective_resource_delay"] = coordinator["resource_delay"]
    summary["global_cooldowns"] = coordinator["global_cooldowns"]
    summary["global_cooldown_seconds"] = coordinator["global_cooldown_seconds"]
    return summary


def diagnose_inventory_errors() -> dict:
    summary = saved_summary("web_inventory.json")
    if not summary:
        raise WebError("diagnosis_unavailable")
    error_files = summary.get("inventory_error_files")
    if not isinstance(error_files, list):
        raise WebError("diagnosis_unavailable")
    root = get_backup_dir().resolve()
    affected = []
    seen = set()
    counts = {"unreadable": 0, "not_object": 0, "invalid_mapping": 0, "no_longer_error": 0}
    for value in error_files:
        try:
            path = Path(value).resolve()
            relative = path.relative_to(root)
            if (path.suffix != ".json" or "conversations" not in relative.parts):
                raise ValueError("Invalid conversation path")
            if not path.is_relative_to(root):
                raise ValueError("Path outside backup")
        except (OSError, ValueError, TypeError) as exc:
            raise WebError("diagnosis_unavailable") from exc
        if path in seen:
            continue
        seen.add(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cause = "unreadable"
        else:
            if not isinstance(data, dict):
                cause = "not_object"
            elif not isinstance(data.get("mapping") or {}, dict):
                cause = "invalid_mapping"
            else:
                cause = "no_longer_error"
        counts[cause] += 1
        affected.append({"file": str(path), "cause": cause})
    return {"counts": counts, "total_errors": len(affected) - counts["no_longer_error"],
            "files": affected}


def diagnose_conversation_downloads(
    project_ids: list[str], include_unassigned: bool,
) -> dict:
    counts = {"401": 0, "403": 0, "404": 0, "429": 0, "network": 0, "other": 0}
    failures = []
    run = database.get_latest_inventory_run()
    if (run is None or bool(run["include_unassigned"]) != bool(include_unassigned)
            or set(run["project_ids"]) != set(project_ids)):
        raise WebError("operation_failed")
    try:
        resources_with_errors = scope_download_failures(
            project_ids, include_unassigned, last_seen=run["created_at"],
        )
    except (OSError, sqlite3.Error) as exc:
        raise WebError("operation_failed") from exc
    for resource in resources_with_errors:
        message = str(resource.get("error_message") or "")
        error_type = str(resource.get("error_type") or "")
        match = re.search(r"\bHTTP(?:/[\d.]+)?\s*[:=]?\s*([1-5]\d{2})\b", message, re.I)
        status = match.group(1) if match else None
        if status:
            category = status if status in counts else "other"
        elif any(term in f"{error_type} {message}".lower() for term in (
            "timeout", "timed out", "error de red", "connectionerror",
            "connection error", "connection reset", "connection refused",
            "could not connect", "couldn't connect", "failed to connect",
            "could not resolve", "couldn't resolve", "network", "dns",
            "sslerror", "ssl error", "proxyerror",
        )):
            category = "network"
        else:
            category = "other"
        counts[category] += 1
        failures.append({
            "project_name": resource.get("project_name"),
            "title": resource.get("conversation_title") or "",
            "conversation_id": resource.get("conversation_id"),
            "http_status": status,
            "error": message or error_type,
            "category": category,
        })
    return {"counts": counts, "total": len(failures), "failures": failures}


def saved_summary(name: str):
    path = get_backup_dir() / name
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None
