# Blueprint Document Platform — Setup

## Quick start (do this)

1. **Prerequisites** — install once if you don't already have them:
   - [Python 3.11+](https://www.python.org/downloads/) (check "Add python.exe to PATH" during install)
   - [VS Code](https://code.visualstudio.com/)
   - [Claude Code](https://claude.com/claude-code) extension/CLI, signed in

2. **Unzip** this project anywhere on your machine, then open the unzipped
   folder in VS Code.

3. Open the Claude Code panel and type:

   ```
   set up the application
   ```

   Claude will create the Python environments, install dependencies, and ask
   you for two things:
   - **Your own email address** — this becomes your login for the app.
   - **A Serper API key**, if you have one ([free tier here](https://serper.dev))
     — optional, only needed for the web-research step.

   It will also open a browser window for you to sign in to Azure (your
   normal Bristlecone login + MFA) — this is what lets document generation
   call Azure OpenAI under your own identity.

   When it's done, it will print your **login email and a one-time
   password** — save that password now, it's only shown once.

4. Type:

   ```
   start application
   ```

   This starts both backend services (and refreshes your Azure sign-in if it
   has expired). Once it says everything is running, open:

   ```
   http://localhost:8010
   ```

   and log in with the email + password from step 3.

That's it — steps 3 and 4 are the only two things you ever need to type.
Whenever you come back to work on this later, just reopen the folder in VS
Code and say "start application" again (no need to repeat setup).

---

## If something goes wrong

- **"Internal server error" mentioning Azure / `DefaultAzureCredential` /
  `AADSTS50078`** — your Azure sign-in session's MFA claim expired (this
  happens periodically and is normal). Just say "start application" again;
  Claude will detect this and prompt you to re-sign in via the browser.
- **Nothing loads at localhost:8010** — ask Claude "is the application
  running?" — it can check both services and restart whichever isn't up.
- **You don't have access to the "ai-adoption-coe" Azure AI resource** — you
  need a Bristlecone account with access to it for document generation to
  work; ask whoever gave you this zip to confirm access, or switch
  `LLM_PROVIDER=anthropic` in `orchestration/brd_convo_app/backend/.env` and
  supply your own `CLAUDE_API_KEY` instead.

## Manual setup (if you're not using Claude Code)

Two services, run in two terminals, both from the repo root:

```powershell
# Terminal 1 — MCP Web Tools server
cd infrastructure/unified_mcp
py -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env   # edit in your SERPER_API_KEY if you have one
venv\Scripts\python.exe kinaxis_http_server.py

# Terminal 2 — Backend + web UI
cd orchestration/brd_convo_app/backend
py -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env   # set ADMIN_EMAIL to your own email
az login --tenant 0876713d-a522-4b15-a887-3e9dacb1c635 --scope "https://cognitiveservices.azure.com/.default"
venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8010
```

The first backend startup prints your one-time admin password to the
terminal — copy it before it scrolls away. Then open http://localhost:8010.

If `az` isn't installed and you don't have admin rights for the MSI
installer, install it into its own short-path venv instead of the backend's:

```powershell
py -m venv C:\azcli-venv
C:\azcli-venv\Scripts\python.exe -m pip install azure-cli
C:\azcli-venv\Scripts\az.bat login --tenant 0876713d-a522-4b15-a887-3e9dacb1c635 --scope "https://cognitiveservices.azure.com/.default"
```
