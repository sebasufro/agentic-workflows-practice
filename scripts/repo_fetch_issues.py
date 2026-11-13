#!/usr/bin/env python3
"""
repo_fetch_issues.py — Descarga issues en crudo y los guarda por repositorio.

Para cada repositorio se crea una carpeta dentro de `issues/` con el nombre
`{dueño}__{repo}`. Dentro de esa carpeta se guarda:

    raw/page_0001.json      Respuesta cruda de cada página de la API
    raw/page_0002.json      (se conservan issues y pull requests tal cual)
    issue_123.json          Un JSON por issue real (sin pull requests)
    issue_124.json
    {repo}_issues.csv       Lista de issues con el esquema de siempre
    metadata.json           Fechas, cantidades y estado de la descarga

Las páginas crudas se descargan siguiendo los headers `Link` de GitHub. En
repositorios con muchísimos issues GitHub usa paginación por cursor
(`after`/`before`) y rechaza `page` a partir de la página 100; el script detecta
ese caso y continúa solo con el cursor.

Los pull requests se conservan en los archivos crudos, pero se descartan al
generar los JSON individuales y el CSV. El CSV mantiene el esquema:

    nombre_repositorio,dueño_repositorio,titulo,salida,link_issue

Con `--with-details` se agregan `descripcion` y `etiquetas` después de `titulo`.

La carpeta de un repositorio se actualiza de forma segura: si la descarga falla
a mitad de camino se conservan los datos anteriores; si termina bien, se
eliminan las páginas y los JSON de issues que ya no existen.

`--parse-only` regenera los JSON individuales, el CSV y los metadatos a partir
de las páginas crudas ya guardadas, sin llamar a la API.

Ejemplos:

    # Un repositorio -> issues/{dueño}__{repo}/
    python scripts/repo_fetch_issues.py --repo PowerShell/vscode-powershell

    # Todos los repositorios de repos.json -> una carpeta por repositorio
    python scripts/repo_fetch_issues.py --repos-file repos.json

    # Los primeros 10 de repos.json
    python scripts/repo_fetch_issues.py --repos-file repos.json --limit 10

    # 10 al azar, de forma reproducible
    python scripts/repo_fetch_issues.py --repos-file repos.json \\
        --sample 10 --seed 42

    # Incluir también descripción y etiquetas en el CSV
    python scripts/repo_fetch_issues.py --repo PowerShell/vscode-powershell \\
        --with-details

    # Regenerar JSON individuales y CSV desde las páginas crudas guardadas
    python scripts/repo_fetch_issues.py --parse-only

    # Regenerar solo un repositorio
    python scripts/repo_fetch_issues.py --parse-only \\
        --repo PowerShell/vscode-powershell

    # Además de las carpetas, escribir un CSV combinado
    python scripts/repo_fetch_issues.py --repos-file repos.json --limit 10 \\
        --combined --out issues-10.csv

    # Ver la selección y las rutas de salida sin llamar a la API
    python scripts/repo_fetch_issues.py --repos-file repos.json --org PowerShell \\
        --limit 5 --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import shutil
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
RAW_DIR_NAME = "raw"
METADATA_NAME = "metadata.json"


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


def repository_from_folder(directory: Path) -> str:
    """Reconstruye owner/repo desde metadata.json o desde el nombre de carpeta."""
    metadata = load_metadata(directory / METADATA_NAME)
    repository = metadata.get("repository") if metadata else None
    if isinstance(repository, str) and "/" in repository:
        return normalize_repository(repository)

    name = directory.name
    if "__" in name:
        owner, repo = name.split("__", 1)
        if owner and repo:
            return f"{owner}/{repo}"
    return name


def discover_local_repositories(issues_dir: Path) -> list[str]:
    """Lista las carpetas de issues/ que ya tienen páginas crudas."""
    if not issues_dir.is_dir():
        raise FileNotFoundError(f"No existe el directorio de issues: {issues_dir}")

    repositories: list[str] = []
    for directory in sorted(path for path in issues_dir.iterdir() if path.is_dir()):
        if not (directory / RAW_DIR_NAME).is_dir():
            continue
        repository = repository_from_folder(directory)
        if repository:
            repositories.append(repository)

    return list(dict.fromkeys(repositories))


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


def write_csv_file(path: Path, fields: tuple[str, ...], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)
    os.replace(temp_path, path)


def load_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def csv_fields(with_details: bool) -> tuple[str, ...]:
    return DETAILED_CSV_FIELDS if with_details else CSV_FIELDS


def issue_labels(issue: dict[str, Any]) -> str:
    names: list[str] = []
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else str(label)
        if name:
            names.append(str(name))
    return ";".join(names)


def issue_row(repo: str, issue: dict[str, Any], with_details: bool) -> list[str]:
    owner, name = split_repository(repo)
    number = issue.get("number")
    link = issue.get("html_url") or (
        f"https://github.com/{repo}/issues/{number}" if number is not None else ""
    )

    row: list[str] = [name, owner, issue.get("title") or ""]
    if with_details:
        row.extend([issue.get("body") or "", issue_labels(issue)])
    row.extend([issue.get("state") or "", link])
    return row


# --------------------------------------------------------------------------- #
# Descarga y parseo
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


def read_raw_pages(raw_dir: Path) -> list[list[Any]]:
    """Lee las páginas crudas en orden y valida que sean listas."""
    pages: list[list[Any]] = []
    for path in sorted(raw_dir.glob("page_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            pages.append(data)
    return pages


def parse_raw_issues(pages: list[list[Any]]) -> list[dict[str, Any]]:
    """Extrae los issues reales de las páginas y descarta pull requests."""
    by_number: dict[Any, dict[str, Any]] = {}
    order: list[Any] = []

    for page in pages:
        for item in page:
            if not isinstance(item, dict) or "pull_request" in item:
                continue
            number = item.get("number")
            key: Any = number if number is not None else f"sin-numero-{len(order)}"
            if key not in by_number:
                order.append(key)
            by_number[key] = item

    return [by_number[key] for key in order]


def write_individual_issues(
    repo_dir: Path,
    issues: list[dict[str, Any]],
    verbose: bool,
) -> None:
    """Escribe issue_{numero}.json y elimina los que ya no existen."""
    staging_dir = repo_dir / ".issues.tmp"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)

    new_numbers: set[Any] = set()
    skipped = 0
    try:
        for issue in issues:
            number = issue.get("number")
            if number is None:
                skipped += 1
                continue
            new_numbers.add(number)
            write_json_file(staging_dir / f"issue_{number}.json", issue)

        for path in repo_dir.glob("issue_*.json"):
            stem = path.stem[len("issue_") :]
            key: Any = int(stem) if stem.isdigit() else stem
            if key not in new_numbers:
                path.unlink()

        for path in staging_dir.iterdir():
            os.replace(path, repo_dir / path.name)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    if skipped and verbose:
        print(f"    aviso: {skipped} issues sin número no se guardaron como JSON")


def write_metadata(
    repo_dir: Path,
    repo: str,
    *,
    base: dict[str, Any] | None = None,
    fetched_at: str | None = None,
    pages: int,
    raw_items: int,
    issue_count: int,
    pull_requests: int,
    complete: bool,
    fields: tuple[str, ...],
    max_pages: int,
) -> None:
    owner, name = split_repository(repo)
    metadata: dict[str, Any] = dict(base or {})
    metadata.update(
        {
            "repository": repo,
            "owner": owner,
            "name": name,
            "pages": pages,
            "raw_items": raw_items,
            "issues": issue_count,
            "pull_requests": pull_requests,
            "complete": complete,
            "csv": f"{name}_issues.csv",
            "csv_fields": list(fields),
            "max_pages": max_pages,
            "parsed_at": utc_timestamp(),
        }
    )
    metadata["fetched_at"] = fetched_at or metadata.get("fetched_at") or utc_timestamp()
    write_json_file(repo_dir / METADATA_NAME, metadata)


def parse_repository(
    repo: str,
    issues_dir: Path,
    *,
    with_details: bool,
    verbose: bool,
) -> tuple[list[dict[str, Any]], list[list[str]], int, int, int]:
    """Regenera JSON individuales, CSV y metadata desde las páginas crudas."""
    _, name = split_repository(repo)
    repo_dir = repository_directory(issues_dir, repo)
    raw_dir = repo_dir / RAW_DIR_NAME
    if not raw_dir.is_dir():
        raise FileNotFoundError(
            f"No hay páginas crudas para {repo} en {display_output_path(raw_dir)}/"
        )

    pages = read_raw_pages(raw_dir)
    issues = parse_raw_issues(pages)
    raw_items = sum(len(page) for page in pages)
    pull_requests = sum(
        1
        for page in pages
        for item in page
        if isinstance(item, dict) and "pull_request" in item
    )

    write_individual_issues(repo_dir, issues, verbose)
    rows = [issue_row(repo, issue, with_details) for issue in issues]
    write_csv_file(repo_dir / f"{name}_issues.csv", csv_fields(with_details), rows)

    metadata = load_metadata(repo_dir / METADATA_NAME)
    write_metadata(
        repo_dir,
        repo,
        base=metadata,
        pages=len(pages),
        raw_items=raw_items,
        issue_count=len(issues),
        pull_requests=pull_requests,
        complete=bool(metadata.get("complete", True)),
        fields=csv_fields(with_details),
        max_pages=int(metadata.get("max_pages") or 0),
    )

    if verbose:
        print(
            f"    {len(pages)} páginas, {len(issues)} issues, "
            f"{pull_requests} PRs descartados"
        )
        print(f"    JSON y CSV regenerados en {display_output_path(repo_dir)}/")

    return issues, rows, len(pages), raw_items, pull_requests


def fetch_repository(
    api: GitHubAPI,
    repo: str,
    issues_dir: Path,
    *,
    max_pages: int,
    with_details: bool,
    verbose: bool,
) -> tuple[list[dict[str, Any]], list[list[str]], int, int, int]:
    """Descarga un repositorio y actualiza su carpeta de forma segura."""
    _, name = split_repository(repo)
    repo_dir = repository_directory(issues_dir, repo)
    repo_dir.mkdir(parents=True, exist_ok=True)

    staging_dir = repo_dir / ".raw.tmp"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)

    fetched_at = utc_timestamp()
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

    raw_pages = read_raw_pages(raw_dir)
    issues = parse_raw_issues(raw_pages)
    write_individual_issues(repo_dir, issues, verbose)
    rows = [issue_row(repo, issue, with_details) for issue in issues]
    write_csv_file(repo_dir / f"{name}_issues.csv", csv_fields(with_details), rows)

    write_metadata(
        repo_dir,
        repo,
        fetched_at=fetched_at,
        pages=pages,
        raw_items=raw_items,
        issue_count=len(issues),
        pull_requests=pull_requests,
        complete=complete,
        fields=csv_fields(with_details),
        max_pages=max_pages,
    )

    if verbose:
        print(
            f"    {pages} páginas, {len(issues)} issues, "
            f"{pull_requests} PRs descartados"
        )
        print(f"    CSV: {display_output_path(repo_dir / f'{name}_issues.csv')}")
        if not complete:
            print("    aviso: descarga parcial por --max-pages")

    return issues, rows, pages, raw_items, pull_requests


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
    parse_only = bool(args.parse_only)

    try:
        if parse_only and not args.repo and not args.repos_file:
            repositories = discover_local_repositories(issues_dir)
        else:
            repositories = resolve_repositories(args)
    except FileNotFoundError:
        source = (
            args.issues_dir
            if parse_only
            else (args.repos_file or "repos.json")
        )
        print(f"Error: no existe el archivo o directorio {source}", file=sys.stderr)
        return 2
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if not repositories:
        if parse_only:
            print(
                f"No hay carpetas con {RAW_DIR_NAME}/ en "
                f"{display_output_path(issues_dir)}/.",
                file=sys.stderr,
            )
        else:
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
        mode = "parseando crudos locales" if parse_only else "descargando de la API"
        print(
            f"Repositorios: {len(repositories)} disponibles; "
            f"{len(selected)} seleccionados ({mode})."
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
        if args.split:
            print(
                "Aviso: --split ya no es necesario; la salida siempre es una "
                "carpeta por repositorio.",
                file=sys.stderr,
            )
        for repo in selected:
            repo_dir = repository_dirs[repo]
            _, name = split_repository(repo)
            print(f"{repo} -> {display_output_path(repo_dir)}/")
            print(
                f"  crudo:  {display_output_path(repo_dir / RAW_DIR_NAME)}/"
                "page_NNNN.json"
            )
            print(f"  issues: {display_output_path(repo_dir)}/issue_N.json")
            print(f"  csv:    {display_output_path(repo_dir / f'{name}_issues.csv')}")
        if args.combined:
            print(f"combinado: {display_output_path(Path(args.out))}")
        return 0

    if args.split:
        print(
            "Aviso: --split ya no es necesario; la salida siempre es una "
            "carpeta por repositorio.",
            file=sys.stderr,
        )

    api: GitHubAPI | None = None
    if not parse_only:
        api = build_api(args)
        if not api.authenticated:
            print(
                "Aviso: sin GITHUB_TOKEN el límite es 60 peticiones/hora; "
                "puede no alcanzar para todos los repositorios.",
                file=sys.stderr,
            )

    total_issues = 0
    total_pages = 0
    processed = 0
    errors: list[tuple[str, str]] = []
    combined_rows: list[list[str]] = []

    for position, repo in enumerate(selected, 1):
        if verbose:
            print(f"[{position}/{len(selected)}] {repo}")

        try:
            if parse_only:
                issues, rows, pages, _, _ = parse_repository(
                    repo,
                    issues_dir,
                    with_details=args.with_details,
                    verbose=verbose,
                )
            else:
                assert api is not None
                issues, rows, pages, _, _ = fetch_repository(
                    api,
                    repo,
                    issues_dir,
                    max_pages=args.max_pages,
                    with_details=args.with_details,
                    verbose=verbose,
                )
        except AuthenticationError:
            raise
        except Exception as exc:  # seguimos con el resto de repositorios
            print(f"  error: {exc}", file=sys.stderr)
            errors.append((repo, str(exc)))
            continue

        total_issues += len(issues)
        total_pages += pages
        combined_rows.extend(rows)
        processed += 1

    if args.combined:
        if processed or not errors:
            combined_output = Path(args.out)
            write_csv_file(
                combined_output, csv_fields(args.with_details), combined_rows
            )
            if verbose:
                print(f"CSV combinado: {display_output_path(combined_output)}")
        elif verbose:
            print(
                "Aviso: no se escribió el CSV combinado porque ningún "
                "repositorio terminó correctamente.",
                file=sys.stderr,
            )

    if verbose:
        print()
        print(f"Repositorios procesados: {processed}")
        print(f"Páginas crudas: {total_pages}")
        print(f"Issues exportados: {total_issues}")
        print(f"Salida: {display_output_path(issues_dir)}/ (una carpeta por repositorio)")

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
            "páginas, y genera JSON individuales y CSV por repositorio dentro "
            "de issues/."
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
        "--parse-only",
        action="store_true",
        help=(
            "No llama a la API: regenera los JSON individuales, el CSV y los "
            "metadatos desde las páginas crudas guardadas"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostrar la selección y las rutas de salida sin llamar a la API",
    )
    parser.add_argument(
        "--split",
        action="store_true",
        help=(
            "Obsoleto: se conserva por compatibilidad. La salida ya es siempre "
            "una carpeta por repositorio"
        ),
    )
    parser.add_argument(
        "--combined",
        action="store_true",
        help=(
            "Además de las carpetas por repositorio, escribir un CSV combinado "
            "en --out"
        ),
    )
    parser.add_argument(
        "--out",
        default="issues.csv",
        help=(
            "Archivo CSV combinado cuando se usa --combined "
            "(por defecto: issues.csv)"
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

    if not args.parse_only and not args.dry_run:
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
