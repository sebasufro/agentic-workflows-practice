#!/usr/bin/env python3
"""
scan_gh_aw.py — Descubre repositorios que usan GitHub Agentic Workflows (gh-aw)
y genera reportes Markdown de sus workflows y ejecuciones de GitHub Actions.

La señal que se busca en el código es `gh-aw-metadata`, la primera línea que
gh-aw escribe en cada archivo `.lock.yml` compilado. Luego, para cada repo, se
emparejan los `.md` (fuente) con los `.lock.yml` (pipeline compilado) usando el
mismo nombre base y se consultan las ejecuciones de Actions.

Ejemplos:

    # GITHUB_TOKEN puede venir del entorno o de un archivo .env en la raíz:
    #   GITHUB_TOKEN=ghp_...
    # El script lo busca automáticamente (./.env o la raíz del repo).

    # 1) Descubrir repositorios (Code Search API)
    python scripts/scan_gh_aw.py discover --out repos.json

    # 2) Analizar un repositorio y generar un reporte
    python scripts/scan_gh_aw.py repo PowerShell/vscode-powershell --runs 100

    # 3) Analizar todos los repositorios descubiertos
    python scripts/scan_gh_aw.py batch --repos-file repos.json \
        --runs 100 --out-dir reports

    # 4) Analizar solo una selección, sin gastar llamadas en el resto
    python scripts/scan_gh_aw.py batch --repo PowerShell/vscode-powershell \
        --repo dotnet/runtime --runs 100 --out-dir reports
    python scripts/scan_gh_aw.py batch --repos-file repos.json \
        --org PowerShell --limit 5 --dry-run

Notas:
- El token solo necesita acceso de lectura al código/acciones de los repos
  que quieras analizar. Para repos públicos, un token clásico sin scopes sirve.
- La Code Search API solo indexa la rama por defecto, archivos < 384 KB y
  devuelve como máximo 1.000 resultados por consulta (10 páginas de 100).
- El listado de ejecuciones de Actions devuelve como máximo 100 por página.
- GitHub conserva las ejecuciones ~90 días por defecto en repos públicos.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API_ROOT = "https://api.github.com"
USER_AGENT = "gh-aw-research-script"
DEFAULT_QUERY = "gh-aw-metadata path:.github/workflows"
LOCK_SUFFIX = ".lock.yml"
MD_SUFFIX = ".md"


class AuthenticationError(RuntimeError):
    """Token ausente, inválido o expirado."""

try:  # PyYAML es opcional: sin él se puede seguir generando el reporte.
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None


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
    ) -> tuple[bytes | None, Any]:
        """GET con reintentos. Devuelve (body, headers) o (None, headers) en 404."""
        request = urllib.request.Request(url, headers=self._headers(accept))

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


# --------------------------------------------------------------------------- #
# Utilidades de fechas / Markdown
# --------------------------------------------------------------------------- #


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def run_duration(run: dict[str, Any]) -> int | None:
    if run.get("status") != "completed":
        return None
    start = parse_datetime(run.get("run_started_at") or run.get("created_at"))
    end = parse_datetime(run.get("updated_at"))
    if not start or not end:
        return None
    return max(0, int((end - start).total_seconds()))


def cell(value: Any, max_len: int = 90) -> str:
    text = "—" if value in (None, "", [], {}) else str(value)
    text = text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def format_datetime(value: str | None) -> str:
    dt = parse_datetime(value)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


CONCLUSION_ICONS = {
    "success": "✅",
    "failure": "❌",
    "cancelled": "⏹️",
    "skipped": "⏭️",
    "timed_out": "⏱️",
    "action_required": "⚠️",
    "neutral": "➖",
    "stale": "🟡",
    "startup_failure": "💥",
}


def format_run_state(run: dict[str, Any]) -> str:
    if run.get("status") != "completed":
        return f"🔄 {run.get('status') or 'unknown'}"
    conclusion = run.get("conclusion") or "unknown"
    return f"{CONCLUSION_ICONS.get(conclusion, '•')} {conclusion}"


# --------------------------------------------------------------------------- #
# Frontmatter de los workflows .md
# --------------------------------------------------------------------------- #

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)


def parse_frontmatter(text: str) -> dict[str, Any]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    raw = match.group(1)
    if yaml is None:
        fallback: dict[str, Any] = {}
        for line in raw.splitlines():
            key, sep, value = line.partition(":")
            if sep and not line.startswith((" ", "\t", "-")):
                fallback[key.strip()] = value.strip()
        return fallback
    try:
        data = yaml.safe_load(raw)
        return data if isinstance(data, dict) else {}
    except Exception:  # YAML inválido: no abortamos el reporte
        return {}


def format_trigger(value: Any) -> str:
    if value in (None, "", [], {}):
        return "—"
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            key = str(key)
            if key == "schedule" and isinstance(item, list):
                crons = [entry.get("cron") for entry in item if isinstance(entry, dict)]
                parts.append("schedule: " + ", ".join(str(c) for c in crons if c))
            elif isinstance(item, dict):
                types = item.get("types")
                parts.append(f"{key}: {', '.join(map(str, types))}" if types else key)
            elif isinstance(item, list):
                parts.append(f"{key}: {', '.join(map(str, item))}")
            else:
                parts.append(key)
        return "; ".join(parts) or "—"
    return str(value)


def format_engine(value: Any) -> str:
    if value in (None, ""):
        return "—"
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("id", "name", "model"):
            if value.get(key):
                return str(value[key])
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def format_safe_outputs(value: Any) -> str:
    if value in (None, "", [], {}):
        return "—"
    if isinstance(value, dict):
        keys = [str(key) for key in value if key not in {"max", "global"}]
        if len(keys) > 6:
            return ", ".join(keys[:6]) + f" (+{len(keys) - 6})"
        return ", ".join(keys) or "—"
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return str(value)


def workflow_metadata(frontmatter: dict[str, Any]) -> dict[str, str]:
    # PyYAML interpreta `on` como booleano True (YAML 1.1); cubrimos ambos casos.
    trigger = frontmatter.get("on", frontmatter.get(True))
    description = (
        frontmatter.get("description")
        or frontmatter.get("intent")
        or frontmatter.get("name")
        or ""
    )
    return {
        "description": str(description),
        "trigger": format_trigger(trigger),
        "engine": format_engine(frontmatter.get("engine")),
        "safe_outputs": format_safe_outputs(frontmatter.get("safe-outputs")),
    }


# --------------------------------------------------------------------------- #
# Descubrimiento de repositorios
# --------------------------------------------------------------------------- #


def discover_repositories(
    api: GitHubAPI,
    queries: list[str],
    max_pages: int = 10,
    pause: float = 6.2,
) -> tuple[dict[str, list[str]], int, bool]:
    """Busca los marcadores de gh-aw y devuelve {repo: [archivos encontrados]}."""
    found: dict[str, set[str]] = {}
    total_count = 0

    for query_index, query in enumerate(queries):
        print(f"  consulta: {query}")
        for page in range(1, max_pages + 1):
            data, _ = api.get_json(
                "/search/code",
                {"q": query, "per_page": 100, "page": page},
            )
            if data is None:
                break

            total_count = max(total_count, int(data.get("total_count") or 0))
            items = data.get("items") or []
            for item in items:
                repo = (item.get("repository") or {}).get("full_name")
                path = item.get("path")
                if repo and path:
                    found.setdefault(repo, set()).add(path)

            print(f"    página {page}: {len(items)} archivos")
            if len(items) < 100:
                break
            if page < max_pages:
                time.sleep(pause)

        if query_index < len(queries) - 1:
            time.sleep(pause)

    repos = {repo: sorted(paths) for repo, paths in sorted(found.items())}
    truncated = total_count > max_pages * 100
    return repos, total_count, truncated


def discover_with_sourcegraph(
    query: str,
    max_results: int = 1000,
) -> tuple[dict[str, list[str]], int, bool]:
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
    return repos, total, total >= max_results


# --------------------------------------------------------------------------- #
# Análisis de un repositorio
# --------------------------------------------------------------------------- #


def get_repository_info(api: GitHubAPI, repo: str) -> dict[str, Any]:
    data, _ = api.get_json(f"/repos/{repo}")
    if not data:
        raise RuntimeError(f"No se encontró el repositorio {repo}")
    return data


def get_tree_paths(api: GitHubAPI, repo: str, ref: str) -> tuple[list[str], bool]:
    encoded_ref = urllib.parse.quote(ref, safe="")
    data, _ = api.get_json(f"/repos/{repo}/git/trees/{encoded_ref}", {"recursive": 1})
    if not data:
        return [], False
    paths = [
        entry["path"]
        for entry in data.get("tree") or []
        if entry.get("type") == "blob" and entry.get("path")
    ]
    return paths, bool(data.get("truncated"))


def find_workflow_pairs(paths: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Empareja foo.md con foo.lock.yml dentro de .github/workflows/."""
    workflows = [path for path in paths if path.startswith(".github/workflows/")]
    lock_files = sorted(path for path in workflows if path.endswith(LOCK_SUFFIX))
    markdown_files = {path for path in workflows if path.endswith(MD_SUFFIX)}

    pairs: list[dict[str, Any]] = []
    for lock_path in lock_files:
        source_path = lock_path[: -len(LOCK_SUFFIX)] + MD_SUFFIX
        pairs.append(
            {
                "lock": lock_path,
                "source": source_path if source_path in markdown_files else None,
            }
        )
        markdown_files.discard(source_path)

    return pairs, sorted(markdown_files)


