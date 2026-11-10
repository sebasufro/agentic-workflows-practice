# agentic-workflows-practice

Herramientas para estudiar el uso de GitHub Agentic Workflows (`gh-aw`):

- [`scripts/repo_search_agentic.py`](scripts/repo_search_agentic.py): descubre
  repositorios que usan `gh-aw` y genera `repos.json`.
- [`scripts/repo_scan_actions.py`](scripts/repo_scan_actions.py): empareja
  `.md` ↔ `.lock.yml` y genera reportes Markdown de ejecuciones.
  Ver [instrucciones](scripts/README.md).
- [`scripts/repo_fetch_issues.py`](scripts/repo_fetch_issues.py): exporta los
  issues de uno o varios repositorios (`repos.json`, `--repo`, filtros) a un
  CSV combinado o a un CSV por repositorio en `issues/`.
- [`scripts/repo_filter_issues.py`](scripts/repo_filter_issues.py): filtra los
  CSV de `issues/` y genera `filtered_issues/agentic_issues.csv` con los issues
  relacionados con `gh-aw`, según título, descripción, etiquetas y CI/CD.
  Ver [instrucciones](scripts/README.md).

```bash
# Opción A: crear .env en la raíz con GITHUB_TOKEN=ghp_...
# Opción B: export GITHUB_TOKEN=ghp_...
python scripts/repo_search_agentic.py discover --out repos.json
python scripts/repo_scan_actions.py repo PowerShell/vscode-powershell --runs 100

# Exportar todos los issues de repos.json a un único CSV
python scripts/repo_fetch_issues.py --repos-file repos.json

# O solamente un repositorio / una cantidad limitada:
# se genera un CSV por repositorio dentro de issues/
python scripts/repo_fetch_issues.py --repo PowerShell/vscode-powershell
python scripts/repo_fetch_issues.py --repos-file repos.json --limit 10

# Filtrar los CSV de issues/ y generar filtered_issues/agentic_issues.csv
python scripts/repo_filter_issues.py

# Elegir un subconjunto antes de gastar llamadas (quitar --dry-run para ejecutar)
python scripts/repo_scan_actions.py batch --repos-file repos.json --org PowerShell --limit 5 --dry-run
```
