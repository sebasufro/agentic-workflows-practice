#!/usr/bin/env python3
"""
repo_search_agentic.py — Descubre repositorios que usan GitHub Agentic Workflows.

Busca el marcador `gh-aw-metadata` que `gh aw compile` escribe en los archivos
`.lock.yml`, usando la Code Search API de GitHub o el índice público de
Sourcegraph. Guarda la lista de repositorios y los workflows encontrados en un
JSON que después consume `repo_scan_actions.py`.

Este archivo también contiene las utilidades compartidas por ambos scripts:
carga de `.env`, cliente REST de GitHub y manejo básico de errores.

Ejemplos:

    # Descubre y guarda en repos.json (opciones por defecto)
    python scripts/repo_search_agentic.py

    # Supera el tope de 1.000 resultados repartiendo por tamaño e incluye forks
    python scripts/repo_search_agentic.py --all-results --include-forks

    # Solo repositorios con 100 o más páginas de issues (10.000 issues)
    python scripts/repo_search_agentic.py --issues-pages 100

    # Explorar el índice público de Sourcegraph
    python scripts/repo_search_agentic.py --source sourcegraph

    # Guardar en otro archivo
    python scripts/repo_search_agentic.py --out /tmp/repos-grandes.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API_ROOT = "https://api.github.com"
USER_AGENT = "gh-aw-research-script"
DEFAULT_QUERY = "gh-aw-metadata path:.github/workflows"


class AuthenticationError(RuntimeError):
    """Token ausente, inválido o expirado."""


# --------------------------------------------------------------------------- #
# Carga del token desde .env
# --------------------------------------------------------------------------- #


def find_dotenv_file(explicit_path: str | None = None) -> Path | None:
    """Busca el .env explícito, GH_AW_ENV_FILE, el cwd o la raíz del repo."""
    if explicit_path:
        return Path(explicit_path).expanduser()

    env_override = os.environ.get("GH_AW_ENV_FILE")
    candidates: list[Path] = []
    if env_override:
        candidates.append(Path(env_override).expanduser())
    candidates.extend(
        [
            Path.cwd() / ".env",
            Path(__file__).resolve().parent.parent / ".env",
            Path(__file__).resolve().parent / ".env",
        ]
    )

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def parse_dotenv(path: Path) -> dict[str, str]:
    """Parser mínimo de .env: KEY=value, export KEY=value y comillas."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return {}

    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()

        key, separator, value = line.partition("=")
        if not separator:
            continue

        key = key.strip()
        value = value.strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue

        if value[:1] in "\"'":
            quote = value[0]
            closing = value.find(quote, 1)
            if closing != -1:
                value = value[1:closing]
        else:
            # Comentario al final de una línea sin comillas.
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()

        values[key] = value

    return values


def token_looks_like_placeholder(token: str) -> bool:
    value = token.strip()
    return (
        value in {"...", "ghp_...", "github_pat_...", "your_token_here"}
        or value.endswith("...")
        or len(value) < 20
    )


def load_token_from_dotenv(
    explicit_path: str | None = None,
    warn: bool = True,
) -> Path | None:
    """Carga GITHUB_TOKEN/GH_TOKEN desde .env.

    Prioridad: --token (se maneja fuera) > --env-file/GH_AW_ENV_FILE > .env
    encontrado automáticamente > variables GITHUB_TOKEN/GH_TOKEN del entorno.

    Un .env local tiene prioridad sobre el token exportado en el shell. Para
    forzar el token del entorno, usá --token "$GITHUB_TOKEN".

    Devuelve la ruta del archivo si cargó un token, o None si no hizo falta.
    """
    env_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    if not explicit_path:
        explicit_path = os.environ.get("GH_AW_ENV_FILE") or None

    if explicit_path:
        path = Path(explicit_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"No existe el archivo .env indicado: {explicit_path}")
    else:
        path = find_dotenv_file()

    if path is None or not path.is_file():
        return None

    values = parse_dotenv(path)
    token = values.get("GITHUB_TOKEN") or values.get("GH_TOKEN")

    if not token:
        if warn and ("GITHUB_TOKEN" in values or "GH_TOKEN" in values):
            print(
                f"Aviso: {path} existe pero GITHUB_TOKEN está vacío.",
                file=sys.stderr,
            )
        return None

    if token_looks_like_placeholder(token):
        if warn:
            print(
                f"Aviso: {path} contiene un token de ejemplo/placeholder; no se usará.",
                file=sys.stderr,
            )
        return None

    if env_token and env_token != token and warn:
        print(
            f"Aviso: se usará el token de {path} en lugar del token exportado "
            "en el entorno. Para forzar el del entorno usá "
            '--token "$GITHUB_TOKEN".',
            file=sys.stderr,
        )

    os.environ["GITHUB_TOKEN"] = token
    return path


