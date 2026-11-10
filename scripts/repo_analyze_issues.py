#!/usr/bin/env python3
"""
repo_analyze_issues.py — Analiza los issues de proyectos que usan gh-aw.

Lee el CSV generado por `repo_filter_issues.py`
(`filtered_issues/agentic_issues.csv` por defecto), consulta cada issue en la
API de GitHub para obtener autor, etiquetas, estado, cierre y comentarios, y
produce un CSV enriquecido más resúmenes para responder:

- ¿El issue lo creó automáticamente GitHub Agentic Workflows o una persona?
- ¿Qué tipo de problema es (fallo, falta de safe outputs, lock desactualizado,
  presupuesto de IA, rate limit, archivo protegido, etc.)?
- ¿Cuál es la causa probable y qué evidencia textual la respalda?
- ¿Cómo se resolvió (abierto, cerrado por un desarrollador, descartado,
  duplicado, cerrado automáticamente)?

Salidas (por defecto en `analysis/`):

    analysis/issues_enriched.csv   Un issue por fila, con la clasificación.
    analysis/summary.md            Resumen general y tablas cruzadas.
    analysis/por_categoria.csv     Agregado por categoría.
    analysis/por_repositorio.csv   Agregado por repositorio.
    analysis/por_flujo.csv         Agregado por workflow agéntico.

Los datos crudos de la API se guardan en `analysis/.cache/` para no repetir
llamadas al volver a ejecutar. Usá `--refresh` para ignorar la caché.

Ejemplos:

    # Analizar todos los issues del CSV filtrado
    python scripts/repo_analyze_issues.py

    # Probar con los primeros 10 issues
    python scripts/repo_analyze_issues.py --limit 10

    # Incluir también los comentarios de issues abiertos
    python scripts/repo_analyze_issues.py --comments all

    # Ignorar la caché y consultar todo de nuevo
    python scripts/repo_analyze_issues.py --refresh
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from repo_search_agentic import (
    AuthenticationError,
    GitHubAPI,
    build_api,
    load_and_report_token,
    utc_now,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = PROJECT_ROOT / "filtered_issues" / "agentic_issues.csv"
DEFAULT_OUT_DIR = PROJECT_ROOT / "analysis"

CACHE_VERSION = 1

# Columnas del CSV enriquecido, en el orden en que se escriben.
ENRICHED_FIELDS = (
    "dueño_repositorio",
    "nombre_repositorio",
    "numero_issue",
    "titulo",
    "link_issue",
    "estado",
    "motivo_estado",
    "autor",
    "tipo_autor",
    "origen",
    "flujo_trabajo",
    "naturaleza",
    "categoria",
    "categorias_secundarias",
    "causa_probable",
    "evidencia",
    "resolucion",
    "cerrado_por",
    "tipo_cerrador",
    "creado_en",
    "cerrado_en",
    "dias_abierto",
    "expiracion",
    "expirado",
    "comentarios",
    "comentarios_humanos",
    "etiquetas",
    "ultimo_comentario",
    "autor_ultimo_comentario",
    "coincidencias",
)

# Categorías de problema. El orden importa: las señales más específicas se
# evalúan antes que el genérico "Workflow Failure".
CATEGORY_SIGNALS = (
    ("seguimiento_no_op", ("gh-aw-noop-runs", "no-op runs")),
    ("seguimiento_deteccion", ("gh-aw-detection-runs", "detection runs")),
    ("archivos_protegidos", ("protected files",)),
    (
        "presupuesto_ia",
        (
            "daily workflow aic guardrail exceeded",
            "exceeded daily ai credits budget",
            "max-daily-ai-credits",
        ),
    ),
    ("rate_limit", ("engine rate limited", "hit engine rate limit", "http 429")),
    (
        "fallo_modelo",
        (
            "has no ai credits pricing for model",
            "model has no ai credits pricing",
            "not in the built-in pricing table",
        ),
    ),
    (
        "fallo_secretos",
        (
            "secret verification failed",
            "credential authentication failed",
            "authentication rejections",
            "missing, expired, or invalid",
        ),
    ),
    ("lock_file_desactualizado", ("lock file out of sync", "stale lock file")),
    ("datos_faltantes", ("missing data reported", "is missing required data")),
    (
        "herramientas_faltantes",
        (
            "missing tools reported",
            "is missing required tool",
            "**missing tools:**",
        ),
    ),
    (
        "resultado_incompleto",
        ("task could not be completed", "reported incomplete result"),
    ),
    ("sin_safe_outputs", ("no safe outputs generated", "produced no safe outputs")),
    (
        "safe_output_fallido",
        ("code push failed", "safe output failed", "push_to_pull_request_branch"),
    ),
    ("fallo_skill", ("skill installation failed",)),
    (
        "fallo_repo_memory",
        ("repo-memory push failed", "push-repo-memory job failed"),
    ),
    ("jobs_fallidos", ("failed jobs in workflow run", "failed jobs:")),
    (
        "timeout_agente",
        (
            "agent timed out",
            "exceeded the maximum allowed execution time",
        ),
    ),
    ("fallo_engine", ("engine failure", "engine terminated unexpectedly")),
    ("fallo_workflow", ("workflow failure",)),
)

CATEGORY_NATURE = {
    "fallo_workflow": "fallo_tecnico",
    "jobs_fallidos": "fallo_tecnico",
    "sin_safe_outputs": "fallo_tecnico",
    "safe_output_fallido": "fallo_tecnico",
    "datos_faltantes": "fallo_tecnico",
    "herramientas_faltantes": "fallo_tecnico",
    "lock_file_desactualizado": "fallo_tecnico",
    "resultado_incompleto": "fallo_tecnico",
    "fallo_engine": "fallo_tecnico",
    "fallo_repo_memory": "fallo_tecnico",
    "fallo_skill": "fallo_tecnico",
    "timeout_agente": "limite_operativo",
    "presupuesto_ia": "limite_operativo",
    "rate_limit": "limite_operativo",
    "fallo_modelo": "configuracion",
    "fallo_secretos": "configuracion",
    "archivos_protegidos": "bloqueo_seguridad",
    "seguimiento_no_op": "seguimiento",
    "seguimiento_deteccion": "seguimiento",
    "prueba_manual": "prueba",
    "solicitud_mejora": "mejora",
    "otro": "otro",
}

CATEGORY_CAUSE = {
    "fallo_workflow": "El workflow agéntico terminó con error durante la ejecución.",
    "jobs_fallidos": "Uno o más jobs del workflow agéntico fallaron.",
    "sin_safe_outputs": (
        "El job del agente terminó bien pero no generó safe outputs; puede ser "
        "un fallo del servidor de safe outputs, un prompt sin resultado útil o "
        "la falta de una llamada explícita a `noop`."
    ),
    "safe_output_fallido": (
        "Falló la publicación de un safe output (por ejemplo, el push a la rama "
        "del pull request)."
    ),
    "datos_faltantes": (
        "El agente no pudo obtener datos que necesitaba para completar la tarea."
    ),
    "herramientas_faltantes": (
        "El agente no tuvo acceso a una o más herramientas necesarias para "
        "completar la tarea."
    ),
    "lock_file_desactualizado": (
        "El archivo `.md` del workflow se editó sin volver a ejecutar "
        "`gh aw compile`, por lo que el `.lock.yml` quedó desactualizado."
    ),
    "resultado_incompleto": (
        "El agente no pudo completar la tarea por un fallo de infraestructura, "
        "de herramientas o de cuota de safe outputs."
    ),
    "fallo_engine": (
        "El engine del agente (por ejemplo, Copilot) terminó de forma "
        "inesperada durante la ejecución."
    ),
    "fallo_repo_memory": (
        "Falló el job que escribe la memoria del agente en el repositorio, "
        "posiblemente por permisos, configuración o red."
    ),
    "fallo_skill": (
        "No se pudieron instalar una o más skills declaradas en el frontmatter "
        "del workflow antes de ejecutar al agente."
    ),
    "timeout_agente": (
        "El agente superó el tiempo máximo de ejecución configurado."
    ),
    "presupuesto_ia": (
        "El workflow superó su presupuesto diario de AI Credits y el agente no "
        "se ejecutó hasta que se reinicie la ventana de 24 horas."
    ),
    "rate_limit": (
        "El engine del agente recibió un rate limit del proveedor (HTTP 429); "
        "conviene reintentar o reducir la concurrencia."
    ),
    "fallo_modelo": (
        "El modelo configurado no tiene precios de AI Credits en la tabla "
        "incorporada; el proxy rechazó las peticiones de inferencia."
    ),
    "fallo_secretos": (
        "Falló la verificación de secretos o credenciales del proveedor de IA "
        "(tokens faltantes, expirados o mal configurados)."
    ),
    "archivos_protegidos": (
        "El push a la rama del pull request fue bloqueado porque el parche "
        "modificaba archivos protegidos."
    ),
    "seguimiento_no_op": (
        "Hilo de seguimiento de ejecuciones no-op: no es un fallo, son corridas "
        "exitosas que decidieron no hacer cambios."
    ),
    "seguimiento_deteccion": (
        "Hilo de seguimiento de advertencias o fallos del sistema de detección "
        "de amenazas de gh-aw."
    ),
    "prueba_manual": (
        "Issue de prueba creado por un desarrollador para validar la creación o "
        "ejecución de un workflow agéntico."
    ),
    "solicitud_mejora": (
        "Solicitud de mejora o funcionalidad creada por un desarrollador."
    ),
    "otro": "No se reconoció un patrón automático conocido; revisar el issue.",
}


# --------------------------------------------------------------------------- #
# Utilidades de fechas y texto
# --------------------------------------------------------------------------- #


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def days_between(start: datetime | None, end: datetime | None) -> str:
    if start is None:
        return ""
    end = end or datetime.now(timezone.utc)
    return f"{(end - start).total_seconds() / 86400:.1f}"


def clean_text(text: str | None, limit: int = 400) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = text.replace("**", "")
    return text[:limit].rstrip()


def first_match(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    return clean_text(match.group(1)) if match else ""


def warning_text(body: str) -> str:
    """Extrae el bloque `> [!WARNING]` de los cuerpos generados por gh-aw."""
    match = re.search(r">\s*\[!WARNING\]\s*\n((?:>.*(?:\n|$))+)", body)
    if not match:
        return ""
    return clean_text(re.sub(r"^>\s?", "", match.group(1), flags=re.MULTILINE))


def bullet_after(body: str, heading: str, limit: int = 3) -> list[str]:
    """Devuelve los primeros bullets que siguen a un encabezado en negrita."""
    index = body.find(heading)
    if index < 0:
        return []
    section = body[index + len(heading) :]
    bullets = []
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            bullets.append(clean_text(stripped[2:]))
        elif bullets and stripped and not stripped.startswith(("(", "[")):
            # Un párrafo nuevo después de los bullets cierra la sección.
            if not line.startswith((" ", "\t")):
                break
        if len(bullets) >= limit:
            break
    return bullets


def compact_bullets(bullets: list[str], limit: int = 250) -> str:
    if not bullets:
        return ""
    text = "; ".join(bullet.rstrip(".") for bullet in bullets)
    return clean_text(text, limit)


# --------------------------------------------------------------------------- #
# Lectura del CSV de entrada
# --------------------------------------------------------------------------- #


def parse_issue_target(row: dict[str, str]) -> tuple[str, str, str] | None:
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


def split_labels(text: str | None) -> list[str]:
    return [item.strip() for item in re.split(r"[;|]", text or "") if item.strip()]


def read_rows(path: Path, limit: int | None) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if limit is not None:
        rows = rows[:limit]
    return rows


# --------------------------------------------------------------------------- #
# Consulta y caché de la API
# --------------------------------------------------------------------------- #


def slim_issue(issue: dict[str, Any]) -> dict[str, Any]:
    """Reduce el objeto de la API a los campos que usa el análisis."""
    user = issue.get("user") or {}
    closed_by = issue.get("closed_by") or {}
    labels = []
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else str(label)
        if name:
            labels.append(str(name))

    return {
        "number": issue.get("number"),
        "title": issue.get("title"),
        "html_url": issue.get("html_url"),
        "body": issue.get("body") or "",
        "state": issue.get("state"),
        "state_reason": issue.get("state_reason"),
        "created_at": issue.get("created_at"),
        "closed_at": issue.get("closed_at"),
        "comments": issue.get("comments") or 0,
        "author": user.get("login"),
        "author_type": user.get("type"),
        "closed_by": closed_by.get("login"),
        "closed_by_type": closed_by.get("type"),
        "labels": labels,
        "pull_request": bool(issue.get("pull_request")),
    }


def slim_comment(comment: dict[str, Any]) -> dict[str, Any]:
    user = comment.get("user") or {}
    return {
        "author": user.get("login"),
        "author_type": user.get("type"),
        "body": (comment.get("body") or "")[:2000],
        "created_at": comment.get("created_at"),
    }


def cache_path(cache_dir: Path, owner: str, repo: str, number: str) -> Path:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{owner}__{repo}__{number}")
    return cache_dir / f"{stem}.json"


def fetch_issue(
    api: GitHubAPI,
    owner: str,
    repo: str,
    number: str,
    comments_mode: str,
    cache_dir: Path,
    refresh: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    """Devuelve (issue, comentarios, vino_de_cache)."""
    path = cache_path(cache_dir, owner, repo, number)
    if not refresh and path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("version") == CACHE_VERSION:
                return payload["issue"], payload.get("comments", []), True
        except (OSError, json.JSONDecodeError):
            pass

    issue, _ = api.get_json(f"/repos/{owner}/{repo}/issues/{number}")
    if not isinstance(issue, dict):
        raise RuntimeError(f"No se pudo leer {owner}/{repo}#{number}")

    wants_comments = comments_mode == "all" or (
        comments_mode == "closed" and issue.get("state") == "closed"
    )
    comments: list[dict[str, Any]] = []
    if wants_comments:
        batch, _ = api.get_json(
            f"/repos/{owner}/{repo}/issues/{number}/comments", {"per_page": 100}
        )
        if isinstance(batch, list):
            comments = [slim_comment(comment) for comment in batch[-20:]]

    payload = {
        "version": CACHE_VERSION,
        "issue": slim_issue(issue),
        "comments": comments,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload["issue"], comments, False


def issue_from_row(row: dict[str, str], number: str) -> dict[str, Any]:
    """Construye un issue mínimo cuando la API falla, usando el CSV."""
    return {
        "number": int(number) if number.isdigit() else number,
        "title": row.get("titulo") or "",
        "html_url": row.get("link_issue") or "",
        "body": row.get("descripcion") or "",
        "state": row.get("salida") or "",
        "state_reason": None,
        "created_at": None,
        "closed_at": None,
        "comments": 0,
        "author": None,
        "author_type": None,
        "closed_by": None,
        "closed_by_type": None,
        "labels": split_labels(row.get("etiquetas")),
        "pull_request": False,
    }


# --------------------------------------------------------------------------- #
# Clasificación
# --------------------------------------------------------------------------- #


def classify_origin(issue: dict[str, Any]) -> str:
    login = (issue.get("author") or "").lower()
    author_type = (issue.get("author_type") or "").lower()
    title = (issue.get("title") or "").strip()
    aw_title = title.startswith(("[aw]", "[gh-aw]"))

    if author_type == "bot" or login.endswith("[bot]"):
        if aw_title or login == "github-actions[bot]":
            return "automatico_gh_aw"
        return "bot_otro"
    if author_type == "user":
        return "humano"
    if aw_title:
        return "automatico_gh_aw"
    return "desconocido"


def detect_categories(issue: dict[str, Any]) -> list[str]:
    """Devuelve las categorías detectadas, de la más específica a la más general."""
    title = (issue.get("title") or "").lower()
    body = (issue.get("body") or "").lower()
    haystack = f"{title}\n{body}"

    categories: list[str] = []
    for category, signals in CATEGORY_SIGNALS:
        if any(signal in haystack for signal in signals):
            categories.append(category)

    if not categories and (
        "testing gh aw" in title or re.search(r"\btest(ing|eando)?\b", title)
    ):
        if "gh aw" in title or "gh-aw" in title:
            categories.append("prueba_manual")

    labels = {label.lower() for label in issue.get("labels") or []}
    if not categories and labels & {
        "enhancement",
        "feature",
        "feature request",
        "improvement",
    }:
        categories.append("solicitud_mejora")

    if not categories and ("failed" in title or "failure" in title):
        categories.append("fallo_workflow")

    if not categories:
        categories.append("otro")
    return categories


def classify_category(issue: dict[str, Any]) -> str:
    return detect_categories(issue)[0]


def classify_resolution(issue: dict[str, Any]) -> str:
    if (issue.get("state") or "").lower() != "closed":
        return "abierto"

    reason = (issue.get("state_reason") or "").lower()
    if reason == "duplicate":
        return "duplicado"
    if reason == "not_planned":
        return "descartado"

    closer = (issue.get("closed_by") or "").strip()
    closer_type = (issue.get("closed_by_type") or "").lower()
    if not closer:
        return "cerrado_sin_cerrador"
    if closer_type == "bot" or closer.lower().endswith("[bot]"):
        return "cerrado_automaticamente"
    if closer.lower() == (issue.get("author") or "").lower():
        return "cerrado_por_autor"
    return "cerrado_por_desarrollador"


def extraer_flujo(body: str) -> str:
    return first_match(r"\*\*Workflow:\*\*\s*\[([^\]]+)\]", body)


def build_cause(
    category: str, issue: dict[str, Any]
) -> tuple[str, str]:
    """Devuelve (causa probable, evidencia textual) para la categoría."""
    body = issue.get("body") or ""
    title = issue.get("title") or ""
    cause = CATEGORY_CAUSE.get(category, CATEGORY_CAUSE["otro"])
    evidence = ""

    if category == "presupuesto_ia":
        usage = first_match(r"24h AIC usage:\*\*\s*`([^`]+)`", body)
        threshold = first_match(r"Configured threshold:\*\*\s*`([^`]+)`", body)
        if usage or threshold:
            cause = (
                f"Se superó el presupuesto diario de AI Credits "
                f"({usage or '?'} usados de {threshold or '?'} configurados); "
                "el agente se detuvo hasta que se reinicie la ventana de 24 h."
            )
        evidence = warning_text(body)

    elif category == "rate_limit":
        engine = first_match(r"The\s+`([^`]+)`\s+engine hit provider rate limits", body)
        if engine:
            cause = (
                f"El engine `{engine}` recibió un rate limit del proveedor "
                "(HTTP 429); hay que reintentar o reducir la concurrencia."
            )
        evidence = warning_text(body)

    elif category == "datos_faltantes":
        bullets = bullet_after(body, "**Missing Data:**")
        detail = compact_bullets(bullets)
        if detail:
            cause = f"El agente no pudo obtener datos requeridos: {detail}"
        evidence = warning_text(body) or detail

    elif category == "resultado_incompleto":
        bullets = bullet_after(body, "**Reasons:**")
        detail = compact_bullets(bullets)
        if detail:
            cause = f"El agente no pudo completar la tarea: {detail}"
        evidence = warning_text(body) or detail

    elif category == "safe_output_fallido":
        bullets = bullet_after(body, "**Code Push Errors:**")
        detail = compact_bullets(bullets)
        if detail:
            cause = f"Falló la publicación de un safe output: {detail}"
        evidence = warning_text(body) or detail

    elif category == "jobs_fallidos":
        jobs = re.findall(r"^- \[`?([^`\]]+)`?\]\(", body, re.MULTILINE)
        jobs = [clean_text(job, 80) for job in jobs[:6]]
        if jobs:
            cause = (
                f"Fallaron jobs del workflow ({', '.join(jobs)}); el run "
                "terminó en error."
            )
        evidence = clean_text("; ".join(jobs), 300)

    elif category == "herramientas_faltantes":
        bullets = bullet_after(body, "**Missing Tools:**")
        detail = compact_bullets(bullets)
        if detail:
            cause = f"El agente no tuvo acceso a herramientas necesarias: {detail}"
        evidence = warning_text(body) or detail

    elif category == "fallo_skill":
        bullets = bullet_after(body, "**Failed skills:**")
        detail = compact_bullets(bullets)
        if detail:
            cause = f"No se pudieron instalar skills del workflow: {detail}"
        evidence = clean_text(detail or body, 300)

    elif category == "fallo_repo_memory":
        evidence = warning_text(body) or clean_text(body, 300)

    elif category == "fallo_engine":
        evidence = warning_text(body) or clean_text(body, 300)

    elif category == "timeout_agente":
        detail = first_match(r"\*\*Agent Timed Out\*\*:\s*([^\n]+)", body)
        if detail:
            cause = f"El agente superó el tiempo máximo de ejecución: {detail}"
        evidence = detail or clean_text(body, 300)

    elif category == "fallo_modelo":
        model = first_match(r"model\s+`([^`]+)`\s+is not in the built-in", body)
        if model:
            cause = (
                f"El modelo `{model}` no tiene precios de AI Credits en la tabla "
                "incorporada; el proxy rechazó las peticiones de inferencia."
            )
        evidence = warning_text(body)

    elif category == "fallo_secretos":
        evidence = warning_text(body)

    elif category == "sin_safe_outputs":
        evidence = warning_text(body)

    elif category == "lock_file_desactualizado":
        evidence = warning_text(body)

    elif category == "archivos_protegidos":
        target = first_match(r"\*\*Target Pull Request:\*\*\s*\[#(\d+)\]", body)
        protected = first_match(r"Protected Files\*\*:\s*([^\n]+)", body)
        blocked = bullet_after(body, "**Blocked Operations:**")
        detail = compact_bullets(blocked)
        if target:
            cause = (
                "El push a la rama del pull request fue bloqueado porque el "
                f"parche modificaba archivos protegidos (PR #{target})."
            )
        elif protected:
            cause = (
                "El push a la rama del pull request fue bloqueado porque el "
                f"parche modificaba archivos protegidos: {protected}"
            )
        if detail:
            cause = f"{cause} Operaciones bloqueadas: {detail}"
        evidence = warning_text(body) or clean_text(
            f"{protected} {detail}".strip(), 400
        )

    elif category in {"seguimiento_no_op", "seguimiento_deteccion"}:
        evidence = clean_text(body, 300)

    elif category == "prueba_manual":
        evidence = clean_text(body, 300) or title

    elif category == "solicitud_mejora":
        evidence = clean_text(body, 300) or title

    elif category == "fallo_workflow":
        evidence = warning_text(body)
        if not evidence:
            evidence = first_match(
                r"(?:Code Push Errors|Reason|Error)[:\s]+(.{0,300})", body
            )

    if not evidence:
        evidence = clean_text(body, 300) or title

    return cause, evidence


def classify_issue(
    issue: dict[str, Any], row: dict[str, str]
) -> dict[str, Any]:
    body = issue.get("body") or ""
    categories = detect_categories(issue)
    category = categories[0]
    # `fallo_workflow` es el encabezado genérico de casi todos los fallos:
    # no aporta como categoría secundaria cuando ya hay una señal específica.
    secondary = ";".join(
        item for item in categories[1:] if item != "fallo_workflow"
    )
    cause, evidence = build_cause(category, issue)
    origin = classify_origin(issue)
    resolution = classify_resolution(issue)

    created = parse_datetime(issue.get("created_at"))
    closed = parse_datetime(issue.get("closed_at"))
    expiration_raw = first_match(r"gh-aw-expires:\s*([0-9T:.Z-]+)", body, )
    expiration = parse_datetime(expiration_raw) if expiration_raw else None
    now = datetime.now(timezone.utc)
    expired = bool(
        expiration
        and expiration < now
        and (issue.get("state") or "").lower() != "closed"
    )

    comments = issue.get("_comments") or []
    human_comments = [
        comment
        for comment in comments
        if (comment.get("author_type") or "").lower() == "user"
    ]
    closing_comment = human_comments[-1] if human_comments else (comments[-1] if comments else {})

    owner = row.get("dueño_repositorio") or ""
    name = row.get("nombre_repositorio") or ""

    return {
        "dueño_repositorio": owner,
        "nombre_repositorio": name,
        "numero_issue": issue.get("number") or "",
        "titulo": issue.get("title") or row.get("titulo") or "",
        "link_issue": row.get("link_issue") or issue.get("html_url") or "",
        "estado": issue.get("state") or row.get("salida") or "",
        "motivo_estado": issue.get("state_reason") or "",
        "autor": issue.get("author") or "",
        "tipo_autor": issue.get("author_type") or "",
        "origen": origin,
        "flujo_trabajo": extraer_flujo(body),
        "naturaleza": CATEGORY_NATURE.get(category, "otro"),
        "categoria": category,
        "categorias_secundarias": secondary,
        "causa_probable": cause,
        "evidencia": evidence,
        "resolucion": resolution,
        "cerrado_por": issue.get("closed_by") or "",
        "tipo_cerrador": issue.get("closed_by_type") or "",
        "creado_en": issue.get("created_at") or "",
        "cerrado_en": issue.get("closed_at") or "",
        "dias_abierto": days_between(created, closed),
        "expiracion": expiration.isoformat() if expiration else "",
        "expirado": "si" if expired else "no",
        "comentarios": str(issue.get("comments") or ""),
        "comentarios_humanos": str(len(human_comments)),
        "etiquetas": ";".join(issue.get("labels") or []),
        "ultimo_comentario": clean_text(closing_comment.get("body"), 300),
        "autor_ultimo_comentario": closing_comment.get("author") or "",
        "coincidencias": row.get("coincidencias") or "",
    }


# --------------------------------------------------------------------------- #
# Agregados y reportes
# --------------------------------------------------------------------------- #


def median(values: list[float]) -> float:
    return round(statistics.median(values), 1) if values else 0.0


def group_counts(
    rows: list[dict[str, Any]], field: str
) -> Counter[str]:
    return Counter(row.get(field) or "(sin dato)" for row in rows)


def pct(part: int, total: int) -> str:
    return f"{(100 * part / total):.1f}%" if total else "0.0%"


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_category_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["categoria"], []).append(row)

    result = []
    for category, items in grouped.items():
        result.append(
            {
                "categoria": category,
                "naturaleza": items[0]["naturaleza"],
                "total": len(items),
                "porcentaje": pct(len(items), len(rows)),
                "automaticos_gh_aw": sum(
                    1 for item in items if item["origen"] == "automatico_gh_aw"
                ),
                "humanos": sum(1 for item in items if item["origen"] == "humano"),
                "abiertos": sum(1 for item in items if item["estado"] == "open"),
                "cerrados": sum(1 for item in items if item["estado"] == "closed"),
                "causa_probable": CATEGORY_CAUSE.get(category, ""),
            }
        )
    result.sort(key=lambda item: (-item["total"], item["categoria"]))
    return result


def build_repo_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        repo = f"{row['dueño_repositorio']}/{row['nombre_repositorio']}"
        grouped.setdefault(repo, []).append(row)

    result = []
    for repo, items in grouped.items():
        categories = Counter(item["categoria"] for item in items)
        top = categories.most_common(3)
        result.append(
            {
                "repositorio": repo,
                "total": len(items),
                "automaticos_gh_aw": sum(
                    1 for item in items if item["origen"] == "automatico_gh_aw"
                ),
                "humanos": sum(1 for item in items if item["origen"] == "humano"),
                "abiertos": sum(1 for item in items if item["estado"] == "open"),
                "cerrados": sum(1 for item in items if item["estado"] == "closed"),
                "categorias_principales": "; ".join(
                    f"{category} ({count})" for category, count in top
                ),
            }
        )
    result.sort(key=lambda item: (-item["total"], item["repositorio"]))
    return result


def build_workflow_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        workflow = row["flujo_trabajo"] or "(sin flujo identificado)"
        grouped.setdefault(workflow, []).append(row)

    result = []
    for workflow, items in grouped.items():
        categories = Counter(item["categoria"] for item in items)
        result.append(
            {
                "flujo_trabajo": workflow,
                "total": len(items),
                "abiertos": sum(1 for item in items if item["estado"] == "open"),
                "cerrados": sum(1 for item in items if item["estado"] == "closed"),
                "categorias": "; ".join(
                    f"{category} ({count})"
                    for category, count in categories.most_common()
                ),
            }
        )
    result.sort(key=lambda item: (-item["total"], item["flujo_trabajo"]))
    return result


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(lines)


RESOLUTION_ORDER = (
    "abierto",
    "cerrado_por_desarrollador",
    "cerrado_por_autor",
    "cerrado_automaticamente",
    "descartado",
    "duplicado",
    "cerrado_sin_cerrador",
)
ORIGIN_ORDER = ("automatico_gh_aw", "humano", "bot_otro", "desconocido")


def build_summary(
    rows: list[dict[str, Any]],
    category_rows: list[dict[str, Any]],
    repo_rows: list[dict[str, Any]],
    workflow_rows: list[dict[str, Any]],
    input_path: Path,
    errors: list[tuple[str, str]],
) -> str:
    total = len(rows)
    origins = group_counts(rows, "origen")
    resolutions = group_counts(rows, "resolucion")
    categories = Counter(row["categoria"] for row in rows)
    closed_days = [
        float(row["dias_abierto"])
        for row in rows
        if row["estado"] == "closed" and row["dias_abierto"]
    ]
    expired_open = sum(1 for row in rows if row["expirado"] == "si")
    tracking = sum(
        1 for row in rows if row["naturaleza"] == "seguimiento"
    )
    auto = origins.get("automatico_gh_aw", 0)
    human = origins.get("humano", 0)

    lines: list[str] = []
    lines.append("# Análisis de issues en proyectos con GitHub Agentic Workflows")
    lines.append("")
    lines.append(f"Generado: {utc_now()}")
    lines.append("")
    lines.append(f"- Entrada: `{display_path(input_path)}`")
    lines.append(f"- Issues analizados: **{total}**")
    lines.append(f"- Repositorios: **{len(repo_rows)}**")
    lines.append("")

    lines.append("## Panorama general")
    lines.append("")
    lines.append(
        markdown_table(
            ["Métrica", "Valor"],
            [
                ["Issues automáticos de gh-aw", f"{auto} ({pct(auto, total)})"],
                ["Issues creados por personas", f"{human} ({pct(human, total)})"],
                ["Otros bots", origins.get("bot_otro", 0)],
                ["Abiertos", resolutions.get("abierto", 0)],
                ["Cerrados", total - resolutions.get("abierto", 0)],
                ["Cerrados por un desarrollador", resolutions.get("cerrado_por_desarrollador", 0)],
                ["Cerrados por el autor", resolutions.get("cerrado_por_autor", 0)],
                ["Cerrados automáticamente", resolutions.get("cerrado_automaticamente", 0)],
                ["Descartados (`not_planned`)", resolutions.get("descartado", 0)],
                ["Duplicados", resolutions.get("duplicado", 0)],
                ["Mediana de días hasta el cierre", median(closed_days)],
                ["Issues de seguimiento (No-Op/Detección)", tracking],
                ["Abiertos con expiración vencida", expired_open],
            ],
        )
    )
    lines.append("")

    lines.append("## Origen × resolución")
    lines.append("")
    matrix_rows = []
    for origin in ORIGIN_ORDER:
        if origin not in origins:
            continue
        counts = Counter(
            row["resolucion"] for row in rows if row["origen"] == origin
        )
        matrix_rows.append(
            [origin, counts.get("abierto", 0)]
            + [counts.get(state, 0) for state in RESOLUTION_ORDER if state != "abierto"]
        )
    lines.append(
        markdown_table(
            ["Origen", "abierto"]
            + [state for state in RESOLUTION_ORDER if state != "abierto"],
            matrix_rows,
        )
    )
    lines.append("")

    lines.append("## Categorías de problema")
    lines.append("")
    category_table = [
        [
            item["categoria"],
            item["naturaleza"],
            item["total"],
            item["porcentaje"],
            item["automaticos_gh_aw"],
            item["humanos"],
            item["abiertos"],
            item["cerrados"],
        ]
        for item in category_rows
    ]
    lines.append(
        markdown_table(
            [
                "Categoría",
                "Naturaleza",
                "Total",
                "%",
                "Auto",
                "Humano",
                "Abiertos",
                "Cerrados",
            ],
            category_table,
        )
    )
    lines.append("")

    lines.append("## Repositorios")
    lines.append("")
    lines.append(
        markdown_table(
            ["Repositorio", "Total", "Auto", "Humano", "Abiertos", "Cerrados", "Categorías principales"],
            [
                [
                    item["repositorio"],
                    item["total"],
                    item["automaticos_gh_aw"],
                    item["humanos"],
                    item["abiertos"],
                    item["cerrados"],
                    item["categorias_principales"],
                ]
                for item in repo_rows
            ],
        )
    )
    lines.append("")

    lines.append("## Workflows más afectados")
    lines.append("")
    lines.append(
        markdown_table(
            ["Workflow", "Issues", "Abiertos", "Cerrados", "Categorías"],
            [
                [
                    item["flujo_trabajo"],
                    item["total"],
                    item["abiertos"],
                    item["cerrados"],
                    item["categorias"],
                ]
                for item in workflow_rows[:15]
            ],
        )
    )
    lines.append("")

    lines.append("## Causas y ejemplos")
    lines.append("")
    for item in category_rows:
        category = item["categoria"]
        examples = [row for row in rows if row["categoria"] == category][:3]
        label = "issue" if item["total"] == 1 else "issues"
        lines.append(f"### `{category}` ({item['total']} {label})")
        lines.append("")
        lines.append(item["causa_probable"])
        lines.append("")
        for example in examples:
            extra = (
                f" — también: {example['categorias_secundarias']}"
                if example.get("categorias_secundarias")
                else ""
            )
            lines.append(
                f"- [{example['titulo']}]({example['link_issue']}) — "
                f"{example['resolucion']}{extra}"
            )
        lines.append("")

    lines.append("## Cómo se resolvieron")
    lines.append("")
    resolution_fields = (
        "cerrado_por_desarrollador",
        "cerrado_por_autor",
        "cerrado_automaticamente",
        "descartado",
        "duplicado",
        "cerrado_sin_cerrador",
    )
    resolution_table = []
    for item in category_rows:
        items = [
            row for row in rows if row["categoria"] == item["categoria"]
        ]
        closed_items = [row for row in items if row["estado"] == "closed"]
        if not closed_items:
            continue
        counts = Counter(row["resolucion"] for row in closed_items)
        durations = [
            float(row["dias_abierto"])
            for row in closed_items
            if row["dias_abierto"]
        ]
        resolution_table.append(
            [item["categoria"], len(closed_items)]
            + [counts.get(field, 0) for field in resolution_fields]
            + [median(durations)]
        )
    resolution_table.sort(key=lambda row: (-row[1], row[0]))
    lines.append(
        markdown_table(
            ["Categoría", "Cerrados"]
            + list(resolution_fields)
            + ["Mediana días"],
            resolution_table,
        )
    )
    lines.append("")

    lines.append("## Comentarios de cierre")
    lines.append("")
    closing_rows = [
        row
        for row in rows
        if row["estado"] == "closed"
        and row["ultimo_comentario"]
        and row["autor_ultimo_comentario"]
        and not row["autor_ultimo_comentario"].lower().endswith("[bot]")
    ]
    closing_rows.sort(key=lambda row: row["cerrado_en"] or "", reverse=True)
    if closing_rows:
        lines.append(
            f"Se encontraron {len(closing_rows)} issues cerrados con un "
            "comentario humano. Son evidencia de cómo se resolvió o descartó "
            "cada caso:"
        )
        lines.append("")
        for row in closing_rows[:10]:
            lines.append(
                f"- **{row['autor_ultimo_comentario']}** en "
                f"[{row['titulo']}]({row['link_issue']}) "
                f"(`{row['categoria']}`, {row['resolucion']}): "
                f"{row['ultimo_comentario']}"
            )
    else:
        lines.append(
            "No se recuperaron comentarios humanos de cierre; ejecutá el "
            "análisis con `--comments closed` o `--comments all`."
        )
    lines.append("")

    lines.append("## Observaciones")
    lines.append("")
    if total:
        top_category, top_count = categories.most_common(1)[0]
        lines.append(
            f"- La categoría más frecuente es `{top_category}` "
            f"con {top_count} issues ({pct(top_count, total)})."
        )
        lines.append(
            f"- {auto} issues ({pct(auto, total)}) fueron creados "
            "automáticamente por `github-actions[bot]`; "
            f"{human} ({pct(human, total)}) son humanos."
        )
        human_rows = [row for row in rows if row["origen"] == "humano"]
        if human_rows:
            human_categories = Counter(row["categoria"] for row in human_rows)
            detail = ", ".join(
                f"`{category}` ({count})"
                for category, count in human_categories.most_common()
            )
            lines.append(f"- Los issues humanos se clasificaron como: {detail}.")
        closed_total = total - resolutions.get("abierto", 0)
        human_closed = (
            resolutions.get("cerrado_por_desarrollador", 0)
            + resolutions.get("cerrado_por_autor", 0)
        )
        if closed_total:
            lines.append(
                f"- De {closed_total} issues cerrados, {human_closed} fueron "
                f"cerrados por personas ({pct(human_closed, closed_total)}) y "
                f"{resolutions.get('cerrado_automaticamente', 0)} por bots."
            )
            closers = Counter(
                row["cerrado_por"] for row in rows if row["cerrado_por"]
            )
            if closers:
                detail = ", ".join(
                    f"`{login}` ({count})"
                    for login, count in closers.most_common(3)
                )
                lines.append(f"- Principales cerradores: {detail}.")
            if len(closing_rows) < closed_total:
                lines.append(
                    f"- Solo {len(closing_rows)} de {closed_total} cierres "
                    "tienen un comentario humano; el resto se cerró sin una "
                    "explicación escrita en el hilo."
                )
        if tracking:
            lines.append(
                f"- {tracking} issues son hilos de seguimiento (No-Op o "
                "Detección) y no representan fallos del workflow."
            )
        if expired_open:
            lines.append(
                f"- {expired_open} issues abiertos conservan una fecha de "
                "expiración (`gh-aw-expires`) ya vencida."
            )
        if repo_rows:
            top_repo = max(repo_rows, key=lambda item: item["total"])
            lines.append(
                f"- El repositorio más representado es "
                f"`{top_repo['repositorio']}` con {top_repo['total']} issues "
                f"({pct(top_repo['total'], total)}); las conclusiones reflejan "
                "la mezcla del CSV de entrada y no necesariamente a todo el "
                "ecosistema de gh-aw."
            )
    if errors:
        lines.append(
            f"- {len(errors)} issues no se pudieron consultar en la API; se "
            "clasificaron con los datos disponibles en el CSV."
        )
    lines.append("")

    lines.append("## Cómo se clasificó")
    lines.append("")
    lines.append(
        "- **Origen:** `automatico_gh_aw` si el autor es un bot y el título "
        "empieza con `[aw]`/`[gh-aw]` (o el autor es `github-actions[bot]`); "
        "`humano` si el autor es una cuenta de usuario; `bot_otro` para otros "
        "bots."
    )
    lines.append(
        "- **Categoría:** se buscan primero las señales específicas del cuerpo "
        "generado por gh-aw (`Protected Files`, `No Safe Outputs Generated`, "
        "`Missing Data Reported`, `Lock File Out of Sync`, "
        "`Daily Workflow AIC Guardrail Exceeded`, `Engine Rate Limited`, etc.) "
        "y luego el mensaje genérico `Workflow Failure`. Un issue puede tener "
        "varias señales; `categoria` es la principal y "
        "`categorias_secundarias` conserva las demás."
    )
    lines.append(
        "- **Causa probable:** se extrae del bloque de advertencia, de los "
        "bullets de `Missing Data`/`Reasons`/`Code Push Errors` o de los jobs "
        "fallidos. La columna `evidencia` conserva el texto que la respalda."
    )
    lines.append(
        "- **Resolución:** `abierto`, `cerrado_por_desarrollador`, "
        "`cerrado_por_autor`, `cerrado_automaticamente`, `descartado` "
        "(`not_planned`), `duplicado` o `cerrado_sin_cerrador`."
    )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Comando principal
# --------------------------------------------------------------------------- #


def command_analyze(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    out_dir = Path(args.out_dir)
    cache_dir = out_dir / ".cache"
    comments_mode = args.comments

    if not input_path.exists():
        print(f"Error: no existe la entrada {display_path(input_path)}", file=sys.stderr)
        print(
            "Generala primero con scripts/repo_filter_issues.py.",
            file=sys.stderr,
        )
        return 2

    rows = read_rows(input_path, args.limit)
    if not rows:
        print(f"Error: {display_path(input_path)} no tiene filas.", file=sys.stderr)
        return 2

    try:
        load_and_report_token(args)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    api = build_api(args)
    if not api.authenticated:
        print(
            "Aviso: sin GITHUB_TOKEN el límite es 60 peticiones/hora; "
            "no alcanzará para analizar todos los issues.",
            file=sys.stderr,
        )

    tasks: list[tuple[dict[str, str], str, str, str]] = []
    skipped: list[dict[str, str]] = []
    for row in rows:
        target = parse_issue_target(row)
        if target is None:
            skipped.append(row)
            continue
        tasks.append((row, target[0], target[1], target[2]))

    verbose = not args.quiet
    results: list[dict[str, Any]] = []
    errors: list[tuple[str, str]] = []
    fetched = 0
    cached = 0
    completed = 0

    def work(
        item: tuple[dict[str, str], str, str, str]
    ) -> tuple[dict[str, str], dict[str, Any], list[dict[str, Any]], bool]:
        row, owner, repo, number = item
        issue, comments, from_cache = fetch_issue(
            api,
            owner,
            repo,
            number,
            comments_mode,
            cache_dir,
            args.refresh,
        )
        issue["_comments"] = comments
        return row, issue, comments, from_cache

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(work, item): item for item in tasks}
        for future in as_completed(futures):
            row, owner, repo, number = futures[future]
            link = row.get("link_issue") or f"{owner}/{repo}#{number}"
            try:
                result_row, issue, _comments, from_cache = future.result()
                results.append(classify_issue(issue, result_row))
                if from_cache:
                    cached += 1
                else:
                    fetched += 1
            except AuthenticationError:
                raise
            except Exception as exc:  # noqa: BLE001 - se informa y se continúa
                errors.append((link, str(exc)[:300]))
                fallback = issue_from_row(row, number)
                fallback["_comments"] = []
                results.append(classify_issue(fallback, row))

            completed += 1
            if verbose and (completed % 25 == 0 or completed == len(tasks)):
                print(f"  analizados {completed}/{len(tasks)} issues...")

    for row in skipped:
        errors.append(
            (
                row.get("link_issue") or "(sin link)",
                "no se pudo obtener dueño/repo/número",
            )
        )

    results.sort(
        key=lambda item: (
            item["dueño_repositorio"].lower(),
            item["nombre_repositorio"].lower(),
            int(item["numero_issue"]) if str(item["numero_issue"]).isdigit() else 0,
        )
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    enriched_path = out_dir / "issues_enriched.csv"
    write_csv(enriched_path, ENRICHED_FIELDS, results)

    category_rows = build_category_rows(results)
    repo_rows = build_repo_rows(results)
    workflow_rows = build_workflow_rows(results)

    write_csv(
        out_dir / "por_categoria.csv",
        (
            "categoria",
            "naturaleza",
            "total",
            "porcentaje",
            "automaticos_gh_aw",
            "humanos",
            "abiertos",
            "cerrados",
            "causa_probable",
        ),
        category_rows,
    )
    write_csv(
        out_dir / "por_repositorio.csv",
        (
            "repositorio",
            "total",
            "automaticos_gh_aw",
            "humanos",
            "abiertos",
            "cerrados",
            "categorias_principales",
        ),
        repo_rows,
    )
    write_csv(
        out_dir / "por_flujo.csv",
        ("flujo_trabajo", "total", "abiertos", "cerrados", "categorias"),
        workflow_rows,
    )

    summary = build_summary(
        results,
        category_rows,
        repo_rows,
        workflow_rows,
        input_path,
        errors,
    )
    (out_dir / "summary.md").write_text(summary + "\n", encoding="utf-8")

    if verbose:
        print()
        print(f"Issues analizados: {len(results)}")
        print(f"Consultados a la API: {fetched}")
        print(f"Leídos desde caché: {cached}")
        print(f"Errores/omitidos: {len(errors)}")
        print()
        print("Origen:")
        for origin, count in group_counts(results, "origen").most_common():
            print(f"  {origin}: {count}")
        print("Resolución:")
        for resolution, count in group_counts(results, "resolucion").most_common():
            print(f"  {resolution}: {count}")
        print("Categorías:")
        for category, count in group_counts(results, "categoria").most_common():
            print(f"  {category}: {count}")
        print()
        print(f"CSV enriquecido: {display_path(enriched_path)}")
        print(f"Resumen: {display_path(out_dir / 'summary.md')}")

    if errors:
        print()
        print("Avisos:", file=sys.stderr)
        for link, message in errors[:10]:
            print(f"  {link}: {message}", file=sys.stderr)
        if len(errors) > 10:
            print(f"  ... y {len(errors) - 10} más", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Analiza los issues filtrados de gh-aw: origen, categoría, causa "
            "probable y resolución. Genera CSV enriquecido y resúmenes."
        )
    )
    parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT),
        help=(
            "CSV de entrada con issues (por defecto: "
            "filtered_issues/agentic_issues.csv)"
        ),
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUT_DIR),
        help="Directorio de salida (por defecto: analysis/)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Analizar solo los primeros N issues (útil para probar)",
    )
    parser.add_argument(
        "--comments",
        choices=("none", "closed", "all"),
        default="closed",
        help=(
            "Qué comentarios consultar: none, closed (solo issues cerrados) "
            "o all (por defecto: closed)"
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Consultas paralelas a la API (por defecto: 8)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignorar la caché y volver a consultar todos los issues",
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
        return int(command_analyze(args))
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
