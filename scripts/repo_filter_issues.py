#!/usr/bin/env python3
"""
repo_filter_issues.py — Filtra issues relacionados con GitHub Agentic Workflows.

Lee los JSON individuales `issue_*.json` que genera `repo_fetch_issues.py`
dentro de `issues/` y busca señales de la herramienta en el título, la
descripción y las etiquetas del issue:

- Palabras clave como `gh-aw`, `agentic workflow`, `[aw]`, `.lock.yml` o
  `safe outputs`.
- Etiquetas como `gh-aw`, `agentic` o `agentic-workflows`.
- Opcionalmente, términos de CI/CD como `.github/workflows`, `workflow_dispatch`
  o `pipeline`.

Como los JSON ya contienen el cuerpo y las etiquetas del issue, el filtro no
necesita consultar la API. También puede leer los CSV históricos de `issues/`;
en modo `auto` usa primero los JSON y completa con los CSV, conservando una sola
fila por `link_issue`.

El resultado es un CSV combinado en `filtered_issues/agentic_issues.csv` con las
filas que coinciden y una columna `coincidencias` que indica por qué se incluyó
cada issue. Así no se mezcla con los datos originales de `issues/`.

Ejemplos:

    python scripts/repo_filter_issues.py
    python scripts/repo_filter_issues.py --include-pipeline
    python scripts/repo_filter_issues.py --keyword "gh-aw" --keyword "agentic"
    python scripts/repo_filter_issues.py --source json
    python scripts/repo_filter_issues.py --source csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT_DIR = PROJECT_ROOT / "issues"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "filtered_issues"
DEFAULT_OUTPUT = DEFAULT_OUTPUT_DIR / "agentic_issues.csv"

DEFAULT_KEYWORDS = (
    "gh-aw",
    "gh aw",
    "github agentic workflow",
    "agentic workflow",
    "agentic-workflow",
    "[aw]",
    ".lock.yml",
    "safe output",
    "safe-output",
)
DEFAULT_LABEL_KEYWORDS = (
    "gh-aw",
    "agentic",
    "agentic-workflow",
    "agentic-workflows",
)
PIPELINE_KEYWORDS = (
    ".github/workflows",
    "github actions",
    "workflow_dispatch",
    "workflow run",
    "ci/cd",
    "pipeline",
)
CANONICAL_FIELDS = (
    "nombre_repositorio",
    "dueño_repositorio",
    "titulo",
    "descripcion",
    "etiquetas",
    "salida",
    "link_issue",
)


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def input_files(
    path: Path,
    output: Path,
    source: str,
) -> tuple[list[Path], list[Path]]:
    """Devuelve (JSON individuales, CSV) según la fuente pedida."""
    if not path.exists():
        raise FileNotFoundError(f"No existe la ruta de entrada: {path}")

    output_resolved = output.resolve()

    if path.is_file():
        suffix = path.suffix.lower()
        if suffix == ".json" and source in {"auto", "json"}:
            return [path], []
        if suffix == ".csv" and source in {"auto", "csv"}:
            return [], [path]
        raise ValueError(
            f"El archivo {path} no coincide con --source {source} "
            "(se esperaba .json o .csv)."
        )

    json_files: list[Path] = []
    csv_files: list[Path] = []
    if source in {"auto", "json"}:
        json_files = [
            file
            for file in sorted(path.rglob("issue_*.json"))
            if file.resolve() != output_resolved
        ]
    if source in {"auto", "csv"}:
        csv_files = [
            file
            for file in sorted(path.rglob("*.csv"))
            if file.resolve() != output_resolved
        ]
    return json_files, csv_files


def split_terms(values: list[str] | None) -> list[str]:
    terms: list[str] = []
    for value in values or []:
        for term in value.split(","):
            term = term.strip()
            if term:
                terms.append(term)
    return terms


def dedupe_terms(terms: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            result.append(term)
    return result


def issue_labels(issue: dict[str, Any]) -> str:
    names: list[str] = []
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else str(label)
        if name:
            names.append(str(name))
    return ";".join(names)


def match_issue(
    row: dict[str, str],
    keywords: list[str],
    label_keywords: list[str],
) -> list[str]:
    title = row.get("titulo") or ""
    description = row.get("descripcion") or ""
    labels_text = row.get("etiquetas") or ""
    haystack = "\n".join([title, description, labels_text])
    haystack_lower = haystack.lower()

    matches: list[str] = []
    for keyword in keywords:
        if keyword.lower() in haystack_lower:
            matches.append(keyword)

    labels = [
        label.strip()
        for label in re.split(r"[;|]", labels_text)
        if label.strip()
    ]
    for label in labels:
        label_lower = label.lower()
        for keyword in label_keywords:
            if keyword.lower() in label_lower:
                matches.append(f"label:{label}")
                break

    return list(dict.fromkeys(matches))


def iter_csv_rows(
    files: list[Path],
) -> Iterator[tuple[Path, list[str], list[dict[str, str]]]]:
    for file in files:
        with file.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = list(reader.fieldnames or [])
            rows = list(reader)
        yield file, fields, rows


def load_json_issues(path: Path) -> list[dict[str, Any]]:
    """Carga un issue individual o una página cruda y descarta pull requests."""
    data = json.loads(path.read_text(encoding="utf-8"))

    if isinstance(data, dict):
        items: list[Any] = [data]
    elif isinstance(data, list):
        items = data
    else:
        return []

    return [
        item
        for item in items
        if isinstance(item, dict) and "pull_request" not in item
    ]


def repository_directory(path: Path) -> Path:
    """Encuentra la carpeta del repositorio desde un issue o una página cruda."""
    for directory in path.parents:
        if (directory / "metadata.json").is_file():
            return directory
        if "__" in directory.name:
            return directory
    return path.parent


def load_metadata(directory: Path) -> dict[str, Any]:
    path = directory / "metadata.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def repository_identity(
    directory: Path,
    link: str,
    cache: dict[Path, dict[str, Any]],
) -> tuple[str, str]:
    """Obtiene dueño y repositorio desde metadata, el link o el nombre de carpeta."""
    key = directory.resolve()
    if key not in cache:
        cache[key] = load_metadata(directory)
    metadata = cache[key]

    repository = metadata.get("repository")
    if isinstance(repository, str) and "/" in repository:
        owner, name = repository.split("/", 1)
        if owner and name:
            return owner, name

    match = re.search(r"github\.com/([^/]+)/([^/]+)/", link)
    if match:
        return match.group(1), match.group(2)

    if "__" in directory.name:
        owner, name = directory.name.split("__", 1)
        if owner and name:
            return owner, name

    return "", ""


def json_issue_row(
    issue: dict[str, Any],
    directory: Path,
    cache: dict[Path, dict[str, Any]],
) -> dict[str, str]:
    """Convierte un issue JSON en una fila con las columnas del filtro."""
    number = issue.get("number")
    link = issue.get("html_url") or ""

    owner, name = repository_identity(directory, link, cache)
    if not link and number is not None and owner and name:
        link = f"https://github.com/{owner}/{name}/issues/{number}"

    return {
        "nombre_repositorio": name,
        "dueño_repositorio": owner,
        "titulo": issue.get("title") or "",
        "descripcion": issue.get("body") or "",
        "etiquetas": issue_labels(issue),
        "salida": issue.get("state") or "",
        "link_issue": link,
    }


def row_richness(row: dict[str, str]) -> int:
    """Cuenta cuántos datos útiles trae una fila duplicada."""
    return sum(
        1
        for field in ("descripcion", "etiquetas")
        if (row.get(field) or "").strip()
    )


def add_field(fields: list[str], field: str) -> None:
    if field not in fields:
        fields.append(field)


def output_fields(fields: list[str]) -> list[str]:
    """Ordena las columnas conocidas y deja `coincidencias` al final."""
    ordered = [field for field in CANONICAL_FIELDS if field in fields]
    ordered.extend(
        field
        for field in fields
        if field not in ordered and field != "coincidencias"
    )
    ordered.append("coincidencias")
    return ordered


def add_match(
    row: dict[str, str],
    matches: list[str],
    matches_rows: list[dict[str, str]],
    seen_links: dict[str, int],
) -> bool:
    """Agrega una coincidencia o mejora la fila duplicada ya guardada."""
    filtered = dict(row)
    filtered["coincidencias"] = "; ".join(matches)

    link = (row.get("link_issue") or "").strip().lower()
    if link and link in seen_links:
        index = seen_links[link]
        if row_richness(filtered) > row_richness(matches_rows[index]):
            matches_rows[index] = filtered
        return False
    if link:
        seen_links[link] = len(matches_rows)

    matches_rows.append(filtered)
    return True


def command_filter(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    output_path = Path(args.out)

    try:
        json_files, csv_files = input_files(input_path, output_path, args.source)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if not json_files and not csv_files:
        print(
            f"No hay archivos para leer en {display_path(input_path)} "
            f"(--source {args.source}).",
            file=sys.stderr,
        )
        return 2

    keywords = dedupe_terms(list(DEFAULT_KEYWORDS) + split_terms(args.keyword))
    if args.include_pipeline:
        keywords = dedupe_terms(keywords + list(PIPELINE_KEYWORDS))
    label_keywords = dedupe_terms(list(DEFAULT_LABEL_KEYWORDS) + split_terms(args.label))

    verbose = not args.quiet
    fields_order: list[str] = []
    matches_rows: list[dict[str, str]] = []
    seen_links: dict[str, int] = {}
    scanned = 0
    read_errors = 0
    warned_missing = False

    # Los JSON individuales traen el cuerpo y las etiquetas completas, así que
    # se procesan primero y sirven para enriquecer cualquier CSV duplicado.
    if json_files:
        groups: dict[Path, list[Path]] = {}
        for file in json_files:
            groups.setdefault(repository_directory(file), []).append(file)

        metadata_cache: dict[Path, dict[str, Any]] = {}

        for directory, files in sorted(
            groups.items(), key=lambda item: display_path(item[0])
        ):
            directory_scanned = 0
            directory_matched = 0

            for file in files:
                try:
                    issues = load_json_issues(file)
                except (OSError, json.JSONDecodeError) as exc:
                    print(f"  aviso: no se pudo leer {display_path(file)}: {exc}", file=sys.stderr)
                    read_errors += 1
                    continue

                for issue in issues:
                    row = json_issue_row(issue, directory, metadata_cache)
                    for field in CANONICAL_FIELDS:
                        add_field(fields_order, field)

                    directory_scanned += 1
                    scanned += 1

                    matches = match_issue(row, keywords, label_keywords)
                    if matches:
                        directory_matched += 1
                        add_match(row, matches, matches_rows, seen_links)

            if verbose:
                print(
                    f"{display_path(directory)}: {directory_scanned} issues, "
                    f"{directory_matched} coincidencias"
                )

    if csv_files:
        for file, file_fields, file_rows in iter_csv_rows(csv_files):
            file_scanned = 0
            file_matched = 0

            for field in file_fields:
                add_field(fields_order, field)

            for row in file_rows:
                file_scanned += 1
                scanned += 1

                if (
                    ("descripcion" not in file_fields or "etiquetas" not in file_fields)
                    and not warned_missing
                ):
                    print(
                        "Aviso: hay CSV sin columnas descripcion/etiquetas; "
                        "se buscará solo en el título. Usá los JSON de "
                        "issues/ o regenerá los CSV con "
                        "repo_fetch_issues.py --with-details.",
                        file=sys.stderr,
                    )
                    warned_missing = True

                matches = match_issue(row, keywords, label_keywords)
                if matches:
                    file_matched += 1
                    add_match(row, matches, matches_rows, seen_links)

            if verbose:
                print(
                    f"{display_path(file)}: {file_scanned} issues, "
                    f"{file_matched} coincidencias"
                )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(output_path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=output_fields(fields_order),
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(matches_rows)
    os.replace(temp_path, output_path)

    if verbose:
        print()
        print(f"Archivos JSON leídos: {len(json_files)}")
        print(f"Archivos CSV leídos: {len(csv_files)}")
        print(f"Issues revisados: {scanned}")
        print(f"Coincidencias: {len(matches_rows)}")
        print(f"CSV filtrado: {display_path(output_path)}")

    if read_errors:
        print(f"Archivos con error de lectura: {read_errors}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Filtra issues relacionados con GitHub Agentic Workflows usando "
            "los JSON individuales (y opcionalmente los CSV) generados por "
            "repo_fetch_issues.py, sin consultar la API."
        )
    )
    parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT_DIR),
        help=(
            "Directorio (se recorre recursivamente) o archivo .json/.csv de "
            "entrada (por defecto: issues/)"
        ),
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUTPUT),
        help=(
            "CSV filtrado de salida "
            "(por defecto: filtered_issues/agentic_issues.csv)"
        ),
    )
    parser.add_argument(
        "--source",
        choices=("auto", "json", "csv"),
        default="auto",
        help=(
            "Fuente de datos: auto (JSON + CSV, priorizando JSON), json "
            "(solo issue_*.json) o csv (solo CSV históricos)"
        ),
    )
    parser.add_argument(
        "--keyword",
        action="append",
        default=[],
        metavar="TEXTO",
        help="Palabra clave adicional, repetible o separada por comas",
    )
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        metavar="TEXTO",
        help="Etiqueta adicional a buscar, repetible o separada por comas",
    )
    parser.add_argument(
        "--include-pipeline",
        action="store_true",
        help="Incluir términos generales de CI/CD (workflow, pipeline, actions)",
    )
    parser.add_argument("--quiet", action="store_true", help="Oculta mensajes de progreso")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        return int(command_filter(args))
    except OSError as exc:
        print(f"Error de archivos: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrumpido.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