# --------------------------------------------------------------------------- #
# Cliente mínimo de la API REST de GitHub
# --------------------------------------------------------------------------- #


class GitHubAPI:
    """Cliente HTTP con manejo básico de rate limits y reintentos."""

    def __init__(self, token: str | None = None, verbose: bool = True) -> None:
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self.verbose = verbose

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def open(
        self,
        url: str,
        accept: str = "application/vnd.github+json",
        retries: int = 4,
        data: bytes | None = None,
        method: str | None = None,
    ) -> tuple[bytes | None, Any]:
        """GET o POST con reintentos. Devuelve (body, headers) o (None, headers) en 404."""
        request = urllib.request.Request(
            url,
            data=data,
            headers=self._headers(accept),
            method=method,
        )
        if data is not None:
            request.add_header("Content-Type", "application/json")

        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    return response.read(), response.headers
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None, exc.headers

                if exc.code == 401:
                    body = exc.read().decode("utf-8", "replace")[:300]
                    raise AuthenticationError(
                        "GitHub rechazó el token (401 Unauthorized). "
                        "Revisá que GITHUB_TOKEN/GH_TOKEN sea válido y no esté "
                        f"expirado o revocado.\n{body}"
                    ) from exc

                if exc.code in (403, 429) and attempt < retries:
                    wait = self._retry_wait(exc, attempt)
                    if self.verbose:
                        print(f"    rate limit/HTTP {exc.code}; esperando {wait}s...", file=sys.stderr)
                    time.sleep(wait)
                    continue

                if 500 <= exc.code < 600 and attempt < retries:
                    wait = 2 ** (attempt + 1)
                    if self.verbose:
                        print(f"    HTTP {exc.code}; reintentando en {wait}s...", file=sys.stderr)
                    time.sleep(wait)
                    continue

                body = exc.read().decode("utf-8", "replace")[:800]
                raise RuntimeError(f"HTTP {exc.code} en {url}\n{body}") from exc
            except urllib.error.URLError as exc:
                if attempt < retries:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise RuntimeError(f"Error de red en {url}: {exc}") from exc

        raise RuntimeError(f"No se pudo completar la petición a {url}")

    @staticmethod
    def _retry_wait(exc: urllib.error.HTTPError, attempt: int) -> int:
        retry_after = exc.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return min(int(retry_after), 900)

        reset = exc.headers.get("x-ratelimit-reset")
        remaining = exc.headers.get("x-ratelimit-remaining")
        if reset and remaining == "0":
            return min(max(1, int(reset) - int(time.time()) + 2), 900)

        return min(5 * (attempt + 1), 60)

    def get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any | None, Any]:
        url = path if path.startswith("http") else API_ROOT + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body, headers = self.open(url)
        if body is None:
            return None, headers
        return json.loads(body.decode("utf-8")), headers

    def get_text(self, url: str) -> str | None:
        body, _ = self.open(url, accept="application/vnd.github.raw")
        if body is None:
            return None
        return body.decode("utf-8", "replace")

    def graphql(self, query: str) -> dict[str, Any]:
        """Ejecuta una consulta GraphQL y devuelve el payload completo."""
        body, _ = self.open(
            API_ROOT + "/graphql",
            data=json.dumps({"query": query}).encode("utf-8"),
            method="POST",
        )
        if body is None:
            raise RuntimeError("GitHub GraphQL devolvió 404.")
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("Respuesta inesperada de GitHub GraphQL.")
        return payload


