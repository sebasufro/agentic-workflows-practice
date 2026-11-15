#!/usr/bin/env python3
"""
repo_fetch_issues.py — Descarga los issues en bruto, página por página.

Para cada repositorio se crea una carpeta dentro de `issues/` con el nombre
`{dueño}__{repo}`. El script guarda únicamente la respuesta cruda de cada página
de la API:

    issues/
    └── {dueño}__{repo}/
        └── raw/
            ├── page_0001.json
            └── page_0002.json

Los archivos conservan issues y pull requests tal como los devuelve GitHub. El
parseo de cada página, la creación de los `issue_{número}.json` individuales y
el filtrado quedan a cargo de `repo_filter_issues.py`.

Las páginas se descargan siguiendo los headers `Link` de GitHub. En repositorios
con muchísimos issues GitHub usa paginación por cursor (`after`/`before`) y
rechaza `page` a partir de la página 100; el script detecta ese caso y continúa
solo con el cursor.

La carpeta `raw/` se actualiza de forma segura: si la descarga falla a mitad de
camino se conservan las páginas anteriores; si termina bien, se reemplazan por
las nuevas.

Ejemplos:

    # Un repositorio -> issues/{dueño}__{repo}/raw/
    python scripts/repo_fetch_issues.py --repo PowerShell/vscode-powershell

    # Todos los repositorios de repos.json
    python scripts/repo_fetch_issues.py --repos-file repos.json

    # Los primeros 10 de repos.json
    python scripts/repo_fetch_issues.py --repos-file repos.json --limit 10

    # 10 al azar, de forma reproducible
    python scripts/repo_fetch_issues.py --repos-file repos.json \\
        --sample 10 --seed 42

    # Cambiar la carpeta base de salida
    python scripts/repo_fetch_issues.py \\
        --repo PowerShell/vscode-powershell --issues-dir /tmp/issues

    # Ver la selección y las rutas de salida sin llamar a la API
    python scripts/repo_fetch_issues.py --repos-file repos.json --org PowerShell \\
        --limit 5 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from repo_search_agentic import (
    AuthenticationError,
    GitHubAPI,
    build_api,
    load_and_report_token,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ISSUES_DIR = PROJECT_ROOT / "issues"
RAW_DIR_NAME = "raw"


# --------------------------------------------------------------------------- #
# Selección de repositorios
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Paginación
# --------------------------------------------------------------------------- #


def next_page_url(headers: Any) -> str | None:
    """Extrae la URL con rel=\"next\" del header Link de GitHub."""
    link_header = headers.get("Link") if headers else None
    if not link_header:
        return None

    for part in link_header.split(","):
        match = re.search(r'<([^>]+)>\s*;\s*rel="([^"]+)"', part.strip())
        if match and match.group(2) == "next":
            return match.group(1)
    return None


def cursor_url(url: str) -> str:
    """Quita `page` cuando la URL usa paginación por cursor (after/before).

    GitHub incluye `page` y `after` juntos en el Link de repositorios con
    muchísimos issues. Si se sigue esa URL tal cual, la API responde 422 a
    partir de la página 100. Para esos casos hay que usar solo el cursor.
    """
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if any(key in {"after", "before"} for key, _ in query):
        query = [(key, value) for key, value in query if key != "page"]
    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urllib.parse.urlencode(query),
            parts.fragment,
        )
    )


# --------------------------------------------------------------------------- #
# Rutas y escritura de archivos
# --------------------------------------------------------------------------- #


def split_repository(repo: str) -> tuple[str, str]:
    owner, separator, name = repo.partition("/")
    if not separator or not owner or not name:
        raise ValueError(f"Nombre de repositorio inválido: {repo}")
    return owner, name


def repository_directory(issues_dir: Path, repo: str) -> Path:
    owner, name = split_repository(repo)
    return issues_dir / f"{owner}__{name}"


def display_output_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def write_json_file(path: Path, data: Any) -> None:
    """Escribe JSON de forma atómica para no dejar archivos a medio escribir."""
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp_path, path)


# --------------------------------------------------------------------------- #
# Descarga
# --------------------------------------------------------------------------- #


