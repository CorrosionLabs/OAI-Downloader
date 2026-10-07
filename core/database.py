from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any

from .paths import get_backup_dir, get_database_file


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id),
    title TEXT NOT NULL,
    updated_at TEXT,
    remote_updated_at TEXT,
    downloaded_updated_at TEXT,
    download_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (download_status IN ('pending', 'modified', 'downloaded', 'failed')),
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resources (
    identifier_kind TEXT NOT NULL,
    identifier TEXT NOT NULL,
    conversation_id TEXT REFERENCES conversations(id),
    project_id TEXT REFERENCES projects(id),
    name TEXT,
    kind TEXT,
    mime_type TEXT,
    file_id TEXT,
    library_file_id TEXT,
    asset_pointer TEXT,
    raw_reference TEXT,
    gizmo_id TEXT,
    format TEXT,
    message_id TEXT,
    source TEXT,
    status TEXT NOT NULL DEFAULT 'inventoried',
    download_status TEXT NOT NULL DEFAULT 'pending',
    downloaded_at TEXT,
    local_path TEXT,
    size_bytes INTEGER,
    content_type TEXT,
    resolved_file_id TEXT,
    error_type TEXT,
    error_message TEXT,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (identifier_kind, identifier)
);

CREATE TABLE IF NOT EXISTS inventory_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    conversation_count INTEGER NOT NULL,
    reference_count INTEGER NOT NULL,
    resource_count INTEGER NOT NULL,
    error_count INTEGER NOT NULL,
    project_ids TEXT NOT NULL DEFAULT '[]',
    include_unassigned INTEGER NOT NULL DEFAULT 0,
    partial INTEGER NOT NULL DEFAULT 0,
    expected_conversation_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legacy_resource_state_import (
    identifier_kind TEXT NOT NULL,
    identifier TEXT NOT NULL,
    download_status TEXT,
    downloaded_at TEXT,
    local_path TEXT,
    size_bytes INTEGER,
    content_type TEXT,
    resolved_file_id TEXT,
    error_type TEXT,
    error_message TEXT,
    PRIMARY KEY (identifier_kind, identifier)
);
"""

RESOURCE_MIGRATIONS = {
    "file_id": "ALTER TABLE resources ADD COLUMN file_id TEXT",
    "library_file_id": "ALTER TABLE resources ADD COLUMN library_file_id TEXT",
    "asset_pointer": "ALTER TABLE resources ADD COLUMN asset_pointer TEXT",
    "raw_reference": "ALTER TABLE resources ADD COLUMN raw_reference TEXT",
    "gizmo_id": "ALTER TABLE resources ADD COLUMN gizmo_id TEXT",
    "format": "ALTER TABLE resources ADD COLUMN format TEXT",
    "message_id": "ALTER TABLE resources ADD COLUMN message_id TEXT",
    "source": "ALTER TABLE resources ADD COLUMN source TEXT",
    "download_status": (
        "ALTER TABLE resources ADD COLUMN download_status "
        "TEXT NOT NULL DEFAULT 'pending'"
    ),
    "downloaded_at": "ALTER TABLE resources ADD COLUMN downloaded_at TEXT",
    "local_path": "ALTER TABLE resources ADD COLUMN local_path TEXT",
    "size_bytes": "ALTER TABLE resources ADD COLUMN size_bytes INTEGER",
    "content_type": "ALTER TABLE resources ADD COLUMN content_type TEXT",
    "resolved_file_id": "ALTER TABLE resources ADD COLUMN resolved_file_id TEXT",
    "error_type": "ALTER TABLE resources ADD COLUMN error_type TEXT",
    "error_message": "ALTER TABLE resources ADD COLUMN error_message TEXT",
}

INVENTORY_RUN_MIGRATIONS = {
    "project_ids": "ALTER TABLE inventory_runs ADD COLUMN project_ids TEXT NOT NULL DEFAULT '[]'",
    "include_unassigned": (
        "ALTER TABLE inventory_runs ADD COLUMN include_unassigned INTEGER NOT NULL DEFAULT 0"
    ),
    "partial": "ALTER TABLE inventory_runs ADD COLUMN partial INTEGER NOT NULL DEFAULT 0",
    "expected_conversation_count": (
        "ALTER TABLE inventory_runs ADD COLUMN expected_conversation_count INTEGER NOT NULL DEFAULT 0"
    ),
}

CONVERSATION_MIGRATIONS = {
    "remote_updated_at": "ALTER TABLE conversations ADD COLUMN remote_updated_at TEXT",
    "downloaded_updated_at": "ALTER TABLE conversations ADD COLUMN downloaded_updated_at TEXT",
    "download_status": (
        "ALTER TABLE conversations ADD COLUMN download_status "
        "TEXT NOT NULL DEFAULT 'pending'"
    ),
}

CONVERSATION_DOWNLOAD_STATUSES = {"pending", "modified", "downloaded", "failed"}


def connect_database() -> sqlite3.Connection:
    database_file = get_database_file()
    database_file.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_file, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    conversation_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(conversations)")
    }
    for name, statement in CONVERSATION_MIGRATIONS.items():
        if name not in conversation_columns:
            connection.execute(statement)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(resources)")
    }
    for name, statement in RESOURCE_MIGRATIONS.items():
        if name not in columns:
            connection.execute(statement)
    inventory_run_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(inventory_runs)")
    }
    for name, statement in INVENTORY_RUN_MIGRATIONS.items():
        if name not in inventory_run_columns:
            connection.execute(statement)
    connection.execute(
        "UPDATE resources SET file_id = identifier "
        "WHERE file_id IS NULL AND identifier_kind = ?",
        ("file_id",),
    )
    connection.execute(
        "UPDATE resources SET library_file_id = identifier "
        "WHERE library_file_id IS NULL AND identifier_kind = ?",
        ("library_file_id",),
    )
    connection.execute(
        "UPDATE conversations SET remote_updated_at = updated_at "
        "WHERE remote_updated_at IS NULL AND updated_at IS NOT NULL"
    )
    _import_legacy_resource_state(connection)
    _apply_legacy_resource_state(connection)
    connection.commit()


def _legacy_identifier(key: str, entry: dict[str, Any]) -> tuple[str, str] | None:
    library_file_id = str(entry.get("library_file_id") or "").strip()
    file_id = str(entry.get("file_id") or "").strip()
    if library_file_id:
        return "library_file_id", library_file_id
    if file_id:
        return "file_id", file_id
    key = str(key or "").strip()
    if key.startswith(("file_", "file-")):
        return "file_id", key
    if key:
        return "library_file_id", key
    return None


def _save_legacy_state_row(
    connection: sqlite3.Connection,
    identifier: tuple[str, str],
    *,
    download_status: str | None = None,
    downloaded_at: str | None = None,
    local_path: str | None = None,
    size_bytes: int | None = None,
    content_type: str | None = None,
    resolved_file_id: str | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO legacy_resource_state_import (
            identifier_kind, identifier, download_status, downloaded_at,
            local_path, size_bytes, content_type, resolved_file_id,
            error_type, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(identifier_kind, identifier) DO UPDATE SET
            download_status = CASE
                WHEN excluded.download_status = 'downloaded' THEN 'downloaded'
                ELSE COALESCE(legacy_resource_state_import.download_status,
                              excluded.download_status)
            END,
            downloaded_at = COALESCE(excluded.downloaded_at,
                                     legacy_resource_state_import.downloaded_at),
            local_path = COALESCE(excluded.local_path,
                                  legacy_resource_state_import.local_path),
            size_bytes = COALESCE(excluded.size_bytes,
                                  legacy_resource_state_import.size_bytes),
            content_type = COALESCE(excluded.content_type,
                                    legacy_resource_state_import.content_type),
            resolved_file_id = COALESCE(excluded.resolved_file_id,
                                        legacy_resource_state_import.resolved_file_id),
            error_type = COALESCE(excluded.error_type,
                                  legacy_resource_state_import.error_type),
            error_message = COALESCE(excluded.error_message,
                                     legacy_resource_state_import.error_message)
        """,
        (
            identifier[0], identifier[1], download_status, downloaded_at,
            local_path, size_bytes, content_type, resolved_file_id,
            error_type, error_message,
        ),
    )