# --------------------------------------------------------------------------- #
# Utilidades compartidas por los dos scripts
# --------------------------------------------------------------------------- #


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def build_api(args: argparse.Namespace) -> GitHubAPI:
    return GitHubAPI(token=args.token, verbose=not args.quiet)


def load_and_report_token(args: argparse.Namespace) -> Path | None:
    """Carga el token y muestra su origen sin imprimir su valor."""
    dotenv_path = (
        None if args.token else load_token_from_dotenv(args.env_file, warn=not args.quiet)
    )
    if args.quiet:
        return dotenv_path

    if args.token:
        source = "--token"
    elif dotenv_path:
        source = str(dotenv_path)
    elif os.environ.get("GITHUB_TOKEN"):
        source = "GITHUB_TOKEN del entorno"
    elif os.environ.get("GH_TOKEN"):
        source = "GH_TOKEN del entorno"
    else:
        source = None

    if source:
        print(f"Token: {source}", file=sys.stderr)

    active_token = (
        args.token
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
        or ""
    )
    if active_token and token_looks_like_placeholder(active_token):
        print(
            "Aviso: el token parece un placeholder de ejemplo; "
            "reemplazalo por un token real.",
            file=sys.stderr,
        )

    return dotenv_path


# --------------------------------------------------------------------------- #
# Descubrimiento de repositorios
# --------------------------------------------------------------------------- #


SEARCH_PER_PAGE = 100
SEARCH_MAX_PAGES = 10

# La Code Search API solo deja recuperar 10 páginas (1.000 coincidencias) por
# consulta. Para superar ese tope se parte la búsqueda por tamaño de archivo en
# bytes (`size:`) y se subdivide cada grupo que siga superando el límite. Los
# rangos iniciales son solo una pista: la partición se adapta recursivamente.
SIZE_BUCKETS = (
    (0, 1_000),
    (1_001, 5_000),
    (5_001, 10_000),
    (10_001, 20_000),
    (20_001, 50_000),
    (50_001, 100_000),
    (100_001, 150_000),
    (150_001, 200_000),
    (200_001, 300_000),
    (300_001, 500_000),
    (500_001, 1_000_000),
    (1_000_001, 100_000_000),
    (100_000_001, 2_000_000_000),
)


@dataclass
class Discovery:
    repositories: dict[str, list[str]]
    total_count: int
    truncated: bool
    queries: list[str]
    per_query: list[dict[str, Any]]
    requests: int = 0


class CodeSearch:
    """Cliente de Code Search con pausa entre peticiones y contador."""

    def __init__(self, api: GitHubAPI, pause: float = 6.2, verbose: bool = True) -> None:
        self.api = api
        self.pause = max(0.0, pause)
        self.verbose = verbose
        self.requests = 0
        self._last_request: float | None = None

    def _throttle(self) -> None:
        if self._last_request is None:
            return
        wait = self.pause - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def request(self, query: str, page: int = 1, per_page: int = SEARCH_PER_PAGE) -> dict[str, Any]:
        self._throttle()
        try:
            data, _ = self.api.get_json(
                "/search/code",
                {"q": query, "per_page": per_page, "page": page},
            )
            return data or {}
        finally:
            self._last_request = time.monotonic()
            self.requests += 1

    def count(self, query: str) -> int:
        return int(self.request(query, page=1, per_page=1).get("total_count") or 0)

    def collect(
        self,
        query: str,
        max_pages: int = SEARCH_MAX_PAGES,
    ) -> tuple[list[dict[str, Any]], int]:
        """Recupera hasta `max_pages` páginas de una consulta."""
        items: list[dict[str, Any]] = []
        total = 0

        for page in range(1, max_pages + 1):
            data = self.request(query, page=page)
            total = max(total, int(data.get("total_count") or 0))
            batch = data.get("items") or []
            items.extend(batch)

            if self.verbose:
                print(f"    página {page}: {len(batch)} archivos")
            if len(batch) < SEARCH_PER_PAGE:
                break

        return items, max(total, len(items))


