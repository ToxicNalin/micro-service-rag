# CLAUDE.md: DocMind
This project is built step by step from PROJECT_SPEC.md, the single source of truth.

## Rules
1. Before working, read the step in PROJECT_SPEC.md §8 plus the sections it lists.
2. Do only the requested step; never create files that belong to later steps.
3. Use the exact names from the spec (files, functions, endpoints, env vars, tables, keys).
4. Tenant isolation invariants (§1.5) are non-negotiable: the tenant ID comes only from the verified JWT.
5. No secrets in code; configuration only via env vars (§5). No connections at import time.
6. Write the tests listed in the step and run them; finish only when `python -m pytest -q` passes.
7. Shell scripts use LF line endings.
8. When done: tick the step in §10, add any decisions to §11, and list the "Done when" checks for the user.

## Commands
- Tests: `python -m pytest -q`        Lint: `ruff check .`
- Start everything: `docker compose up -d --build`
- Logs: `docker compose logs -f <service>`     Reset all data: `docker compose down -v`
