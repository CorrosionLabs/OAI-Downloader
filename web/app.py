from __future__ import annotations

import json
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from core.config import AUTO_SAFE_MODE_DEFAULT, SAFE_MODE_DEFAULT, VERSION
from web import services
from web.jobs import JobManager, WebError
from web.settings import Settings

WEB_ROOT = Path(__file__).resolve().parent


def about_information() -> dict[str, str | None]:
    try:
        data = json.loads((WEB_ROOT / "about.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def available_languages() -> dict[str, dict[str, str]]:
    languages = {}
    for file_path in sorted((WEB_ROOT / "i18n").glob("*.json")):
        try:
            translations = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = translations.get("language.name")
        if isinstance(name, str) and name.strip():
            languages[file_path.stem] = {"code": file_path.stem, "name": name.strip()}
    return languages


class CookiePayload(BaseModel):
    cookie: str = Field(min_length=1, max_length=32768, repr=False)


class StoragePayload(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class CleanupPayload(BaseModel):
    include_downloaded: bool = False


class AnalysisPayload(BaseModel):
    request_delay: float = Field(ge=0, allow_inf_nan=False)


class InventoryPayload(BaseModel):
    project_ids: list[str] = Field(default_factory=list)
    include_unassigned: bool = False


class DownloadPayload(InventoryPayload):
    mode: Literal["optimal", "manual"] = "optimal"
    workers: int = Field(default=1, ge=1)
    resource_delay: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    safe_mode: bool = SAFE_MODE_DEFAULT
    auto_safe_mode: bool = AUTO_SAFE_MODE_DEFAULT


def create_app(settings_file: Path | None = None) -> FastAPI:
    settings = Settings(settings_file or WEB_ROOT.parent / ".web" / "settings.json")
    jobs = JobManager()

    @asynccontextmanager
    async def lifespan(_app):
        settings.load()
        yield

    app = FastAPI(title="OAI-Downloader", lifespan=lifespan)
    app.state.jobs = jobs
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])
    app.mount("/static", StaticFiles(directory=WEB_ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if (request.headers.get("x-oai-ui") != "1"
                    or (origin and origin != str(request.base_url).rstrip("/"))):
                return JSONResponse({"error": "invalid_origin"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'"
        )
        return response

    @app.exception_handler(WebError)
    async def web_error(_request, exc):
        return JSONResponse({"error": str(exc)}, status_code=409 if str(exc) == "busy" else 400)

    # FastAPI's standard validation body includes submitted inputs, including cookies.
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _exc):
        return JSONResponse({"error": "invalid_input"}, status_code=422)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, lang: str = "es"):
        languages = available_languages()
        language = lang if lang in languages else ("es" if "es" in languages else next(iter(languages), "es"))
        translations = json.loads((WEB_ROOT / "i18n" / f"{language}.json").read_text(encoding="utf-8"))
        return templates.TemplateResponse(request=request, name="index.html", context={
            "language": language, "languages": list(languages.values()),
            "translations": translations, "version": VERSION, "about": about_information(),
        })

    @app.get("/api/system/storage")
    def storage():
        return settings.status()

    @app.post("/api/system/storage/select")
    def select_storage(payload: StoragePayload):
        with jobs.exclusive():
            settings.save(payload.path)
            jobs.clear()
            return settings.status()

    @app.post("/api/system/storage/browse")
    def browse_storage():
        with jobs.exclusive():
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "web.folder_picker", settings.status()["storage_root"]],
                    cwd=WEB_ROOT.parent, capture_output=True, text=True, check=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                selected = json.loads(result.stdout)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                raise WebError("storage_failed") from exc
            if not selected:
                return {**settings.status(), "cancelled": True}
            settings.save(selected)
            jobs.clear()
            return {**settings.status(), "cancelled": False}

    @app.get("/api/system/status")
    def status():
        return services.session_status()

    @app.post("/api/system/cookie")
    def cookie(payload: CookiePayload):
        with jobs.exclusive():
            return services.save_cookie(payload.cookie)

    @app.post("/api/system/connect", status_code=202)
    def connect():
        window = getattr(app.state, "webview_window", None)
        if window is None:
            raise WebError("browser_unavailable")
        return jobs.start("connection", lambda progress, _stop_requested: services.connect_webview(
            progress, window, app.state.webview_url,
        ))

    @app.post("/api/system/cleanup")
    def cleanup(payload: CleanupPayload):
        with jobs.exclusive():
            result = services.clear_local_data(
                include_downloaded=payload.include_downloaded,
            )
            jobs.clear()
            return result

    @app.post("/api/about/updates")
    def about_updates():
        return services.check_for_updates(
            about_information().get("release_api_url"), VERSION,
        )

    @app.post("/api/backup/analyze", status_code=202)
    def backup(payload: AnalysisPayload):
        settings.save_analysis_request_delay(payload.request_delay)
        return jobs.start("backup", lambda progress, _stop_requested: services.list_account_projects(
            progress, payload.request_delay,
        ))

    @app.post("/api/inventory/analyze", status_code=202)
    @app.post("/api/inventory", status_code=202)
    def inventory(payload: InventoryPayload):
        if not payload.project_ids and not payload.include_unassigned:
            raise WebError("empty_scope")
        return jobs.start("inventory", lambda progress, _stop_requested: services.inventory_local(
            progress, payload.project_ids, payload.include_unassigned,
        ))

    @app.get("/api/jobs")
    def job_list():
        return jobs.snapshot()

    @app.post("/api/download", status_code=202)
    def download(payload: DownloadPayload):
        if not payload.project_ids and not payload.include_unassigned:
            raise WebError("download_empty_scope")
        if payload.mode == "manual":
            workers = payload.workers
            resource_delay = payload.resource_delay
        else:
            scope_count = len(set(payload.project_ids)) + int(payload.include_unassigned)
            workers = min(3, max(1, scope_count))
            resource_delay = 0.5
        return jobs.start("download", lambda progress, stop_requested: services.download_resources(
            progress, payload.project_ids, payload.include_unassigned,
            resource_delay,
            workers,
            payload.safe_mode,
            payload.auto_safe_mode,
            stop_requested=stop_requested,
        ))

    @app.post("/api/download/stop")
    def stop_download():
        return jobs.request_stop("download")

    @app.get("/api/backup/overview")
    def overview():
        return services.saved_summary("web_projects_overview.json")

    @app.get("/api/inventory/overview")
    def inventory_overview():
        return services.inventory_overview()

    @app.post("/api/inventory/recover")
    def recover_inventory():
        with jobs.exclusive():
            result = services.inventory_overview()
            if not result or not result.get("usable"):
                raise WebError("download_inventory_required")
            return result

    @app.get("/api/inventory/diagnostics")
    def inventory_diagnostics():
        with jobs.exclusive():
            return services.diagnose_inventory_errors()

    @app.post("/api/conversations/diagnostics")
    def conversation_diagnostics(payload: InventoryPayload):
        with jobs.exclusive():
            return services.diagnose_conversation_downloads(
                payload.project_ids, payload.include_unassigned,
            )

    return app


app = create_app()
