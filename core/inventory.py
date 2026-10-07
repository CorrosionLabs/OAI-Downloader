from __future__ import annotations

from collections import Counter
from collections import defaultdict
from typing import Any
from collections.abc import Callable
from pathlib import Path
import json

from .config import APP_NAME, INTERESTING_KEYWORDS, VERSION
from .database import update_inventory_index
from .errors import BackupCLIError
from .paths import get_backup_paths


def walk_json(
    value: Any,
    *,
    path: str = "$",
    key_counter: Counter[str],
    interesting_paths: defaultdict[str, set[str]],
    string_samples: defaultdict[str, list[str]],
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_str = str(key)
            key_lower = key_str.lower()
            child_path = f"{path}.{key_str}"

            key_counter[key_str] += 1

            if any(keyword in key_lower for keyword in INTERESTING_KEYWORDS):
                interesting_paths[key_str].add(child_path)

                if isinstance(child, (str, int, float, bool)) and len(string_samples[key_str]) < 5:
                    sample = str(child)
                    if len(sample) > 180:
                        sample = sample[:177] + "..."
                    string_samples[key_str].append(sample)

            walk_json(
                child,
                path=child_path,
                key_counter=key_counter,
                interesting_paths=interesting_paths,
                string_samples=string_samples,
            )

    elif isinstance(value, list):
        for index, child in enumerate(value):
            walk_json(
                child,
                path=f"{path}[{index}]",
                key_counter=key_counter,
                interesting_paths=interesting_paths,
                string_samples=string_samples,
            )


def run_inspect(project_name: str | None = None) -> int:
    paths = get_backup_paths(project_name)
    conversations_dir = paths["conversations"]
    files = sorted(conversations_dir.glob("*.json"))

    if not files:
        raise BackupCLIError(
            "No hay JSON en backup/conversations.\n"
            "Ejecuta primero: python main.py --sample 5"
        )

    print()
    print(APP_NAME)
    print("=" * len(APP_NAME))
    print(f"Versión: {VERSION}")
    print()
    print(f"Auditando {len(files)} conversación(es) descargada(s)...")
    print()

    global_key_counter: Counter[str] = Counter()
    global_paths: defaultdict[str, set[str]] = defaultdict(set)
    global_samples: defaultdict[str, list[str]] = defaultdict(list)

    titles: list[str] = []

    for file_path in files:
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"ERROR leyendo {file_path.name}: {exc}")
            continue

        if isinstance(data, dict):
            title = data.get("title")
            if title:
                titles.append(str(title))

        walk_json(
            data,
            key_counter=global_key_counter,
            interesting_paths=global_paths,
            string_samples=global_samples,
        )

    interesting_keys = sorted(
        global_paths.keys(),
        key=lambda key: (-global_key_counter[key], key.lower()),
    )

    print("CONVERSACIONES")
    print("--------------")
    for title in titles:
        print(f"- {title}")

    print()
    print("CLAVES DE RECURSOS DETECTADAS")
    print("-----------------------------")

    if not interesting_keys:
        print("No se detectaron claves relacionadas con recursos.")
        return 0

    for key in interesting_keys:
        count = global_key_counter[key]
        paths = sorted(global_paths[key])

        print()
        print(f"{key}  [apariciones: {count}]")

        for path in paths[:3]:
            print(f"  ruta: {path}")

        samples = global_samples.get(key) or []
        for sample in samples[:3]:
            print(f"  valor: {sample}")

    print()
    print("RESUMEN")
    print("-------")
    print(f"JSON analizados:            {len(files)}")
    print(f"Claves distintas totales:   {len(global_key_counter)}")
    print(f"Claves de recursos:         {len(interesting_keys)}")
    print()
    print("No se ha descargado ningún recurso adicional.")
    return 0