def collect_exhaustive(
    search: CodeSearch,
    query: str,
    max_pages: int = SEARCH_MAX_PAGES,
    initial_ranges: tuple[tuple[int, int], ...] = SIZE_BUCKETS,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Recupera todos los resultados posibles repartiendo la consulta por tamaño.

    Cada grupo (`size:lo..hi`) se subdivide a la mitad mientras su `total_count`
    supere `max_pages * 100`. Devuelve las coincidencias, el total reportado y
    si algún grupo quedó incompleto (por ejemplo, todos los archivos del mismo
    tamaño).
    """
    cap = max_pages * SEARCH_PER_PAGE

    # Primero se prueba la consulta completa: si entra en el tope, no hace
    # falta repartirla. `total_count` es aproximado, así que si se llenó el
    # tope de páginas igual se subdivide para no perder resultados.
    base_total = search.count(query)
    if base_total == 0:
        return [], 0, False
    if base_total <= cap:
        batch, observed = search.collect(query, max_pages=max_pages)
        if len(batch) < cap:
            return batch, max(base_total, observed), False
        if search.verbose:
            print(
                f"    se llenó el tope de {cap} resultados; "
                "se reparte por tamaño para verificar.",
                file=sys.stderr,
            )

    items: list[dict[str, Any]] = []
    reported_total = 0
    truncated = False

    stack: list[tuple[int, int]] = list(reversed(initial_ranges))
    while stack:
        lo, hi = stack.pop()
        bucket_query = f"{query} size:{lo}..{hi}"
        total = search.count(bucket_query)

        if total == 0:
            continue

        if total <= cap:
            if search.verbose:
                print(f"    size:{lo}..{hi}: {total} resultados")
            batch, observed = search.collect(bucket_query, max_pages=max_pages)

            # `total_count` es aproximado: si se llenó el tope de páginas,
            # puede haber más resultados. Se subdivide para no perderlos.
            if len(batch) >= cap and lo < hi:
                if search.verbose:
                    print(
                        f"    size:{lo}..{hi}: se llenó el tope de "
                        f"{cap}; se subdivide para verificar.",
                        file=sys.stderr,
                    )
                mid = lo + (hi - lo) // 2
                stack.append((mid + 1, hi))
                stack.append((lo, mid))
                continue

            if len(batch) >= cap:
                truncated = True
            reported_total += max(total, observed)
            items.extend(batch)
            continue

        if lo >= hi:
            if search.verbose:
                print(
                    f"    size:{lo}..{hi}: {total} resultados "
                    f"(no se puede dividir más; se guardan {cap})",
                    file=sys.stderr,
                )
            batch, observed = search.collect(bucket_query, max_pages=max_pages)
            reported_total += max(total, observed)
            items.extend(batch)
            truncated = True
            continue

        mid = lo + (hi - lo) // 2
        stack.append((mid + 1, hi))
        stack.append((lo, mid))

    return items, reported_total, truncated


def prepare_query(query: str, include_forks: bool) -> str:
    if include_forks and "fork:" not in query:
        return f"{query} fork:true"
    return query


def discover_repositories(
    api: GitHubAPI,
    queries: list[str],
    max_pages: int = SEARCH_MAX_PAGES,
    pause: float = 6.2,
    *,
    exhaustive: bool = False,
    include_forks: bool = False,
    verbose: bool = True,
) -> Discovery:
    """Busca los marcadores de gh-aw y devuelve los repositorios encontrados."""
    search = CodeSearch(api, pause=pause, verbose=verbose)
    found: dict[str, set[str]] = {}
    total_count = 0
    truncated = False
    per_query: list[dict[str, Any]] = []
    effective_queries: list[str] = []

    for raw_query in queries:
        query = prepare_query(raw_query, include_forks)
        effective_queries.append(query)
        print(f"  consulta: {query}")

        if exhaustive and "size:" not in query:
            items, reported, cut = collect_exhaustive(
                search, query, max_pages=max_pages
            )
        else:
            if exhaustive:
                print(
                    "    aviso: la consulta ya incluye size:; "
                    "no se puede repartir automáticamente.",
                    file=sys.stderr,
                )
            items, reported = search.collect(query, max_pages=max_pages)
            cut = reported > max_pages * SEARCH_PER_PAGE

        total_count += reported
        truncated = truncated or cut
        per_query.append(
            {"query": query, "total_count": reported, "truncated": cut}
        )

        for item in items:
            repo = (item.get("repository") or {}).get("full_name")
            path = item.get("path")
            if repo and path:
                found.setdefault(repo, set()).add(path)

    repositories = {
        repo: sorted(paths) for repo, paths in sorted(found.items())
    }
    return Discovery(
        repositories=repositories,
        total_count=total_count,
        truncated=truncated,
        queries=effective_queries,
        per_query=per_query,
        requests=search.requests,
    )


def load_existing_repositories(path: Path) -> dict[str, list[str]]:
    """Lee repositorios de un JSON previo (formato del script o lista simple)."""
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"Aviso: no se pudo leer {path} para --merge: {exc}",
            file=sys.stderr,
        )
        return {}

    repositories = data.get("repositories", data) if isinstance(data, dict) else data
    loaded: dict[str, list[str]] = {}

    if isinstance(repositories, dict):
        for repo, paths in repositories.items():
            if isinstance(paths, list):
                loaded[str(repo)] = [str(path) for path in paths]
            else:
                loaded[str(repo)] = []
    elif isinstance(repositories, list):
        for item in repositories:
            if isinstance(item, str):
                loaded[item] = []

    return loaded


def merge_repositories(
    current: dict[str, list[str]],
    previous: dict[str, list[str]],
) -> dict[str, list[str]]:
    merged = {repo: set(paths) for repo, paths in current.items()}
    for repo, paths in previous.items():
        merged.setdefault(repo, set()).update(paths)
    return {repo: sorted(paths) for repo, paths in sorted(merged.items())}


def write_json_atomic(path: Path, payload: Any) -> None:
    """Escribe el JSON sin dejar el archivo a medias si se interrumpe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def discover_with_sourcegraph(
    query: str,
    max_results: int = 1000,
) -> Discovery:
    """Alternativa sin token: busca en el índice público de Sourcegraph.

    Sourcegraph no indexa todo GitHub, pero permite superar el tope de 1.000
    resultados de la Code Search API y no consume el rate limit de GitHub.
    """
    sourcegraph_query = (
        f"context:global {query} "
        r"file:\.github/workflows/.*\.lock\.yml$ "
        f"count:{max_results}"
    )
    url = "https://sourcegraph.com/.api/search/stream?" + urllib.parse.urlencode(
        {"q": sourcegraph_query, "v": "V3", "t": "literal"}
    )
    request = urllib.request.Request(
        url,
        headers={"Accept": "text/event-stream", "User-Agent": USER_AGENT},
    )

    with urllib.request.urlopen(request, timeout=120) as response:
        text = response.read().decode("utf-8", "replace")

    found: dict[str, set[str]] = {}
    total = 0
    event: str | None = None
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal total
        if event != "matches" or not data_lines:
            return
        try:
            matches = json.loads("".join(data_lines))
        except json.JSONDecodeError:
            return
        for match in matches:
            repo = match.get("repository") or ""
            path = match.get("path") or ""
            if repo.startswith("github.com/"):
                repo = repo[len("github.com/") :]
            if repo and path:
                found.setdefault(repo, set()).add(path)
                total += 1

    for line in text.splitlines():
        if line.startswith("event: "):
            event = line[len("event: ") :]
        elif line.startswith("data: "):
            data_lines.append(line[len("data: ") :])
        elif not line.strip():
            flush()
            event = None
            data_lines = []
    flush()

    repos = {repo: sorted(paths) for repo, paths in sorted(found.items())}
    return Discovery(
        repositories=repos,
        total_count=total,
        truncated=total >= max_results,
        queries=[query],
        per_query=[
            {
                "query": query,
                "total_count": total,
                "truncated": total >= max_results,
            }
        ],
        requests=1,
    )


ISSUES_PER_PAGE = 100
ISSUE_COUNT_CHUNK = 50


def issue_page_count(total_issues: int) -> int:
    """Convierte una cantidad de issues en páginas de 100."""
    return (total_issues + ISSUES_PER_PAGE - 1) // ISSUES_PER_PAGE


def issue_page_counts(
    api: GitHubAPI,
    repositories: list[str],
    *,
    chunk_size: int = ISSUE_COUNT_CHUNK,
    verbose: bool = True,
) -> dict[str, int | None]:
    """Cuenta los issues abiertos y cerrados de cada repositorio vía GraphQL.

    Devuelve {repo: total_issues}. Un valor None indica que no se pudo obtener
    el dato (repositorio borrado, sin permisos o error puntual). Los pull
    requests no se cuentan.
    """
    counts: dict[str, int | None] = {}
    total = len(repositories)

    for start in range(0, total, chunk_size):
        chunk = repositories[start : start + chunk_size]
        parts: list[str] = []
        aliases: dict[str, str] = {}

        for index, repo in enumerate(chunk):
            owner, separator, name = repo.partition("/")
            if not separator or not owner or not name:
                counts[repo] = None
                continue
            alias = f"r{index}"
            aliases[alias] = repo
            parts.append(
                f"{alias}: repository(owner: {json.dumps(owner)}, "
                f"name: {json.dumps(name)}) {{ hasIssuesEnabled "
                "issues(states: [OPEN, CLOSED]) { totalCount } }"
            )

        if not parts:
            continue

        payload = api.graphql("query {\n  " + "\n  ".join(parts) + "\n}")
        data = payload.get("data") or {}
        errors = payload.get("errors") or []

        for error in errors:
            if not isinstance(error, dict):
                continue
            path = error.get("path") or []
            if not path:
                message = error.get("message") or "error desconocido"
                raise RuntimeError(f"GitHub GraphQL: {message}")

        for alias, repo in aliases.items():
            node = data.get(alias)
            if not isinstance(node, dict):
                counts[repo] = None
                continue
            if not node.get("hasIssuesEnabled"):
                counts[repo] = 0
                continue
            issues = node.get("issues") or {}
            value = issues.get("totalCount")
            counts[repo] = int(value) if isinstance(value, int) else None

        if verbose:
            done = min(start + chunk_size, total)
            print(f"    issues: {done}/{total} repositorios")

    return counts


def filter_by_issue_pages(
    repositories: dict[str, list[str]],
    counts: dict[str, int | None],
    min_pages: int,
) -> tuple[dict[str, list[str]], list[str]]:
    """Deja solo los repos con al menos `min_pages` páginas de issues.

    Si no se pudo contar un repositorio, se conserva y se informa aparte para
    no descartarlo por un error temporal.
    """
    kept: dict[str, list[str]] = {}
    unknown: list[str] = []

    for repo, paths in repositories.items():
        total_issues = counts.get(repo)
        if total_issues is None:
            kept[repo] = paths
            unknown.append(repo)
            continue
        if issue_page_count(total_issues) >= min_pages:
            kept[repo] = paths

    return kept, unknown


def command_discover(args: argparse.Namespace) -> int:
    if args.issues_pages < 0:
        print("Error: --issues-pages debe ser >= 0.", file=sys.stderr)
        return 2
    if args.max_pages < 1:
        print("Error: --max-pages debe ser >= 1.", file=sys.stderr)
        return 2
    if args.max_results < 1:
        print("Error: --max-results debe ser >= 1.", file=sys.stderr)
        return 2
    if args.pause < 0:
        print("Error: --pause debe ser >= 0.", file=sys.stderr)
        return 2

    output = Path(args.out)
    api = build_api(args)

    if args.issues_pages > 0 and not api.authenticated:
        print(
            "Error: --issues-pages necesita un token para contar los issues "
            "de cada repositorio. Configurá GITHUB_TOKEN en .env, usá --token "
            "o quitá --issues-pages.",
            file=sys.stderr,
        )
        return 2

    if args.all_results and args.source == "sourcegraph":
        print(
            "Aviso: --all-results solo aplica a la Code Search API; "
            "se ignora con --source sourcegraph.",
            file=sys.stderr,
        )

    if args.source == "sourcegraph":
        print("Buscando repositorios en el índice público de Sourcegraph...")
        discovery = discover_with_sourcegraph(
            query=args.query[0],
            max_results=args.max_results,
        )
    else:
        if not api.authenticated:
            print(
                "Error: la Code Search API requiere autenticación. "
                "Exporta GITHUB_TOKEN, usa --token o prueba --source sourcegraph.",
                file=sys.stderr,
            )
            return 2

        print("Buscando repositorios con workflows de gh-aw...")
        if args.all_results:
            print(
                "  modo exhaustivo: se reparte la búsqueda por tamaño de "
                "archivo para superar el tope de 1.000 resultados.\n"
                "  puede tardar varios minutos y consumir rate limit."
            )
        if args.include_forks:
            print("  se incluyen forks (fork:true).")

        discovery = discover_repositories(
            api,
            queries=args.query,
            max_pages=args.max_pages,
            pause=args.pause,
            exhaustive=args.all_results,
            include_forks=args.include_forks,
            verbose=not args.quiet,
        )

    repos = discovery.repositories
    merged = False

    if args.merge:
        previous = load_existing_repositories(output)
        if previous:
            before = len(repos)
            repos = merge_repositories(repos, previous)
            merged = True
            print(
                f"  fusionado con {output}: {before} -> {len(repos)} "
                "repositorios (sin duplicados)."
            )
        else:
            print(
                f"  aviso: --merge no encontró repositorios en {output}; "
                "se guarda solo la búsqueda actual.",
                file=sys.stderr,
            )

    payload: dict[str, Any] = {
        "generated_at": utc_now(),
        "source": args.source,
        "queries": discovery.queries,
        "total_count": discovery.total_count,
        "truncated": discovery.truncated,
        "repositories": repos,
    }
    if args.source == "github":
        payload["exhaustive"] = bool(args.all_results)
        payload["include_forks"] = bool(args.include_forks)
        payload["search_requests"] = discovery.requests
        payload["query_stats"] = discovery.per_query
    if merged:
        payload["merged"] = True

    found_count = len(repos)
    issue_pages: dict[str, int] = {}
    unknown_repos: list[str] = []

    if args.issues_pages > 0 and repos:
        print()
        print(
            f"Contando issues para exigir al menos {args.issues_pages} "
            f"páginas de {ISSUES_PER_PAGE}..."
        )
        counts = issue_page_counts(api, list(repos), verbose=not args.quiet)
        repos, unknown_repos = filter_by_issue_pages(
            repos, counts, args.issues_pages
        )
        issue_pages = {
            repo: issue_page_count(counts[repo])
            for repo in repos
            if counts.get(repo) is not None
        }
        payload["repositories"] = repos
        payload["min_issue_pages"] = args.issues_pages
        payload["issue_pages"] = issue_pages
        payload["repos_before_filter"] = found_count

        if unknown_repos:
            print(
                f"Aviso: no se pudo contar {len(unknown_repos)} repositorios; "
                "se conservan para no descartarlos por un error temporal.",
                file=sys.stderr,
            )

    write_json_atomic(output, payload)

    print()
    print(f"Repositorios encontrados: {len(repos)}")
    if args.issues_pages > 0:
        print(
            f"Filtro aplicado: >= {args.issues_pages} páginas de issues "
            f"(~{args.issues_pages * ISSUES_PER_PAGE} issues)"
        )
        removed = found_count - len(repos)
        if removed:
            print(f"Descartados por pocos issues: {removed}")
    print(f"Resultados reportados por la fuente: {discovery.total_count}")
    if args.source == "github" and not args.quiet:
        print(f"Peticiones de búsqueda realizadas: {discovery.requests}")

    if discovery.truncated:
        if args.source == "sourcegraph":
            print(
                "⚠️ Sourcegraph devolvió el máximo solicitado; la lista puede "
                "estar incompleta. Aumentá --max-results o usá "
                "--source github --all-results.",
                file=sys.stderr,
            )
        elif args.all_results:
            print(
                "⚠️ Quedaron grupos de archivos que superan el tope incluso "
                "después de repartirlos por tamaño (probablemente muchos "
                "archivos del mismo tamaño). La lista puede estar incompleta.",
                file=sys.stderr,
            )
        else:
            print(
                "⚠️ Se alcanzó el tope de resultados de la búsqueda; "
                "la lista puede estar incompleta. Probá --all-results para "
                "repartirla por tamaño y --include-forks para sumar forks.",
                file=sys.stderr,
            )

    print(f"Guardado en: {output}")
    print()
    print("Siguiente paso:")
    scan_script = Path(__file__).with_name("repo_scan_actions.py").as_posix()
    print(
        f"  python {scan_script} batch --repos-file {output} "
        f"--runs 100 --out-dir reports"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Descubre repositorios que usan GitHub Agentic Workflows y guarda "
            "la lista para repo_scan_actions.py. Sin argumentos busca en la "
            "Code Search API y guarda repos.json."
        )
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
    parser.add_argument(
        "--source",
        choices=("github", "sourcegraph"),
        default="github",
        help="Fuente del descubrimiento (github usa Code Search API)",
    )
    parser.add_argument(
        "--query",
        action="append",
        default=None,
        metavar="TEXTO",
        help=f"Consulta de búsqueda (repetible). Por defecto: {DEFAULT_QUERY!r}",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=10,
        metavar="N",
        help=(
            "Máximo de páginas (100 resultados cada una) por consulta. En "
            "modo --all-results define el tamaño máximo de cada grupo"
        ),
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=1000,
        metavar="N",
        help="Máximo de coincidencias en Sourcegraph",
    )
    parser.add_argument(
        "--all-results",
        action="store_true",
        help=(
            "Recuperar todos los resultados posibles repartiendo la consulta "
            "por tamaño de archivo (size:). Supera el tope de 1.000 de la "
            "Code Search API, pero puede tardar varios minutos"
        ),
    )
    parser.add_argument(
        "--include-forks",
        action="store_true",
        help=(
            "Incluir forks (agrega fork:true a la consulta). La búsqueda de "
            "código los excluye por defecto"
        ),
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help=(
            "Fusionar los repositorios encontrados con los del archivo --out "
            "si ya existe, sin duplicados"
        ),
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=6.2,
        metavar="SEGUNDOS",
        help=(
            "Pausa entre peticiones de búsqueda para respetar el rate limit "
            "(por defecto: 6.2)"
        ),
    )
    parser.add_argument(
        "--issues-pages",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Conservar solo repositorios con N o más páginas de issues "
            "(100 issues por página, abiertos y cerrados). 0 = sin filtro"
        ),
    )
    parser.add_argument(
        "--out",
        default="repos.json",
        metavar="ARCHIVO",
        help="Archivo JSON de salida (por defecto: repos.json)",
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

    if not args.query:
        args.query = [DEFAULT_QUERY]

    try:
        return int(command_discover(args))
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
