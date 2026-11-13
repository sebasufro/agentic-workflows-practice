# agentic-workflows-practice

Herramientas para estudiar el uso de GitHub Agentic Workflows (`gh-aw`):

- [`scripts/repo_search_agentic.py`](scripts/repo_search_agentic.py): descubre
  repositorios que usan `gh-aw` y genera `repos.json`.
- [`scripts/repo_scan_actions.py`](scripts/repo_scan_actions.py): empareja
  `.md` ↔ `.lock.yml` y genera reportes Markdown de ejecuciones.
  Ver [instrucciones](scripts/README.md).
- [`scripts/repo_fetch_issues.py`](scripts/repo_fetch_issues.py): descarga los
  issues de uno o varios repositorios (`repos.json`, `--repo`, filtros) en
  bruto, página por página, y genera un JSON por issue y un CSV dentro de
  `issues/{dueño}__{repo}/`. Opcionalmente escribe un CSV combinado.
- [`scripts/repo_filter_issues.py`](scripts/repo_filter_issues.py): filtra los
  JSON individuales de `issues/` (y opcionalmente los CSV históricos) y genera
  `filtered_issues/agentic_issues.csv` con los issues relacionados con `gh-aw`,
  según título, descripción, etiquetas y CI/CD, sin consultar la API.
  Ver [instrucciones](scripts/README.md).
- [`scripts/repo_analyze_issues.py`](scripts/repo_analyze_issues.py): clasifica
  los issues filtrados por origen (automático de `gh-aw` o humano), tipo de
  problema, causa probable y resolución. Genera `analysis/issues_enriched.csv`
  y `analysis/summary.md`. Ver [instrucciones](scripts/README.md).

```bash
# Opción A: crear .env en la raíz con GITHUB_TOKEN=ghp_...
# Opción B: export GITHUB_TOKEN=ghp_...
python scripts/repo_search_agentic.py discover --out repos.json
python scripts/repo_scan_actions.py repo PowerShell/vscode-powershell --runs 100

# Descargar todos los issues de repos.json, una carpeta por repositorio
python scripts/repo_fetch_issues.py --repos-file repos.json

# O solamente un repositorio / una cantidad limitada
python scripts/repo_fetch_issues.py --repo PowerShell/vscode-powershell
python scripts/repo_fetch_issues.py --repos-file repos.json --limit 10

# Regenerar JSON individuales y CSV desde las páginas crudas ya guardadas
python scripts/repo_fetch_issues.py --parse-only

# Filtrar los JSON de issues/ y generar filtered_issues/agentic_issues.csv
python scripts/repo_filter_issues.py

# Analizar los issues filtrados y generar reportes en analysis/
python scripts/repo_analyze_issues.py

# Elegir un subconjunto antes de gastar llamadas (quitar --dry-run para ejecutar)
python scripts/repo_scan_actions.py batch --repos-file repos.json --org PowerShell --limit 5 --dry-run
```
