# Kinaxis Blueprint Document platform

Auth-gated internal tool for generating BRD/SOW documents. Two independent
Python/FastAPI services must both be running:

| # | Service | Folder | Port |
|---|---------|--------|------|
| 1 | MCP Web Tools server (web search, doc retrieval) | `infrastructure/unified_mcp/` | 8000 |
| 2 | Backend + web UI (auth, projects, generation) | `orchestration/brd_convo_app/backend/` | 8010 |

Document generation calls Azure OpenAI via the signed-in user's own Azure AD
identity (`DefaultAzureCredential` / `az login`), not a shared API key.

## First time on this machine

If the user asks to set up, install, or provision this repo, use the
**setup-app** skill — it creates the venvs, installs dependencies, writes
real `.env` files, creates the user's own admin login, and signs in Azure CLI.

## Every time they want to run it

If the user asks to start, run, or launch the app, use the **start-app**
skill — it refreshes the Azure login if needed, then starts both services in
the background.

## Notes for whoever is editing this repo (not just running it)

- `orchestration/brd_convo_app/backend/app/claude_provider.py` is the LLM
  boundary — `LLM_PROVIDER=azure` (default, Azure AD/`az login`, no key) or
  `anthropic` (`CLAUDE_API_KEY`).
- `infrastructure/unified_mcp/kinaxis_http_server.py` is the MCP web-tools
  server; the backend reaches it via `infrastructure/unified_mcp/MCP_client.py`.
  It reads its own `.env` from `infrastructure/unified_mcp/.env` (not the
  backend's) — its auto-discovery doesn't find the backend's `.env` in this
  repo's nested folder layout, so both need their own copy of any shared key.
- `app/auth.py` bootstraps a single admin account on first run from
  `ADMIN_EMAIL` in the backend `.env` (random one-time password printed to
  stdout if `ADMIN_PASSWORD` isn't set). Everyone else signs up and is
  approved by that admin from the UI.
- Azure's conditional-access MFA claim expires well before the `az` CLI
  session looks expired — `az account show` can succeed while real Azure
  OpenAI calls still fail with `AADSTS50078`. If generation 500s with a
  `DefaultAzureCredential` traceback, that's why; re-run `az login` (the
  start-app skill checks and refreshes this automatically).
- azure-cli must NOT be installed into `backend/venv` — its deeply nested
  package paths blow past Windows' 260-char MAX_PATH inside this repo's
  already-long path, and its pinned deps conflict with `azure-identity`
  (a real backend dependency). Keep it in its own isolated venv
  (`C:\azcli-venv`), used only for the `az` command itself.
- Don't commit real `.env` files, `users.db`, `uploads/`, `output/`, or
  `venv/` — see `.gitignore` for the full exclude list. `.env.example` files
  are the templates and must stay free of real secrets.