def normalize_asset_pointer(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    value = value.strip()
    if not value:
        return None

    return value


def extract_file_id_from_reference(reference: Any) -> str | None:
    if not isinstance(reference, str):
        return None

    value = reference.strip()
    if not value:
        return None

    if value.startswith("sediment://"):
        value = value[len("sediment://"):]

        if "#" in value:
            parts = [
                part
                for part in value.split("#")
                if part.startswith(("file_", "file-"))
            ]
            if parts:
                value = parts[0]

        if value.startswith(("file_", "file-")):
            return value

    if value.startswith("file-service://"):
        value = value[len("file-service://"):]
        if value.startswith(("file_", "file-")):
            return value

    if value.startswith(("file_", "file-")):
        return value

    return None


def extract_resources_from_message(
    *,
    conversation_id: str,
    conversation_title: str,
    conversation_gizmo_id: str | None,
    message_id: str,
    message: dict[str, Any],
    resources: list[dict[str, Any]],
) -> None:
    content = message.get("content") or {}
    metadata = message.get("metadata") or {}
    message_gizmo_id = metadata.get("gizmo_id") if isinstance(metadata, dict) else None
    gizmo_id = str(message_gizmo_id or conversation_gizmo_id or "").strip() or None

    parts = content.get("parts") if isinstance(content, dict) else None

    if isinstance(parts, list):
        for index, part in enumerate(parts):
            if not isinstance(part, dict):
                continue

            pointer = normalize_asset_pointer(part.get("asset_pointer"))
            if pointer:
                part_metadata = part.get("metadata") or {}

                resources.append(
                    {
                        "conversation_id": conversation_id,
                        "conversation_title": conversation_title,
                        "message_id": message_id,
                        "source": f"content.parts[{index}]",
                        "resource_kind": "asset",
                        "raw_reference": pointer,
                        "asset_pointer": pointer,
                        "file_id": extract_file_id_from_reference(pointer),
                        "library_file_id": None,
                        "gizmo_id": gizmo_id,
                        "mime_type": part.get("mime_type"),
                        "name": part.get("name")
                        or part.get("filename")
                        or part_metadata.get("name")
                        or part_metadata.get("filename"),
                        "format": None,
                        "watermarked_asset_pointer": normalize_asset_pointer(
                            part_metadata.get("watermarked_asset_pointer")
                        ),
                    }
                )

    attachments = metadata.get("attachments")

    if isinstance(attachments, list):
        for index, attachment in enumerate(attachments):
            if not isinstance(attachment, dict):
                continue

            pointer = normalize_asset_pointer(
                attachment.get("asset_pointer")
                or attachment.get("file_pointer")
                or attachment.get("pointer")
            )

            library_file_id = attachment.get("library_file_id")
            attachment_id = extract_file_id_from_reference(attachment.get("id"))
            file_id = attachment_id or extract_file_id_from_reference(pointer)

            resources.append(
                {
                    "conversation_id": conversation_id,
                    "conversation_title": conversation_title,
                    "message_id": message_id,
                    "source": f"metadata.attachments[{index}]",
                    "resource_kind": "attachment",
                    "raw_reference": pointer or library_file_id,
                    "asset_pointer": pointer,
                    "file_id": file_id,
                    "library_file_id": library_file_id,
                    "gizmo_id": gizmo_id,
                    "mime_type": attachment.get("mime_type"),
                    "name": attachment.get("name")
                    or attachment.get("filename")
                    or attachment.get("file_name"),
                    "format": None,
                    "watermarked_asset_pointer": None,
                }
            )

    dictation_pointer = normalize_asset_pointer(
        metadata.get("dictation_asset_pointer")
    )

    if dictation_pointer:
        resources.append(
            {
                "conversation_id": conversation_id,
                "conversation_title": conversation_title,
                "message_id": message_id,
                "source": "metadata.dictation_asset_pointer",
                "resource_kind": "dictation_audio",
                "raw_reference": dictation_pointer,
                "asset_pointer": dictation_pointer,
                "file_id": extract_file_id_from_reference(dictation_pointer),
                "library_file_id": None,
                "gizmo_id": gizmo_id,
                "mime_type": None,
                "name": None,
                "format": metadata.get("dictation_asset_format"),
                "watermarked_asset_pointer": None,
            }
        )


def run_inventory(
    project_name: str | None = None,
    *,
    allow_empty: bool = False,
) -> int:
    paths = get_backup_paths(project_name)
    conversations_dir = paths["conversations"]
    files = sorted(conversations_dir.glob("*.json"))

    if allow_empty:
        files = []

    if not files and not allow_empty:
        raise BackupCLIError(
            "No hay JSON en backup/conversations.\n"
            "Ejecuta primero: python main.py --sample 5"
        )

    print()
    print(APP_NAME)
    print("=" * len(APP_NAME))
    print(f"Versión: {VERSION}")
    print()
    print(f"Inventariando recursos de {len(files)} conversación(es)...")
    print()

    payload = build_inventory(files)
    project = None
    if project_name and paths["project_metadata"].exists():
        try:
            metadata = json.loads(paths["project_metadata"].read_text(encoding="utf-8"))
            if isinstance(metadata, dict) and metadata.get("id"):
                project = {"id": metadata["id"], "name": metadata.get("name") or project_name}
        except (OSError, ValueError, TypeError):
            project = None
    payload["database_comparison"] = update_inventory_index(
        scopes=[{
            "is_root": project_name is None,
            "project": project,
            "inventory": payload,
        }],
        conversation_count=payload["conversation_files_scanned"],
        reference_count=payload["total_references"],
        resource_count=payload["resource_count"],
        error_count=len(payload["errors"]),
    )
    print("RECURSOS DETECTADOS")
    print("-------------------")
    for kind, count in payload["counts_by_kind"].items():
        print(f"{kind:20} {count}")
    print(f"TOTAL: {payload['resource_count']}")
    for field in ("file_id", "library_file_id", "gizmo_id"):
        count = sum(1 for resource in payload["resources"] if resource.get(field))
        print(f"Con {field}: {count}")
    for name in payload["errors"]:
        print(f"ERROR leyendo {name}")
    print("Inventario actualizado en SQLite.")
    print("No se ha descargado ningún archivo ni audio.")
    return 0


def resource_identity(resource: dict[str, Any]) -> tuple[Any, ...]:
    key = (resource.get("asset_pointer"), resource.get("library_file_id"),
           resource.get("resource_kind"))
    if key[:2] == (None, None):
        return (resource.get("conversation_id"), resource.get("message_id"),
                resource.get("source"))
    return key


def build_inventory(
    files: list[Path],
    *,
    progress: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Analyze local conversations without writing or requiring authentication."""
    resources: list[dict[str, Any]] = []
    errors: list[str] = []
    conversations: dict[str, dict[str, Any]] = {}

    for index, file_path in enumerate(files, start=1):
        if progress:
            progress(current=index - 1, total=len(files), resources_detected=len(resources))
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            errors.append(file_path.name)
            continue

        if not isinstance(data, dict):
            errors.append(file_path.name)
            continue

        conversation_id = str(data.get("conversation_id") or data.get("id") or file_path.stem)
        conversation_title = str(data.get("title") or "(sin título)")
        conversation_gizmo_id = str(data.get("gizmo_id") or "").strip() or None
        conversations[conversation_id] = {
            "id": conversation_id,
            "title": conversation_title,
            "updated_at": data.get("updated_at")
            if data.get("updated_at") is not None
            else data.get("update_time"),
        }
        mapping = data.get("mapping") or {}

        if not isinstance(mapping, dict):
            errors.append(file_path.name)
            continue

        for node_id, node in mapping.items():
            if not isinstance(node, dict):
                continue

            message = node.get("message")
            if not isinstance(message, dict):
                continue

            message_id = str(message.get("id") or node_id)

            extract_resources_from_message(
                conversation_id=conversation_id,
                conversation_title=conversation_title,
                conversation_gizmo_id=conversation_gizmo_id,
                message_id=message_id,
                message=message,
                resources=resources,
            )

    # Deduplicar por los identificadores que realmente pueden representar
    # el mismo recurso persistente.
    deduped: dict[tuple[Any, ...], dict[str, Any]] = {}

    for resource in resources:
        deduped[resource_identity(resource)] = resource

    final_resources = list(deduped.values())

    counts = Counter(
        resource.get("resource_kind") or "unknown"
        for resource in final_resources
    )

    payload = {
        "version": VERSION,
        "conversation_files_scanned": len(files),
        "resource_count": len(final_resources),
        "total_references": len(resources),
        "errors": errors,
        "counts_by_kind": dict(sorted(counts.items())),
        "conversations": list(conversations.values()),
        "resources": final_resources,
    }

    if progress:
        progress(current=len(files), total=len(files), resources_detected=len(resources))
    return payload


def extract_file_id_from_pointer(pointer: str) -> str | None:
    return extract_file_id_from_reference(pointer)
