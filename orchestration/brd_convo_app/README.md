# BRD Conversational App (FastAPI + React)

This is a minimal, sleek conversational UI that orchestrates the existing BRD agent workflow you already built (similar to `run_agent.py`):
- Research phase (tool calls happen once)
- Paragraph-by-paragraph approvals
- Private question capture
- BRD document generation (TXT + optional DOCX)

## Why this matches your current code
- Your current `run_agent.py` separates Research and Generation phases and does paragraph approvals + private questions in order. (See your CLI flow.)
- Your web tools client already posts to `/web_search` and `/open_url` on the MCP web tools FastAPI server.

## Run (dev)

### 1) Start web tools server
Run your existing MCP web tools FastAPI server (the one that exposes `/web_search` and `/open_url`).

### 2) Start conversation backend
```bash
cd backend
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --port 8010 --reload
```

### 3) Start frontend
```bash
cd frontend
npm install
npm run dev
```

Open: http://localhost:5173

## Notes
- Backend uses in-memory sessions (simple for dev). Swap to Redis later if you want scale.
- The backend tries to import your existing agents from either `agents.brd_gen_agent` or `brd_gen_agent`.