def fetch_raw_pages(
    api: GitHubAPI,
    repo: str,
    staging_dir: Path,
    max_pages: int,
    verbose: bool,
) -> tuple[int, int, int, bool]:
    """Descarga las páginas crudas en staging_dir.

    Devuelve (páginas guardadas, elementos crudos, pull requests, completo).
    """
    url = f"/repos/{repo}/issues?state=all&per_page=100"
    page = 0
    pages_saved = 0
    raw_items = 0
    pull_requests = 0
    complete = True

    while True:
        page += 1
        data, headers = api.get_json(url)
        if data is None:
            if verbose:
                print("    sin issues o issues deshabilitados")
            break

        batch = data if isinstance(data, list) else []
        if not batch:
            break

        write_json_file(staging_dir / f"page_{page:04d}.json", batch)
        pages_saved += 1
        raw_items += len(batch)
        page_prs = sum(
            1
            for item in batch
            if isinstance(item, dict) and "pull_request" in item
        )
        pull_requests += page_prs

        if verbose:
            print(
                f"    página {page}: {len(batch)} elementos, "
                f"{len(batch) - page_prs} issues"
            )

        next_url = next_page_url(headers)
        if max_pages and page >= max_pages:
            complete = not bool(next_url)
            break
        if not next_url:
            break
        url = cursor_url(next_url)

    return pages_saved, raw_items, pull_requests, complete


def fetch_repository(
    api: GitHubAPI,
    repo: str,
    issues_dir: Path,
    *,
    max_pages: int,
    verbose: bool,
) -> tuple[int, int, int, bool]:
    """Descarga un repositorio y actualiza únicamente su carpeta raw/."""
    repo_dir = repository_directory(issues_dir, repo)
    repo_dir.mkdir(parents=True, exist_ok=True)

    staging_dir = repo_dir / ".raw.tmp"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)

    try:
        pages, raw_items, pull_requests, complete = fetch_raw_pages(
            api, repo, staging_dir, max_pages, verbose
        )
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    raw_dir = repo_dir / RAW_DIR_NAME
    old_dir = repo_dir / ".raw.old"
    if old_dir.exists():
        shutil.rmtree(old_dir)

    try:
        if raw_dir.exists():
            raw_dir.rename(old_dir)
        staging_dir.rename(raw_dir)
    except Exception:
        if old_dir.exists() and not raw_dir.exists():
            old_dir.rename(raw_dir)
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    finally:
        if old_dir.exists():
            shutil.rmtree(old_dir, ignore_errors=True)

    if verbose:
        print(
            f"    {pages} páginas, {raw_items} elementos crudos, "
            f"{pull_requests} PRs"
        )
        print(f"    Crudo: {display_output_path(raw_dir)}/")
        if not complete:
            print("    aviso: descarga parcial por --max-pages")

    return pages, raw_items, pull_requests, complete


# --------------------------------------------------------------------------- #
# Comando principal
# --------------------------------------------------------------------------- #


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

    issues_dir = Path(args.issues_dir)

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

    try:
        repository_dirs = {
            repo: repository_directory(issues_dir, repo) for repo in selected
        }
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        for repo in selected:
            repo_dir = repository_dirs[repo]
            print(f"{repo} -> {display_output_path(repo_dir / RAW_DIR_NAME)}/")
        return 0

    api = build_api(args)
    if not api.authenticated:
        print(
            "Aviso: sin GITHUB_TOKEN el límite es 60 peticiones/hora; "
            "puede no alcanzar para todos los repositorios.",
            file=sys.stderr,
        )

    total_pages = 0
    total_items = 0
    total_prs = 0
    processed = 0
    errors: list[tuple[str, str]] = []

    for position, repo in enumerate(selected, 1):
        if verbose:
            print(f"[{position}/{len(selected)}] {repo}")

        try:
            pages, raw_items, pull_requests, _ = fetch_repository(
                api,
                repo,
                issues_dir,
                max_pages=args.max_pages,
                verbose=verbose,
            )
        except AuthenticationError:
            raise
        except Exception as exc:  # seguimos con el resto de repositorios
            print(f"  error: {exc}", file=sys.stderr)
            errors.append((repo, str(exc)))
            continue

        total_pages += pages
        total_items += raw_items
        total_prs += pull_requests
        processed += 1

    if verbose:
        print()
        print(f"Repositorios procesados: {processed}")
        print(f"Páginas crudas: {total_pages}")
        print(f"Elementos descargados: {total_items} ({total_prs} PRs)")
        print(f"Salida: {display_output_path(issues_dir)}/{{dueño}}__{{repo}}/raw/")

    if errors:
        print(f"Repositorios con error: {len(errors)}", file=sys.stderr)
        for repo, message in errors:
            print(f"  - {repo}: {message}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Descarga los issues de uno o varios repositorios en bruto, por "
            "páginas, y los guarda en issues/{dueño}__{repo}/raw/. El parseo y "
            "el filtrado los hace repo_filter_issues.py."
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
        "--issues-dir",
        default=str(ISSUES_DIR),
        metavar="DIR",
        help="Carpeta base de salida (por defecto: issues/)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostrar la selección y las rutas de salida sin llamar a la API",
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

    if not args.dry_run:
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
