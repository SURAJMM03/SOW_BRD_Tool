"""
identity.py — who is the current user, when the parent app owns the login.

This tool has no login page of its own. It is mounted inside a host
application (typically under /brd-generator, see _StripPrefixMiddleware in
main.py) that has already authenticated the user, and that host passes the
signed-in identity down on every request as a header.

    host app (user already signed in)
      └─ proxy /brd-generator/*  ──►  this app
           X-Forwarded-User: someone@bristlecone.com
           X-Forwarded-Role: admin            (optional)

SECURITY — this trusts a request header, so it is only safe when this app is
unreachable except through the host/proxy. Anything that can talk to this
process directly can set the header and become any user. Bind the app to
localhost (or a private network the proxy alone can reach) and make sure the
proxy *overwrites* the header rather than passing a client-supplied one
through.

Config (backend/.env):
    AUTH_USER_HEADER   header carrying the email      (default X-Forwarded-User)
    AUTH_ROLE_HEADER   header carrying the role       (default X-Forwarded-Role)
    AUTH_DEFAULT_ROLE  role when the host sends none  (default document_editor)
    DEV_FALLBACK_USER  email to assume when the header is absent. Leave unset
                       in production; set it to run this app standalone.
"""

from __future__ import annotations

import os

AUTH_USER_HEADER = os.getenv("AUTH_USER_HEADER", "X-Forwarded-User")
AUTH_ROLE_HEADER = os.getenv("AUTH_ROLE_HEADER", "X-Forwarded-Role")
AUTH_DEFAULT_ROLE = os.getenv("AUTH_DEFAULT_ROLE", "document_editor")
DEV_FALLBACK_USER = os.getenv("DEV_FALLBACK_USER", "").strip().lower()

VALID_ROLES = ("admin", "document_editor")


def resolve_user(request) -> dict | None:
    """Return {"email", "role"} for this request, or None if the host sent no
    identity and no DEV_FALLBACK_USER is configured."""
    email = (request.headers.get(AUTH_USER_HEADER) or "").strip().lower()
    if not email:
        email = DEV_FALLBACK_USER
    if not email:
        return None

    role = (request.headers.get(AUTH_ROLE_HEADER) or "").strip().lower()
    if role not in VALID_ROLES:
        role = AUTH_DEFAULT_ROLE if AUTH_DEFAULT_ROLE in VALID_ROLES else "document_editor"

    return {"email": email, "role": role}


def current_user(request) -> dict:
    """Identity for templating/page rendering — never None, so a page can
    still render (with an empty user badge) if the host sent no header."""
    return resolve_user(request) or {"email": "", "role": ""}
