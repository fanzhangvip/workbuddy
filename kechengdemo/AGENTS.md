# Repository Guidelines

## Project Structure & Module Organization

This is a Chinese-language WeCom (企微) customer-intent scoring system: a FastAPI backend plus a Vue 3 UI.

- `backend/` — FastAPI service. `main.py` is the entry point; `api_*.py` are routers; `scoring_service.py`, `sync_service.py`, `eval_runner.py`, `scheduler.py` hold domain logic; `schema.sql` defines the PostgreSQL schema.
- Root modules — shared logic imported by both backend and scripts: `weiban.py`, `ww_chatdata.py`, `rules.py`, `contracts.py`, `confidence.py`, `config.py`, `analyze.py`.
- `frontend/` — Vue 3 + Vite app. Views live in `frontend/src/views/`, routing in `router.js`, HTTP calls in `api.js`.
- `tests/` — standalone script-style tests. `导出/` and `backups/` are runtime artifacts and are git-ignored.
- Requirement/spec documents are the `客户意向评分_*.md` files; treat `客户意向评分_架构设计说明书.md` as the source of truth.

## Build, Test, and Development Commands

- `docker compose up -d` — start PostgreSQL (host port 5433).
- `.venv\Scripts\python.exe -m pip install -r requirements.txt` — install Python dependencies.
- `start_backend.bat` — run the API at `http://127.0.0.1:8848`.
- `start_frontend.bat` — run the Vite dev server at `http://127.0.0.1:5173` (proxies `/api` to 8848).
- `build_frontend.bat` — build the UI into `frontend/dist`, served by the backend.

## Coding Style & Naming Conventions

Python 3.13, 4-space indentation, `snake_case` functions/variables, `PascalCase` classes. Keep the `# -*- coding: utf-8 -*-` header and Chinese module docstrings. Files start with a docstring stating purpose and the application entry (`python -m uvicorn backend.main:app`). Endpoints live in `api_<area>.py` and expose `router`. Add `# noqa: E402` for imports that must follow `sys.path` setup. No formatter/linter is configured; match surrounding code.

## Testing Guidelines

Tests are self-contained scripts (no pytest) that print PASS/FAIL and exit non-zero on failure. Name them `test_<topic>.py`, mirroring the architecture feature (e.g. `test_confidence.py`, `test_g6_outbox_survives_watermark.py`). Run them directly:

```bat
.venv\Scripts\python.exe tests\test_confidence.py
```

Most tests need PostgreSQL on 5433; they create and clean up their own rows.

## Commit & Pull Request Guidelines

Commit messages are short and Chinese, prefixed `feat:` (e.g. `feat: 初步完成`). PRs should describe the change, link the relevant issue or spec section, note DB/schema impact, and include screenshots for UI changes.

## Security & Configuration Tips

Configuration is read from `.env` (git-ignored) via `config.py`; system environment variables take precedence. Never commit `.env`, `.cache/`, or exported chat data, and never print secrets to logs.