def download_source_metadata(
    api: GitHubAPI,
    repo: str,
    ref: str,
    pairs: list[dict[str, Any]],
) -> None:
    for pair in pairs:
        source = pair.get("source")
        if not source:
            pair["metadata"] = {}
            continue

        encoded_path = urllib.parse.quote(source, safe="/")
        encoded_ref = urllib.parse.quote(ref, safe="")
        url = f"{API_ROOT}/repos/{repo}/contents/{encoded_path}?ref={encoded_ref}"
        text = api.get_text(url)
        pair["metadata"] = workflow_metadata(parse_frontmatter(text)) if text else {}


def list_workflow_runs(
    api: GitHubAPI,
    repo: str,
    limit: int,
    only_agentic: bool,
    max_pages: int = 20,
) -> tuple[list[dict[str, Any]], int | None, int]:
    """Devuelve las últimas `limit` ejecuciones (filtrando .lock.yml por defecto)."""
    selected: list[dict[str, Any]] = []
    total_count: int | None = None
    page = 1

    while len(selected) < limit and page <= max_pages:
        data, _ = api.get_json(
            f"/repos/{repo}/actions/runs",
            {"per_page": 100, "page": page},
        )
        if data is None:
            break

        total_count = data.get("total_count", total_count)
        batch = data.get("workflow_runs") or []
        for run in batch:
            path = run.get("path") or ""
            if not only_agentic or path.endswith(LOCK_SUFFIX):
                selected.append(run)

        print(
            f"    ejecuciones página {page}: {len(batch)} leídas, "
            f"{len(selected)} acumuladas"
        )
        if len(batch) < 100:
            break
        page += 1

    selected.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return selected[:limit], total_count, page