def _import_legacy_resource_state(connection: sqlite3.Connection) -> None:
    imported = connection.execute(
        "SELECT value FROM schema_meta WHERE key = ?",
        ("legacy_resource_state_imported",),
    ).fetchone()
    if imported:
        return

    for state_file in get_backup_dir().rglob("resource_state.json"):
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(state, dict):
            continue

        for key, entry in (state.get("failed") or {}).items():
            if not isinstance(entry, dict):
                continue
            identifier = _legacy_identifier(str(key), entry)
            if identifier:
                _save_legacy_state_row(
                    connection,
                    identifier,
                    download_status="failed",
                    resolved_file_id=str(entry.get("file_id") or "").strip() or None,
                    error_type=str(entry.get("error_type") or "").strip() or None,
                    error_message=str(entry.get("error") or "").strip() or None,
                )

        for key, entry in (state.get("completed") or {}).items():
            if not isinstance(entry, dict):
                continue
            file_id = str(entry.get("file_id") or key or "").strip() or None
            identifiers = []
            identifier = _legacy_identifier(str(key), entry)
            if identifier:
                identifiers.append(identifier)
            if file_id:
                identifiers.append(("file_id", file_id))
            for current in dict.fromkeys(identifiers):
                _save_legacy_state_row(
                    connection,
                    current,
                    download_status="downloaded",
                    downloaded_at=datetime.now(timezone.utc).isoformat(),
                    local_path=str(entry.get("path") or "").strip() or None,
                    size_bytes=entry.get("size_bytes")
                    if isinstance(entry.get("size_bytes"), int) else None,
                    content_type=str(entry.get("content_type") or "").strip() or None,
                    resolved_file_id=file_id,
                )

        for library_file_id, file_id in (state.get("resolved_ids") or {}).items():
            library_value = str(library_file_id or "").strip()
            file_value = str(file_id or "").strip()
            if library_value and file_value:
                _save_legacy_state_row(
                    connection,
                    ("library_file_id", library_value),
                    resolved_file_id=file_value,
                )

    connection.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
        ("legacy_resource_state_imported", datetime.now(timezone.utc).isoformat()),
    )


