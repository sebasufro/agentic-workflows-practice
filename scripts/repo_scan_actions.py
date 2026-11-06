#!/usr/bin/env python3
"""
repo_scan_actions.py — Analiza repositorios con gh-aw y sus ejecuciones de Actions.

Para cada repositorio empareja los `.md` con los `.lock.yml`, lee el frontmatter
de los workflows y consulta las últimas ejecuciones de GitHub Actions. Genera un
reporte Markdown por repositorio y un índice cuando se usa `batch`.

La lista de repositorios puede venir de `repo_search_agentic.py` (repos.json),
de un archivo de texto o de `--repo owner/repo`.

Ejemplos:

    python scripts/repo_scan_actions.py repo PowerShell/vscode-powershell --runs 100
    python scripts/repo_scan_actions.py batch --repos-file repos.json --runs 100
    python scripts/repo_scan_actions.py batch --repos-file repos.json --org PowerShell --limit 5
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

try:  # PyYAML es opcional: sin él se puede seguir generando el reporte.
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None

from repo_search_agentic import (
    API_ROOT,
    AuthenticationError,
    GitHubAPI,
    build_api,
    load_and_report_token,
    utc_now,
)

LOCK_SUFFIX = ".lock.yml"
MD_SUFFIX = ".md"


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
        "Generado con `scripts/repo_scan_actions.py`. Fuentes: GitHub REST API "
        "`/repos/{owner}/{repo}/git/trees`, `/repos/{owner}/{repo}/contents` y "
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
            "Analiza repositorios con GitHub Agentic Workflows, sus archivos "
            ".md/.lock.yml y sus ejecuciones de GitHub Actions."
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
        help="JSON generado por repo_search_agentic.py o texto con owner/repo por línea",
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
        load_and_report_token(args)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

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
