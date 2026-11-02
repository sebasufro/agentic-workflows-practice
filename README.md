# agentic-workflows-practice

Herramientas para estudiar el uso de GitHub Agentic Workflows (`gh-aw`):

- [`scripts/scan_gh_aw.py`](scripts/scan_gh_aw.py): descubre repositorios,
  empareja `.md` ↔ `.lock.yml` y genera reportes Markdown de ejecuciones.
  Ver [instrucciones](scripts/README.md).

```bash
# Opción A: crear .env en la raíz con GITHUB_TOKEN=ghp_...
# Opción B: export GITHUB_TOKEN=ghp_...
python scripts/scan_gh_aw.py discover --out repos.json
python scripts/scan_gh_aw.py repo PowerShell/vscode-powershell --runs 100

# Elegir un subconjunto antes de gastar llamadas (quitar --dry-run para ejecutar)
python scripts/scan_gh_aw.py batch --repos-file repos.json --org PowerShell --limit 5 --dry-run
```
