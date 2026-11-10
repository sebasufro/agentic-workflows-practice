#!/usr/bin/env python3
"""
repo_fetch_issues.py — Exporta los issues de uno o varios repositorios a CSV.

Recorre los repositorios indicados, consulta sus issues (abiertos y cerrados),
descarta los pull requests y escribe una fila por issue con:

    nombre_repositorio,dueño_repositorio,titulo,salida,link_issue

`titulo` contiene el título del issue, `salida` su estado actual (`open` o
`closed`) y `link_issue` es la URL pública directa al issue, no la URL de la API.
Con `--with-details` se agregan `descripcion` (cuerpo del issue) y `etiquetas`
(labels separadas por `;`) después de `titulo`; el filtro de issues puede usar
esas columnas sin volver a consultar la API.

Cuando se seleccionan repositorios (`--repo`, `--limit`, `--sample`, `--org`,
etc.), se genera un CSV por repositorio dentro de `issues/`, con el nombre
`{repo}_issues.csv`. Sin selección se genera un único `issues.csv`.

Ejemplos:

    # Todos los repositorios de repos.json -> issues.csv
    python scripts/repo_fetch_issues.py

    # Un repositorio -> issues/vscode-powershell_issues.csv
    python scripts/repo_fetch_issues.py --repo PowerShell/vscode-powershell

    # Los primeros 10 de repos.json -> un CSV por repo en issues/
    python scripts/repo_fetch_issues.py --repos-file repos.json --limit 10

    # 10 al azar, de forma reproducible -> un CSV por repo en issues/
    python scripts/repo_fetch_issues.py --repos-file repos.json \\
        --sample 10 --seed 42

    # Incluir también descripción y etiquetas (columnas adicionales)
    python scripts/repo_fetch_issues.py --repo PowerShell/vscode-powershell \\
        --with-details

    # Forzar un único CSV aunque haya selección
    python scripts/repo_fetch_issues.py --repos-file repos.json --limit 10 \\
        --combined --out issues-10.csv

    # Ver la selección y los archivos de salida sin llamar a la API
    python scripts/repo_fetch_issues.py --repos-file repos.json --org PowerShell \\
        --limit 5 --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path
from typing import Any, Iterator

from repo_search_agentic import (
    AuthenticationError,
    GitHubAPI,
    build_api,
    load_and_report_token,
)

CSV_FIELDS = (
    "nombre_repositorio",
    "dueño_repositorio",
    "titulo",
    "salida",
    "link_issue",
)
DETAILED_CSV_FIELDS = (
    "nombre_repositorio",
    "dueño_repositorio",
    "titulo",
    "descripcion",
    "etiquetas",
    "salida",
    "link_issue",
)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ISSUES_DIR = PROJECT_ROOT / "issues"


def normalize_repository(value: str) -> str:
    """Acepta owner/repo, una URL de GitHub o una ruta terminada en .git."""
    repo = value.strip()
    if repo.startswith(("https://github.com/", "http://github.com/")):
        repo = repo.split("github.com/", 1)[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return repo.strip("/")


def load_repositories(path: Path) -> list[str]:
    """Lee repos.json, una lista JSON o texto con owner/repo por línea."""
    text = path.read_text(encoding="utf-8")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [
            normalize_repository(line)
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    if isinstance(data, dict):
        repositories = data.get("repositories", data)
        if isinstance(repositories, dict):
            return sorted(normalize_repository(key) for key in repositories)
        if isinstance(repositories, list):
            return sorted(normalize_repository(str(item)) for item in repositories)

    if isinstance(data, list):
        return sorted(normalize_repository(str(item)) for item in data)

    raise ValueError(f"Formato de repositorios no reconocido: {path}")


def select_repositories(
    repos: list[str],
    *,
    only: list[str] | None = None,
    exclude: list[str] | None = None,
    orgs: list[str] | None = None,
    limit: int | None = None,
    sample: int | None = None,
    seed: int | None = None,
) -> list[str]:
    """Filtra la lista de repositorios antes de gastar llamadas a la API."""
    only_set = {normalize_repository(item).lower() for item in (only or [])}
    exclude_set = {normalize_repository(item).lower() for item in (exclude or [])}
    org_set = {item.strip().lower().lstrip("@") for item in (orgs or []) if item.strip()}

    selected: list[str] = []
    for repo in repos:
        key = repo.lower()
        if only_set and key not in only_set:
            continue
        if key in exclude_set:
            continue
        owner = key.split("/", 1)[0]
        if org_set and owner not in org_set:
            continue
        selected.append(repo)

    if sample is not None and sample < len(selected):
        selected = sorted(random.Random(seed).sample(selected, sample))

    if limit is not None:
        selected = selected[:limit]

    return selected


def resolve_repositories(args: argparse.Namespace) -> list[str]:
    """Combina --repo y --repos-file; si no hay ninguno, usa repos.json."""
    repos: list[str] = [normalize_repository(repo) for repo in (args.repo or [])]

    if args.repos_file:
        repos.extend(load_repositories(Path(args.repos_file)))
    elif not repos:
        repos.extend(load_repositories(Path("repos.json")))

    # Deduplica sin perder el orden de entrada.
    return list(dict.fromkeys(repo for repo in repos if repo))


def iter_issues(
    api: GitHubAPI,
    repo: str,
    max_pages: int = 0,
    verbose: bool = True,
) -> Iterator[dict[str, Any]]:
    """Itera issues reales (sin pull requests) de todas las páginas."""
    page = 1

    while True:
        data, _ = api.get_json(
            f"/repos/{repo}/issues",
            {"state": "all", "per_page": 100, "page": page},
        )
        if data is None:
            if verbose:
                print("    sin issues o issues deshabilitados")
            return

        batch = data if isinstance(data, list) else []
        real_issues = [issue for issue in batch if "pull_request" not in issue]

        if verbose:
            print(
                f"    issues página {page}: {len(batch)} elementos, "
                f"{len(real_issues)} issues"
            )

        yield from real_issues

        if len(batch) < 100:
            break
        if max_pages and page >= max_pages:
            break
        page += 1


def repository_name(repo: str) -> str:
    _, _, name = repo.partition("/")
    return name or repo.replace("/", "__")


def build_repo_output_paths(repos: list[str], directory: Path) -> dict[str, Path]:
    """Asigna issues/{repo}_issues.csv, desambiguando nombres repetidos."""
    counts: dict[str, int] = {}
    for repo in repos:
        name = repository_name(repo)
        counts[name] = counts.get(name, 0) + 1

    paths: dict[str, Path] = {}
    used: set[str] = set()
    for repo in repos:
        owner, _, name = repo.partition("/")
        stem = name or repo.replace("/", "__")
        if counts.get(stem, 0) > 1 and owner:
            stem = f"{owner}__{name}"
        stem = stem.replace("/", "__").replace("\\", "__").strip() or "repo"

        filename = f"{stem}_issues.csv"
        candidate = directory / filename
        suffix = 2
        while candidate.name.lower() in used:
            candidate = directory / f"{stem}_{suffix}_issues.csv"
            suffix += 1
        used.add(candidate.name.lower())
        paths[repo] = candidate

    return paths


def display_output_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def should_split_output(args: argparse.Namespace) -> bool:
    if args.split:
        return True
    if args.combined:
        return False

    default_repos_file = (PROJECT_ROOT / "repos.json").resolve()
    repos_file = Path(args.repos_file).resolve() if args.repos_file else None
    custom_repos_file = repos_file is not None and repos_file != default_repos_file

    return bool(
        args.repo
        or args.only
        or args.exclude
        or args.org
        or args.limit is not None
        or args.sample is not None
        or custom_repos_file
    )


def csv_fields(with_details: bool) -> tuple[str, ...]:
    return DETAILED_CSV_FIELDS if with_details else CSV_FIELDS


def issue_labels(issue: dict[str, Any]) -> str:
    names: list[str] = []
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else str(label)
        if name:
            names.append(str(name))
    return ";".join(names)


def write_repo_issues(
    api: GitHubAPI,
    repo: str,
    writer: Any,
    max_pages: int,
    verbose: bool,
    with_details: bool = False,
) -> int:
    owner, separator, name = repo.partition("/")
    if not separator or not owner or not name:
        raise ValueError(f"Nombre de repositorio inválido: {repo}")

    count = 0
    for issue in iter_issues(api, repo, max_pages=max_pages, verbose=verbose):
        number = issue.get("number")
        link = issue.get("html_url") or (
            f"https://github.com/{repo}/issues/{number}"
            if number is not None
            else ""
        )
        row: list[str] = [name, owner, issue.get("title") or ""]
        if with_details:
            row.extend([issue.get("body") or "", issue_labels(issue)])
        row.extend([issue.get("state") or "", link])
        writer.writerow(row)
        count += 1
    return count


def command_fetch_issues(args: argparse.Namespace) -> int:
    if args.max_pages < 0:
        print("Error: --max-pages debe ser >= 0.", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit < 0:
        print("Error: --limit debe ser >= 0.", file=sys.stderr)
        return 2
    if args.sample is not None and args.sample < 0:
        print("Error: --sample debe ser >= 0.", file=sys.stderr)
        return 2

    try:
        repositories = resolve_repositories(args)
    except FileNotFoundError:
        source = args.repos_file or "repos.json"
        print(f"Error: no existe el archivo {source}", file=sys.stderr)
        return 2
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if not repositories:
        print("La lista de repositorios está vacía.", file=sys.stderr)
        return 2

    available = {repo.lower() for repo in repositories}
    missing = [
        repo
        for repo in (args.only or [])
        if normalize_repository(repo).lower() not in available
    ]

    selected = select_repositories(
        repositories,
        only=args.only,
        exclude=args.exclude,
        orgs=args.org,
        limit=args.limit,
        sample=args.sample,
        seed=args.seed,
    )

    if missing:
        print(
            "Aviso: estos repositorios de --only no están en la lista: "
            + ", ".join(missing),
            file=sys.stderr,
        )

    verbose = not args.quiet
    if verbose:
        print(
            f"Repositorios: {len(repositories)} disponibles; "
            f"{len(selected)} seleccionados."
        )

    if not selected:
        print(
            "No quedó ningún repositorio después de aplicar los filtros.",
            file=sys.stderr,
        )
        return 2

    split_output = should_split_output(args)
    output_paths: dict[str, Path] = {}
    if split_output:
        output_paths = build_repo_output_paths(selected, ISSUES_DIR)
        if verbose:
            print(
                f"Salida: {display_output_path(ISSUES_DIR)}/ "
                "(un CSV por repositorio)"
            )
        if args.out != "issues.csv":
            print(
                "Aviso: --out se ignora en modo separado; usá --combined "
                "para generar un solo CSV.",
                file=sys.stderr,
            )

    if args.dry_run:
        for repo in selected:
            if split_output:
                print(f"{repo} -> {display_output_path(output_paths[repo])}")
            else:
                print(repo)
        return 0

    api = build_api(args)
    if not api.authenticated:
        print(
            "Aviso: sin GITHUB_TOKEN el límite es 60 peticiones/hora; "
            "puede no alcanzar para todos los repositorios.",
            file=sys.stderr,
        )

    total_issues = 0
    files_written = 0
    errors: list[tuple[str, str]] = []
    combined_output: Path | None = None

    if split_output:
        try:
            ISSUES_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(
                f"Error al crear {display_output_path(ISSUES_DIR)}/: {exc}",
                file=sys.stderr,
            )
            return 2

        for position, repo in enumerate(selected, 1):
            if verbose:
                print(f"[{position}/{len(selected)}] {repo}")

            output_path = output_paths[repo]
            temp_path = output_path.with_name(output_path.name + ".tmp")
            try:
                with temp_path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(csv_fields(args.with_details))
                    repo_issues = write_repo_issues(
                        api,
                        repo,
                        writer,
                        max_pages=args.max_pages,
                        verbose=verbose,
                        with_details=args.with_details,
                    )
                temp_path.replace(output_path)
            except AuthenticationError:
                temp_path.unlink(missing_ok=True)
                raise
            except Exception as exc:  # seguimos con el resto de repositorios
                temp_path.unlink(missing_ok=True)
                print(f"  error: {exc}", file=sys.stderr)
                errors.append((repo, str(exc)))
                continue

            total_issues += repo_issues
            files_written += 1
            if verbose:
                print(
                    f"    {repo_issues} issues -> "
                    f"{display_output_path(output_path)}"
                )
    else:
        combined_output = Path(args.out)
        combined_output.parent.mkdir(parents=True, exist_ok=True)
        with combined_output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(csv_fields(args.with_details))

            for position, repo in enumerate(selected, 1):
                if verbose:
                    print(f"[{position}/{len(selected)}] {repo}")
                try:
                    total_issues += write_repo_issues(
                        api,
                        repo,
                        writer,
                        max_pages=args.max_pages,
                        verbose=verbose,
                        with_details=args.with_details,
                    )
                except AuthenticationError:
                    raise
                except Exception as exc:  # seguimos con el resto de repositorios
                    print(f"  error: {exc}", file=sys.stderr)
                    errors.append((repo, str(exc)))

    if verbose:
        print()
        print(f"Issues exportados: {total_issues}")
        if split_output:
            print(
                f"Archivos generados: {files_written} en "
                f"{display_output_path(ISSUES_DIR)}/"
            )
        else:
            print(f"CSV: {combined_output}")

    if errors:
        print(f"Repositorios con error: {len(errors)}", file=sys.stderr)
        for repo, message in errors:
            print(f"  - {repo}: {message}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Exporta todos los issues de uno o varios repositorios a un CSV "
            "con nombre, dueño, estado y link directo."
        )
    )
    parser.add_argument(
        "--repos-file",
        help=(
            "JSON generado por repo_search_agentic.py o texto con owner/repo "
            "por línea. Si se omite y no se usa --repo, se lee repos.json."
        ),
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help=(
            "Consultar este repositorio (repetible). Si se indica, no se carga "
            "repos.json salvo que también pases --repos-file."
        ),
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help="Incluir solo este repositorio de la lista (repetible)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help="Excluir este repositorio (repetible)",
    )
    parser.add_argument(
        "--org",
        action="append",
        default=[],
        metavar="OWNER",
        help="Incluir solo repositorios de esta organización/usuario (repetible)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Procesar como máximo N repositorios después de filtrar",
    )
    parser.add_argument(
        "--sample",
        type=int,
        help="Elegir al azar N repositorios después de filtrar",
    )
    parser.add_argument(
        "--seed",
        type=int,
        help="Semilla para que --sample sea reproducible",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostrar la selección y los CSV de salida sin llamar a la API",
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--split",
        action="store_true",
        help="Generar un CSV por repositorio en issues/ (automático al seleccionar)",
    )
    output_group.add_argument(
        "--combined",
        action="store_true",
        help="Generar un solo CSV en --out aunque haya selección",
    )
    parser.add_argument(
        "--out",
        default="issues.csv",
        help=(
            "Archivo CSV combinado (por defecto: issues.csv). No se usa en "
            "modo separado, que escribe en issues/"
        ),
    )
    parser.add_argument(
        "--with-details",
        action="store_true",
        help=(
            "Agregar descripcion y etiquetas al CSV (útil para "
            "repo_filter_issues.py)"
        ),
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Máximo de páginas de 100 issues por repo; 0 = sin límite",
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
        load_and_report_token(args)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        return int(command_fetch_issues(args))
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
