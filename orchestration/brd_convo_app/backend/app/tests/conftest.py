"""
pytest conftest — ensures the app package and its dependencies are importable
when running tests from the backend directory.
"""
import sys
from pathlib import Path

# backend/  (so `from app.screen3_routes import ...` works)
_BACKEND = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# repo root so `from agents.brd_agent...` and `from config.settings...` work
_REPO_ROOT = _BACKEND.parent.parent.parent.parent
for _p in (_BACKEND, _REPO_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
