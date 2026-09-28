# Deploying the SOW tool on the server

Five steps. Do them in order. Step 5 tells you whether it worked.

## 1. Get the latest code

```
git pull origin main
```

You need commit `a52b244` or later. Older builds send generation to Anthropic
with a placeholder key, which is where
`401 ... 'API key is invalid'` comes from. They also pick the wrong SOW template.

## 2. Install

Python 3.11 or later. There are two services, and each has its own venv:

```
cd infrastructure/unified_mcp
python -m venv venv && venv/bin/pip install -r requirements.txt

cd orchestration/brd_convo_app/backend
python -m venv venv && venv/bin/pip install -r requirements.txt
```

Optional: `apt install libreoffice`. It is only needed to read old-format
`.ppt` / `.doc` files. PDF, DOCX, PPTX, XLSX, TXT and images work without it.

## 3. Create the two `.env` files

Copy each `.env.example` to `.env` in the same folder:

- `orchestration/brd_convo_app/backend/.env`
- `infrastructure/unified_mcp/.env`

Then set these in the **backend** `.env`:

| Setting | Value | Why |
|---|---|---|
| `LLM_PROVIDER` | `azure` | Anything else sends generation to Anthropic, and you get the 401 error |
| `AZURE_TENANT_ID` | `0876713d-a522-4b15-a887-3e9dacb1c635` | Bristlecone tenant |
| `AZURE_CLIENT_ID` | the service principal's app ID | Lets the server sign in to Azure OpenAI |
| `AZURE_CLIENT_SECRET` | the service principal's secret | Same as above |
| `DEV_FALLBACK_USER` | leave it unset | Only set it if nothing in front of the app sends `X-Forwarded-User` |

The service principal needs the **Cognitive Services OpenAI User** role on the
`ai-adoption-coe` Azure AI resource. If the server runs on Azure with a managed
identity, give that identity the role instead and skip the client ID and secret.

`az login` on the server is not enough. Its MFA token expires within hours,
and after that every Generate fails.

**Settings in the hosting platform override the `.env` file.** If the platform
defines `LLM_PROVIDER`, `CLAUDE_API_KEY` or `OPENAI_API_KEY` anywhere, fix
them there too, or remove them.

## 4. Start both services

```
# web tools (port 8000)
cd infrastructure/unified_mcp && venv/bin/python kinaxis_http_server.py

# app (port 8010)
cd orchestration/brd_convo_app/backend && \
  venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

Bind the app to `127.0.0.1`. The proxy in front must forward
`/brd-generator/*` to it and set `X-Forwarded-User` to the signed-in user's
email. It must **overwrite** that header, never pass on one the browser sent.

The app writes to `app/uploads/`, `app/sow_template_files/` and the JSON files
in `app/`. The service user needs write access to the `backend/app` folder.

## 5. Check it

Open, through the proxy:

```
https://<server>/brd-generator/api/diagnostics?probe=true
```

Everything should report OK:

- **llm.provider** is `azure`, **llm.credential_sources.service_principal_env**
  (or `managed_identity`) is `true`, and **llm.probe.ok** is `true`. That
  means Azure answered a real call.
- **storage.writable** is `true`. If not, uploads fail and documents never
  get processed.
- **identity.header_on_this_request** is `true`. If it's `false`, the proxy
  isn't sending `X-Forwarded-User`, and every `/api` call returns 401.
- **readers.unreadable** lists the file types this server can't read.
  `.doc` / `.ppt` show up there until LibreOffice is installed; that's fine.
- A `warning` in any section says what to fix.

If anything is red, send the whole JSON back and it can be diagnosed from there.