def _apply_legacy_resource_state(connection: sqlite3.Connection) -> None:
    for row in connection.execute(
        """
        SELECT identifier_kind, identifier, download_status, downloaded_at,
               local_path, size_bytes, content_type, resolved_file_id,
               error_type, error_message
        FROM legacy_resource_state_import
        """
    ):
        connection.execute(
            """
            UPDATE resources SET
                download_status = CASE
                    WHEN download_status = 'pending' AND ? IS NOT NULL THEN ?
                    ELSE download_status
                END,
                downloaded_at = COALESCE(downloaded_at, ?),
                local_path = COALESCE(local_path, ?),
                size_bytes = COALESCE(size_bytes, ?),
                content_type = COALESCE(content_type, ?),
                resolved_file_id = COALESCE(resolved_file_id, ?),
                error_type = COALESCE(error_type, ?),
                error_message = COALESCE(error_message, ?)
            WHERE (identifier_kind = ? AND identifier = ?)
               OR (? = 'file_id' AND file_id = ?)
               OR (? = 'library_file_id' AND library_file_id = ?)
            """,
            (
                row[2], row[2], row[3], row[4], row[5], row[6], row[7],
                row[8], row[9], row[0], row[1], row[0], row[1], row[0], row[1],
            ),
        )


def resource_identifier(resource: dict[str, Any]) -> tuple[str, str] | None:
    for field in ("file_id", "library_file_id", "asset_pointer", "raw_reference"):
        value = str(resource.get(field) or "").strip()
        if value:
            return field, value
    return None


def _scope_project(scope: dict[str, Any]) -> tuple[str | None, str | None]:
    project = scope.get("project")
    if not isinstance(project, dict):
        return None, None
    project_id = str(project.get("id") or "").strip() or None
    project_name = str(project.get("name") or "").strip() or None
    return project_id, project_name


