#!/usr/bin/env python3
"""
repo_filter_issues.py — Filtra issues relacionados con GitHub Agentic Workflows.

Lee los CSV generados por `repo_fetch_issues.py` en `issues/` y busca señales de
la herramienta en el título, la descripción y las etiquetas del issue:

- Palabras clave como `gh-aw`, `agentic workflow`, `[aw]`, `.lock.yml` o
  `safe outputs`.
- Etiquetas como `gh-aw`, `agentic` o `agentic-workflows`.
- Opcionalmente, términos de CI/CD como `.github/workflows`, `workflow_dispatch`
  o `pipeline`.

El resultado es un CSV combinado en `filtered_issues/agentic_issues.csv` con las
filas que coinciden y una columna `coincidencias` que indica por qué se incluyó
cada issue. Así no se mezcla con los CSV originales de `issues/`.

Los CSV que solo tienen `titulo` se pueden filtrar igual, pero solo por título.
Para buscar también descripción y etiquetas:
- regenerarlos con `repo_fetch_issues.py --with-details`, o
- usar `--fetch-details` para consultar cada issue a la API.

Ejemplos:

    python scripts/repo_filter_issues.py
    python scripts/repo_filter_issues.py --include-pipeline
    python scripts/repo_filter_issues.py --keyword "gh-aw" --keyword "agentic"
    python scripts/repo_filter_issues.py --fetch-details
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Any, Iterator

from repo_search_agentic import (
    AuthenticationError,
    GitHubAPI,
    build_api,
    load_and_report_token,
)

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


def input_csv_files(path: Path, output: Path) -> list[Path]:
    """Devuelve los CSV a leer, excluyendo el archivo de salida."""
    output_resolved = output.resolve()

    if path.is_dir():
        files = sorted(path.glob("*.csv"))
    elif path.is_file():
        files = [path]
    else:
        raise FileNotFoundError(f"No existe la ruta de entrada: {path}")

    return [file for file in files if file.resolve() != output_resolved]


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


def parse_issue_target(row: dict[str, str]) -> tuple[str, str, str] | None:
    """Obtiene owner, repo y número desde link_issue o desde las columnas."""
    link = row.get("link_issue") or ""
    match = re.search(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)", link)
    if match:
        return match.group(1), match.group(2), match.group(3)

    number_match = re.search(r"/issues/(\d+)", link)
    owner = (row.get("dueño_repositorio") or "").strip()
    name = (row.get("nombre_repositorio") or "").strip()
    if number_match and owner and name:
        return owner, name, number_match.group(1)
    return None


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


def command_filter(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    output_path = Path(args.out)

    try:
        files = input_csv_files(input_path, output_path)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if not files:
        print(f"No hay archivos CSV para leer en {display_path(input_path)}.", file=sys.stderr)
        return 2

    keywords = dedupe_terms(list(DEFAULT_KEYWORDS) + split_terms(args.keyword))
    if args.include_pipeline:
        keywords = dedupe_terms(keywords + list(PIPELINE_KEYWORDS))
    label_keywords = dedupe_terms(list(DEFAULT_LABEL_KEYWORDS) + split_terms(args.label))

    api: GitHubAPI | None = None
    if args.fetch_details:
        try:
            load_and_report_token(args)
        except FileNotFoundError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        api = build_api(args)
        if not api.authenticated:
            print(
                "Aviso: sin GITHUB_TOKEN el límite es 60 peticiones/hora; "
                "puede no alcanzar para consultar todos los detalles.",
                file=sys.stderr,
            )

    verbose = not args.quiet
    fields_order: list[str] = []
    matches_rows: list[dict[str, str]] = []
    seen_links: set[str] = set()
    scanned = 0
    detail_errors = 0
    details_fetched = 0
    warned_missing = False
    detail_cache: dict[tuple[str, str, str], dict[str, Any] | None] = {}

    for file, file_fields, file_rows in iter_csv_rows(files):
        file_scanned = 0
        file_matched = 0

        for field in file_fields:
            add_field(fields_order, field)

        for row in file_rows:
            file_scanned += 1
            scanned += 1

            has_description = "descripcion" in file_fields
            has_labels = "etiquetas" in file_fields

            if not has_description or not has_labels:
                if args.fetch_details:
                    target = parse_issue_target(row)
                    if target is None:
                        print(
                            f"  aviso: no se pudo obtener owner/repo/número "
                            f"para {row.get('link_issue') or '(sin link)'}",
                            file=sys.stderr,
                        )
                    else:
                        if target not in detail_cache:
                            try:
                                detail_cache[target] = api.get_json(
                                    f"/repos/{target[0]}/{target[1]}/issues/{target[2]}"
                                )[0]
                                details_fetched += 1
                            except AuthenticationError:
                                raise
                            except RuntimeError as exc:
                                print(f"  aviso: {exc}", file=sys.stderr)
                                detail_cache[target] = None
                                detail_errors += 1

                        issue = detail_cache[target]
                        if issue:
                            if not has_description:
                                row["descripcion"] = issue.get("body") or ""
                                add_field(fields_order, "descripcion")
                            if not has_labels:
                                row["etiquetas"] = issue_labels(issue)
                                add_field(fields_order, "etiquetas")
                elif not warned_missing:
                    print(
                        "Aviso: hay CSV sin columnas descripcion/etiquetas; "
                        "se buscará solo en el título. Regenerá los CSV con "
                        "repo_fetch_issues.py --with-details o usá "
                        "--fetch-details.",
                        file=sys.stderr,
                    )
                    warned_missing = True

            matches = match_issue(row, keywords, label_keywords)
            if not matches:
                continue

            link = (row.get("link_issue") or "").strip().lower()
            if link and link in seen_links:
                continue
            if link:
                seen_links.add(link)

            filtered = dict(row)
            filtered["coincidencias"] = "; ".join(matches)
            matches_rows.append(filtered)
            file_matched += 1

        if verbose:
            print(
                f"{display_path(file)}: {file_scanned} issues, "
                f"{file_matched} coincidencias"
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=output_fields(fields_order),
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(matches_rows)

    if verbose:
        print()
        print(f"Issues revisados: {scanned}")
        print(f"Coincidencias: {len(matches_rows)}")
        if args.fetch_details:
            print(f"Detalles consultados: {details_fetched}")
        print(f"CSV filtrado: {display_path(output_path)}")

    if detail_errors:
        print(f"Detalles con error: {detail_errors}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Filtra issues relacionados con GitHub Agentic Workflows a partir "
            "de los CSV generados por repo_fetch_issues.py."
        )
    )
    parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT_DIR),
        help="Directorio con CSV o archivo CSV de entrada (por defecto: issues/)",
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
    parser.add_argument(
        "--fetch-details",
        action="store_true",
        help="Consultar la API para completar descripción y etiquetas faltantes",
    )
    parser.add_argument(
        "--token",
        help="Token de GitHub. Si se omite, se usa GITHUB_TOKEN/GH_TOKEN "
        "del entorno o del archivo .env",
    )
    parser.add_argument(
        "--env-file",
        help="Archivo .env desde el que leer GITHUB_TOKEN (por defecto: ./.env)",
    )
    parser.add_argument("--quiet", action="store_true", help="Oculta mensajes de progreso")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        return int(command_filter(args))
    except AuthenticationError as exc:
        print(f"Error de autenticación: {exc}", file=sys.stderr)
        print(
            "Revisá el token en .env o en las variables GITHUB_TOKEN/GH_TOKEN.",
            file=sys.stderr,
        )
        return 2
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrumpido.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
