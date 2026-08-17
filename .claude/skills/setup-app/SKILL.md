---
name: setup-app
description: Use when the user asks to "set up the application", "set up this project", "install/provision this repo", or first-time-configure the Kinaxis Blueprint Document platform (BRD/SOW generator). Creates Python venvs for both services, installs dependencies, writes real .env files from the .env.example templates, creates the user's own admin login with a one-time password, and logs the Azure CLI in for Azure OpenAI access. Run once per machine, before "start application".
---

# Setting up the Blueprint Document platform

This repo is two independent Python/FastAPI services:

| # | Service | Folder | Port |
|---|---------|--------|------|
| 1 | MCP Web Tools server (web search / doc retrieval) | `infrastructure/unified_mcp/` | 8000 |
| 2 | Backend + web UI (auth, projects, BRD/SOW generation) | `orchestration/brd_convo_app/backend/` | 8010 |

Generation calls Azure OpenAI using the signed-in user's own Azure AD identity
(`DefaultAzureCredential` / `az login`) — there is no shared API key for this.
The person running setup needs a Bristlecone account with access to the
"ai-adoption-coe" Azure AI resource.

Work through these steps yourself using your tools (Bash/PowerShell, Read,
Edit) — don't just print them for the user to run by hand. Narrate briefly as
you go so they can follow along and answer the prompts.

## 1. Locate the repo root

The root is the folder containing `agents/`, `infrastructure/`, `orchestration/`,
and `config/` as siblings. All paths below are relative to it.

## 2. Python venvs + dependencies

Windows note: bare `python`/`pip` may alias to the Microsoft Store stub and
silently no-op. Prefer the `py` launcher, and always call the venv's own
`python.exe` directly rather than relying on `activate`.

For **both** of these folders — `infrastructure/unified_mcp/` and
`orchestration/brd_convo_app/backend/` — do the same thing:

1. If `venv/` doesn't already exist: `py -m venv venv`
2. Install deps: `venv\Scripts\python.exe -m pip install -r requirements.txt`
   (this can take a few minutes for the backend — it pulls in langchain,
   azure-identity, playwright, etc.)
3. If `.env` doesn't already exist in that folder, copy `.env.example` to
   `.env`. If `.env` already exists, leave it alone — don't overwrite a
   working config.

## 3. Ask the user two things

Use AskUserQuestion (or just ask in chat if that tool isn't available) — don't
guess these:

1. **Their own email address** (the one they'll log into the app with — should
   be their real Bristlecone email, e.g. `firstname.lastname@bristlecone.com`).
2. **Do they have a Serper API key already** (free tier at https://serper.dev),
   or should web-research features be left unconfigured for now (the app
   still works for document generation without it — only the "research this
   company" web-search step needs it)?

## 4. Wire the answers into the .env files

In `orchestration/brd_convo_app/backend/.env`:
- Set `ADMIN_EMAIL=<their email, lowercase>`.
- Leave `ADMIN_PASSWORD` commented out / unset — a one-time password gets
  generated automatically on first startup (step 6).

In `infrastructure/unified_mcp/.env`:
- If they gave you a Serper key, set `SERPER_API_KEY=<key>` and keep
  `WEB_SEARCH_BACKEND=serper`.
- If not, leave the placeholder — don't invent a key.

If `orchestration/brd_convo_app/backend/app/users.db` already exists from a
previous run on this machine, **ask before deleting it** — it holds real
account data. If the user confirms a fresh start, delete it so the admin
bootstrap below creates a clean account for the new email.

## 5. Azure CLI login

Check whether `az` is usable: `az --version`.

- **If missing**: don't install azure-cli into the backend venv (its deep
  `azure/cli/command_modules/.../aaz/...` paths blow past Windows' 260-char
  MAX_PATH limit inside an already-long project path, and its pinned
  dependencies conflict with `azure-identity`, a real backend dependency).
  Don't use the MSI installer either — it needs admin rights this shell may
  not have. Instead create an isolated venv at a short path:
  ```
  py -m venv C:\azcli-venv
  C:\azcli-venv\Scripts\python.exe -m pip install azure-cli
  ```
  Use `C:\azcli-venv\Scripts\az.bat` as the `az` command from here on (both
  now and in the start-app skill).

- Run:
  ```
  az login --tenant 0876713d-a522-4b15-a887-3e9dacb1c635 --scope "https://cognitiveservices.azure.com/.default"
  ```
  This opens a browser window on the user's machine — tell them to complete
  sign-in with their Bristlecone account (MFA required) before you continue.
  Wait for the command to return successfully.

## 6. Bootstrap the admin account and capture the one-time password

Start the backend once, briefly, just to trigger `auth.init_db()`:

```
cd orchestration/brd_convo_app/backend
PYTHONUNBUFFERED=1 venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8010
```

Run it in the background, wait a couple seconds for "Application startup
complete", then read the log. Look for a line like:

```
[AUTH] ADMIN_PASSWORD not set — generated a one-time admin password:
[AUTH]   <email> / <generated-password>
```

Capture that password. It is only ever printed this once — if you miss it,
delete `app/users.db` and restart to regenerate it. Then stop this instance
(the real long-running start happens via the "start application" flow, not
this bootstrap run) — kill the background process.

## 7. Report back to the user

Tell them, plainly:
- Their login email and the generated one-time password (tell them to save
  it now — there's no "change password" flow yet, it's just their real
  password going forward).
- That setup is complete and they should now say **"start application"**.
