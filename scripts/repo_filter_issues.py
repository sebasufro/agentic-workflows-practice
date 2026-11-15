#!/usr/bin/env python3
"""
repo_filter_issues.py — Separa los issues y filtra los de GitHub Agentic Workflows.

Lee las páginas crudas `raw/page_*.json` que genera `repo_fetch_issues.py`,
extrae cada issue (descartando pull requests) y guarda un JSON individual
`issue_{número}.json` en la carpeta de su repositorio:

    issues/{dueño}__{repo}/
    ├── raw/
    │   ├── page_0001.json
    │   └── page_0002.json
    ├── issue_123.json
    └── issue_124.json

Además, busca señales de `gh-aw` en el título, la descripción y las etiquetas
de cada issue:

- Palabras clave como `gh-aw`, `agentic workflow`, `[aw]`, `.lock.yml` o
  `safe outputs`.
- Etiquetas como `gh-aw`, `agentic` o `agentic-workflows`.
- Opcionalmente, términos de CI/CD como `.github/workflows`, `workflow_dispatch`
  o `pipeline`.

El resultado es un CSV combinado en `filtered_issues/agentic_issues.csv` con las
filas que coinciden y una columna `coincidencias` que indica por qué se incluyó
cada issue. Todo se hace con datos locales: no se consulta la API.

También puede leer los `issue_*.json` ya existentes (`--source json`) o los CSV
históricos (`--source csv`). En modo `auto` usa primero las páginas crudas,
después los JSON y por último los CSV, conservando una sola fila por issue.

Ejemplos:

    python scripts/repo_filter_issues.py
    python scripts/repo_filter_issues.py --include-pipeline
    python scripts/repo_filter_issues.py --keyword "gh-aw" --keyword "agentic"
    python scripts/repo_filter_issues.py --source raw
    python scripts/repo_filter_issues.py --source json
    python scripts/repo_filter_issues.py --source csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT_DIR = PROJECT_ROOT / "issues"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "filtered_issues"
DEFAULT_OUTPUT = DEFAULT_OUTPUT_DIR / "agentic_issues.csv"
RAW_DIR_NAME = "raw"

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
) -> tuple[list[Path], list[Path], list[Path]]:
    """Devuelve (páginas crudas, JSON individuales, CSV) según la fuente."""
    if not path.exists():
        raise FileNotFoundError(f"No existe la ruta de entrada: {path}")

    output_resolved = output.resolve()

    if path.is_file():
        suffix = path.suffix.lower()
        if suffix == ".csv":
            if source not in {"auto", "csv"}:
                raise ValueError(
                    f"El archivo {path} no coincide con --source {source}."
                )
            return [], [], [path]
        if suffix == ".json":
            if source == "raw":
                return [path], [], []
            if source == "json":
                return [], [path], []
            if source == "auto":
                if path.name.startswith("page_"):
                    return [path], [], []
                return [], [path], []
            raise ValueError(f"El archivo {path} no coincide con --source {source}.")
        raise ValueError(
            f"Extensión no soportada en {path}: se esperaba .json o .csv."
        )

    raw_files: list[Path] = []
    json_files: list[Path] = []
    csv_files: list[Path] = []

    if source in {"auto", "raw"}:
        raw_files = [
            file
            for file in sorted(path.rglob("page_*.json"))
            if file.parent.name == RAW_DIR_NAME and file.resolve() != output_resolved
        ]
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

    return raw_files, json_files, csv_files


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


def issues_from_raw_pages(files: list[Path]) -> tuple[list[dict[str, Any]], int]:
    """Extrae y deduplica los issues de varias páginas crudas.

    Devuelve (issues, cantidad de archivos con error de lectura).
    """
    by_number: dict[Any, dict[str, Any]] = {}
    order: list[Any] = []
    errors = 0

    for file in sorted(files):
        try:
            issues = load_json_issues(file)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  aviso: no se pudo leer {display_path(file)}: {exc}", file=sys.stderr)
            errors += 1
            continue

        for issue in issues:
            number = issue.get("number")
            key: Any = number if number is not None else f"sin-numero-{len(order)}"
            if key not in by_number:
                order.append(key)
            by_number[key] = issue

    return [by_number[key] for key in order], errors


def repository_directory(path: Path) -> Path:
    """Encuentra la carpeta del repositorio desde un issue o una página cruda."""
    for directory in path.parents:
        if (directory / "metadata.json").is_file():
            return directory
        if "__" in directory.name:
            return directory
    return path.parent


def write_json_file(path: Path, data: Any) -> None:
    """Escribe JSON de forma atómica para no dejar archivos a medio escribir."""
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp_path, path)


def write_individual_issues(
    repo_dir: Path,
    issues: list[dict[str, Any]],
    *,
    cleanup: bool,
    verbose: bool,
) -> int:
    """Escribe issue_{número}.json y elimina los obsoletos si cleanup es True."""
    staging_dir = repo_dir / ".issues.tmp"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)

    new_numbers: set[Any] = set()
    written = 0
    skipped = 0
    try:
        for issue in issues:
            number = issue.get("number")
            if number is None:
                skipped += 1
                continue
            new_numbers.add(number)
            write_json_file(staging_dir / f"issue_{number}.json", issue)
            written += 1

        # Primero se escriben los nuevos y recién después se borran los
        # obsoletos, para no perder datos si algo falla a mitad de camino.
        for path in staging_dir.iterdir():
            os.replace(path, repo_dir / path.name)

        if cleanup:
            for path in repo_dir.glob("issue_*.json"):
                stem = path.stem[len("issue_") :]
                key: Any = int(stem) if stem.isdigit() else stem
                if key not in new_numbers:
                    path.unlink()
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    if skipped and verbose:
        print(f"    aviso: {skipped} issues sin número no se guardaron como JSON")

    return written


def full_raw_page_set(directory: Path, files: list[Path]) -> bool:
    """Indica si `files` cubre todas las páginas crudas del repositorio."""
    raw_dir = directory / RAW_DIR_NAME
    if not raw_dir.is_dir():
        return False

    processed = {file.resolve() for file in files}
    return all(file.resolve() in processed for file in raw_dir.glob("page_*.json"))


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
        raw_files, json_files, csv_files = input_files(
            input_path, output_path, args.source
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if not raw_files and not json_files and not csv_files:
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
    metadata_cache: dict[Path, dict[str, Any]] = {}
    scanned = 0
    read_errors = 0
    json_written = 0
    warned_missing = False
    regenerated: set[Path] = set()

    # Las páginas crudas son la fuente principal: primero se separan los
    # issues en JSON individuales y después se filtran.
    if raw_files:
        groups: dict[Path, list[Path]] = {}
        for file in raw_files:
            groups.setdefault(repository_directory(file), []).append(file)

        for directory, files in sorted(
            groups.items(), key=lambda item: display_path(item[0])
        ):
            issues, errors = issues_from_raw_pages(files)
            read_errors += errors

            cleanup = full_raw_page_set(directory, files) and errors == 0
            written = 0
            try:
                written = write_individual_issues(
                    directory,
                    issues,
                    cleanup=cleanup,
                    verbose=verbose,
                )
                json_written += written
                if cleanup:
                    regenerated.add(directory.resolve())
            except OSError as exc:
                print(
                    f"  aviso: no se pudieron escribir los JSON de "
                    f"{display_path(directory)}: {exc}",
                    file=sys.stderr,
                )
                read_errors += 1

            directory_scanned = 0
            directory_matched = 0

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
                    f"{directory_matched} coincidencias, "
                    f"{written} JSON individuales"
                )

    # JSON individuales ya existentes (o generados en una ejecución anterior).
    # Se omiten los que se acaban de regenerar desde `raw/` y los que hayan
    # quedado obsoletos, para no releer datos duplicados ni inexistentes.
    json_files = [
        file
        for file in json_files
        if file.is_file()
        and repository_directory(file).resolve() not in regenerated
    ]
    if json_files:
        groups = {}
        for file in json_files:
            groups.setdefault(repository_directory(file), []).append(file)

        for directory, files in sorted(
            groups.items(), key=lambda item: display_path(item[0])
        ):
            directory_scanned = 0
            directory_matched = 0

            for file in files:
                try:
                    issues = load_json_issues(file)
                except (OSError, json.JSONDecodeError) as exc:
                    print(
                        f"  aviso: no se pudo leer {display_path(file)}: {exc}",
                        file=sys.stderr,
                    )
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
                        "se buscará solo en el título.",
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
        print(f"Páginas crudas leídas: {len(raw_files)}")
        print(f"JSON individuales escritos: {json_written}")
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
            "Separa los issues de las páginas crudas en JSON individuales y "
            "filtra los relacionados con GitHub Agentic Workflows, sin "
            "consultar la API."
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
        choices=("auto", "raw", "json", "csv"),
        default="auto",
        help=(
            "Fuente de datos: auto (raw + JSON + CSV, en ese orden), raw "
            "(solo page_*.json), json (solo issue_*.json) o csv (solo CSV)"
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
