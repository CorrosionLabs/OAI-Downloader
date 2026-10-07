from __future__ import annotations


APP_NAME = "OpenAI Backup CLI"


TECHNICAL_LOGGING_ENABLED = True


SAFE_MODE_DEFAULT = True


SAFE_MODE_WORKERS = 1


SAFE_MODE_RESOURCE_DELAY_SECONDS = 5.0


AUTO_SAFE_MODE_DEFAULT = True


AUTO_SAFE_MODE_TRANSIENT_FAILURE_THRESHOLD = 3


GLOBAL_COOLDOWN_DEFAULT_SECONDS = 20.0


VERSION = "0.7.18"


BASE_URL = "https://chatgpt.com"


SESSION_ENDPOINT = "/api/auth/session"


CONVERSATIONS_ENDPOINT = "/backend-api/conversations"


CONVERSATION_ENDPOINT = "/backend-api/conversation/{conversation_id}"


PROJECTS_ENDPOINT = "/backend-api/gizmos/snorlax/sidebar"


PROJECT_CONVERSATIONS_ENDPOINT = "/backend-api/gizmos/{project_id}/conversations"


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/152.0.0.0 Safari/537.36"
)


RESOURCE_DELAY_SECONDS = 0.35


DEFAULT_ANALYSIS_REQUEST_DELAY = 0.5


RESOURCE_MAX_ATTEMPTS = 5


RESOURCE_BACKOFF_BASE_SECONDS = 1.0


RESOURCE_AUTH_ABORT_THRESHOLD = 3


RESOURCE_TRANSIENT_COOLDOWN_THRESHOLD = 3


RESOURCE_TRANSIENT_COOLDOWN_SECONDS = 20.0


TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}


INTERESTING_KEYWORDS = (
    "file",
    "image",
    "audio",
    "video",
    "attachment",
    "asset",
    "upload",
    "download",
    "url",
    "uri",
    "mime",
    "content_type",
    "project",
    "gizmo",
    "canvas",
    "artifact",
    "sandbox",
)