def analyze_repository(
    api: GitHubAPI,
    repo: str,
    runs_limit: int,
    only_agentic: bool,
    max_run_pages: int,
) -> dict[str, Any]:
    print(f"  metadata del repositorio...")
    info = get_repository_info(api, repo)
    branch = info.get("default_branch") or "main"

    print(f"  árbol de archivos ({branch})...")
    paths, tree_truncated = get_tree_paths(api, repo, branch)
    pairs, orphan_markdown = find_workflow_pairs(paths)

    print(f"  {len(pairs)} pares .md/.lock.yml; leyendo frontmatter...")
    download_source_metadata(api, repo, branch, pairs)

    print(f"  ejecuciones de Actions (límite {runs_limit})...")
    runs, total_runs, pages_read = list_workflow_runs(
        api,
        repo,
        limit=runs_limit,
        only_agentic=only_agentic,
        max_pages=max_run_pages,
    )

    return {
        "repo": repo,
        "info": info,
        "branch": branch,
        "tree_truncated": tree_truncated,
        "pairs": pairs,
        "orphan_markdown": orphan_markdown,
        "runs": runs,
        "total_runs": total_runs,
        "run_pages_read": pages_read,
        "only_agentic": only_agentic,
        "generated_at": utc_now(),
    }


# --------------------------------------------------------------------------- #
# Reporte Markdown
# --------------------------------------------------------------------------- #


