---
name: start-app
description: Use when the user asks to "start the application", "run the app", "start backend and frontend", or "launch the platform" for the Kinaxis Blueprint Document platform in this repo. Refreshes the Azure CLI login (needed for Azure OpenAI generation), then starts the MCP web-tools server (port 8000) and the FastAPI backend + web UI (port 8010) in the background. Assumes setup-app already ran once on this machine.
---

# Starting the Blueprint Document platform

Two services, both must be running:

| # | Service | Folder | Command | Port |
|---|---------|--------|---------|------|
| 1 | MCP Web Tools server | `infrastructure/unified_mcp/` | `venv\Scripts\python.exe kinaxis_http_server.py` | 8000 |
| 2 | Backend + web UI | `orchestration/brd_convo_app/backend/` | `venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8010` | 8010 |

Do this yourself with your tools — don't just hand the user a list of commands.

## 1. Sanity check setup happened

If either `infrastructure/unified_mcp/venv/` or
`orchestration/brd_convo_app/backend/venv/` doesn't exist, or the backend
`.env` doesn't exist, stop and tell the user to run setup first (say "set up
the application").

## 2. Make sure Azure is actually logged in — this is what "start with Azure
   running" means

Generation calls Azure OpenAI using the signed-in user's Azure AD token
(`DefaultAzureCredential`). Azure's conditional-access policy expires that
MFA claim well before the CLI session itself looks expired, so `az account
show` can succeed while real API calls still fail with
`AADSTS50078: ...multi-factor authentication has expired...`. Don't trust
`az account show` alone — verify the actual token you need:

```
az account get-access-token --resource https://cognitiveservices.azure.com --tenant 0876713d-a522-4b15-a887-3e9dacb1c635
```

(Use `C:\azcli-venv\Scripts\az.bat` in place of `az` if azure-cli isn't on
PATH — that's where setup-app installs it.)

- If that succeeds, move on.
- If it fails, run:
  ```
  az login --tenant 0876713d-a522-4b15-a887-3e9dacb1c635 --scope "https://cognitiveservices.azure.com/.default"
  ```
  Tell the user a browser window just opened on their machine and they need
  to complete sign-in (MFA) before you continue. Wait for the command to
  return successfully before starting the backend — otherwise every
  generation call will 500 with an Azure auth error.

## 3. Start the MCP Web Tools server

From `infrastructure/unified_mcp/`, run in the background, redirecting
output to a log file:

```
PYTHONUNBUFFERED=1 venv\Scripts\python.exe kinaxis_http_server.py > mcp_run_latest.log 2>&1
```

Confirm it's actually up — check the log for "Application startup complete"
and/or that port 8000 is listening (e.g.
`Get-NetTCPConnection -LocalPort 8000` in PowerShell).

## 4. Start the backend

From `orchestration/brd_convo_app/backend/`, run in the background:

```
PYTHONUNBUFFERED=1 venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8010 > backend_run_latest.log 2>&1
```

Confirm port 8010 is listening and the log shows "Application startup
complete" with no traceback.

## 5. Report back

Tell the user:
- The app is running at **http://localhost:8010**.
- Log in with the email + password from setup (or whatever they've since
  changed it to).
- If they ever see "Internal server error" mentioning
  `DefaultAzureCredential` / `AADSTS50078`, their Azure MFA session expired —
  just say "start application" again and this skill will refresh it.