def _collect_records(scopes: list[dict[str, Any]]) -> tuple[
    dict[str, str],
    dict[str, dict[str, Any]],
    dict[tuple[str, str], dict[str, Any]],
]:
    projects: dict[str, str] = {}
    conversations: dict[str, dict[str, Any]] = {}
    resources: dict[tuple[str, str], dict[str, Any]] = {}

    for scope in scopes:
        project_id, project_name = _scope_project(scope)
        is_root = bool(scope.get("is_root"))
        if project_id and project_name:
            projects[project_id] = project_name
        if not is_root and not project_id:
            continue

        inventory = scope.get("inventory")
        if not isinstance(inventory, dict):
            continue

        for conversation in inventory.get("conversations") or []:
            if not isinstance(conversation, dict):
                continue
            conversation_id = str(conversation.get("id") or "").strip()
            if not conversation_id:
                continue
            conversations[conversation_id] = {
                "id": conversation_id,
                "project_id": project_id,
                "title": str(conversation.get("title") or "(sin título)"),
                "updated_at": conversation.get("updated_at"),
            }

        for resource in inventory.get("resources") or []:
            if not isinstance(resource, dict):
                continue
            identifier = resource_identifier(resource)
            if identifier is None:
                continue
            resources[identifier] = {
                "identifier_kind": identifier[0],
                "identifier": identifier[1],
                "conversation_id": str(resource.get("conversation_id") or "").strip() or None,
                "project_id": project_id,
                "name": str(resource.get("name") or "").strip() or None,
                "kind": str(resource.get("resource_kind") or "").strip() or None,
                "mime_type": str(resource.get("mime_type") or "").strip() or None,
                "file_id": str(resource.get("file_id") or "").strip() or None,
                "library_file_id": str(resource.get("library_file_id") or "").strip() or None,
                "asset_pointer": str(resource.get("asset_pointer") or "").strip() or None,
                "raw_reference": str(resource.get("raw_reference") or "").strip() or None,
                "gizmo_id": str(resource.get("gizmo_id") or "").strip() or None,
                "format": str(resource.get("format") or "").strip() or None,
                "message_id": str(resource.get("message_id") or "").strip() or None,
                "source": str(resource.get("source") or "").strip() or None,
            }

    return projects, conversations, resources