def summarize_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        grouped[run.get("path") or "(sin archivo)"].append(run)

    summaries: list[dict[str, Any]] = []
    for path, group in grouped.items():
        conclusions = Counter(
            run.get("conclusion")
            for run in group
            if run.get("status") == "completed"
        )
        completed = sum(conclusions.values())
        success = conclusions.get("success", 0)
        failure = conclusions.get("failure", 0)
        other = completed - success - failure
        durations = [d for run in group if (d := run_duration(run)) is not None]
        median = int(statistics.median(durations)) if durations else None
        latest = max((run.get("created_at") or "" for run in group), default="")

        summaries.append(
            {
                "path": path,
                "name": next(
                    (run.get("name") for run in group if run.get("name")),
                    Path(path).name,
                ),
                "runs": len(group),
                "success": success,
                "failure": failure,
                "other": other,
                "completed": completed,
                "rate": (success / completed) if completed else None,
                "median": median,
                "latest": latest,
            }
        )

    summaries.sort(key=lambda item: (-item["runs"], item["path"]))
    return summaries


def render_report(data: dict[str, Any]) -> str:
    info = data["info"]
    repo = data["repo"]
    pairs = data["pairs"]
    runs = data["runs"]
    only_agentic = data["only_agentic"]

    branches = data["branch"]
    stars = info.get("stargazers_count")
    html_url = info.get("html_url") or f"https://github.com/{repo}"
    description = info.get("description") or ""

    lines: list[str] = [
        f"# {repo}",
        "",
    ]
    if description:
        lines += [f"> {cell(description, 300)}", ""]

    lines += [
        f"- **Repositorio:** <{html_url}>",
        f"- **Rama por defecto:** `{branches}`",
        f"- **Estrellas:** {stars:,}" if isinstance(stars, int) else "- **Estrellas:** —",
        f"- **Generado:** {data['generated_at']}",
        f"- **Pares `.md` ↔ `.lock.yml`:** {len(pairs)}",
        f"- **Ejecuciones analizadas:** {len(runs)}"
        + (" (solo workflows `.lock.yml`)" if only_agentic else " (todos los workflows)"),
        f"- **Ejecuciones totales del repo (según API):** {data['total_runs']:,}"
        if isinstance(data.get("total_runs"), int)
        else "- **Ejecuciones totales del repo (según API):** —",
        "",
    ]

    if data.get("tree_truncated"):
        lines += [
            "> ⚠️ El árbol de archivos devuelto por la API está truncado; "
            "puede faltar algún workflow en repositorios muy grandes.",
            "",
        ]

    lines += [
        "## Archivos agénticos (`.md` ↔ `.lock.yml`)",
        "",
    ]

    if pairs:
        lines += [
            "| # | Fuente `.md` | Pipeline `.lock.yml` | Propósito | Disparador | Engine | Safe outputs |",
            "|---:|---|---|---|---|---|---|",
        ]
        for index, pair in enumerate(pairs, 1):
            metadata = pair.get("metadata") or {}
            source = f"`{pair['source']}`" if pair.get("source") else "⚠️ no encontrado"
            lines.append(
                "| {index} | {source} | `{lock}` | {purpose} | {trigger} | {engine} | {outputs} |".format(
                    index=index,
                    source=source,
                    lock=pair["lock"],
                    purpose=cell(metadata.get("description")),
                    trigger=cell(metadata.get("trigger")),
                    engine=cell(metadata.get("engine")),
                    outputs=cell(metadata.get("safe_outputs")),
                )
            )
    else:
        lines.append("No se encontraron pares `.md` / `.lock.yml` en la rama por defecto.")

    if data.get("orphan_markdown"):
        lines += [
            "",
            "### Archivos `.md` sin `.lock.yml` asociado",
            "",
            "Pueden ser imports (`shared/`), sub-agentes o workflows sin compilar.",
            "",
        ]
        for path in data["orphan_markdown"][:30]:
            lines.append(f"- `{path}`")
        if len(data["orphan_markdown"]) > 30:
            lines.append(f"- … y {len(data['orphan_markdown']) - 30} más")

    lines += [
        "",
        "## Resumen de ejecuciones",
        "",
    ]

    summaries = summarize_runs(runs)
    if summaries:
        lines += [
            "| Workflow | Archivo | Runs | ✅ | ❌ | Otros | Tasa de éxito | Duración mediana | Última ejecución (UTC) |",
            "|---|---|---:|---:|---:|---:|---:|---:|---|",
        ]
        for summary in summaries:
            rate = f"{summary['rate'] * 100:.0f}%" if summary["rate"] is not None else "—"
            lines.append(
                "| {name} | `{file}` | {runs} | {success} | {failure} | {other} | {rate} | {avg} | {latest} |".format(
                    name=cell(summary["name"], 45),
                    file=cell(Path(summary["path"]).name, 55),
                    runs=summary["runs"],
                    success=summary["success"],
                    failure=summary["failure"],
                    other=summary["other"],
                    rate=rate,
                    avg=format_duration(summary["median"]),
                    latest=format_datetime(summary["latest"]),
                )
            )
        lines += [
            "",
            "> La duración se calcula con `run_started_at → updated_at`. En ejecuciones "
            "que esperan aprobación puede incluir tiempo en cola; por eso el resumen usa "
            "la mediana y no el promedio.",
        ]
    else:
        lines.append("No hay ejecuciones para el filtro solicitado.")

    lines += [
        "",
        f"## Últimas {len(runs)} ejecuciones",
        "",
    ]

    if runs:
        lines += [
            "| # | Fecha (UTC) | Workflow | Archivo | Evento | Rama | Estado | Duración | Actor | Run |",
            "|---:|---|---|---|---|---|---|---:|---|---|",
        ]
        for index, run in enumerate(runs, 1):
            path = run.get("path") or ""
            workflow_url = run.get("html_url") or ""
            run_number = run.get("run_number")
            attempt = run.get("run_attempt")
            label = f"#{run_number}" if run_number is not None else "run"
            if attempt and attempt > 1:
                label += f" (intento {attempt})"
            lines.append(
                "| {index} | {date} | {name} | `{file}` | {event} | {branch} | {state} | {duration} | {actor} | [{label}]({url}) |".format(
                    index=index,
                    date=format_datetime(run.get("created_at")),
                    name=cell(run.get("name"), 40),
                    file=cell(Path(path).name if path else "—", 45),
                    event=cell(run.get("event"), 24),
                    branch=cell(run.get("head_branch"), 30),
                    state=format_run_state(run),
                    duration=format_duration(run_duration(run)),
                    actor=cell((run.get("actor") or {}).get("login"), 24),
                    label=label,
                    url=workflow_url,
                )
            )
    else:
        lines.append("No se encontraron ejecuciones.")

    lines += [
        "",
        "---",
        "",
        "Generado con `scripts/scan_gh_aw.py`. Fuentes: GitHub REST API "
        "`/search/code`, `/repos/{owner}/{repo}/git/trees` y "
        "`/repos/{owner}/{repo}/actions/runs`.",
        "",
    ]

    return "\n".join(lines)


