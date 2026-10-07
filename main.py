# main.py
"""
OpenAI Backup CLI - fase 2.1
Diagnóstico, descarga controlada de conversaciones y auditoría de recursos.

Uso:
    python main.py --check
    python main.py --sample 5
    python main.py --inspect

Requisitos:
    Python 3.10+
    No requiere paquetes externos.

Credenciales:
    core/.secrets/cookies.txt
"""

from __future__ import annotations

import sys

from core.audits import run_audit_failures, run_audit_resolution
from core.cli import build_parser, configure_console_output
from core.conversations import run_download_conversations, run_sample
from core.errors import BackupCLIError
from core.inventory import run_inspect, run_inventory
from core.library import (
    run_delete_library_file,
    run_list_pdfs,
    run_resolve_library_file,
    run_test_delete_pdf,
)
from core.probes import (
    run_check,
    run_probe_file_id,
    run_probe_library_resource,
    run_probe_orphan_file,
    run_probe_resolution,
    run_probe_resource,
)
from core.projects import run_download_all_projects, run_list_projects
from core.resources import run_download_resources_resolved


def main() -> int:
    configure_console_output()
    parser = build_parser()
    args = parser.parse_args()

    if args.list_projects:
        action = run_list_projects
    elif args.check:
        action = run_check
    elif args.sample is not None:
        action = lambda: run_sample(args.sample)
    elif args.inspect:
        action = lambda: run_inspect(args.project)
    elif args.inventory:
        action = lambda: run_inventory(args.project)
    elif args.probe_resource:
        action = lambda: run_probe_resource(args.project)
    elif args.download_resources:
        action = lambda: run_download_resources_resolved(
            args.project,
            resource_delay=args.resource_delay,
        )
    elif args.download_all_projects:
        action = lambda: run_download_all_projects(args.resource_delay, workers=args.workers)
    elif args.download_conversations:
        action = lambda: run_download_conversations(args.project)
    elif args.refresh_conversations:
        action = lambda: run_download_conversations(args.project, refresh=True)
    elif args.probe_orphan_file:
        action = lambda: run_probe_orphan_file(args.probe_orphan_file, args.project)
    elif args.audit_failures:
        action = lambda: run_audit_failures(args.project)
    elif args.probe_library_resource:
        action = lambda: run_probe_library_resource(args.project)
    elif args.audit_resolution:
        action = lambda: run_audit_resolution(args.project)
    elif args.probe_resolution:
        action = lambda: run_probe_resolution(args.project)
    elif args.test_delete_pdf:
        action = lambda: run_test_delete_pdf(args.project)
    elif args.list_pdfs:
        action = lambda: run_list_pdfs(args.project)
    elif args.resolve_library_file:
        action = lambda: run_resolve_library_file(args.resolve_library_file)
    elif args.delete_library_file:
        action = lambda: run_delete_library_file(*args.delete_library_file)
    elif args.probe_file_id:
        action = lambda: run_probe_file_id(args.probe_file_id)
    else:
        parser.print_help()
        return 0

    if args.project and (
        args.list_projects
        or args.download_all_projects
        or args.check
        or args.sample is not None
        or args.resolve_library_file is not None
        or args.delete_library_file is not None
        or args.probe_file_id is not None
    ):
        raise BackupCLIError(
            "--project no se usa con --list-projects, --download-all-projects, "
            "--check o --sample. "
            "Se usa con --download-conversations, --refresh-conversations, --probe-orphan-file, --inspect, --inventory, "
            "--probe-resource, --download-resources, --audit-failures, --audit-resolution, "
            "--probe-library-resource, --probe-resolution, --test-delete-pdf, --list-pdfs "
            "--resolve-library-file, --delete-library-file o --probe-file-id."
        )

    try:
        return action()

    except BackupCLIError as exc:
        print()
        print("ERROR")
        print("-----")
        print(exc)
        return 1

    except KeyboardInterrupt:
        print("\nCancelado por el usuario.")
        return 130

    except Exception as exc:
        print()
        print("ERROR NO CONTROLADO")
        print("-------------------")
        print(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