def update_inventory_index(
    *,
    scopes: list[dict[str, Any]],
    conversation_count: int,
    reference_count: int,
    resource_count: int,
    error_count: int,
    project_ids: list[str] | None = None,
    include_unassigned: bool = False,
    partial: bool = False,
    expected_conversation_count: int | None = None,
) -> dict[str, int]:
    projects, conversations, resources = _collect_records(scopes)
    seen_at = datetime.now(timezone.utc).isoformat()
    connection = connect_database()
    try:
        ensure_schema(connection)
        connection.execute("BEGIN")

        known_conversations = {
            row[0] for row in connection.execute("SELECT id FROM conversations")
        }
        known_resources = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT identifier_kind, identifier FROM resources"
            )
        }

        run_cursor = connection.execute(
            """
            INSERT INTO inventory_runs (
                created_at, conversation_count, reference_count, resource_count, error_count,
                project_ids, include_unassigned, partial, expected_conversation_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seen_at, conversation_count, reference_count, resource_count, error_count,
                json.dumps(list(dict.fromkeys(project_ids or []))),
                int(include_unassigned), int(partial),
                expected_conversation_count
                if expected_conversation_count is not None else conversation_count,
            ),
        )

        for project_id, name in projects.items():
            connection.execute(
                """
                INSERT INTO projects (id, name, last_seen) VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    last_seen = excluded.last_seen
                """,
                (project_id, name, seen_at),
            )

        for conversation in conversations.values():
            updated_at = conversation["updated_at"]
            connection.execute(
                """
                INSERT INTO conversations (id, project_id, title, updated_at, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    project_id = excluded.project_id,
                    title = excluded.title,
                    updated_at = COALESCE(excluded.updated_at, conversations.updated_at),
                    last_seen = excluded.last_seen
                """,
                (
                    conversation["id"],
                    conversation["project_id"],
                    conversation["title"],
                    str(updated_at) if updated_at is not None else None,
                    seen_at,
                ),
            )

        for resource in resources.values():
            conversation_id = resource["conversation_id"]
            if conversation_id not in conversations and conversation_id not in known_conversations:
                conversation_id = None
            connection.execute(
                """
                INSERT INTO resources (
                    identifier_kind, identifier, conversation_id, project_id,
                    name, kind, mime_type, file_id, library_file_id,
                    asset_pointer, raw_reference, gizmo_id, format, message_id,
                    source, status, download_status, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(identifier_kind, identifier) DO UPDATE SET
                    conversation_id = COALESCE(excluded.conversation_id, resources.conversation_id),
                    project_id = excluded.project_id,
                    name = COALESCE(excluded.name, resources.name),
                    kind = COALESCE(excluded.kind, resources.kind),
                    mime_type = COALESCE(excluded.mime_type, resources.mime_type),
                    file_id = COALESCE(excluded.file_id, resources.file_id),
                    library_file_id = COALESCE(excluded.library_file_id, resources.library_file_id),
                    asset_pointer = COALESCE(excluded.asset_pointer, resources.asset_pointer),
                    raw_reference = COALESCE(excluded.raw_reference, resources.raw_reference),
                    gizmo_id = COALESCE(excluded.gizmo_id, resources.gizmo_id),
                    format = COALESCE(excluded.format, resources.format),
                    message_id = COALESCE(excluded.message_id, resources.message_id),
                    source = COALESCE(excluded.source, resources.source),
                    last_seen = excluded.last_seen
                """,
                (
                    resource["identifier_kind"],
                    resource["identifier"],
                    conversation_id,
                    resource["project_id"],
                    resource["name"],
                    resource["kind"],
                    resource["mime_type"],
                    resource["file_id"],
                    resource["library_file_id"],
                    resource["asset_pointer"],
                    resource["raw_reference"],
                    resource["gizmo_id"],
                    resource["format"],
                    resource["message_id"],
                    resource["source"],
                    "inventoried",
                    "pending",
                    seen_at,
                ),
            )

        _apply_legacy_resource_state(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    conversation_keys = set(conversations)
    resource_keys = set(resources)
    return {
        "inventory_run_id": int(run_cursor.lastrowid),
        "new_conversations": len(conversation_keys - known_conversations),
        "known_conversations": len(conversation_keys & known_conversations),
        "new_resources": len(resource_keys - known_resources),
        "known_resources": len(resource_keys & known_resources),
    }


def get_latest_inventory_run() -> dict[str, Any] | None:
    connection = connect_database()
    try:
        ensure_schema(connection)
        row = connection.execute(
            """
            SELECT id, created_at, conversation_count, reference_count,
                   resource_count, error_count, project_ids,
                   include_unassigned, partial, expected_conversation_count
            FROM inventory_runs
            WHERE project_ids != '[]' OR include_unassigned = 1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    try:
        project_ids = json.loads(row[6])
    except (TypeError, ValueError):
        return None
    if not isinstance(project_ids, list) or not all(isinstance(value, str) for value in project_ids):
        return None
    result = dict(row)
    result["project_ids"] = project_ids
    result["include_unassigned"] = bool(result["include_unassigned"])
    result["partial"] = bool(result["partial"])
    return result


def backfill_latest_inventory_scope(
    *,
    project_ids: list[str],
    include_unassigned: bool,
    partial: bool,
    expected_conversation_count: int,
    conversation_count: int,
    reference_count: int,
    resource_count: int,
) -> bool:
    connection = connect_database()
    try:
        ensure_schema(connection)
        cursor = connection.execute(
            """
            UPDATE inventory_runs
            SET project_ids = ?, include_unassigned = ?, partial = ?,
                expected_conversation_count = ?
            WHERE id = (SELECT MAX(id) FROM inventory_runs)
              AND project_ids = '[]' AND include_unassigned = 0
              AND conversation_count = ? AND reference_count = ? AND resource_count = ?
            """,
            (
                json.dumps(list(dict.fromkeys(project_ids))), int(include_unassigned),
                int(partial), expected_conversation_count, conversation_count,
                reference_count, resource_count,
            ),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def get_inventory_scope_details(
    project_ids: list[str], include_unassigned: bool,
) -> dict[str, Any]:
    ordered_ids = list(dict.fromkeys(project_ids))
    connection = connect_database()
    try:
        ensure_schema(connection)
        records = {}
        if ordered_ids:
            placeholders = ", ".join("?" for _ in ordered_ids)
            rows = connection.execute(
                f"""
                SELECT p.id, p.name, COUNT(DISTINCT c.id) AS conversation_count
                FROM projects AS p
                LEFT JOIN conversations AS c ON c.project_id = p.id
                WHERE p.id IN ({placeholders})
                GROUP BY p.id, p.name
                """,
                ordered_ids,
            ).fetchall()
            records = {str(row[0]): dict(row) for row in rows}
        root_count = 0
        if include_unassigned:
            root_count = int(connection.execute(
                "SELECT COUNT(*) FROM conversations WHERE project_id IS NULL"
            ).fetchone()[0])
    finally:
        connection.close()
    return {
        "projects": [records[project_id] for project_id in ordered_ids if project_id in records],
        "root_conversation_count": root_count,
    }


RESOURCE_SELECT = """
SELECT r.identifier_kind, r.identifier, r.conversation_id, r.project_id,
       r.name, r.kind AS resource_kind, r.mime_type, r.file_id,
       r.library_file_id, r.asset_pointer, r.raw_reference, r.gizmo_id,
       r.format, r.message_id, r.source, r.resolved_file_id,
       r.download_status, r.downloaded_at, r.local_path, r.size_bytes,
       r.content_type, r.error_type, r.error_message,
       c.title AS conversation_title, p.name AS project_name
FROM resources AS r
LEFT JOIN conversations AS c ON c.id = r.conversation_id
LEFT JOIN projects AS p ON p.id = r.project_id
"""


def get_scope_resources(
    project_id: str | None,
    *,
    download_statuses: tuple[str, ...] | None = None,
    last_seen: str | None = None,
) -> list[dict[str, Any]]:
    connection = connect_database()
    try:
        ensure_schema(connection)
        if project_id is None:
            where = " WHERE r.project_id IS NULL"
            parameters: tuple[Any, ...] = ()
        else:
            where = " WHERE r.project_id = ?"
            parameters = (project_id,)
        if last_seen is not None:
            where += " AND r.last_seen = ?"
            parameters += (last_seen,)
        rows = connection.execute(RESOURCE_SELECT + where, parameters).fetchall()
    finally:
        connection.close()

    resources = [dict(row) for row in rows]
    if download_statuses is not None:
        allowed = set(download_statuses)
        resources = [
            resource for resource in resources
            if resource.get("download_status") in allowed
        ]
    for resource in resources:
        if not resource.get("file_id") and resource.get("resolved_file_id"):
            resource["file_id"] = resource["resolved_file_id"]
    return resources


def get_download_status_counts(
    project_id: str | None, *, last_seen: str | None = None,
) -> dict[str, int]:
    connection = connect_database()
    try:
        ensure_schema(connection)
        if project_id is None:
            where = "WHERE project_id IS NULL"
            parameters: tuple[Any, ...] = ()
        else:
            where = "WHERE project_id = ?"
            parameters = (project_id,)
        if last_seen is not None:
            where += " AND last_seen = ?"
            parameters += (last_seen,)
        rows = connection.execute(
            "SELECT download_status, COUNT(*) FROM resources " + where +
            " GROUP BY download_status",
            parameters,
        ).fetchall()
    finally:
        connection.close()
    counts = {"pending": 0, "downloaded": 0, "failed": 0}
    counts.update({str(row[0]): int(row[1]) for row in rows})
    return counts


def get_conversation_updated_at(conversation_id: str) -> str | None:
    connection = connect_database()
    try:
        ensure_schema(connection)
        row = connection.execute(
            "SELECT updated_at FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
    finally:
        connection.close()
    return str(row[0]) if row and row[0] is not None else None


def mark_conversation_downloaded(
    conversation_id: str,
    downloaded_updated_at: str | None,
) -> None:
    connection = connect_database()
    try:
        ensure_schema(connection)
        connection.execute(
            """
            UPDATE conversations SET
                download_status = 'downloaded', downloaded_updated_at = ?
            WHERE id = ?
            """,
            (downloaded_updated_at, conversation_id),
        )
        connection.commit()
    finally:
        connection.close()


def mark_conversation_failed(conversation_id: str) -> None:
    connection = connect_database()
    try:
        ensure_schema(connection)
        connection.execute(
            "UPDATE conversations SET download_status = 'failed' WHERE id = ?",
            (conversation_id,),
        )
        connection.commit()
    finally:
        connection.close()


def _remote_timestamp_after(remote_updated_at: str | None, previous_updated_at: str | None) -> bool:
    if not remote_updated_at or not previous_updated_at:
        return False
    try:
        remote = datetime.fromisoformat(remote_updated_at.replace("Z", "+00:00"))
        previous = datetime.fromisoformat(previous_updated_at.replace("Z", "+00:00"))
        return remote > previous
    except ValueError:
        return False


def classify_remote_conversations(remote_records: list[dict[str, Any]]) -> dict[str, int]:
    records: dict[str, dict[str, Any]] = {}
    for record in remote_records:
        if not isinstance(record, dict):
            continue
        conversation_id = str(record.get("id") or "").strip()
        if conversation_id:
            records[conversation_id] = record

    counts = {status: 0 for status in CONVERSATION_DOWNLOAD_STATUSES}
    seen_at = datetime.now(timezone.utc).isoformat()
    connection = connect_database()
    try:
        ensure_schema(connection)
        known = {
            str(row["id"]): row
            for row in connection.execute(
                "SELECT id, remote_updated_at, downloaded_updated_at, download_status "
                "FROM conversations"
            )
        }
        connection.execute("BEGIN")
        for conversation_id, record in records.items():
            project_id = str(record.get("project_id") or "").strip() or None
            project_name = str(record.get("project_name") or "").strip()
            title = str(record.get("title") or "(sin título)")
            remote_value = record.get("update_time")
            if remote_value is None:
                remote_value = record.get("updated_at")
            remote_updated_at = str(remote_value) if remote_value is not None else None

            if project_id and project_name:
                connection.execute(
                    """
                    INSERT INTO projects (id, name, last_seen) VALUES (?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        last_seen = excluded.last_seen
                    """,
                    (project_id, project_name, seen_at),
                )
            elif project_id:
                project_id = None

            previous = known.get(conversation_id)
            if previous is None:
                status = "pending"
            elif previous["download_status"] == "failed":
                if previous["remote_updated_at"] is not None:
                    changed_after_failure = _remote_timestamp_after(
                        remote_updated_at, previous["remote_updated_at"]
                    )
                else:
                    changed_after_failure = _remote_timestamp_after(
                        remote_updated_at, previous["downloaded_updated_at"]
                    )
                if changed_after_failure:
                    status = "modified"
                else:
                    status = "failed"
            elif previous["downloaded_updated_at"] is None:
                status = "pending"
            elif _remote_timestamp_after(remote_updated_at, previous["downloaded_updated_at"]):
                status = "modified"
            else:
                status = "downloaded"

            connection.execute(
                """
                INSERT INTO conversations (
                    id, project_id, title, remote_updated_at, download_status, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    project_id = excluded.project_id,
                    title = excluded.title,
                    remote_updated_at = excluded.remote_updated_at,
                    download_status = excluded.download_status,
                    last_seen = excluded.last_seen
                """,
                (conversation_id, project_id, title, remote_updated_at, status, seen_at),
            )
            counts[status] += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return counts


def get_conversations_by_status(status: str) -> list[dict[str, Any]]:
    if status not in CONVERSATION_DOWNLOAD_STATUSES:
        raise ValueError("Estado de descarga de conversación no válido.")
    connection = connect_database()
    try:
        ensure_schema(connection)
        rows = connection.execute(
            "SELECT id, project_id, title, updated_at, remote_updated_at, "
            "downloaded_updated_at, download_status, last_seen "
            "FROM conversations WHERE download_status = ? ORDER BY last_seen DESC, id",
            (status,),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def get_all_resources() -> list[dict[str, Any]]:
    connection = connect_database()
    try:
        ensure_schema(connection)
        rows = connection.execute(RESOURCE_SELECT).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def get_project_id_by_name(project_name: str) -> str | None:
    connection = connect_database()
    try:
        ensure_schema(connection)
        row = connection.execute(
            "SELECT id FROM projects WHERE name = ? LIMIT 1",
            (project_name,),
        ).fetchone()
    finally:
        connection.close()
    return str(row[0]) if row else None


def find_resource_by_file_id(file_id: str) -> dict[str, Any] | None:
    connection = connect_database()
    try:
        ensure_schema(connection)
        row = connection.execute(
            RESOURCE_SELECT
            + " WHERE r.file_id = ? OR r.resolved_file_id = ? LIMIT 1",
            (file_id, file_id),
        ).fetchone()
    finally:
        connection.close()
    return dict(row) if row else None


def get_downloaded_resource_by_file_id(file_id: str) -> dict[str, Any] | None:
    connection = connect_database()
    try:
        ensure_schema(connection)
        row = connection.execute(
            RESOURCE_SELECT
            + " WHERE r.download_status = 'downloaded' "
              "AND (r.file_id = ? OR r.resolved_file_id = ?) LIMIT 1",
            (file_id, file_id),
        ).fetchone()
    finally:
        connection.close()
    return dict(row) if row else None


def inventory_scope_exists(project_id: str | None) -> bool:
    connection = connect_database()
    try:
        ensure_schema(connection)
        rows = connection.execute(
            "SELECT project_ids, include_unassigned FROM inventory_runs "
            "ORDER BY id DESC"
        ).fetchall()
    finally:
        connection.close()

    for project_ids_json, include_unassigned in rows:
        if project_id is None:
            if bool(include_unassigned):
                return True
            continue
        try:
            project_ids = json.loads(project_ids_json)
        except (TypeError, ValueError):
            continue
        if isinstance(project_ids, list) and project_id in project_ids:
            return True
    return False


def set_resolved_file_id(
    identifier_kind: str,
    identifier: str,
    file_id: str,
) -> None:
    connection = connect_database()
    try:
        ensure_schema(connection)
        connection.execute(
            """
            UPDATE resources SET file_id = ?, resolved_file_id = ?
            WHERE identifier_kind = ? AND identifier = ?
            """,
            (file_id, file_id, identifier_kind, identifier),
        )
        connection.commit()
    finally:
        connection.close()


def mark_resource_downloaded(
    identifier_kind: str,
    identifier: str,
    *,
    file_id: str,
    local_path: str,
    size_bytes: int,
    content_type: str | None,
) -> None:
    connection = connect_database()
    try:
        ensure_schema(connection)
        connection.execute(
            """
            UPDATE resources SET
                file_id = ?, resolved_file_id = ?, download_status = 'downloaded',
                downloaded_at = ?, local_path = ?, size_bytes = ?,
                content_type = ?, error_type = NULL, error_message = NULL
            WHERE identifier_kind = ? AND identifier = ?
            """,
            (
                file_id, file_id, datetime.now(timezone.utc).isoformat(),
                local_path, size_bytes, content_type, identifier_kind, identifier,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def mark_resource_failed(
    identifier_kind: str,
    identifier: str,
    *,
    file_id: str | None,
    error_type: str,
    error_message: str,
) -> None:
    connection = connect_database()
    try:
        ensure_schema(connection)
        connection.execute(
            """
            UPDATE resources SET
                file_id = COALESCE(?, file_id),
                resolved_file_id = COALESCE(?, resolved_file_id),
                download_status = 'failed', downloaded_at = NULL,
                error_type = ?, error_message = ?
            WHERE identifier_kind = ? AND identifier = ?
            """,
            (
                file_id, file_id, error_type, error_message,
                identifier_kind, identifier,
            ),
        )
        connection.commit()
    finally:
        connection.close()