def write_report(data: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = data["repo"].replace("/", "__") + ".md"
    path = output_dir / filename
    path.write_text(render_report(data), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Comandos CLI
# --------------------------------------------------------------------------- #


def build_api(args: argparse.Namespace) -> GitHubAPI:
    return GitHubAPI(token=args.token, verbose=not args.quiet)


def command_discover(args: argparse.Namespace) -> int:
    api = build_api(args)

    if args.source == "sourcegraph":
        print("Buscando repositorios en el índice público de Sourcegraph...")
        repos, total_count, truncated = discover_with_sourcegraph(
            query=args.query[0],
            max_results=args.max_results,
        )
        queries = [args.query[0]]
    else:
        if not api.authenticated:
            print(
                "Error: la Code Search API requiere autenticación. "
                "Exporta GITHUB_TOKEN, usa --token o prueba --source sourcegraph.",
                file=sys.stderr,
            )
            return 2

        print("Buscando repositorios con workflows de gh-aw...")
        repos, total_count, truncated = discover_repositories(
            api,
            queries=args.query,
            max_pages=args.max_pages,
        )
        queries = args.query

    payload = {
        "generated_at": utc_now(),
        "source": args.source,
        "queries": queries,
        "total_count": total_count,
        "truncated": truncated,
        "repositories": repos,
    }

    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print()
    print(f"Repositorios encontrados: {len(repos)}")
    print(f"Resultados reportados por la fuente: {total_count}")
    if truncated:
        print(
            "⚠️ Se alcanzó el tope de resultados de la búsqueda; "
            "la lista puede estar incompleta. Divide la búsqueda por org/user "
            "o complementa con otra fuente (Sourcegraph/BigQuery).",
            file=sys.stderr,
        )
    print(f"Guardado en: {output}")
    print()
    print("Siguiente paso:")
    print(
        f"  python {Path(__file__).as_posix()} batch --repos-file {output} "
        f"--runs 100 --out-dir reports"
    )
    return 0


def command_repo(args: argparse.Namespace) -> int:
    api = build_api(args)
    if not api.authenticated:
        print(
            "Aviso: sin GITHUB_TOKEN el límite es 60 peticiones/hora; "
            "puede no alcanzar para varios repositorios.",
            file=sys.stderr,
        )

    output_dir = Path(args.out_dir)
    for repo in args.repo:
        print(f"Analizando {repo}...")
        data = analyze_repository(
            api,
            repo,
            runs_limit=args.runs,
            only_agentic=not args.all_runs,
            max_run_pages=args.max_run_pages,
        )
        if args.json_out:
            json_path = Path(args.json_out)
            if len(args.repo) > 1:
                json_path = json_path.with_name(
                    f"{json_path.stem}__{repo.replace('/', '__')}{json_path.suffix}"
                )
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            print(f"  JSON: {json_path}")
        report_path = write_report(data, output_dir)
        print(f"  reporte: {report_path}")
    return 0


def normalize_repository(value: str) -> str:
    repo = value.strip()
    if repo.startswith(("https://github.com/", "http://github.com/")):
        repo = repo.split("github.com/", 1)[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return repo.strip("/")


def load_repository_list(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # También acepta una lista simple: una línea owner/repo por renglón.
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
    raise ValueError(f"Formato de lista de repositorios no reconocido: {path}")


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


def command_batch(args: argparse.Namespace) -> int:
    repos: list[str] = []
    for repo in args.repo or []:
        repos.append(normalize_repository(repo))
    if args.repos_file:
        repos.extend(load_repository_list(Path(args.repos_file)))

    # Deduplica sin perder el orden de entrada.
    repositories = list(dict.fromkeys(repo for repo in repos if repo))
    if not repositories:
        print(
            "Error: indicá --repos-file o al menos un --repo owner/repo.",
            file=sys.stderr,
        )
        return 2

    if args.limit is not None and args.limit < 0:
        print("Error: --limit debe ser >= 0.", file=sys.stderr)
        return 2
    if args.sample is not None and args.sample < 0:
        print("Error: --sample debe ser >= 0.", file=sys.stderr)
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
    print(
        f"Repositorios: {len(repositories)} disponibles; "
        f"{len(selected)} seleccionados."
    )

    if not selected:
        print("No quedó ningún repositorio después de aplicar los filtros.", file=sys.stderr)
        return 2

    if args.dry_run:
        for repo in selected:
            print(f"  {repo}")
        print()
        print(
            "Cada repositorio consume ~2 llamadas fijas (metadata + árbol), "
            "1 por par .md/.lock.yml y 1 o más por página de ejecuciones."
        )
        return 0

    api = build_api(args)
    if not api.authenticated and len(selected) > 5:
        print(
            "Aviso: sin GITHUB_TOKEN el límite es 60 peticiones/hora. "
            "Para un lote grande, exporta un token.",
            file=sys.stderr,
        )

    output_dir = Path(args.out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    index_rows: list[str] = []
    errors: list[tuple[str, str]] = []

    for position, repo in enumerate(selected, 1):
        print(f"[{position}/{len(selected)}] {repo}")
        try:
            data = analyze_repository(
                api,
                repo,
                runs_limit=args.runs,
                only_agentic=not args.all_runs,
                max_run_pages=args.max_run_pages,
            )
            report_path = write_report(data, output_dir)
            summaries = summarize_runs(data["runs"])
            total = sum(item["runs"] for item in summaries)
            success = sum(item["success"] for item in summaries)
            failure = sum(item["failure"] for item in summaries)
            rate = f"{success / total * 100:.0f}%" if total else "—"
            index_rows.append(
                "| [{repo}](https://github.com/{repo}) | {pairs} | {runs} | {success} | {failure} | {rate} | [{file}](./{file}) |".format(
                    repo=repo,
                    pairs=len(data["pairs"]),
                    runs=total,
                    success=success,
                    failure=failure,
                    rate=rate,
                    file=report_path.name,
                )
            )
        except Exception as exc:  # el lote sigue aunque un repo falle
            print(f"  error: {exc}", file=sys.stderr)
            errors.append((repo, str(exc)))

        if args.sleep and position < len(selected):
            time.sleep(args.sleep)

    index_path = output_dir / "index.md"
    index_lines = [
        "# Repositorios con GitHub Agentic Workflows",
        "",
        f"Generado: {utc_now()}",
        "",
        f"- Repositorios procesados: {len(selected) - len(errors)}/{len(selected)}",
        f"- Ejecuciones analizadas por repo (máximo): {args.runs}",
        "",
        "| Repositorio | Pares `.md`/`.lock.yml` | Runs | ✅ | ❌ | Tasa | Reporte |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    index_lines += index_rows
    if errors:
        index_lines += ["", "## Errores", ""]
        index_lines += [f"- `{repo}`: {cell(message, 240)}" for repo, message in errors]
    index_lines.append("")
    index_path.write_text("\n".join(index_lines), encoding="utf-8")

    print()
    print(f"Reportes en: {output_dir}")
    print(f"Índice: {index_path}")
    if errors:
        print(f"Repositorios con error: {len(errors)}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Descubre repositorios que usan GitHub Agentic Workflows y genera "
            "reportes Markdown de sus archivos .md/.lock.yml y ejecuciones."
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
    parser.add_argument("--quiet", action="store_true", help="Oculta mensajes de progreso")

    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser(
        "discover",
        help="Busca repositorios con el marcador gh-aw-metadata (Code Search API)",
    )
    discover.add_argument(
        "--source",
        choices=("github", "sourcegraph"),
        default="github",
        help="Fuente del descubrimiento (github usa Code Search API)",
    )
    discover.add_argument(
        "--query",
        action="append",
        default=None,
        help=f"Consulta de búsqueda (repetible). Por defecto: {DEFAULT_QUERY!r}",
    )
    discover.add_argument("--max-pages", type=int, default=10, help="Máximo de páginas (100 c/u)")
    discover.add_argument(
        "--max-results",
        type=int,
        default=1000,
        help="Máximo de coincidencias en Sourcegraph",
    )
    discover.add_argument("--out", default="repos.json", help="Archivo JSON de salida")
    discover.set_defaults(func=command_discover)

    repo = subparsers.add_parser("repo", help="Analiza uno o más repositorios")
    repo.add_argument("repo", nargs="+", help="owner/repo")
    repo.add_argument("--runs", type=int, default=100, help="Máximo de ejecuciones a listar")
    repo.add_argument(
        "--all-runs",
        action="store_true",
        help="Incluye todos los workflows, no solo los .lock.yml",
    )
    repo.add_argument(
        "--max-run-pages",
        type=int,
        default=20,
        help="Máximo de páginas de 100 ejecuciones a recorrer",
    )
    repo.add_argument("--out-dir", default="reports", help="Directorio de reportes")
    repo.add_argument("--json-out", help="Guarda también los datos crudos en JSON")
    repo.set_defaults(func=command_repo)

    batch = subparsers.add_parser("batch", help="Analiza una lista o selección de repositorios")
    batch.add_argument(
        "--repos-file",
        help="JSON generado por discover o texto con owner/repo por línea",
    )
    batch.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help="Repositorio a analizar (repetible; se suma a --repos-file)",
    )
    batch.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help="Incluir solo este repositorio (repetible)",
    )
    batch.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help="Excluir este repositorio (repetible)",
    )
    batch.add_argument(
        "--org",
        action="append",
        default=[],
        metavar="OWNER",
        help="Incluir solo repositorios de esta organización/usuario (repetible)",
    )
    batch.add_argument(
        "--limit",
        type=int,
        help="Procesar como máximo N repositorios después de filtrar",
    )
    batch.add_argument(
        "--sample",
        type=int,
        help="Elegir al azar N repositorios después de filtrar",
    )
    batch.add_argument(
        "--seed",
        type=int,
        help="Semilla para que --sample sea reproducible",
    )
    batch.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostrar la selección y salir sin llamar a la API",
    )
    batch.add_argument("--runs", type=int, default=100, help="Máximo de ejecuciones por repo")
    batch.add_argument(
        "--all-runs",
        action="store_true",
        help="Incluye todos los workflows, no solo los .lock.yml",
    )
    batch.add_argument(
        "--max-run-pages",
        type=int,
        default=20,
        help="Máximo de páginas de 100 ejecuciones a recorrer",
    )
    batch.add_argument("--out-dir", default="reports", help="Directorio de reportes")
    batch.add_argument("--sleep", type=float, default=0.0, help="Pausa entre repositorios (s)")
    batch.set_defaults(func=command_batch)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        dotenv_path = (
            None if args.token else load_token_from_dotenv(args.env_file, warn=not args.quiet)
        )
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        if args.token:
            token_source = "--token"
        elif dotenv_path:
            token_source = str(dotenv_path)
        elif os.environ.get("GITHUB_TOKEN"):
            token_source = "GITHUB_TOKEN del entorno"
        elif os.environ.get("GH_TOKEN"):
            token_source = "GH_TOKEN del entorno"
        else:
            token_source = None

        if token_source:
            print(f"Token: {token_source}", file=sys.stderr)

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

    if args.command == "discover" and not args.query:
        args.query = [DEFAULT_QUERY]

    try:
        return int(args.func(args))
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
