# Repository Guidelines

## Project Structure & Module Organization

`rentwatch/` contains the Python application. `__main__.py` defines the CLI; `scraper.py` fetches Immobiliare.it listings; `db.py` owns the SQLite schema and migrations; `web.py`, `auth.py`, and `static/` provide the FastAPI dashboard; and `notify.py`/`bot.py` handle Telegram. Deployment files live in `deploy/`, while `docs/` contains tracked documentation assets. Runtime output under `data/` and `reports/`, plus personal `config.local.toml`, is intentionally ignored.

## Setup, Run, and Verification Commands

Use Python 3.10 or newer:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m rentwatch scrape --max-pages 2
.venv/bin/python -m rentwatch serve
.venv/bin/python -m rentwatch report
```

The limited scrape is the standard smoke test and does not deactivate unseen listings. `serve` starts the dashboard at `http://127.0.0.1:8777`; create an account first with `.venv/bin/python -m rentwatch set-password`. Run `.venv/bin/python -m compileall rentwatch` for a quick syntax check.

## Coding Style & Naming Conventions

Follow the existing PEP 8-style Python: four-space indentation, `snake_case` functions and variables, `PascalCase` classes, and uppercase module constants. Add type hints where they clarify interfaces, and keep SQL parameterized. There is no configured formatter or linter, so match nearby code and keep imports grouped standard-library first. User-facing UI and documentation are in Italian; code identifiers and logs are in English. Avoid new dependencies unless they provide clear value.

## Testing Guidelines

There is currently no automated test suite or coverage threshold. For each change, run `compileall` and the two-page scrape, then exercise the affected CLI or web flow. Web/auth changes should verify `/login`, an unauthenticated `/api/overview` response (401), and an authenticated dashboard session. Include screenshots for visible changes under `rentwatch/static/`. Never remove scraper delays or perform deactivation during a limited scrape.

## Commit & Pull Request Guidelines

History favors short, imperative subjects, commonly `feat: ...`, `docs: ...`, or area prefixes such as `scraper:` and `Telegram:`. Keep commits focused. Pull requests should explain the behavior change, configuration or schema impact, and manual checks performed; link relevant issues and attach UI screenshots when applicable.

## Security & Configuration

Put tokens, password hashes, and local overrides only in `config.local.toml`; never commit `data/`, generated reports, or session secrets. Preserve the default-authenticated API model and keep `/healthz` and `/login` as the only intentional public routes.
