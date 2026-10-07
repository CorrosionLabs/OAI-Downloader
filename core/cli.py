from __future__ import annotations

import argparse
import sys

from .config import APP_NAME, RESOURCE_DELAY_SECONDS


def configure_console_output() -> None:
    """Avoid aborting a backup on characters unsupported by the console."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="replace")
            except (AttributeError, ValueError):
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=APP_NAME,
    )

    group = parser.add_mutually_exclusive_group()

    parser.add_argument(
        "--project",
        type=str,
        metavar="NAME",
        help=(
            "Limita la operación al Project indicado. "
            "Ejemplo: --project \"Backup CLI Lab\""
        ),
    )

    parser.add_argument(
        "--resource-delay",
        type=float,
        default=RESOURCE_DELAY_SECONDS,
        metavar="SECONDS",
        help=(
            "Pausa entre descargas de recursos "
            f"(predeterminado: {RESOURCE_DELAY_SECONDS:.2f} s)."
        ),
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="Número de Projects procesados simultáneamente (mínimo: 1; predeterminado: 1).",
    )

    group.add_argument(
        "--probe-orphan-file",
        type=str,
        metavar="FILE_ID",
        help=(
            "Prueba un file_id conocido contra el primer library_file_id "
            "que ya no resuelva en el inventario actual."
        ),
    )

    group.add_argument(
        "--list-projects",
        action="store_true",
        help="Lista los Projects visibles con su ID y número de conversaciones.",
    )

    group.add_argument(
        "--check",
        action="store_true",
        help="Comprueba autenticación y enumera conversaciones/Projects.",
    )

    group.add_argument(
        "--sample",
        type=int,
        metavar="N",
        help="Descarga N conversaciones recientes como JSON bruto.",
    )

    group.add_argument(
        "--inspect",
        action="store_true",
        help="Audita los JSON descargados buscando referencias a recursos.",
    )

    group.add_argument(
        "--inventory",
        action="store_true",
        help="Genera un inventario JSON de archivos, imágenes y audio referenciados.",
    )

    group.add_argument(
        "--probe-resource",
        action="store_true",
        help="Resuelve y descarga un único recurso del inventario como prueba.",
    )

    group.add_argument(
        "--download-resources",
        action="store_true",
        help="Resuelve y descarga de forma masiva y reanudable recursos con file_id o library_file_id.",
    )

    group.add_argument(
        "--download-all-projects",
        action="store_true",
        help=(
            "Descarga conversaciones, inventario y recursos "
            "de todos los Projects visibles."
        ),
    )

    group.add_argument(
        "--download-conversations",
        action="store_true",
        help="Descarga todas las conversaciones activas y archivadas de forma reanudable.",
    )

    group.add_argument(
        "--refresh-conversations",
        action="store_true",
        help="Vuelve a descargar y sobrescribe los JSON locales de las conversaciones, aunque ya existan.",
    )

    group.add_argument(
        "--audit-failures",
        action="store_true",
        help="Clasifica los recursos fallidos y detecta referencias aún no cubiertas.",
    )

    group.add_argument(
        "--probe-library-resource",
        action="store_true",
        help="Descarga un único recurso que dependa solo de library_file_id.",
    )

    group.add_argument(
        "--audit-resolution",
        action="store_true",
        help="Audita todos los recursos únicos con una prueba binaria mínima de 1 byte, sin descargarlos completos.",
    )

    group.add_argument(
        "--probe-resolution",
        action="store_true",
        help="Prueba estrategias de resolución para un file_id y un library_file_id sin descargar recursos completos.",
    )

    group.add_argument(
        "--test-delete-pdf",
        action="store_true",
        help=(
            "Localiza un PDF ya respaldado y muestra sus identificadores "
            "como candidato para una futura prueba de borrado. No borra nada."
        ),
    )

    group.add_argument(
        "--list-pdfs",
        action="store_true",
        help="Lista todos los PDF encontrados en los inventarios del backup.",
    )

    group.add_argument(
        "--resolve-library-file",
        type=str,
        metavar="LIBFILE_ID",
        help=(
            "Resuelve un library_file_id a file_id sin descargar ni borrar nada."
        ),
    )

    group.add_argument(
        "--delete-library-file",
        nargs=3,
        metavar=("LIBFILE_ID", "FILE_ID", "FILE_NAME"),
        help=(
            "Envía un soft delete real para un archivo de Library. "
            "Requiere confirmación escribiendo BORRAR."
        ),
    )

    group.add_argument(
        "--probe-file-id",
        type=str,
        metavar="FILE_ID",
        help=(
            "Comprueba si un file_id sigue resolviendo y si su binario "
            "sigue accesible leyendo como máximo 1 byte."
        ),
    )

    return parser
